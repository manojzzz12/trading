"""HMAC-SHA256 Approval Token Subsystem for Trader-Swing Phase 8.1.

Enforces:
1. Cryptographically secure 256-bit CSPRNG secret management.
2. Zero hardcoded secrets, zero secrets in log outputs.
3. Strict binding of approval token to canonical order intent, order ID, and price levels.
4. Time-to-live expiration validation (default: 300s / 5 minutes).
5. Single-use token enforcement and replay protection.
6. Constant-time signature verification (hmac.compare_digest) to prevent timing attacks.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hmac
import hashlib
import os
import secrets
import sys
from typing import Any, Dict, Optional

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from accounting import quantize_cent, to_decimal
from authorization_models import (
    HMACApprovalToken,
    SecretKeyUnavailableError,
    TokenAlreadyUsedError,
    TokenBindingMismatchError,
    TokenExpiredError,
    TokenTamperedError,
)
from order_models import PaperOrder
from paper_models import PaperOrderSide

# Default approval token validity window (5 minutes)
DEFAULT_TOKEN_TTL_SECONDS = 300


class SecretKeyManager:
    """Manages secure HMAC secret generation, loading, and rotation."""

    def __init__(self, secret_dir: Optional[str] = None, allow_ephemeral: bool = False):
        self.secret_dir = secret_dir
        self.allow_ephemeral = allow_ephemeral
        self._cached_secret: Optional[bytes] = None

    def get_signing_secret(self) -> bytes:
        """Retrieve signing secret from environment, secure keyfile, or generate one.

        Resolution priority:
        1. Environment variable TRADER_AUTH_SECRET.
        2. Keyfile in self.secret_dir/.secrets/auth_signing.key.
        3. If keyfile doesn't exist and secret_dir is valid, securely generate and persist.
        4. Ephemeral fallback ONLY IF explicitly allowed via allow_ephemeral=True.

        Raises:
            SecretKeyUnavailableError: If secret is missing, empty, insecure, or cannot be established.
        """
        if self._cached_secret is not None:
            return self._cached_secret

        # 1. Check environment variable
        if "TRADER_AUTH_SECRET" in os.environ:
            env_secret = os.environ.get("TRADER_AUTH_SECRET", "")
            cleaned = env_secret.strip()
            if not cleaned:
                raise SecretKeyUnavailableError("TRADER_AUTH_SECRET environment variable is set but empty or whitespace.")
            # Fail closed on known insecure default/placeholder patterns
            known_insecure = {
                "your_secret_here",
                "default_secret",
                "changeme",
                "production_secret",
                "password12345678",
                "1234567890123456",
            }
            if cleaned.lower() in known_insecure:
                raise SecretKeyUnavailableError("TRADER_AUTH_SECRET uses an insecure default/placeholder secret.")
            sec_bytes = cleaned.encode("utf-8")
            if len(sec_bytes) < 16:
                raise SecretKeyUnavailableError("TRADER_AUTH_SECRET is too short (< 16 bytes).")
            self._cached_secret = sec_bytes
            return self._cached_secret

        # 2. Check keyfile if directory is specified
        if self.secret_dir:
            secrets_folder = os.path.join(self.secret_dir, ".secrets")
            key_file = os.path.join(secrets_folder, "auth_signing.key")
            if os.path.exists(key_file):
                try:
                    with open(key_file, "rb") as f:
                        data = f.read().strip()
                        if len(data) >= 16:
                            self._cached_secret = data
                            return self._cached_secret
                        else:
                            raise SecretKeyUnavailableError(f"Keyfile '{key_file}' contains invalid secret (< 16 bytes).")
                except SecretKeyUnavailableError:
                    raise
                except Exception as err:
                    raise SecretKeyUnavailableError(f"Could not read keyfile '{key_file}': {err}") from err

            # Generate new keyfile in secure directory
            try:
                os.makedirs(secrets_folder, exist_ok=True)
                new_key = secrets.token_hex(32).encode("utf-8")  # 256-bit key
                with open(key_file, "wb") as f:
                    f.write(new_key)
                    f.flush()
                    os.fsync(f.fileno())
                # Restrict permissions on POSIX if available
                try:
                    os.chmod(key_file, 0o600)
                except Exception:
                    pass
                self._cached_secret = new_key
                return self._cached_secret
            except Exception as err:
                raise SecretKeyUnavailableError(f"Failed to generate signing keyfile: {err}") from err

        # 3. Ephemeral in-memory fallback (only permitted if explicitly allowed)
        if self.allow_ephemeral:
            self._cached_secret = secrets.token_hex(32).encode("utf-8")
            return self._cached_secret

        # Fail closed: no environment secret and no secret_dir provided
        raise SecretKeyUnavailableError(
            "Missing authorization secret: TRADER_AUTH_SECRET is not configured and no valid "
            "secret_dir was provided. Refusing operation (fail-closed)."
        )

    get_secret = get_signing_secret

    def rotate_secret(self) -> bytes:
        """Rotate to a newly generated 256-bit signing key."""
        new_secret = secrets.token_hex(32).encode("utf-8")
        if self.secret_dir:
            secrets_folder = os.path.join(self.secret_dir, ".secrets")
            key_file = os.path.join(secrets_folder, "auth_signing.key")
            os.makedirs(secrets_folder, exist_ok=True)
            with open(key_file, "wb") as f:
                f.write(new_secret)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.chmod(key_file, 0o600)
            except Exception:
                pass
        self._cached_secret = new_secret
        return self._cached_secret


class HMACApprovalTokenManager:
    """Issues, verifies, and single-use redeems HMAC approval tokens."""

    def __init__(self, key_manager: Optional[SecretKeyManager] = None):
        self.key_manager = key_manager or SecretKeyManager()

    @staticmethod
    def build_canonical_payload(
        token_id: str,
        order_id: str,
        idempotency_key: str,
        intent_hash: str,
        session_date: str,
        ticker: str,
        side: PaperOrderSide,
        shares: int,
        limit_price: Optional[Decimal],
        stop_loss: Decimal,
        target: Optional[Decimal],
        issued_at: str,
        expires_at: str,
        nonce: str,
    ) -> str:
        """Construct deterministic canonical pipe-delimited payload for HMAC signing."""
        s_side = side.value if isinstance(side, PaperOrderSide) else str(side)
        s_limit = f"{quantize_cent(limit_price):.2f}" if limit_price is not None else "NONE"
        s_stop = f"{quantize_cent(stop_loss):.2f}"
        s_target = f"{quantize_cent(target):.2f}" if target is not None else "NONE"
        return (
            f"{token_id}|{order_id}|{idempotency_key}|{intent_hash}|{session_date}|"
            f"{ticker.upper()}|{s_side}|{shares}|{s_limit}|{s_stop}|{s_target}|"
            f"{issued_at}|{expires_at}|{nonce}"
        )

    def issue_token(
        self,
        order: PaperOrder,
        ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS,
        reference_now: Optional[datetime] = None,
    ) -> HMACApprovalToken:
        """Issue a cryptographically signed HMAC approval token bound to an exact order."""
        secret = self.key_manager.get_signing_secret()

        now_utc = reference_now or datetime.now(timezone.utc)
        issued_at = now_utc.isoformat()
        expires_at = (now_utc + timedelta(seconds=ttl_seconds)).isoformat()
        nonce = secrets.token_hex(8)

        token_id = f"TOK-{order.order_id[-8:]}-{secrets.token_hex(4).upper()}"

        payload = self.build_canonical_payload(
            token_id=token_id,
            order_id=order.order_id,
            idempotency_key=order.idempotency_key,
            intent_hash=order.intent_hash,
            session_date=order.session_date,
            ticker=order.ticker,
            side=order.side,
            shares=order.shares_initial,
            limit_price=order.limit_price,
            stop_loss=order.stop_loss,
            target=order.target,
            issued_at=issued_at,
            expires_at=expires_at,
            nonce=nonce,
        )

        signature = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()

        return HMACApprovalToken(
            token_id=token_id,
            order_id=order.order_id,
            idempotency_key=order.idempotency_key,
            intent_hash=order.intent_hash,
            session_date=order.session_date,
            ticker=order.ticker,
            side=order.side,
            shares=order.shares_initial,
            limit_price=order.limit_price,
            stop_loss=order.stop_loss,
            target=order.target,
            issued_at=issued_at,
            expires_at=expires_at,
            nonce=nonce,
            signature=signature,
            used=False,
            used_at=None,
        )

    def verify_token(
        self,
        token: HMACApprovalToken,
        order: PaperOrder,
        reference_now: Optional[datetime] = None,
    ) -> bool:
        """Verify token cryptographic integrity, expiration, single-use, and exact order binding.

        Raises:
            TokenTamperedError: If HMAC signature verification fails.
            TokenExpiredError: If token time-to-live has passed.
            TokenAlreadyUsedError: If token has already been redeemed.
            TokenBindingMismatchError: If token fields do not match the target order.
        """
        secret = self.key_manager.get_signing_secret()
        now_utc = reference_now or datetime.now(timezone.utc)

        # 1. Single-use check
        if token.used:
            raise TokenAlreadyUsedError(
                f"Token '{token.token_id}' has already been redeemed at {token.used_at}."
            )

        # 2. Expiration check
        if token.is_expired(now_utc):
            raise TokenExpiredError(
                f"Token '{token.token_id}' expired at {token.expires_at} (current time: {now_utc.isoformat()})."
            )

        # 3. Cryptographic signature check (constant time)
        expected_payload = self.build_canonical_payload(
            token_id=token.token_id,
            order_id=token.order_id,
            idempotency_key=token.idempotency_key,
            intent_hash=token.intent_hash,
            session_date=token.session_date,
            ticker=token.ticker,
            side=token.side,
            shares=token.shares,
            limit_price=token.limit_price,
            stop_loss=token.stop_loss,
            target=token.target,
            issued_at=token.issued_at,
            expires_at=token.expires_at,
            nonce=token.nonce,
        )
        expected_sig = hmac.new(secret, expected_payload.encode("utf-8"), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(token.signature, expected_sig):
            raise TokenTamperedError(
                f"Cryptographic signature verification failed for token '{token.token_id}'."
            )

        # 4. Strict order binding checks
        if token.order_id != order.order_id:
            raise TokenBindingMismatchError(
                f"Token order ID '{token.order_id}' does not match target order ID '{order.order_id}'."
            )
        if token.intent_hash != order.intent_hash:
            raise TokenBindingMismatchError(
                f"Token intent hash '{token.intent_hash}' does not match order intent hash '{order.intent_hash}'."
            )
        if token.ticker.upper() != order.ticker.upper():
            raise TokenBindingMismatchError(
                f"Token ticker '{token.ticker}' does not match order ticker '{order.ticker}'."
            )
        if token.shares != order.shares_initial:
            raise TokenBindingMismatchError(
                f"Token shares ({token.shares}) does not match order initial shares ({order.shares_initial})."
            )
        if quantize_cent(token.stop_loss) != quantize_cent(order.stop_loss):
            raise TokenBindingMismatchError(
                f"Token stop loss (${token.stop_loss}) does not match order stop loss (${order.stop_loss})."
            )
        if (token.limit_price is None) != (order.limit_price is None):
            raise TokenBindingMismatchError("Token limit price presence mismatch with order.")
        if token.limit_price is not None and order.limit_price is not None:
            if quantize_cent(token.limit_price) != quantize_cent(order.limit_price):
                raise TokenBindingMismatchError(
                    f"Token limit price (${token.limit_price}) does not match order limit price (${order.limit_price})."
                )

        return True

    def redeem_token(
        self,
        token: HMACApprovalToken,
        reference_now: Optional[datetime] = None,
    ) -> None:
        """Mark token as redeemed (single-use enforcement)."""
        if token.used:
            raise TokenAlreadyUsedError(f"Token '{token.token_id}' already redeemed.")
        now_utc = reference_now or datetime.now(timezone.utc)
        token.used = True
        token.used_at = now_utc.isoformat()
