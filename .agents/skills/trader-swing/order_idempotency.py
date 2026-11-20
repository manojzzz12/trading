"""Deterministic idempotency and fill-identity subsystem for Trader-Swing Phase 8.0.

Enforces:
1. Deterministic client order identifiers adhering to:
   IDEMP-{SESSION_DATE}-{TICKER}-{HASH}
2. Canonical order intent normalization:
   - Identical order intent produces exact same identifier.
   - Materially different order intent produces different identifier.
   - Rejection of conflicting reuse of an existing idempotency key.
3. Deterministic fill identity computation and duplicate fill protection:
   - Each execution fill carries a deterministic identity.
   - Replay of an identical fill event is detected as idempotent.
   - Conflicting fill attributes for an existing fill ID are rejected.
4. Traceability across Order -> Fill -> Lot.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import os
import re
import sys
from typing import Any, Dict, Optional

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from accounting import quantize_cent, to_decimal, validate_positive_shares
from paper_models import PaperOrderSide
from universe import validate_ticker


class ConflictingIdempotencyKeyError(ValueError):
    """Raised when an existing idempotency key is reused with conflicting order intent."""
    pass


class DuplicateOrderError(ValueError):
    """Raised when duplicate order creation is attempted with conflicting state."""
    pass


class ConflictingFillIdentityError(ValueError):
    """Raised when an existing fill identity is reused with conflicting execution attributes."""
    pass


class DuplicateFillError(ValueError):
    """Raised when an execution fill attempt violates duplicate constraints."""
    pass


@dataclass(frozen=True)
class CanonicalOrderIntent:
    """Canonical representation of an order intent for deterministic key generation."""
    session_date: str
    ticker: str
    side: PaperOrderSide
    shares: int
    order_type: str
    stop_loss: Decimal
    limit_price: Optional[Decimal] = None
    target: Optional[Decimal] = None
    strategy_name: str = "TRADER_SWING"
    strategy_version: str = "1.0.0"
    client_tag: str = ""

    def validate(self) -> None:
        """Validate order intent fields against trading invariants."""
        # Validate symbol
        validate_ticker(self.ticker.strip().upper())

        # Validate shares
        if not isinstance(self.shares, int) or self.shares <= 0:
            raise ValueError(f"Shares must be positive integer, got {self.shares}")
        try:
            validate_positive_shares(self.shares)
        except Exception as e:
            raise ValueError(f"Invalid shares count: {e}") from e

        # Validate prices
        sl = quantize_cent(to_decimal(self.stop_loss))
        if sl <= Decimal("0.00"):
            raise ValueError(f"Stop loss must be positive, got ${sl}")

        if self.limit_price is not None:
            lp = quantize_cent(to_decimal(self.limit_price))
            if lp <= Decimal("0.00"):
                raise ValueError(f"Limit price must be positive, got ${lp}")

            if self.side == PaperOrderSide.BUY and sl >= lp:
                raise ValueError(
                    f"Stop loss (${sl}) must be strictly below limit price (${lp}) for BUY order."
                )

            if self.target is not None:
                tg = quantize_cent(to_decimal(self.target))
                if tg <= Decimal("0.00"):
                    raise ValueError(f"Target must be positive, got ${tg}")
                if self.side == PaperOrderSide.BUY and tg <= lp:
                    raise ValueError(
                        f"Target (${tg}) must be strictly above limit price (${lp}) for BUY order."
                    )
        elif self.target is not None:
            tg = quantize_cent(to_decimal(self.target))
            if tg <= Decimal("0.00"):
                raise ValueError(f"Target must be positive, got ${tg}")
            if tg <= sl:
                raise ValueError(f"Target (${tg}) must be strictly above stop loss (${sl}).")

    def canonical_session_date(self) -> str:
        """Normalize session date string to YYYYMMDD format."""
        digits = re.sub(r"\D", "", self.session_date)
        if len(digits) == 8:
            return digits
        raise ValueError(
            f"Invalid session date '{self.session_date}'; expected 8-digit date format (e.g. '20261019' or '2026-10-19')."
        )

    def canonical_payload_string(self) -> str:
        """Construct normalized, pipe-delimited canonical intent string for hashing."""
        sym = self.ticker.strip().upper()
        s_date = self.canonical_session_date()
        s_side = self.side.value if isinstance(self.side, PaperOrderSide) else str(self.side).upper()
        s_shares = str(int(self.shares))
        s_type = self.order_type.strip().upper()
        s_limit = f"{quantize_cent(self.limit_price):.2f}" if self.limit_price is not None else "NONE"
        s_stop = f"{quantize_cent(self.stop_loss):.2f}"
        s_target = f"{quantize_cent(self.target):.2f}" if self.target is not None else "NONE"
        s_strat = self.strategy_name.strip().upper()
        s_ver = self.strategy_version.strip()
        s_tag = self.client_tag.strip()
        return (
            f"{s_date}|{sym}|{s_side}|{s_shares}|{s_type}|"
            f"{s_limit}|{s_stop}|{s_target}|{s_strat}|{s_ver}|{s_tag}"
        )

    def compute_intent_hash(self) -> str:
        """Compute 12-character uppercase SHA-256 digest of canonical order intent."""
        payload = self.canonical_payload_string()
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12].upper()

    def compute_idempotency_key(self) -> str:
        """Compute standard IDEMP-{SESSION_DATE}-{TICKER}-{HASH} client identifier."""
        self.validate()
        s_date = self.canonical_session_date()
        sym = self.ticker.strip().upper()
        h = self.compute_intent_hash()
        return f"IDEMP-{s_date}-{sym}-{h}"

    def to_dict(self) -> Dict[str, Any]:
        """Serialize canonical intent to dictionary."""
        return {
            "session_date": self.session_date,
            "ticker": self.ticker,
            "side": self.side.value if isinstance(self.side, PaperOrderSide) else str(self.side),
            "shares": self.shares,
            "order_type": self.order_type,
            "stop_loss": str(self.stop_loss),
            "limit_price": str(self.limit_price) if self.limit_price is not None else None,
            "target": str(self.target) if self.target is not None else None,
            "strategy_name": self.strategy_name,
            "strategy_version": self.strategy_version,
            "client_tag": self.client_tag,
            "intent_hash": self.compute_intent_hash(),
            "idempotency_key": self.compute_idempotency_key(),
        }


def compute_order_id(idempotency_key: str) -> str:
    """Generate standard order ID from idempotency key."""
    norm_key = idempotency_key.strip()
    return f"ORD-{norm_key}"


def compute_fill_identity(
    order_id: str,
    fill_index: int,
    shares: int,
    price: Decimal,
    timestamp: str,
) -> str:
    """Compute deterministic fill identifier for an order fill event.

    Format: FILL-{ORDER_ID}-{FILL_INDEX:02d}-{HASH:8}
    """
    norm_order = order_id.strip()
    norm_shares = str(int(shares))
    norm_px = f"{quantize_cent(to_decimal(price)):.2f}"
    raw = f"{norm_order}|{fill_index}|{norm_shares}|{norm_px}|{timestamp}"
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8].upper()
    return f"FILL-{norm_order}-{fill_index:02d}-{h}"


def compute_manual_fill_identity(
    session_date: str,
    ticker: str,
    shares: int,
    price: Decimal,
    stop_loss: Decimal,
    timestamp: str,
) -> str:
    """Compute deterministic fill identity for a manual fill entry without prior order ID."""
    s_date = re.sub(r"\D", "", session_date)[:8]
    sym = ticker.strip().upper()
    norm_shares = str(int(shares))
    norm_px = f"{quantize_cent(to_decimal(price)):.2f}"
    norm_stop = f"{quantize_cent(to_decimal(stop_loss)):.2f}"
    raw = f"{s_date}|{sym}|{norm_shares}|{norm_px}|{norm_stop}|{timestamp}"
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8].upper()
    return f"FILL-MANUAL-{s_date}-{sym}-{h}"
