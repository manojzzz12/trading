"""Paper trading coordinator and execution engine for Phase 6.

Enforces:
1. Strict requirement for --paper-only flag on every state mutation.
2. Market-session and calendar validation (NYSE regular hours 09:30-16:00 ET,
   rejection of weekends and exchange holidays).
3. Timestamp freshness validation (no future timestamps, maximum backdating window).
4. Frozen risk controls:
   - Stock-only, long-only.
   - Max 4 simultaneous positions.
   - 0.50% risk per trade limit (measured from entry to stop loss).
   - 20% minimum cash reserve.
   - 80% maximum invested capital.
   - 25% single-ticker allocation cap.
5. Invariant-preserving ledger transactions via AccountingLedger with
   commit-manifest transactional persistence across all state files.
6. Zero-recovery delisting quantity integrity, explicit lot-id selection on
   multi-lot positions, and strict prohibition of nonzero fees on zero-recovery write-offs.
7. Zero network or broker imports.
"""

from datetime import datetime, timedelta, timezone, time
from decimal import Decimal
import os
import sys
import uuid
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from accounting import (
    AccountingLedger,
    AccountingError,
    AccountingInvariantViolation,
    quantize_cent,
    to_decimal,
)
from market_data_models import (
    EASTERN,
    MarketClock,
    MarketSessionState,
    parse_iso_timestamp,
    to_eastern,
    to_utc,
    UTC,
)
from paper_models import (
    PaperExitReason,
    PaperExitRecord,
    PaperFillRecord,
    PaperOrderSide,
    PaperPositionMetadata,
    PaperTradeAuditEntry,
)
from order_models import (
    IllegalStateTransitionError,
    OrderStateTransition,
    PaperOrder,
    PaperOrderState,
    TerminalOrderStateError,
    TERMINAL_ORDER_STATES,
)
from order_idempotency import (
    CanonicalOrderIntent,
    ConflictingFillIdentityError,
    ConflictingIdempotencyKeyError,
    DuplicateFillError,
    DuplicateOrderError,
    compute_fill_identity,
    compute_manual_fill_identity,
    compute_order_id,
)
from order_service import PaperOrderEngine
from authorization_models import (
    AuthorizationError,
    AuthorizationRecord,
    AuthorizationState,
    GateVerdict,
    HMACApprovalToken,
    HumanGateRejectionError,
    PlatformPermissionError,
    RiskGateRejectionError,
    TokenExpiredError,
    TokenTamperedError,
    TokenAlreadyUsedError,
    TokenBindingMismatchError,
)
from authorization_service import HumanAuthorizationService
from paper_storage import PaperStorageManager
from universe import validate_ticker

# Default maximum backdating window for manual paper fill entry (24 hours)
DEFAULT_MAX_BACKDATING_WINDOW = timedelta(hours=24)


class PaperSafetyViolation(ValueError):
    """Raised when a paper safety boundary, forbidden flag, or missing --paper-only is encountered."""
    pass


class MarketSessionValidationError(ValueError):
    """Raised when an operation is attempted with an invalid market session or timestamp."""
    pass


class RiskViolationError(ValueError):
    """Raised when a paper trade violates frozen portfolio risk parameters."""
    pass


class PaperTradingService:
    """Coordinates manual paper trading operations against the external paper state."""

    def __init__(
        self,
        storage: Optional[PaperStorageManager] = None,
        max_backdating_window: Optional[timedelta] = None,
        allow_stale: bool = False,
    ):
        self.storage = storage or PaperStorageManager()
        self.max_backdating_window = (
            max_backdating_window if max_backdating_window is not None else DEFAULT_MAX_BACKDATING_WINDOW
        )
        self.allow_stale = allow_stale
        self.order_engine = PaperOrderEngine(storage=self.storage)
        self.auth_service = HumanAuthorizationService(storage=self.storage)

    # -------------------------------------------------------------------------
    # Multi-Gate Human Authorization Management (Phase 8.1)
    # -------------------------------------------------------------------------

    def render_authorization_ticket(self, order_id: str) -> str:
        """Render unambiguous plain-text authorization ticket for human confirmation."""
        order = self.get_order(order_id)
        if order is None:
            raise KeyError(f"Order '{order_id}' not found.")
        return self.auth_service.render_authorization_ticket(order)

    def authorize_order(
        self,
        order_id: str,
        user_confirmation: str,
        platform_verifier: Optional[Callable[[PaperOrder, HMACApprovalToken], bool]] = None,
        reference_now: Optional[datetime] = None,
    ) -> AuthorizationRecord:
        """Execute 3-gate authorization (Gate 1: Risk, Gate 2: Human, Gate 3: Platform)."""
        return self.auth_service.authorize_order(
            order_id=order_id,
            user_confirmation=user_confirmation,
            platform_verifier=platform_verifier,
            reference_now=reference_now,
        )

    def get_authorization(self, order_id: str) -> Optional[AuthorizationRecord]:
        """Retrieve authorization record by order ID."""
        return self.auth_service.get_authorization(order_id)

    # -------------------------------------------------------------------------
    # Order Lifecycle & Idempotency Management (Phase 8.0)
    # -------------------------------------------------------------------------

    def create_order(
        self,
        intent: CanonicalOrderIntent,
        reference_now: Optional[datetime] = None,
        order_notes: str = "",
    ) -> PaperOrder:
        """Create a new proposed paper order with deterministic idempotency deduplication."""
        return self.order_engine.create_order(
            intent=intent,
            reference_now=reference_now,
            order_notes=order_notes,
        )

    def transition_order(
        self,
        order_id: str,
        new_state: PaperOrderState,
        reason: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        timestamp: Optional[str] = None,
    ) -> PaperOrder:
        """Transition an order to a new state enforcing the legal transition table."""
        return self.order_engine.transition_order(
            order_id=order_id,
            new_state=new_state,
            reason=reason,
            metadata=metadata,
            timestamp=timestamp,
        )

    def get_order(self, order_id: str) -> Optional[PaperOrder]:
        """Retrieve an order by its order ID."""
        return self.order_engine.get_order(order_id)

    def get_order_by_idempotency_key(self, idempotency_key: str) -> Optional[PaperOrder]:
        """Retrieve an order by its idempotency key."""
        return self.order_engine.get_order_by_idempotency_key(idempotency_key)

    def list_orders(
        self,
        state: Optional[PaperOrderState] = None,
        ticker: Optional[str] = None,
    ) -> List[PaperOrder]:
        """List orders matching optional state and ticker filters."""
        return self.order_engine.list_orders(state=state, ticker=ticker)

    # -------------------------------------------------------------------------
    # Safety and Validation Rules
    # -------------------------------------------------------------------------

    @staticmethod
    def assert_paper_only(paper_only: bool) -> None:
        """Enforce that every state-modifying action explicitly includes --paper-only."""
        if not paper_only:
            raise PaperSafetyViolation(
                "CRITICAL SAFETY VIOLATION: State mutation refused. "
                "Every state-changing paper command requires the explicit --paper-only flag."
            )

    @staticmethod
    def validate_symbol(ticker: str) -> str:
        """Ensure symbol is an approved, valid US equity ticker."""
        sym = ticker.strip().upper()
        if not sym:
            raise ValueError("Ticker symbol cannot be empty.")
        # Validate syntax and character set using core universe validator
        try:
            return validate_ticker(sym)
        except ValueError as err:
            raise ValueError(f"Invalid equity ticker '{sym}': {err}") from err

    @staticmethod
    def validate_timestamp(
        ts_input: Any,
        reference_now: Optional[datetime] = None,
        max_backdating_window: Optional[timedelta] = None,
        allow_stale: bool = False,
    ) -> Tuple[datetime, str]:
        """Validate format, session, freshness, and backdating window of timestamp."""
        if isinstance(ts_input, datetime):
            dt = ts_input
        elif isinstance(ts_input, str):
            dt = parse_iso_timestamp(ts_input)
        else:
            raise TypeError(f"Expected datetime or ISO string, got {type(ts_input)}")

        now_utc = reference_now or datetime.now(timezone.utc)
        dt_utc = to_utc(dt)

        # 1. Future timestamp check
        if dt_utc > now_utc:
            raise MarketSessionValidationError(
                f"Future timestamp rejected: '{dt.isoformat()}' is after current reference time '{now_utc.isoformat()}'"
            )

        # 2. Maximum backdating window check (default: 24 hours)
        window = max_backdating_window if max_backdating_window is not None else DEFAULT_MAX_BACKDATING_WINDOW
        if not allow_stale and window is not None:
            elapsed = now_utc - dt_utc
            if elapsed > window:
                elapsed_hours = elapsed.total_seconds() / 3600.0
                window_hours = window.total_seconds() / 3600.0
                raise MarketSessionValidationError(
                    f"STALE_FILL_REJECTED: Fill timestamp '{dt.isoformat()}' is {elapsed_hours:.1f} hours old, "
                    f"exceeding the maximum allowed backdating window of {window_hours:.1f} hours. "
                    "Manual TradingView paper fills must be recorded within 24 hours of execution."
                )

        # 3. Market session check in US Eastern time
        dt_et = to_eastern(dt)
        d_date = dt_et.date()

        # Check trading day (excludes weekends and holidays)
        is_trading, reason = MarketClock.is_historical_trading_day(d_date)
        if not is_trading:
            # Fall back to live calendar check if year is 2026-2027
            is_live_trading, live_reason = MarketClock.is_trading_day(d_date)
            if not is_live_trading:
                raise MarketSessionValidationError(
                    f"Non-trading day rejected: {d_date.isoformat()} is not a regular trading session ({reason} / {live_reason})"
                )

        # Check regular trading hours (09:30 - 16:00 ET, or 13:00 on early close)
        session = MarketClock.get_historical_session(d_date) or MarketClock.get_session(d_date)
        if not session:
            raise MarketSessionValidationError(f"Market closed on {d_date.isoformat()}")

        open_time, close_time = session
        t = dt_et.time()
        if not (open_time <= t <= close_time):
            raise MarketSessionValidationError(
                f"Off-hours timestamp rejected: {t} ET is outside NYSE regular session hours "
                f"({open_time.strftime('%H:%M')} - {close_time.strftime('%H:%M')} ET) on {d_date.isoformat()}."
            )

        return dt_utc, dt_utc.isoformat()

    # -------------------------------------------------------------------------
    # Core Queries & Status
    # -------------------------------------------------------------------------

    def get_status(self) -> Dict[str, Any]:
        """Read-only view of current paper trading portfolio status and invariants."""
        journal = self.storage.load_journal()
        ledger = journal.ledger
        if ledger is None:
            raise ValueError("Uninitialized ledger state.")

        invariant_result = ledger.validate_equity_invariant()
        portfolio_meta = self.storage.load_portfolio()
        lots_meta = portfolio_meta.get("lots", {})

        # Active tickers
        active_tickers = [
            ticker for ticker, pos in ledger.positions.items()
            if pos.total_shares > 0
        ]

        active_lots_list = []
        for ticker, pos in ledger.positions.items():
            for lot in pos.lots:
                if lot.shares_remaining > 0:
                    meta = lots_meta.get(lot.lot_id, {})
                    active_lots_list.append({
                        "lot_id": lot.lot_id,
                        "ticker": ticker,
                        "shares": lot.shares_remaining,
                        "entry_price": str(lot.entry_price),
                        "cost_basis": str(lot.total_lot_cost_basis_usd),
                        "stop_loss": meta.get("stop_loss", "N/A"),
                        "target": meta.get("target", "N/A"),
                        "entry_timestamp": meta.get("entry_timestamp", lot.entry_timestamp),
                        "order_notes": meta.get("order_notes", ""),
                    })

        total_fees = sum(
            (to_decimal(t.get("entry_fee_usd", "0.00")) + to_decimal(t.get("exit_fee_usd", "0.00")))
            for t in ledger.closed_trades
        ) + sum(
            p.total_entry_fee_remaining_usd for p in ledger.positions.values()
        )

        return {
            "initial_capital_usd": str(ledger.initial_capital_usd),
            "cash_usd": str(ledger.cash_usd),
            "total_market_value_usd": str(ledger.gross_market_value_usd),
            "total_equity_usd": str(ledger.total_equity_usd),
            "realized_pnl_usd": str(ledger.cumulative_realized_trading_pnl_usd),
            "unrealized_pnl_usd": str(ledger.unrealized_trading_pnl_usd),
            "total_fees_usd": str(quantize_cent(total_fees)),
            "open_positions_count": len(active_tickers),
            "active_tickers": active_tickers,
            "max_simultaneous_positions_limit": 4,
            "cash_reserve_pct": str((ledger.cash_usd / ledger.total_equity_usd * Decimal("100.0")).quantize(Decimal("0.01"))),
            "invested_capital_pct": str((ledger.gross_market_value_usd / ledger.total_equity_usd * Decimal("100.0")).quantize(Decimal("0.01"))),
            "equity_invariant_holds": invariant_result["holds"],
            "active_lots": active_lots_list,
        }

    # -------------------------------------------------------------------------
    # Manual Fill Recording (Entry)
    # -------------------------------------------------------------------------

    def record_paper_entry(
        self,
        ticker: str,
        shares: int,
        price: Any,
        stop_loss: Any,
        timestamp: Any,
        fee: Any = "0.00",
        target: Optional[Any] = None,
        order_notes: str = "",
        paper_only: bool = False,
        reference_now: Optional[datetime] = None,
        max_backdating_window: Optional[timedelta] = None,
        allow_stale: bool = False,
        failure_injection_point: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        order_id: Optional[str] = None,
        fill_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record and validate a user-confirmed TradingView manual paper entry fill."""
        self.assert_paper_only(paper_only)

        # 1. Clean & validate inputs
        sym = self.validate_symbol(ticker)
        q_shares = int(shares)
        if q_shares <= 0:
            raise ValueError(f"Share quantity must be positive integer, got {q_shares}")

        px = quantize_cent(to_decimal(price))
        if px <= Decimal("0.00"):
            raise ValueError(f"Entry price must be positive, got {px}")

        stop_px = quantize_cent(to_decimal(stop_loss))
        if stop_px <= Decimal("0.00"):
            raise ValueError(f"Stop-loss price must be positive, got {stop_px}")
        if stop_px >= px:
            raise RiskViolationError(
                f"INVALID_STOP_LOSS: Stop price (${stop_px}) must be strictly below "
                f"entry price (${px}) for long positions."
            )

        target_px = quantize_cent(to_decimal(target)) if target is not None else None
        if target_px is not None and target_px <= px:
            raise RiskViolationError(
                f"INVALID_TARGET: Target price (${target_px}) must be strictly above "
                f"entry price (${px})."
            )

        fee_usd = quantize_cent(to_decimal(fee))
        if fee_usd < Decimal("0.00"):
            raise ValueError(f"Fee cannot be negative, got {fee_usd}")

        window = max_backdating_window if max_backdating_window is not None else self.max_backdating_window
        dt_utc, ts_str = self.validate_timestamp(
            timestamp,
            reference_now=reference_now,
            max_backdating_window=window,
            allow_stale=allow_stale or self.allow_stale,
        )

        # Check for existing duplicate fill event (GAP-01 Duplicate Fill Protection)
        orders_data = self.storage.load_orders()
        existing_fills = orders_data.get("fills", [])

        target_fill_id = fill_id
        if not target_fill_id and idempotency_key:
            for f in existing_fills:
                if f.get("idempotency_key") == idempotency_key or f.get("fill_id") == f"FILL-{idempotency_key}":
                    target_fill_id = f.get("fill_id")
                    break

        if target_fill_id:
            matched_fill = next((f for f in existing_fills if f.get("fill_id") == target_fill_id), None)
            if matched_fill is not None:
                m_sym = matched_fill.get("ticker")
                m_shares = int(matched_fill.get("shares", 0))
                m_price = quantize_cent(to_decimal(matched_fill.get("price", "0")))
                m_stop = quantize_cent(to_decimal(matched_fill.get("stop_loss", "0")))

                if m_sym == sym and m_shares == q_shares and m_price == px and m_stop == stop_px:
                    # Idempotent match: return existing fill record without mutating ledger or cash
                    journal = self.storage.load_journal()
                    ledger = journal.ledger
                    return {
                        "status": "RECORDED",
                        "fill_id": matched_fill.get("fill_id"),
                        "lot_id": matched_fill.get("lot_id"),
                        "order_id": matched_fill.get("order_id"),
                        "idempotency_key": matched_fill.get("idempotency_key"),
                        "ticker": sym,
                        "shares": q_shares,
                        "entry_price": str(px),
                        "stop_loss": str(stop_px),
                        "target": str(target_px) if target_px is not None else None,
                        "fee": str(fee_usd),
                        "cash_remaining": str(ledger.cash_usd) if ledger else "0.00",
                        "equity_total": str(ledger.total_equity_usd) if ledger else "0.00",
                        "timestamp": matched_fill.get("timestamp", ts_str),
                        "idempotent_replay": True,
                    }
                else:
                    raise ConflictingFillIdentityError(
                        f"Conflicting fill identity '{target_fill_id}': attributes do not match existing fill "
                        f"(existing: {m_sym} {m_shares}sh @ ${m_price}, requested: {sym} {q_shares}sh @ ${px})."
                    )

        # If linked to an order, validate order state and remaining shares
        linked_order = None
        if order_id:
            linked_order = self.storage.get_order(order_id)
            if linked_order is None:
                raise KeyError(f"Linked order '{order_id}' not found in orders registry.")
            if linked_order.is_terminal:
                raise TerminalOrderStateError(
                    f"Linked order '{order_id}' is in terminal state '{linked_order.state.value}'."
                )
            if q_shares > linked_order.shares_remaining:
                raise ValueError(
                    f"Fill quantity ({q_shares}) exceeds order remaining shares ({linked_order.shares_remaining})."
                )
            if not target_fill_id:
                target_fill_id = compute_fill_identity(
                    order_id=order_id,
                    fill_index=len(linked_order.fill_ids) + 1,
                    shares=q_shares,
                    price=px,
                    timestamp=ts_str,
                )

        # 2. Load state
        journal = self.storage.load_journal()
        ledger = journal.ledger
        if ledger is None:
            raise ValueError("Uninitialized ledger state.")

        # Re-assert invariant before mutation
        ledger.validate_equity_invariant()
        equity = ledger.total_equity_usd

        # 3. Enforce Frozen Risk Controls
        # 3a. Maximum 4 simultaneous open positions
        active_tickers: Set[str] = {
            t for t, p in ledger.positions.items() if p.total_shares > 0
        }
        if sym not in active_tickers and len(active_tickers) >= 4:
            raise RiskViolationError(
                f"MAX_POSITIONS_EXCEEDED: Cannot open position in '{sym}'. Maximum 4 simultaneous "
                f"positions already held: {sorted(list(active_tickers))}."
            )

        # 3b. Per-trade risk limit: (Price - Stop) * Shares <= 0.50% of Equity
        risk_per_share = px - stop_px
        total_trade_risk = (risk_per_share * Decimal(q_shares)).quantize(Decimal("0.01"))
        max_allowed_risk = (equity * Decimal("0.0050")).quantize(Decimal("0.01"))
        if total_trade_risk > max_allowed_risk:
            raise RiskViolationError(
                f"PER_TRADE_RISK_EXCEEDED: Total trade risk (${total_trade_risk}) exceeds "
                f"0.50% limit (${max_allowed_risk}) based on portfolio equity (${equity}). "
                f"Maximum allowed shares at this stop: {int(max_allowed_risk / risk_per_share)}."
            )

        # 3c. Available cash & 20% minimum cash reserve
        principal = (px * Decimal(q_shares)).quantize(Decimal("0.01"))
        total_cost = principal + fee_usd
        if total_cost > ledger.cash_usd:
            raise RiskViolationError(
                f"INSUFFICIENT_CASH: Total cost (${total_cost}) exceeds available cash (${ledger.cash_usd})."
            )

        min_cash_required = (equity * Decimal("0.20")).quantize(Decimal("0.01"))
        cash_after = ledger.cash_usd - total_cost
        if cash_after < min_cash_required:
            raise RiskViolationError(
                f"MIN_CASH_RESERVE_VIOLATION: Purchase would leave ${cash_after} cash, "
                f"violating the 20% minimum cash reserve requirement (${min_cash_required})."
            )

        # 3d. Maximum 80% invested capital
        market_val_after = ledger.gross_market_value_usd + principal
        max_invested_allowed = (equity * Decimal("0.80")).quantize(Decimal("0.01"))
        if market_val_after > max_invested_allowed:
            raise RiskViolationError(
                f"MAX_INVESTED_CAPITAL_VIOLATION: Total market value after trade (${market_val_after}) "
                f"would exceed 80% maximum invested capital cap (${max_invested_allowed})."
            )

        # 3e. Maximum 25% allocation to single ticker
        existing_pos = ledger.positions.get(sym)
        existing_basis = existing_pos.total_cost_basis_usd if existing_pos else Decimal("0.00")
        ticker_alloc_after = existing_basis + principal
        max_ticker_alloc_allowed = (equity * Decimal("0.25")).quantize(Decimal("0.01"))
        if ticker_alloc_after > max_ticker_alloc_allowed:
            raise RiskViolationError(
                f"TICKER_ALLOCATION_CAP_EXCEEDED: Allocation to '{sym}' after trade (${ticker_alloc_after}) "
                f"would exceed 25% single-ticker limit (${max_ticker_alloc_allowed})."
            )

        # 4. Authoritative Ledger Execution
        lot = ledger.buy(
            ticker=sym,
            shares=q_shares,
            price=px,
            fee=fee_usd,
            timestamp=ts_str,
        )
        lot_id = lot.lot_id

        # 5. Prepare Lot Metadata
        portfolio_data = self.storage.load_portfolio()
        lot_meta = PaperPositionMetadata(
            ticker=sym,
            lot_id=lot_id,
            shares=q_shares,
            entry_price=px,
            entry_fee=fee_usd,
            entry_timestamp=ts_str,
            stop_loss=stop_px,
            target=target_px,
            order_notes=order_notes,
        )
        portfolio_data.setdefault("lots", {})[lot_id] = lot_meta.to_dict()

        # 6. Prepare Orders Log
        effective_fill_id = (
            target_fill_id
            if target_fill_id
            else (f"FILL-{idempotency_key}" if idempotency_key else f"FILL-{uuid.uuid4().hex[:8].upper()}")
        )
        fill_record = PaperFillRecord(
            fill_id=effective_fill_id,
            ticker=sym,
            side=PaperOrderSide.BUY,
            shares=q_shares,
            price=px,
            fee=fee_usd,
            timestamp=ts_str,
            stop_loss=stop_px,
            target=target_px,
            order_notes=order_notes,
            lot_id=lot_id,
            order_id=order_id,
            idempotency_key=idempotency_key,
        )
        orders_data.setdefault("fills", []).append(fill_record.to_dict())

        # If order linked, update order state and remaining shares
        if linked_order:
            if linked_order.state == PaperOrderState.APPROVED:
                linked_order.transition_to(PaperOrderState.SUBMITTED, timestamp=ts_str, reason="AUTO_SUBMIT_FOR_FILL")
            linked_order.apply_fill(
                fill_id=effective_fill_id,
                shares=q_shares,
                price=px,
                timestamp=ts_str,
                fee=fee_usd,
            )
            orders_data.setdefault("orders", {})[linked_order.order_id] = linked_order.to_dict()

        # 7. Prepare Immutable Audit Event
        audit_entry = PaperTradeAuditEntry(
            event_id=f"EVT-{uuid.uuid4().hex[:8].upper()}",
            timestamp=datetime.now(timezone.utc).isoformat(),
            event_type="RECORD_ENTRY",
            ticker=sym,
            shares=q_shares,
            price=str(px),
            fee=str(fee_usd),
            equity_after=str(ledger.total_equity_usd),
            cash_after=str(ledger.cash_usd),
            details={
                "fill_id": effective_fill_id,
                "lot_id": lot_id,
                "order_id": order_id,
                "idempotency_key": idempotency_key,
                "stop_loss": str(stop_px),
                "target": str(target_px) if target_px is not None else None,
                "notes": order_notes,
            },
        )

        # 8. Transactional Multi-File Commit Manifest
        journal_payload = {
            "schema_version": journal.metadata.get("schema_version", "1.0.0"),
            "system": "trader-swing",
            "metadata": journal.metadata,
            "ledger": ledger.to_dict(),
        }

        self.storage.commit_transaction(
            journal_payload=journal_payload,
            portfolio_data=portfolio_data,
            orders_data=orders_data,
            audit_entry=audit_entry,
            failure_injection_point=failure_injection_point,
        )

        return {
            "status": "RECORDED",
            "fill_id": effective_fill_id,
            "lot_id": lot_id,
            "order_id": order_id,
            "idempotency_key": idempotency_key,
            "ticker": sym,
            "shares": q_shares,
            "entry_price": str(px),
            "stop_loss": str(stop_px),
            "target": str(target_px) if target_px is not None else None,
            "fee": str(fee_usd),
            "cash_remaining": str(ledger.cash_usd),
            "equity_total": str(ledger.total_equity_usd),
            "timestamp": ts_str,
        }

    # -------------------------------------------------------------------------
    # Manual Exit Recording
    # -------------------------------------------------------------------------

    def record_paper_exit(
        self,
        ticker: str,
        shares: Optional[int],
        price: Any,
        timestamp: Any,
        fee: Any = "0.00",
        reason: str = "MANUAL",
        lot_id: Optional[str] = None,
        order_notes: str = "",
        paper_only: bool = False,
        reference_now: Optional[datetime] = None,
        max_backdating_window: Optional[timedelta] = None,
        allow_stale: bool = False,
        failure_injection_point: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        exit_id: Optional[str] = None,
        order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record and validate a user-confirmed TradingView manual paper exit or write-off."""
        self.assert_paper_only(paper_only)

        sym = self.validate_symbol(ticker)
        window = max_backdating_window if max_backdating_window is not None else self.max_backdating_window
        dt_utc, ts_str = self.validate_timestamp(
            timestamp,
            reference_now=reference_now,
            max_backdating_window=window,
            allow_stale=allow_stale or self.allow_stale,
        )

        px = quantize_cent(to_decimal(price))
        fee_usd = quantize_cent(to_decimal(fee))
        if fee_usd < Decimal("0.00"):
            raise ValueError(f"Fee cannot be negative, got {fee_usd}")

        # Parse exit reason
        reason_enum: PaperExitReason
        try:
            reason_enum = PaperExitReason(reason.upper())
        except ValueError:
            reason_enum = PaperExitReason.MANUAL

        # Check for existing duplicate exit event (Duplicate Exit Replay Protection)
        orders_data = self.storage.load_orders()
        existing_exits = orders_data.get("exits", [])

        target_exit_id = exit_id
        if not target_exit_id and idempotency_key:
            for ex in existing_exits:
                if ex.get("idempotency_key") == idempotency_key or ex.get("exit_id") == f"EXIT-{idempotency_key}":
                    target_exit_id = ex.get("exit_id")
                    break

        if target_exit_id:
            matched_exit = next((ex for ex in existing_exits if ex.get("exit_id") == target_exit_id), None)
            if matched_exit is not None:
                m_sym = matched_exit.get("ticker")
                m_px = quantize_cent(to_decimal(matched_exit.get("price", "0")))
                m_shares = int(matched_exit.get("shares", 0))
                req_shares = int(shares) if shares is not None else m_shares

                if m_sym == sym and m_px == px and (shares is None or m_shares == req_shares):
                    journal = self.storage.load_journal()
                    ledger = journal.ledger
                    return {
                        "status": "RECORDED",
                        "exit_id": matched_exit.get("exit_id"),
                        "ticker": sym,
                        "shares": m_shares,
                        "exit_price": str(px),
                        "fee": str(fee_usd),
                        "reason": matched_exit.get("reason", reason_enum.value),
                        "realized_pnl": matched_exit.get("realized_pnl", "0.00"),
                        "lot_id": matched_exit.get("lot_id"),
                        "cash_after": str(ledger.cash_usd) if ledger else "0.00",
                        "equity_after": str(ledger.total_equity_usd) if ledger else "0.00",
                        "timestamp": matched_exit.get("timestamp", ts_str),
                        "idempotent_replay": True,
                    }
                else:
                    raise ConflictingFillIdentityError(
                        f"Conflicting exit identity '{target_exit_id}': attributes do not match existing exit."
                    )

        # Load state
        journal = self.storage.load_journal()
        ledger = journal.ledger
        if ledger is None:
            raise ValueError("Uninitialized ledger state.")

        ledger.validate_equity_invariant()

        pos = ledger.positions.get(sym)
        if not pos or pos.total_shares <= 0:
            raise RiskViolationError(f"NO_OPEN_POSITION: No open shares found for ticker '{sym}'.")

        open_lots = [l for l in pos.lots if l.shares_remaining > 0]
        if not open_lots:
            raise RiskViolationError(f"NO_OPEN_POSITION: No open lots found for ticker '{sym}'.")

        effective_exit_id = (
            target_exit_id
            if target_exit_id
            else (f"EXIT-{idempotency_key}" if idempotency_key else f"EXIT-{uuid.uuid4().hex[:8].upper()}")
        )
        realized_pnl: Decimal
        actual_shares: int
        target_lot_id: Optional[str] = None

        if px <= Decimal("0.00"):
            # -----------------------------------------------------------------
            # Zero-recovery write-off path (Strict Phase 6.1 Integrity)
            # -----------------------------------------------------------------
            if reason_enum != PaperExitReason.DELISTING_ZERO_RECOVERY:
                raise RiskViolationError(
                    f"ZERO_PRICE_EXIT_REQUIRES_WRITE_OFF: Exit price <= 0 is only permitted for "
                    f"DELISTING_ZERO_RECOVERY, got {reason_enum.value}"
                )

            # Rule 1: Zero price permits ZERO exit fee only
            if fee_usd > Decimal("0.00"):
                raise RiskViolationError(
                    f"ZERO_RECOVERY_FEE_PROHIBITED: Exit fee must be 0.00 for zero-recovery write-offs, got ${fee_usd}."
                )

            # Rule 2: Multi-lot selection requirement
            if len(open_lots) > 1 and not lot_id:
                raise RiskViolationError(
                    f"AMBIGUOUS_LOT_SELECTION: Multiple open lots exist for ticker '{sym}' "
                    f"({[l.lot_id for l in open_lots]}); --lot-id must be explicitly specified."
                )

            # Find target lot
            if lot_id:
                matched = [l for l in open_lots if l.lot_id == lot_id]
                if not matched:
                    raise RiskViolationError(
                        f"LOT_NOT_FOUND: Open lot '{lot_id}' not found for ticker '{sym}'."
                    )
                target_lot = matched[0]
            else:
                target_lot = open_lots[0]

            target_lot_id = target_lot.lot_id
            target_shares = target_lot.shares_remaining

            # Rule 3: Reject partial zero-recovery write-offs
            if shares is not None and int(shares) != target_shares:
                raise RiskViolationError(
                    f"PARTIAL_ZERO_RECOVERY_REJECTED: Zero-recovery write-offs must be for the entire lot quantity "
                    f"({target_shares} shares), requested {shares} shares."
                )

            actual_shares = target_shares

            # Authoritative write off in ledger
            write_off_result = ledger.write_off(
                ticker=sym,
                lot_id=target_lot_id,
                timestamp=ts_str,
            )
            realized_pnl = to_decimal(write_off_result["realized_net_pnl"])

        else:
            # -----------------------------------------------------------------
            # Positive-price exit path (Standard or Positive Delisting)
            # -----------------------------------------------------------------
            q_shares = int(shares) if shares is not None else pos.total_shares
            if q_shares <= 0:
                raise ValueError(f"Exit shares must be positive, got {q_shares}")
            if q_shares > pos.total_shares:
                raise RiskViolationError(
                    f"INSUFFICIENT_SHARES: Requested {q_shares} shares to exit, but only {pos.total_shares} "
                    f"open shares available for '{sym}'."
                )

            target_lot_id = lot_id
            actual_shares = q_shares

            sell_result = ledger.sell(
                ticker=sym,
                shares=q_shares,
                price=px,
                fee=fee_usd,
                timestamp=ts_str,
                lot_id=lot_id,
            )
            realized_pnl = to_decimal(sell_result["realized_net_pnl"])

        # ---------------------------------------------------------------------
        # Multi-File Metadata & Commit Preparation
        # ---------------------------------------------------------------------
        portfolio_data = self.storage.load_portfolio()
        lots_dict = portfolio_data.get("lots", {})

        # Re-check open lots in ledger to clean up metadata for closed lots
        open_lot_ids: Set[str] = set()
        pos_after = ledger.positions.get(sym)
        if pos_after:
            for l in pos_after.lots:
                if l.shares_remaining > 0:
                    open_lot_ids.add(l.lot_id)
                    # Update shares in portfolio metadata if partially sold
                    if l.lot_id in lots_dict:
                        lots_dict[l.lot_id]["shares"] = l.shares_remaining

        # Remove closed lots for this ticker from portfolio metadata
        to_delete = [
            lid for lid, meta in lots_dict.items()
            if meta.get("ticker") == sym and lid not in open_lot_ids
        ]
        for lid in to_delete:
            del lots_dict[lid]

        # Record in orders registry
        exit_record = PaperExitRecord(
            exit_id=effective_exit_id,
            ticker=sym,
            shares=actual_shares,
            price=px,
            fee=fee_usd,
            timestamp=ts_str,
            reason=reason_enum,
            realized_pnl=realized_pnl,
            lot_id=target_lot_id,
            order_notes=order_notes,
            order_id=order_id,
            idempotency_key=idempotency_key,
        )
        orders_data = self.storage.load_orders()
        orders_data.setdefault("exits", []).append(exit_record.to_dict())

        # Append audit entry
        audit_entry = PaperTradeAuditEntry(
            event_id=f"EVT-{uuid.uuid4().hex[:8].upper()}",
            timestamp=datetime.now(timezone.utc).isoformat(),
            event_type="RECORD_EXIT",
            ticker=sym,
            shares=actual_shares,
            price=str(px),
            fee=str(fee_usd),
            equity_after=str(ledger.total_equity_usd),
            cash_after=str(ledger.cash_usd),
            details={
                "exit_id": effective_exit_id,
                "order_id": order_id,
                "idempotency_key": idempotency_key,
                "lot_id": target_lot_id,
                "shares_written_off" if px <= Decimal("0.00") else "shares_exited": actual_shares,
                "reason": reason_enum.value,
                "realized_pnl": str(realized_pnl),
                "notes": order_notes,
            },
        )

        # Atomically commit transaction manifest
        journal_payload = {
            "schema_version": journal.metadata.get("schema_version", "1.0.0"),
            "system": "trader-swing",
            "metadata": journal.metadata,
            "ledger": ledger.to_dict(),
        }

        self.storage.commit_transaction(
            journal_payload=journal_payload,
            portfolio_data=portfolio_data,
            orders_data=orders_data,
            audit_entry=audit_entry,
            failure_injection_point=failure_injection_point,
        )

        return {
            "status": "RECORDED",
            "exit_id": effective_exit_id,
            "order_id": order_id,
            "idempotency_key": idempotency_key,
            "ticker": sym,
            "shares": actual_shares,
            "exit_price": str(px),
            "fee": str(fee_usd),
            "reason": reason_enum.value,
            "realized_pnl": str(realized_pnl),
            "lot_id": target_lot_id,
            "cash_after": str(ledger.cash_usd),
            "equity_after": str(ledger.total_equity_usd),
            "timestamp": ts_str,
        }
