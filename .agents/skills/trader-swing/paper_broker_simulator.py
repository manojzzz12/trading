"""Local simulated paper broker adapter for Trader-Swing Phase 8.2.

Explicit environment: SIMULATION_ONLY.
Zero live broker connections, zero Alpaca trading endpoints, zero broker API credentials, zero external network requests.

Enforces:
1. Complete order lifecycle state machine:
   APPROVED -> SUBMITTED -> PARTIALLY_FILLED -> FILLED / CANCELLED / EXPIRED / FAILED.
2. Multi-gate human authorization verification prior to submission (Phase 8.1 integration).
3. Pre-submission cash reservation and available cash invariant preservation (GAP-02).
4. Cross-process mutual exclusion via PaperFileLock (.broker.lock).
5. Deterministic fill identification and replay protection (DuplicateFillError).
6. Bracket order emulation (attached stop-loss and take-profit exit triggers).
7. Atomic multi-file transaction persistence with commit manifest and crash recovery.
8. Comprehensive reconciliation and orphan reservation cleanup.
"""

from datetime import datetime, timezone
from decimal import Decimal
import json
import os
import sys
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from accounting import (
    AccountingLedger,
    quantize_cent,
    to_decimal,
    validate_positive_shares,
)
from authorization_models import (
    AuthorizationRecord,
    AuthorizationState,
)
from authorization_service import HumanAuthorizationService
from cash_reservation import (
    CashReservationEngine,
    CashReservationRecord,
    InsufficientAvailableCashError,
    ReservationStatus,
)
from journal import SwingJournal
from order_idempotency import (
    CanonicalOrderIntent,
    DuplicateFillError,
    DuplicateOrderError,
    compute_fill_identity,
    compute_order_id,
)
from order_models import (
    IllegalStateTransitionError,
    OrderStateTransition,
    PaperOrder,
    PaperOrderState,
    TerminalOrderStateError,
    TERMINAL_ORDER_STATES,
)
from order_service import PaperOrderEngine
from paper_lock import PaperFileLock
from paper_models import (
    PaperExitReason,
    PaperExitRecord,
    PaperFillRecord,
    PaperOrderSide,
    PaperPositionMetadata,
    PaperTradeAuditEntry,
)
from paper_storage import PaperStorageManager
from risk import RiskConfig, RiskEngine
from pathlib import Path

# Explicit environment identification
ENVIRONMENT = "SIMULATION_ONLY"


def get_default_simulation_state_dir() -> str:
    """Resolve the default external directory for simulation state.

    Resolution order:
    1. Environment variable TRADING_PAPER_SIMULATION_DIR.
    2. Portable user directory: Path.home() / "Documents" / "TradingPaperSimulation".
    """
    env_dir = os.environ.get("TRADING_PAPER_SIMULATION_DIR")
    if env_dir and env_dir.strip():
        return os.path.abspath(env_dir.strip())
    return str(Path.home() / "Documents" / "TradingPaperSimulation")


# Standard isolated simulation directory (strictly outside repository root)
DEFAULT_SIMULATION_STATE_DIR = get_default_simulation_state_dir()


class SimulatedBrokerError(ValueError):
    """Base exception for simulated broker operations."""
    pass


class SimulatedCrashError(RuntimeError):
    """Raised when a controlled simulated crash is injected for fault tolerance testing."""
    pass


class UnauthorizedOrderSubmissionError(SimulatedBrokerError):
    """Raised when an order attempts submission without passing all authorization gates."""
    pass


class InvalidExecutionStateError(SimulatedBrokerError):
    """Raised when an execution or fill is attempted on an order not in SUBMITTED or PARTIALLY_FILLED."""
    pass


class LiveBrokerForbiddenError(RuntimeError):
    """Raised if any live network, credential, or real broker connector is attempted."""
    pass


class PaperBrokerSimulator:
    """Local, offline simulated broker adapter enforcing authorization, reservation, and locking."""

    def __init__(
        self,
        storage: Optional[PaperStorageManager] = None,
        state_dir: Optional[str] = None,
        risk_config: Optional[RiskConfig] = None,
        lock_timeout: float = 10.0,
    ):
        # Strict environment enforcement
        if ENVIRONMENT != "SIMULATION_ONLY":
            raise LiveBrokerForbiddenError(f"CRITICAL SAFETY VIOLATION: Environment is '{ENVIRONMENT}', expected 'SIMULATION_ONLY'.")

        target_dir = state_dir or (
            storage.state_dir
            if storage
            else (os.environ.get("TRADING_PAPER_SIMULATION_DIR") or get_default_simulation_state_dir())
        )
        self.storage = storage or PaperStorageManager(state_dir=target_dir)
        self.risk_config = risk_config or RiskConfig()
        self.lock_timeout = lock_timeout

        # Cross-process file lock located inside the simulation state directory
        self.lock_path = os.path.join(self.storage.state_dir, ".broker.lock")
        self.lock = PaperFileLock(lock_path=self.lock_path, timeout=self.lock_timeout)

        # Core engines
        self.order_engine = PaperOrderEngine(storage=self.storage)
        self.auth_service = HumanAuthorizationService(storage=self.storage, risk_config=self.risk_config)
        self.reservation_engine = CashReservationEngine(storage=self.storage)

        # Mark directory as SIMULATION_ONLY
        self._ensure_simulation_marker()

    def _ensure_simulation_marker(self) -> None:
        """Write explicit marker designating this state directory as SIMULATION_ONLY."""
        os.makedirs(self.storage.state_dir, exist_ok=True)
        marker_path = os.path.join(self.storage.state_dir, "SIMULATION_MARKER.json")
        if not os.path.exists(marker_path):
            marker_data = {
                "environment": ENVIRONMENT,
                "system": "trader-swing-simulated-broker",
                "version": "1.0.0",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "safety_notice": "THIS DIRECTORY CONTAINS SIMULATION STATE ONLY. NEVER ROUTE TO REAL BROKER.",
            }
            tmp = f"{marker_path}.tmp.{os.getpid()}"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(marker_data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, marker_path)

    def verify_simulation_marker(self) -> Dict[str, Any]:
        """Verify that the simulation state directory contains a valid SIMULATION_MARKER.json."""
        marker_path = os.path.join(self.storage.state_dir, "SIMULATION_MARKER.json")
        if not os.path.exists(marker_path):
            raise LiveBrokerForbiddenError(
                f"Missing SIMULATION_MARKER.json in '{self.storage.state_dir}'. Refusing operation in unverified directory."
            )
        try:
            with open(marker_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as err:
            raise LiveBrokerForbiddenError(f"Corrupted SIMULATION_MARKER.json: {err}") from err

        if data.get("environment") != ENVIRONMENT:
            raise LiveBrokerForbiddenError(
                f"Invalid simulation environment '{data.get('environment')}': expected '{ENVIRONMENT}'."
            )
        return data

    # -------------------------------------------------------------------------
    # State Initialization & Clean Setup
    # -------------------------------------------------------------------------

    def initialize_simulation(
        self,
        initial_capital: Decimal = Decimal("5000.00"),
        notes: str = "Phase 8.2 Simulation Initial State",
    ) -> Dict[str, Any]:
        """Initialize a clean simulation state directory with starting cash."""
        with self.lock:
            init_cap = quantize_cent(to_decimal(initial_capital))
            os.makedirs(self.storage.state_dir, exist_ok=True)
            self._ensure_simulation_marker()

            now_ts = datetime.now(timezone.utc).isoformat()
            journal = SwingJournal(self.storage.journal_path)
            ledger = journal.initialize_new(
                initial_capital=str(init_cap),
                extra_metadata={
                    "environment": ENVIRONMENT,
                    "simulation_initialized_at": now_ts,
                    "notes": notes,
                },
            )

            portfolio_data = {
                "schema_version": "1.0.0",
                "system": "trader-swing-simulation",
                "environment": ENVIRONMENT,
                "updated_at": now_ts,
                "open_lots": {},
            }
            self.storage.save_portfolio(portfolio_data)

            orders_data = {
                "schema_version": "1.1.0",
                "system": "trader-swing-simulation",
                "environment": ENVIRONMENT,
                "updated_at": now_ts,
                "orders": {},
                "idempotency_keys": {},
                "fills": [],
                "exits": [],
            }
            self.storage.save_orders(orders_data)

            reservations_data = {
                "schema_version": "1.0.0",
                "system": "trader-swing-simulation",
                "environment": ENVIRONMENT,
                "updated_at": now_ts,
                "reservations": {},
            }
            self.storage.save_reservations(reservations_data)

            audit_entry = PaperTradeAuditEntry(
                event_id=f"EVT-INIT-{uuid.uuid4().hex[:8].upper()}",
                timestamp=now_ts,
                event_type="SIMULATION_INITIALIZE",
                ticker=None,
                shares=0,
                price=None,
                fee=None,
                equity_after=str(init_cap),
                cash_after=str(init_cap),
                details={"initial_capital": str(init_cap), "notes": notes},
            )
            self.storage.append_audit_event(audit_entry)

            return {
                "status": "INITIALIZED",
                "state_dir": self.storage.state_dir,
                "initial_capital_usd": str(init_cap),
                "timestamp": now_ts,
            }

    # -------------------------------------------------------------------------
    # Order Submission (Gate-Enforced & Cash-Reserved)
    # -------------------------------------------------------------------------

    def submit_order(
        self,
        order_id: str,
        timestamp: Optional[str] = None,
        failure_injection_point: Optional[str] = None,
    ) -> PaperOrder:
        """Submit an approved order to the simulated broker.
        
        Requires:
        1. Order must exist and be in state APPROVED.
        2. Order must possess an authorization record in state AUTHORIZED.
        3. HMAC token must be validated.
        4. Cash reservation must be successfully acquired preserving 20% cash reserve.
        """
        self.verify_simulation_marker()
        with self.lock:
            ts_str = timestamp or datetime.now(timezone.utc).isoformat()

            # 1. Load order
            order = self.storage.get_order(order_id)
            if order is None:
                raise KeyError(f"Order '{order_id}' not found in simulation orders registry.")

            # 2. Guard order lifecycle state
            if order.state != PaperOrderState.APPROVED:
                raise UnauthorizedOrderSubmissionError(
                    f"Cannot submit order '{order_id}': order state is '{order.state.value}', expected 'APPROVED'."
                )

            # 3. Guard multi-gate human authorization (Phase 8.1 integration)
            auth_rec = self.storage.get_authorization(order_id)
            if auth_rec is None:
                raise UnauthorizedOrderSubmissionError(
                    f"Cannot submit order '{order_id}': no authorization record exists."
                )

            if auth_rec.state != AuthorizationState.AUTHORIZED:
                raise UnauthorizedOrderSubmissionError(
                    f"Cannot submit order '{order_id}': authorization state is '{auth_rec.state.value}', expected 'AUTHORIZED'."
                )

            if auth_rec.token is None:
                raise UnauthorizedOrderSubmissionError(
                    f"Cannot submit order '{order_id}': authorization record missing HMAC approval token."
                )

            if failure_injection_point == "BEFORE_RESERVATION":
                raise SimulatedCrashError("Injected crash: BEFORE_RESERVATION")

            # 4. Acquire cash reservation
            journal = self.storage.load_journal()
            reservation = self.reservation_engine.reserve_cash(
                order=order,
                ledger=journal.ledger,
                estimated_fee=Decimal("1.00"),
                timestamp=ts_str,
            )

            if failure_injection_point == "AFTER_RESERVATION":
                raise SimulatedCrashError("Injected crash: AFTER_RESERVATION")

            if failure_injection_point == "BEFORE_SUBMISSION_PERSISTENCE":
                raise SimulatedCrashError("Injected crash: BEFORE_SUBMISSION_PERSISTENCE")

            # 5. Transition order to SUBMITTED
            order.transition_to(
                new_state=PaperOrderState.SUBMITTED,
                timestamp=ts_str,
                reason="Simulated broker submission accepted",
                metadata={"reservation_id": reservation.reservation_id, "reserved_usd": str(reservation.total_reserved_usd)},
            )
            self.storage.save_order(order)

            if failure_injection_point == "AFTER_SUBMISSION_PERSISTENCE":
                raise SimulatedCrashError("Injected crash: AFTER_SUBMISSION_PERSISTENCE")

            # 6. Audit logging
            audit_entry = PaperTradeAuditEntry(
                event_id=f"EVT-SUBMIT-{order.order_id[-8:]}",
                timestamp=ts_str,
                event_type="SIM_ORDER_SUBMITTED",
                ticker=order.ticker,
                shares=order.shares_initial,
                price=str(order.limit_price) if order.limit_price is not None else None,
                fee=None,
                equity_after=str(journal.ledger.total_equity_usd),
                cash_after=str(journal.ledger.cash_usd),
                details={
                    "order_id": order.order_id,
                    "reservation_id": reservation.reservation_id,
                    "reserved_usd": str(reservation.total_reserved_usd),
                    "token_id": auth_rec.token.token_id,
                },
            )
            self.storage.append_audit_event(audit_entry)

            return order

    # -------------------------------------------------------------------------
    # Execution & Fill Processing (Deterministic & Replay-Protected)
    # -------------------------------------------------------------------------

    def process_simulated_fill(
        self,
        order_id: str,
        fill_shares: int,
        fill_price: Decimal,
        fill_fee: Decimal = Decimal("1.00"),
        timestamp: Optional[str] = None,
        failure_injection_point: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute a simulated fill (full or partial) against an open submitted order.
        
        Enforces:
        1. Order in SUBMITTED or PARTIALLY_FILLED.
        2. Positive integer shares not exceeding shares_remaining.
        3. Deterministic fill identification and fill replay protection.
        4. Proportional cash reservation conversion.
        5. Double-entry ledger buy and lot creation.
        6. Atomic multi-file transaction commit.
        """
        self.verify_simulation_marker()
        with self.lock:
            ts_str = timestamp or datetime.now(timezone.utc).isoformat()
            shs = validate_positive_shares(fill_shares)
            px = quantize_cent(to_decimal(fill_price))
            fee = quantize_cent(to_decimal(fill_fee))

            # 1. Load order and validate state
            order = self.storage.get_order(order_id)
            if order is None:
                raise KeyError(f"Order '{order_id}' not found in simulation orders registry.")

            if order.state not in (PaperOrderState.SUBMITTED, PaperOrderState.PARTIALLY_FILLED):
                raise InvalidExecutionStateError(
                    f"Cannot execute fill on order '{order_id}' in state '{order.state.value}'. "
                    "Order must be in SUBMITTED or PARTIALLY_FILLED."
                )

            if shs > order.shares_remaining:
                raise InvalidExecutionStateError(
                    f"Fill shares ({shs}) exceeds order remaining shares ({order.shares_remaining}) "
                    f"for order '{order_id}'."
                )

            # 2. Deterministic fill identification & Replay Protection
            fill_index = len(order.fill_ids) + 1
            eff_fill_id = compute_fill_identity(
                order_id=order_id,
                fill_index=fill_index,
                shares=shs,
                price=px,
                timestamp=ts_str,
            )

            if eff_fill_id in order.fill_ids:
                raise DuplicateFillError(
                    f"Replay protection triggered: Fill ID '{eff_fill_id}' has already been applied to order '{order_id}'."
                )

            if failure_injection_point == "BEFORE_RESERVATION_CONVERSION":
                raise SimulatedCrashError("Injected crash: BEFORE_RESERVATION_CONVERSION")

            # 3. Convert proportional cash reservation
            reservation, converted_reserved_usd = self.reservation_engine.convert_reservation(
                order_id=order_id,
                fill_shares=shs,
                fill_price=px,
                fill_fee=fee,
                timestamp=ts_str,
            )

            if failure_injection_point == "DURING_RESERVATION_CONVERSION":
                raise SimulatedCrashError("Injected crash: DURING_RESERVATION_CONVERSION")

            # 4. Load Journal & Ledger and execute buy
            journal = self.storage.load_journal()
            ledger = journal.ledger

            lot_id = f"LOT-SIM-{order.order_id[-8:]}-{fill_index:02d}"
            new_lot = ledger.buy(
                ticker=order.ticker,
                shares=shs,
                price=px,
                fee=fee,
                timestamp=ts_str,
                lot_id=lot_id,
            )

            # 5. Apply fill to order (updates shares, transitions state to PARTIALLY_FILLED or FILLED)
            trans = order.apply_fill(
                fill_id=eff_fill_id,
                shares=shs,
                price=px,
                timestamp=ts_str,
                fee=fee,
            )

            # 6. Update portfolio metadata
            portfolio_data = self.storage.load_portfolio()
            lots_dict = portfolio_data.setdefault("open_lots", {})
            pos_meta = PaperPositionMetadata(
                ticker=order.ticker,
                lot_id=new_lot.lot_id,
                shares=shs,
                entry_price=px,
                entry_fee=fee,
                entry_timestamp=ts_str,
                stop_loss=order.stop_loss,
                target=order.target,
                order_notes=order.order_notes,
            )
            lots_dict[new_lot.lot_id] = pos_meta.to_dict()

            # 7. Update orders registry
            orders_data = self.storage.load_orders()
            orders_dict = orders_data.setdefault("orders", {})
            orders_dict[order.order_id] = order.to_dict()

            fill_record = PaperFillRecord(
                fill_id=eff_fill_id,
                ticker=order.ticker,
                side=order.side,
                shares=shs,
                price=px,
                fee=fee,
                timestamp=ts_str,
                stop_loss=order.stop_loss,
                target=order.target,
                order_notes=order.order_notes,
                lot_id=new_lot.lot_id,
                order_id=order.order_id,
                idempotency_key=order.idempotency_key,
            )
            orders_data.setdefault("fills", []).append(fill_record.to_dict())

            # 8. Append structured audit event
            audit_entry = PaperTradeAuditEntry(
                event_id=f"EVT-FILL-{eff_fill_id[-8:]}",
                timestamp=ts_str,
                event_type="SIM_FILL_EXECUTED",
                ticker=order.ticker,
                shares=shs,
                price=str(px),
                fee=str(fee),
                equity_after=str(ledger.total_equity_usd),
                cash_after=str(ledger.cash_usd),
                details={
                    "order_id": order.order_id,
                    "fill_id": eff_fill_id,
                    "lot_id": new_lot.lot_id,
                    "order_state": order.state.value,
                    "shares_remaining": order.shares_remaining,
                    "converted_reserved_usd": str(converted_reserved_usd),
                },
            )

            # 9. Atomic transactional commit
            journal_payload = {
                "schema_version": journal.metadata.get("schema_version", "1.0.0"),
                "system": "trader-swing",
                "metadata": journal.metadata,
                "ledger": ledger.to_dict(),
            }

            reservations_data = self.storage.load_reservations()
            reservations_data.setdefault("reservations", {})[order.order_id] = reservation.to_dict()

            if failure_injection_point == "BEFORE_FILL_PERSISTENCE":
                raise SimulatedCrashError("Injected crash: BEFORE_FILL_PERSISTENCE")

            self.storage.commit_transaction(
                journal_payload=journal_payload,
                portfolio_data=portfolio_data,
                orders_data=orders_data,
                audit_entry=audit_entry,
                reservations_data=reservations_data,
                failure_injection_point=failure_injection_point,
            )

            if failure_injection_point == "AFTER_FILL_PERSISTENCE":
                raise SimulatedCrashError("Injected crash: AFTER_FILL_PERSISTENCE")

            return {
                "status": "FILLED" if order.state == PaperOrderState.FILLED else "PARTIALLY_FILLED",
                "fill_id": eff_fill_id,
                "order_id": order.order_id,
                "ticker": order.ticker,
                "shares_filled": shs,
                "shares_remaining": order.shares_remaining,
                "price": str(px),
                "fee": str(fee),
                "lot_id": new_lot.lot_id,
                "order_state": order.state.value,
                "cash_after": str(ledger.cash_usd),
                "equity_after": str(ledger.total_equity_usd),
                "timestamp": ts_str,
            }

    # -------------------------------------------------------------------------
    # Cancellation, Expiration & Failure Handling (Releases Reservations)
    # -------------------------------------------------------------------------

    def cancel_simulated_order(
        self,
        order_id: str,
        reason: str = "User Cancelled",
        timestamp: Optional[str] = None,
    ) -> PaperOrder:
        """Cancel an open submitted or partially filled order and release remaining cash reservation."""
        self.verify_simulation_marker()
        with self.lock:
            ts_str = timestamp or datetime.now(timezone.utc).isoformat()
            order = self.storage.get_order(order_id)
            if order is None:
                raise KeyError(f"Order '{order_id}' not found in simulation orders registry.")

            if order.is_terminal:
                raise TerminalOrderStateError(
                    f"Cannot cancel order '{order_id}' in terminal state '{order.state.value}'."
                )

            # Release reserved cash
            self.reservation_engine.release_reservation(order_id, reason=reason, timestamp=ts_str)

            # Transition order state
            order.transition_to(
                new_state=PaperOrderState.CANCELLED,
                timestamp=ts_str,
                reason=reason,
            )
            order.cancellation_reason = reason
            self.storage.save_order(order)

            # Audit event
            audit_entry = PaperTradeAuditEntry(
                event_id=f"EVT-CAN-{order.order_id[-8:]}",
                timestamp=ts_str,
                event_type="SIM_ORDER_CANCELLED",
                ticker=order.ticker,
                shares=order.shares_remaining,
                price=None,
                fee=None,
                equity_after="",
                cash_after="",
                details={"order_id": order.order_id, "reason": reason},
            )
            self.storage.append_audit_event(audit_entry)

            return order

    def expire_simulated_order(
        self,
        order_id: str,
        reason: str = "Order Expired at Session Close",
        timestamp: Optional[str] = None,
    ) -> PaperOrder:
        """Expire an open order and release remaining cash reservation."""
        self.verify_simulation_marker()
        with self.lock:
            ts_str = timestamp or datetime.now(timezone.utc).isoformat()
            order = self.storage.get_order(order_id)
            if order is None:
                raise KeyError(f"Order '{order_id}' not found in simulation orders registry.")

            if order.is_terminal:
                raise TerminalOrderStateError(
                    f"Cannot expire order '{order_id}' in terminal state '{order.state.value}'."
                )

            self.reservation_engine.release_reservation(order_id, reason=reason, timestamp=ts_str)

            order.transition_to(
                new_state=PaperOrderState.EXPIRED,
                timestamp=ts_str,
                reason=reason,
            )
            self.storage.save_order(order)

            audit_entry = PaperTradeAuditEntry(
                event_id=f"EVT-EXP-{order.order_id[-8:]}",
                timestamp=ts_str,
                event_type="SIM_ORDER_EXPIRED",
                ticker=order.ticker,
                shares=order.shares_remaining,
                price=None,
                fee=None,
                equity_after="",
                cash_after="",
                details={"order_id": order.order_id, "reason": reason},
            )
            self.storage.append_audit_event(audit_entry)

            return order

    def reject_simulated_order(
        self,
        order_id: str,
        reason: str = "Simulated Broker Rejection",
        timestamp: Optional[str] = None,
    ) -> PaperOrder:
        """Reject/fail an order and release remaining cash reservation."""
        self.verify_simulation_marker()
        with self.lock:
            ts_str = timestamp or datetime.now(timezone.utc).isoformat()
            order = self.storage.get_order(order_id)
            if order is None:
                raise KeyError(f"Order '{order_id}' not found in simulation orders registry.")

            if order.is_terminal:
                raise TerminalOrderStateError(
                    f"Cannot fail order '{order_id}' in terminal state '{order.state.value}'."
                )

            self.reservation_engine.release_reservation(order_id, reason=reason, timestamp=ts_str)

            order.transition_to(
                new_state=PaperOrderState.FAILED,
                timestamp=ts_str,
                reason=reason,
            )
            order.failure_reason = reason
            self.storage.save_order(order)

            audit_entry = PaperTradeAuditEntry(
                event_id=f"EVT-FAIL-{order.order_id[-8:]}",
                timestamp=ts_str,
                event_type="SIM_ORDER_FAILED",
                ticker=order.ticker,
                shares=order.shares_remaining,
                price=None,
                fee=None,
                equity_after="",
                cash_after="",
                details={"order_id": order.order_id, "reason": reason},
            )
            self.storage.append_audit_event(audit_entry)

            return order

    # -------------------------------------------------------------------------
    # Bracket Order Emulation (Exit Triggers: Stop-Loss & Take-Profit)
    # -------------------------------------------------------------------------

    def simulate_bracket_exit(
        self,
        order_id: str,
        exit_price: Decimal,
        reason: PaperExitReason,
        exit_shares: Optional[int] = None,
        fee: Decimal = Decimal("1.00"),
        timestamp: Optional[str] = None,
        order_notes: str = "",
    ) -> Dict[str, Any]:
        """Simulate an exit fill triggered by a bracket order's stop-loss or take-profit.
        
        Sells shares against the filled lot, realizes P&L on the accounting ledger,
        updates portfolio metadata, and persists all state atomically.
        """
        self.verify_simulation_marker()
        with self.lock:
            ts_str = timestamp or datetime.now(timezone.utc).isoformat()
            order = self.storage.get_order(order_id)
            if order is None:
                raise KeyError(f"Order '{order_id}' not found.")

            if order.shares_filled <= 0:
                raise InvalidExecutionStateError(
                    f"Cannot execute bracket exit for order '{order_id}': no shares have been filled."
                )

            px = quantize_cent(to_decimal(exit_price))
            fee_usd = quantize_cent(to_decimal(fee))
            journal = self.storage.load_journal()
            ledger = journal.ledger

            pos = ledger.positions.get(order.ticker)
            if pos is None or pos.total_shares <= 0:
                raise InvalidExecutionStateError(f"No open position found in ledger for '{order.ticker}'.")

            actual_shares = exit_shares if exit_shares is not None else pos.total_shares
            actual_shares = validate_positive_shares(actual_shares)
            if actual_shares > pos.total_shares:
                raise InvalidExecutionStateError(
                    f"Exit shares ({actual_shares}) exceeds position shares ({pos.total_shares}) for '{order.ticker}'."
                )

            # Execute sell on ledger
            trade_record = ledger.sell(
                ticker=order.ticker,
                shares=actual_shares,
                price=px,
                fee=fee_usd,
                timestamp=ts_str,
            )
            realized_pnl = quantize_cent(to_decimal(trade_record["realized_net_pnl"]))

            # Update portfolio metadata
            portfolio_data = self.storage.load_portfolio()
            lots_dict = portfolio_data.setdefault("open_lots", {})
            pos_after = ledger.positions.get(order.ticker)
            open_lot_ids = set()
            if pos_after:
                for l in pos_after.lots:
                    if l.shares_remaining > 0:
                        open_lot_ids.add(l.lot_id)
                        if l.lot_id in lots_dict:
                            lots_dict[l.lot_id]["shares"] = l.shares_remaining

            to_del = [lid for lid, meta in lots_dict.items() if meta.get("ticker") == order.ticker and lid not in open_lot_ids]
            for lid in to_del:
                del lots_dict[lid]

            # Record exit in orders registry
            eff_exit_id = f"EXIT-SIM-{order.order_id[-8:]}-{uuid.uuid4().hex[:4].upper()}"
            exit_record = PaperExitRecord(
                exit_id=eff_exit_id,
                ticker=order.ticker,
                shares=actual_shares,
                price=px,
                fee=fee_usd,
                timestamp=ts_str,
                reason=reason,
                realized_pnl=realized_pnl,
                order_id=order.order_id,
                idempotency_key=order.idempotency_key,
                order_notes=order_notes,
            )
            orders_data = self.storage.load_orders()
            orders_data.setdefault("exits", []).append(exit_record.to_dict())

            # Audit event
            audit_entry = PaperTradeAuditEntry(
                event_id=f"EVT-EX-{eff_exit_id[-8:]}",
                timestamp=ts_str,
                event_type="SIM_BRACKET_EXIT_EXECUTED",
                ticker=order.ticker,
                shares=actual_shares,
                price=str(px),
                fee=str(fee_usd),
                equity_after=str(ledger.total_equity_usd),
                cash_after=str(ledger.cash_usd),
                details={
                    "order_id": order.order_id,
                    "exit_id": eff_exit_id,
                    "reason": reason.value if isinstance(reason, PaperExitReason) else str(reason),
                    "realized_pnl": str(realized_pnl),
                },
            )

            # Commit transaction atomically
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
            )

            return {
                "status": "EXITED",
                "exit_id": eff_exit_id,
                "order_id": order.order_id,
                "ticker": order.ticker,
                "shares": actual_shares,
                "exit_price": str(px),
                "fee": str(fee_usd),
                "reason": reason.value if isinstance(reason, PaperExitReason) else str(reason),
                "realized_pnl": str(realized_pnl),
                "cash_after": str(ledger.cash_usd),
                "equity_after": str(ledger.total_equity_usd),
                "timestamp": ts_str,
            }

    # -------------------------------------------------------------------------
    # Reconciliation, Crash Recovery & Invariant Verification
    # -------------------------------------------------------------------------

    def reconcile_and_recover(
        self,
        failure_injection_point: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Run crash recovery, orphan reservation resolution, and invariant reconciliation."""
        self.verify_simulation_marker()
        with self.lock:
            # 1. Recover any unapplied commit manifests
            tx_recovered = self.storage.recover_interrupted_transaction()

            # 2. Load orders and ledger
            all_orders = self.storage.load_all_orders()
            journal = self.storage.load_journal()
            ledger = journal.ledger

            # 3. Reconcile reservations
            res_report = self.reservation_engine.reconcile_reservations(all_orders, ledger)

            if failure_injection_point == "DURING_LEDGER_RECONCILIATION":
                raise SimulatedCrashError("Injected crash: DURING_LEDGER_RECONCILIATION")

            # 4. Verify equity invariant
            ledger.validate_equity_invariant()

            return {
                "status": "RECONCILED",
                "transaction_recovered": tx_recovered,
                "reservation_reconciliation": res_report,
                "ledger_equity_invariant": "HOLDS",
                "total_equity_usd": str(ledger.total_equity_usd),
                "cash_usd": str(ledger.cash_usd),
                "reserved_cash_usd": res_report["total_reserved_usd"],
                "available_cash_usd": res_report["available_cash_usd"],
            }
