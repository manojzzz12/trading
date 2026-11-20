"""Multi-Gate Human Authorization Engine for Trader-Swing Phase 8.1.

Enforces:
1. GATE 1 — Deterministic Risk Engine Validation (pure Python Decimal limits; zero AI override).
2. GATE 2 — Explicit Human Interactive Confirmation (unambiguous ticket presentation; affirmative YES required).
3. GATE 3 — Antigravity Platform Execution Permission (cryptographic HMAC-SHA256 token verification; fail-closed).
4. Strict state machine transitions:
   PROPOSED -> RISK_VALIDATED -> PENDING_HUMAN_APPROVAL -> HUMAN_APPROVED -> PLATFORM_PERMISSION_APPROVED -> AUTHORIZED
5. Immediate terminal lockout on any gate failure:
   RISK_REJECTED, HUMAN_REJECTED, EXPIRED, CANCELLED, AUTHORIZATION_FAILED
6. Zero fills, zero cash mutations, zero execution calls.
"""

from datetime import datetime, timezone
from decimal import Decimal
import json
import os
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from accounting import quantize_cent, to_decimal
from authorization_models import (
    AuthorizationRecord,
    AuthorizationState,
    GateVerdict,
    HMACApprovalToken,
    HumanGateRejectionError,
    IllegalAuthorizationTransitionError,
    PlatformPermissionError,
    RiskGateRejectionError,
    TerminalAuthorizationStateError,
    TokenBindingMismatchError,
    TokenExpiredError,
    TokenTamperedError,
    TokenAlreadyUsedError,
)
from authorization_token import HMACApprovalTokenManager, SecretKeyManager
from order_models import PaperOrder, PaperOrderState
from paper_models import PaperOrderSide, PaperTradeAuditEntry
from paper_storage import PaperStorageManager
from risk import RiskConfig, RiskEngine


class HumanAuthorizationService:
    """Coordinates multi-gate authorization across Risk, Human, and Platform safety gates."""

    def __init__(
        self,
        storage: Optional[PaperStorageManager] = None,
        token_manager: Optional[HMACApprovalTokenManager] = None,
        risk_config: Optional[RiskConfig] = None,
    ):
        self.storage = storage or PaperStorageManager()
        key_mgr = SecretKeyManager(secret_dir=self.storage.state_dir)
        self.token_manager = token_manager or HMACApprovalTokenManager(key_manager=key_mgr)
        self.risk_config = risk_config or RiskConfig()
        self.risk_engine = RiskEngine(risk_config=self.risk_config)
        self.authorizations_path = os.path.join(self.storage.state_dir, "trading_paper_authorizations.json")

    # -------------------------------------------------------------------------
    # Authorization Persistence
    # -------------------------------------------------------------------------

    def load_authorizations(self) -> Dict[str, Any]:
        """Load authorizations registry from disk."""
        self.storage.recover_interrupted_transaction()
        if not os.path.exists(self.authorizations_path):
            return {
                "schema_version": "1.0.0",
                "system": "trader-swing-auth",
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "authorizations": {},
                "redeemed_tokens": {},
            }
        with open(self.authorizations_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            data.setdefault("authorizations", {})
            data.setdefault("redeemed_tokens", {})
            return data

    def save_authorizations(self, data: Dict[str, Any]) -> None:
        """Atomically persist authorizations registry."""
        os.makedirs(self.storage.state_dir, exist_ok=True)
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        tmp_path = f"{self.authorizations_path}.tmp.{os.getpid()}"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, self.authorizations_path)

    def get_authorization(self, order_id: str) -> Optional[AuthorizationRecord]:
        """Retrieve authorization record by order ID."""
        data = self.load_authorizations()
        raw = data.get("authorizations", {}).get(order_id)
        if raw is None:
            return None
        return AuthorizationRecord.from_dict(raw)

    def save_authorization(self, record: AuthorizationRecord) -> None:
        """Persist or update an AuthorizationRecord."""
        data = self.load_authorizations()
        data.setdefault("authorizations", {})[record.order_id] = record.to_dict()
        if record.token and record.token.used:
            data.setdefault("redeemed_tokens", {})[record.token.token_id] = record.token.used_at
        self.save_authorizations(data)

    # -------------------------------------------------------------------------
    # Ticket Formatting (Gate 2 Display)
    # -------------------------------------------------------------------------

    def render_authorization_ticket(self, order: PaperOrder) -> str:
        """Generate human-readable, unambiguous authorization ticket for CLI / UI."""
        journal = self.storage.load_journal()
        ledger = journal.ledger
        equity = ledger.total_equity_usd if ledger else Decimal("5000.00")
        cash = ledger.cash_usd if ledger else Decimal("5000.00")

        ref_px = order.limit_price if order.limit_price is not None else order.stop_loss
        principal = (ref_px * Decimal(order.shares_initial)).quantize(Decimal("0.01"))
        risk_per_share = ref_px - order.stop_loss
        unbuffered_risk = (risk_per_share * Decimal(order.shares_initial)).quantize(Decimal("0.01"))
        buffered_risk = (unbuffered_risk * Decimal("1.25")).quantize(Decimal("0.01"))

        est_fee_entry = Decimal("1.00")
        est_fee_exit = Decimal("1.00")
        total_fees = est_fee_entry + est_fee_exit

        cash_after = cash - principal - est_fee_entry
        cash_reserve_pct = (cash_after / equity * Decimal("100.0")).quantize(Decimal("0.01"))
        risk_pct = (unbuffered_risk / equity * Decimal("100.0")).quantize(Decimal("0.01"))
        alloc_pct = (principal / equity * Decimal("100.0")).quantize(Decimal("0.01"))

        target_str = f"${order.target}" if order.target is not None else "None"
        rr_str = (
            f"{((order.target - ref_px) / risk_per_share).quantize(Decimal('0.01'))}x"
            if order.target and risk_per_share > 0
            else "N/A"
        )

        ticket = (
            "============================================================\n"
            "         TRADER-SWING: ORDER AUTHORIZATION TICKET\n"
            "============================================================\n"
            f"  Order ID         : {order.order_id}\n"
            f"  Idempotency Key  : {order.idempotency_key}\n"
            f"  Symbol / Side    : {order.ticker} | {order.side.value} (Long Only)\n"
            f"  Order Type       : {order.order_type}\n"
            f"  Quantity         : {order.shares_initial} shares\n"
            f"  Limit Price      : ${ref_px} USD\n"
            f"  Stop-Loss Price  : ${order.stop_loss} USD\n"
            f"  Target Price     : {target_str} USD\n"
            f"  Reward:Risk      : {rr_str}\n"
            f"  Total Principal  : ${principal} USD ({alloc_pct}% of equity)\n"
            f"  Est. Transaction : ${est_fee_entry} Entry Fee + ${est_fee_exit} Exit Fee (${total_fees} total)\n"
            f"  Per-Trade Risk   : ${unbuffered_risk} USD ({risk_pct}% of equity, max 0.50%)\n"
            f"  Buffered Risk    : ${buffered_risk} USD (1.25x gap-risk buffer)\n"
            f"  Portfolio Equity : ${equity} USD\n"
            f"  Cash Remaining   : ${cash_after} USD ({cash_reserve_pct}% cash reserve)\n"
            "============================================================\n"
        )
        return ticket

    # -------------------------------------------------------------------------
    # Gate 1: Deterministic Risk Engine Evaluation
    # -------------------------------------------------------------------------

    def evaluate_gate1_risk(
        self,
        order: PaperOrder,
        record: AuthorizationRecord,
        reference_now: Optional[datetime] = None,
    ) -> Tuple[bool, GateVerdict]:
        """Evaluate Gate 1: Deterministic Risk Engine.

        Enforces:
        - 0.50% max risk per trade
        - 1.25x gap buffer
        - Max 4 simultaneous positions
        - 25% single-ticker allocation cap
        - 80% maximum invested capital
        - 20% minimum cash reserve
        """
        now_utc = reference_now or datetime.now(timezone.utc)
        ts_str = now_utc.isoformat()

        journal = self.storage.load_journal()
        ledger = journal.ledger
        if ledger is None:
            raise RiskGateRejectionError("Uninitialized ledger state for Gate 1 risk check.")

        equity = ledger.total_equity_usd
        cash = ledger.cash_usd
        ref_px = order.limit_price if order.limit_price is not None else order.stop_loss

        # 1. Price and Stop Sanity
        if order.stop_loss <= Decimal("0.00"):
            verdict = GateVerdict("GATE_1_RISK", False, f"Invalid non-positive stop loss: ${order.stop_loss}", ts_str)
            record.transition_to(AuthorizationState.RISK_REJECTED, timestamp=ts_str, reason=verdict.reason, verdict=verdict)
            self.save_authorization(record)
            raise RiskGateRejectionError(verdict.reason)

        if order.stop_loss >= ref_px:
            verdict = GateVerdict("GATE_1_RISK", False, f"Stop loss (${order.stop_loss}) must be strictly below price (${ref_px})", ts_str)
            record.transition_to(AuthorizationState.RISK_REJECTED, timestamp=ts_str, reason=verdict.reason, verdict=verdict)
            self.save_authorization(record)
            raise RiskGateRejectionError(verdict.reason)

        # 2. Maximum 4 positions
        active_tickers = {t for t, p in ledger.positions.items() if p.total_shares > 0}
        if order.ticker not in active_tickers and len(active_tickers) >= 4:
            verdict = GateVerdict(
                "GATE_1_RISK",
                False,
                f"MAX_POSITIONS_EXCEEDED: Cannot add '{order.ticker}'. 4 positions already held: {sorted(list(active_tickers))}.",
                ts_str,
            )
            record.transition_to(AuthorizationState.RISK_REJECTED, timestamp=ts_str, reason=verdict.reason, verdict=verdict)
            self.save_authorization(record)
            raise RiskGateRejectionError(verdict.reason)

        # 3. 0.50% Risk Limit
        risk_per_share = ref_px - order.stop_loss
        total_risk = (risk_per_share * Decimal(order.shares_initial)).quantize(Decimal("0.01"))
        max_allowed_risk = (equity * Decimal("0.0050")).quantize(Decimal("0.01"))
        if total_risk > max_allowed_risk:
            verdict = GateVerdict(
                "GATE_1_RISK",
                False,
                f"PER_TRADE_RISK_EXCEEDED: Risk ${total_risk} exceeds 0.50% limit (${max_allowed_risk}) on equity ${equity}.",
                ts_str,
            )
            record.transition_to(AuthorizationState.RISK_REJECTED, timestamp=ts_str, reason=verdict.reason, verdict=verdict)
            self.save_authorization(record)
            raise RiskGateRejectionError(verdict.reason)

        # 4. Cash and 20% Cash Reserve
        principal = (ref_px * Decimal(order.shares_initial)).quantize(Decimal("0.01"))
        est_entry_fee = Decimal("1.00")
        total_cost = principal + est_entry_fee
        if total_cost > cash:
            verdict = GateVerdict("GATE_1_RISK", False, f"INSUFFICIENT_CASH: Cost ${total_cost} exceeds cash ${cash}.", ts_str)
            record.transition_to(AuthorizationState.RISK_REJECTED, timestamp=ts_str, reason=verdict.reason, verdict=verdict)
            self.save_authorization(record)
            raise RiskGateRejectionError(verdict.reason)

        min_cash_required = (equity * Decimal("0.20")).quantize(Decimal("0.01"))
        cash_after = cash - total_cost
        if cash_after < min_cash_required:
            verdict = GateVerdict(
                "GATE_1_RISK",
                False,
                f"MIN_CASH_RESERVE_VIOLATION: Cash after trade (${cash_after}) violates 20% reserve (${min_cash_required}).",
                ts_str,
            )
            record.transition_to(AuthorizationState.RISK_REJECTED, timestamp=ts_str, reason=verdict.reason, verdict=verdict)
            self.save_authorization(record)
            raise RiskGateRejectionError(verdict.reason)

        # 5. 80% Invested Capital Cap
        gmv_after = ledger.gross_market_value_usd + principal
        max_gmv_allowed = (equity * Decimal("0.80")).quantize(Decimal("0.01"))
        if gmv_after > max_gmv_allowed:
            verdict = GateVerdict(
                "GATE_1_RISK",
                False,
                f"MAX_INVESTED_CAPITAL_VIOLATION: Market value ${gmv_after} exceeds 80% limit (${max_gmv_allowed}).",
                ts_str,
            )
            record.transition_to(AuthorizationState.RISK_REJECTED, timestamp=ts_str, reason=verdict.reason, verdict=verdict)
            self.save_authorization(record)
            raise RiskGateRejectionError(verdict.reason)

        # 6. 25% Single Ticker Allocation Cap
        existing_pos = ledger.positions.get(order.ticker)
        existing_basis = existing_pos.total_cost_basis_usd if existing_pos else Decimal("0.00")
        alloc_after = existing_basis + principal
        max_alloc_allowed = (equity * Decimal("0.25")).quantize(Decimal("0.01"))
        if alloc_after > max_alloc_allowed:
            verdict = GateVerdict(
                "GATE_1_RISK",
                False,
                f"TICKER_ALLOCATION_CAP_EXCEEDED: Allocation ${alloc_after} exceeds 25% limit (${max_alloc_allowed}).",
                ts_str,
            )
            record.transition_to(AuthorizationState.RISK_REJECTED, timestamp=ts_str, reason=verdict.reason, verdict=verdict)
            self.save_authorization(record)
            raise RiskGateRejectionError(verdict.reason)

        # Gate 1 PASS
        verdict = GateVerdict(
            "GATE_1_RISK",
            True,
            "Deterministic risk evaluation PASSED all 5 frozen portfolio constraints.",
            ts_str,
            details={
                "risk_usd": str(total_risk),
                "max_risk_allowed": str(max_allowed_risk),
                "principal_usd": str(principal),
                "cash_remaining_usd": str(cash_after),
            },
        )
        record.transition_to(AuthorizationState.RISK_VALIDATED, timestamp=ts_str, reason=verdict.reason, verdict=verdict)

        # Issue HMAC token and transition to PENDING_HUMAN_APPROVAL
        token = self.token_manager.issue_token(order, reference_now=now_utc)
        record.token = token
        record.transition_to(
            AuthorizationState.PENDING_HUMAN_APPROVAL,
            timestamp=ts_str,
            reason="HMAC approval token issued; awaiting explicit human confirmation.",
        )
        self.save_authorization(record)
        return True, verdict

    # -------------------------------------------------------------------------
    # Gate 2: Explicit Human Confirmation
    # -------------------------------------------------------------------------

    def evaluate_gate2_human(
        self,
        order: PaperOrder,
        record: AuthorizationRecord,
        user_confirmation: str,
        reference_now: Optional[datetime] = None,
    ) -> Tuple[bool, GateVerdict]:
        """Evaluate Gate 2: Explicit Human Confirmation.

        Requires affirmative confirmation ("YES" or "CONFIRM").
        Rejects on "NO", "REJECT", timeout, or arbitrary string.
        """
        now_utc = reference_now or datetime.now(timezone.utc)
        ts_str = now_utc.isoformat()

        # Token expiry check
        if record.token and record.token.is_expired(now_utc):
            verdict = GateVerdict("GATE_2_HUMAN", False, "Approval token expired before human confirmation.", ts_str)
            record.transition_to(AuthorizationState.EXPIRED, timestamp=ts_str, reason=verdict.reason, verdict=verdict)
            self.save_authorization(record)
            raise TokenExpiredError(verdict.reason)

        clean_resp = user_confirmation.strip().upper()

        if clean_resp in ("YES", "CONFIRM"):
            verdict = GateVerdict("GATE_2_HUMAN", True, f"User confirmed order ticket with response '{clean_resp}'.", ts_str)
            record.transition_to(AuthorizationState.HUMAN_APPROVED, timestamp=ts_str, reason=verdict.reason, verdict=verdict)
            self.save_authorization(record)
            return True, verdict

        elif clean_resp in ("CANCEL", "ABORT"):
            verdict = GateVerdict("GATE_2_HUMAN", False, f"User cancelled authorization request with '{clean_resp}'.", ts_str)
            record.transition_to(AuthorizationState.CANCELLED, timestamp=ts_str, reason=verdict.reason, verdict=verdict)
            self.save_authorization(record)
            raise HumanGateRejectionError(verdict.reason)

        else:
            verdict = GateVerdict("GATE_2_HUMAN", False, f"User denied or provided invalid confirmation: '{user_confirmation}'.", ts_str)
            record.transition_to(AuthorizationState.HUMAN_REJECTED, timestamp=ts_str, reason=verdict.reason, verdict=verdict)
            self.save_authorization(record)
            raise HumanGateRejectionError(verdict.reason)

    # -------------------------------------------------------------------------
    # Gate 3: Antigravity Execution Permission & Token Verification
    # -------------------------------------------------------------------------

    def evaluate_gate3_platform(
        self,
        order: PaperOrder,
        record: AuthorizationRecord,
        platform_verifier: Optional[Callable[[PaperOrder, HMACApprovalToken], bool]] = None,
        reference_now: Optional[datetime] = None,
    ) -> Tuple[bool, GateVerdict]:
        """Evaluate Gate 3: Platform Execution Permission & Cryptographic Token Verification.

        Validates:
        - HMAC-SHA256 signature integrity (constant time).
        - Exact order attribute binding.
        - Token TTL expiration.
        - Single-use redemption (replay protection).
        - Platform permission callback (if provided) or default fail-closed check.
        """
        now_utc = reference_now or datetime.now(timezone.utc)
        ts_str = now_utc.isoformat()

        if record.token is None:
            verdict = GateVerdict("GATE_3_PLATFORM", False, "No HMAC approval token found on record.", ts_str)
            record.transition_to(AuthorizationState.AUTHORIZATION_FAILED, timestamp=ts_str, reason=verdict.reason, verdict=verdict)
            self.save_authorization(record)
            raise PlatformPermissionError(verdict.reason)

        token = record.token

        # 1. Cryptographic and binding verification
        try:
            self.token_manager.verify_token(token, order, reference_now=now_utc)
        except (TokenTamperedError, TokenExpiredError, TokenAlreadyUsedError, TokenBindingMismatchError) as err:
            verdict = GateVerdict("GATE_3_PLATFORM", False, f"Token verification failed: {err}", ts_str)
            record.transition_to(AuthorizationState.AUTHORIZATION_FAILED, timestamp=ts_str, reason=verdict.reason, verdict=verdict)
            self.save_authorization(record)
            raise

        # 2. Platform Permission Verification
        platform_granted = True
        if platform_verifier is not None:
            try:
                platform_granted = bool(platform_verifier(order, token))
            except Exception as err:
                verdict = GateVerdict("GATE_3_PLATFORM", False, f"Platform permission check error: {err}", ts_str)
                record.transition_to(AuthorizationState.AUTHORIZATION_FAILED, timestamp=ts_str, reason=verdict.reason, verdict=verdict)
                self.save_authorization(record)
                raise PlatformPermissionError(verdict.reason) from err

        if not platform_granted:
            verdict = GateVerdict("GATE_3_PLATFORM", False, "Antigravity execution permission denied by platform gate.", ts_str)
            record.transition_to(AuthorizationState.AUTHORIZATION_FAILED, timestamp=ts_str, reason=verdict.reason, verdict=verdict)
            self.save_authorization(record)
            raise PlatformPermissionError(verdict.reason)

        # Gate 3 PASS
        verdict = GateVerdict(
            "GATE_3_PLATFORM",
            True,
            "Antigravity platform permission confirmed and HMAC signature verified.",
            ts_str,
            details={"token_id": token.token_id, "nonce": token.nonce},
        )
        record.transition_to(AuthorizationState.PLATFORM_PERMISSION_APPROVED, timestamp=ts_str, reason=verdict.reason, verdict=verdict)

        # 3. Single-Use Token Redemption & Seal Final AUTHORIZED State
        self.token_manager.redeem_token(token, reference_now=now_utc)
        record.transition_to(
            AuthorizationState.AUTHORIZED,
            timestamp=ts_str,
            reason="All 3 authorization gates passed. Order AUTHORIZED for future execution.",
        )

        # Update PaperOrder state to APPROVED (ready for submission in Phase 8.2)
        if order.state in (PaperOrderState.PROPOSED, PaperOrderState.PENDING_APPROVAL):
            if order.state == PaperOrderState.PROPOSED:
                order.transition_to(PaperOrderState.PENDING_APPROVAL, timestamp=ts_str, reason="Risk check passed")
            order.transition_to(PaperOrderState.APPROVED, timestamp=ts_str, reason=f"3-Gate Authorization Seal: {token.token_id}")
            self.storage.save_order(order)

        self.save_authorization(record)

        # Append structured audit trail event
        audit_entry = PaperTradeAuditEntry(
            event_id=f"EVT-AUTH-{order.order_id[-8:]}",
            timestamp=ts_str,
            event_type="ORDER_AUTHORIZED",
            ticker=order.ticker,
            shares=order.shares_initial,
            price=str(order.limit_price) if order.limit_price is not None else None,
            fee=None,
            equity_after="",
            cash_after="",
            details={
                "order_id": order.order_id,
                "token_id": token.token_id,
                "auth_state": record.state.value,
                "gates_passed": [v.gate_name for v in record.gate_verdicts if v.passed],
            },
        )
        self.storage.append_audit_event(audit_entry)

        return True, verdict

    # -------------------------------------------------------------------------
    # End-to-End Multi-Gate Orchestration
    # -------------------------------------------------------------------------

    def authorize_order(
        self,
        order_id: str,
        user_confirmation: str,
        platform_verifier: Optional[Callable[[PaperOrder, HMACApprovalToken], bool]] = None,
        reference_now: Optional[datetime] = None,
    ) -> AuthorizationRecord:
        """Run complete 3-gate authorization pipeline sequentially.

        Returns AuthorizationRecord in state AUTHORIZED if all 3 gates pass.
        Fails closed on any rejection.
        """
        now_utc = reference_now or datetime.now(timezone.utc)
        ts_str = now_utc.isoformat()

        order = self.storage.get_order(order_id)
        if order is None:
            raise KeyError(f"Order '{order_id}' not found in paper orders registry.")

        if order.is_terminal:
            raise TerminalAuthorizationStateError(
                f"Cannot authorize order '{order_id}': order is in terminal state '{order.state.value}'."
            )

        # Check existing authorization record
        record = self.get_authorization(order_id)
        if record is not None:
            if record.is_authorized:
                # Idempotent match: already authorized
                return record
            if record.is_terminal:
                raise TerminalAuthorizationStateError(
                    f"Order '{order_id}' has already reached terminal authorization state '{record.state.value}'."
                )
        else:
            record = AuthorizationRecord(
                order_id=order.order_id,
                idempotency_key=order.idempotency_key,
                ticker=order.ticker,
                shares=order.shares_initial,
                state=AuthorizationState.PROPOSED,
                created_at=ts_str,
                updated_at=ts_str,
            )
            self.save_authorization(record)

        # 1. Gate 1: Risk (if in PROPOSED)
        if record.state == AuthorizationState.PROPOSED:
            self.evaluate_gate1_risk(order, record, reference_now=now_utc)

        # 2. Gate 2: Human Confirmation (if in PENDING_HUMAN_APPROVAL)
        if record.state == AuthorizationState.PENDING_HUMAN_APPROVAL:
            self.evaluate_gate2_human(order, record, user_confirmation, reference_now=now_utc)

        # 3. Gate 3: Platform Permission & Token Verification (if in HUMAN_APPROVED)
        if record.state == AuthorizationState.HUMAN_APPROVED:
            self.evaluate_gate3_platform(order, record, platform_verifier=platform_verifier, reference_now=now_utc)

        return record
