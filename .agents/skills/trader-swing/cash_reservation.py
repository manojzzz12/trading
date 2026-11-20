"""Pre-submission cash reservation engine and accounting integrity models for Trader-Swing Phase 8.2.

Enforces:
1. Pre-submission cash reservation for pending paper orders (GAP-02).
2. Available cash invariant:
   available_cash_usd = ledger.cash_usd - sum(active_reservations.total_reserved_usd)
   available_cash_usd - new_reservation >= 0.20 * ledger.total_equity_usd
3. Prevention of concurrent cash over-allocation across multiple proposed/pending orders.
4. Immediate release on cancellation, expiration, rejection, or failure.
5. Proportional conversion of reserved cash on partial and full fills.
6. Automatic orphan detection and reconciliation on startup or recovery.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import json
import os
import sys
from typing import Any, Dict, List, Optional, Set, Tuple

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from accounting import (
    AccountingLedger,
    quantize_cent,
    to_decimal,
    validate_positive_shares,
)
from order_models import PaperOrder, PaperOrderState, TERMINAL_ORDER_STATES
from paper_models import PaperOrderSide


class ReservationStatus(str, Enum):
    """Lifecycle states of a cash reservation."""
    ACTIVE = "ACTIVE"
    CONVERTED_PARTIAL = "CONVERTED_PARTIAL"
    CONVERTED_FULL = "CONVERTED_FULL"
    RELEASED = "RELEASED"


ACTIVE_RESERVATION_STATES: Set[ReservationStatus] = {
    ReservationStatus.ACTIVE,
    ReservationStatus.CONVERTED_PARTIAL,
}


class CashReservationError(ValueError):
    """Base exception for cash reservation failures."""
    pass


class InsufficientAvailableCashError(CashReservationError):
    """Raised when available cash (after reservations) is insufficient or violates 20% equity reserve."""
    pass


class ReservationNotFoundError(CashReservationError):
    """Raised when referencing an unknown reservation record."""
    pass


class ConflictingReservationError(CashReservationError):
    """Raised when attempting to create a reservation with conflicting parameters."""
    pass


class TerminalReservationStateError(CashReservationError):
    """Raised when attempting an operation on an already finalized reservation."""
    pass


@dataclass
class CashReservationRecord:
    """Immutable audit record and active balance tracker for reserved order cash."""
    reservation_id: str
    order_id: str
    idempotency_key: str
    ticker: str
    side: PaperOrderSide
    shares_initial: int
    shares_reserved_remaining: int
    price_reserved: Decimal
    fee_reserved: Decimal
    total_reserved_usd: Decimal
    status: ReservationStatus = ReservationStatus.ACTIVE
    created_at: str = ""
    updated_at: str = ""
    release_reason: Optional[str] = None
    conversion_history: List[Dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.ticker = self.ticker.strip().upper()
        self.shares_initial = validate_positive_shares(self.shares_initial)
        if self.shares_reserved_remaining < 0:
            raise ValueError(f"shares_reserved_remaining cannot be negative: {self.shares_reserved_remaining}")
        if self.shares_reserved_remaining > self.shares_initial:
            raise ValueError(
                f"shares_reserved_remaining ({self.shares_reserved_remaining}) cannot exceed initial ({self.shares_initial})"
            )
        self.price_reserved = quantize_cent(to_decimal(self.price_reserved))
        if self.price_reserved <= Decimal("0.00"):
            raise ValueError(f"price_reserved must be positive, got {self.price_reserved}")
        self.fee_reserved = quantize_cent(to_decimal(self.fee_reserved))
        if self.fee_reserved < Decimal("0.00"):
            raise ValueError(f"fee_reserved cannot be negative, got {self.fee_reserved}")
        self.total_reserved_usd = quantize_cent(to_decimal(self.total_reserved_usd))
        if self.total_reserved_usd < Decimal("0.00"):
            raise ValueError(f"total_reserved_usd cannot be negative, got {self.total_reserved_usd}")
        now_ts = datetime.now(timezone.utc).isoformat()
        if not self.created_at:
            self.created_at = now_ts
        if not self.updated_at:
            self.updated_at = self.created_at

    @property
    def is_active(self) -> bool:
        """Return True if reservation is currently locking cash."""
        return self.status in ACTIVE_RESERVATION_STATES and self.total_reserved_usd > Decimal("0.00")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "reservation_id": self.reservation_id,
            "order_id": self.order_id,
            "idempotency_key": self.idempotency_key,
            "ticker": self.ticker,
            "side": self.side.value if isinstance(self.side, PaperOrderSide) else str(self.side),
            "shares_initial": self.shares_initial,
            "shares_reserved_remaining": self.shares_reserved_remaining,
            "price_reserved": str(self.price_reserved),
            "fee_reserved": str(self.fee_reserved),
            "total_reserved_usd": str(self.total_reserved_usd),
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "release_reason": self.release_reason,
            "conversion_history": list(self.conversion_history),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CashReservationRecord":
        return cls(
            reservation_id=d["reservation_id"],
            order_id=d["order_id"],
            idempotency_key=d["idempotency_key"],
            ticker=d["ticker"],
            side=PaperOrderSide(d["side"]),
            shares_initial=int(d["shares_initial"]),
            shares_reserved_remaining=int(d["shares_reserved_remaining"]),
            price_reserved=Decimal(str(d["price_reserved"])),
            fee_reserved=Decimal(str(d["fee_reserved"])),
            total_reserved_usd=Decimal(str(d["total_reserved_usd"])),
            status=ReservationStatus(d["status"]),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
            release_reason=d.get("release_reason"),
            conversion_history=list(d.get("conversion_history", [])),
        )


class CashReservationEngine:
    """Manages pre-submission cash reservations and accounting integrity."""

    def __init__(self, storage: Any):
        self.storage = storage

    @staticmethod
    def compute_reservation_amount(
        shares: int,
        price: Decimal,
        estimated_fee: Decimal = Decimal("1.00"),
    ) -> Decimal:
        """Compute exact cash required to reserve an order: (shares * price) + fee."""
        shares_cnt = validate_positive_shares(shares)
        px = quantize_cent(to_decimal(price))
        fee = quantize_cent(to_decimal(estimated_fee))
        return quantize_cent((px * Decimal(shares_cnt)) + fee)

    def load_all_reservations(self) -> Dict[str, CashReservationRecord]:
        """Load all reservations from storage."""
        data = self.storage.load_reservations()
        records: Dict[str, CashReservationRecord] = {}
        for ord_id, r_dict in data.get("reservations", {}).items():
            records[ord_id] = CashReservationRecord.from_dict(r_dict)
        return records

    def save_reservation(self, record: CashReservationRecord) -> None:
        """Persist a single reservation record."""
        data = self.storage.load_reservations()
        res_dict = data.setdefault("reservations", {})
        res_dict[record.order_id] = record.to_dict()
        self.storage.save_reservations(data)

    def get_reservation(self, order_id: str) -> Optional[CashReservationRecord]:
        """Retrieve reservation for a specific order."""
        records = self.load_all_reservations()
        return records.get(order_id)

    def get_active_reservations(self) -> List[CashReservationRecord]:
        """Return all reservations that are actively reserving cash."""
        records = self.load_all_reservations()
        return [r for r in records.values() if r.is_active]

    def get_total_reserved_cash(self) -> Decimal:
        """Compute the aggregate cash currently locked in active reservations."""
        active = self.get_active_reservations()
        return quantize_cent(sum((r.total_reserved_usd for r in active), Decimal("0.00")))

    def compute_available_cash(self, ledger_cash: Decimal) -> Decimal:
        """Compute unreserved cash: ledger cash minus active reserved cash."""
        total_reserved = self.get_total_reserved_cash()
        return quantize_cent(to_decimal(ledger_cash) - total_reserved)

    def reserve_cash(
        self,
        order: PaperOrder,
        ledger: AccountingLedger,
        estimated_fee: Decimal = Decimal("1.00"),
        timestamp: Optional[str] = None,
    ) -> CashReservationRecord:
        """Reserve cash for a buy order prior to submission.
        
        Validates:
        1. Order side is BUY (long-only swing).
        2. Order is not terminal.
        3. Existing reservation deduplication (idempotent if identical, error if conflicting).
        4. Invariant: Available cash after reservation must satisfy the 20% minimum equity reserve:
           available_cash - required_reservation >= 0.20 * total_equity.
        """
        if order.side != PaperOrderSide.BUY:
            raise CashReservationError(f"Cash reservation only applies to BUY orders, got {order.side}")

        if order.is_terminal:
            raise TerminalReservationStateError(
                f"Cannot reserve cash for order '{order.order_id}' in terminal state '{order.state.value}'."
            )

        if order.limit_price is None or order.limit_price <= Decimal("0.00"):
            raise CashReservationError(
                f"Order '{order.order_id}' must specify a positive limit_price for cash reservation."
            )

        # 1. Check existing reservation
        existing = self.get_reservation(order.order_id)
        if existing is not None:
            if existing.status in ACTIVE_RESERVATION_STATES:
                # Idempotent match if active
                return existing
            else:
                raise ConflictingReservationError(
                    f"Reservation for order '{order.order_id}' already finalized in state '{existing.status.value}'."
                )

        # 2. Calculate required reservation
        price_to_reserve = order.limit_price
        shares_to_reserve = order.shares_remaining
        fee_to_reserve = quantize_cent(to_decimal(estimated_fee))
        required_reservation = self.compute_reservation_amount(
            shares=shares_to_reserve,
            price=price_to_reserve,
            estimated_fee=fee_to_reserve,
        )

        # 3. Check cash availability and 20% minimum cash reserve invariant
        total_equity = ledger.total_equity_usd
        min_cash_reserve = quantize_cent(total_equity * Decimal("0.20"))
        current_available = self.compute_available_cash(ledger.cash_usd)

        if required_reservation > current_available:
            raise InsufficientAvailableCashError(
                f"Cannot reserve ${required_reservation} for order '{order.order_id}': "
                f"available cash is only ${current_available} (ledger cash: ${ledger.cash_usd}, "
                f"currently reserved: ${self.get_total_reserved_cash()})."
            )

        post_reservation_available = quantize_cent(current_available - required_reservation)
        if post_reservation_available < min_cash_reserve:
            raise InsufficientAvailableCashError(
                f"Cannot reserve ${required_reservation} for order '{order.order_id}': "
                f"post-reservation available cash (${post_reservation_available}) violates "
                f"the 20% minimum cash reserve threshold (${min_cash_reserve}) of total equity (${total_equity})."
            )

        # 4. Construct reservation record
        now_ts = timestamp or datetime.now(timezone.utc).isoformat()
        res_id = f"RES-{order.order_id[-8:]}"
        record = CashReservationRecord(
            reservation_id=res_id,
            order_id=order.order_id,
            idempotency_key=order.idempotency_key,
            ticker=order.ticker,
            side=order.side,
            shares_initial=order.shares_initial,
            shares_reserved_remaining=shares_to_reserve,
            price_reserved=price_to_reserve,
            fee_reserved=fee_to_reserve,
            total_reserved_usd=required_reservation,
            status=ReservationStatus.ACTIVE,
            created_at=now_ts,
            updated_at=now_ts,
            release_reason=None,
            conversion_history=[],
        )

        self.save_reservation(record)
        return record

    def release_reservation(
        self,
        order_id: str,
        reason: str = "Order Cancelled / Rejected",
        timestamp: Optional[str] = None,
    ) -> Optional[CashReservationRecord]:
        """Release any active cash reservation associated with an order."""
        record = self.get_reservation(order_id)
        if record is None:
            return None

        if record.status in (ReservationStatus.RELEASED, ReservationStatus.CONVERTED_FULL):
            # Already finalized
            return record

        now_ts = timestamp or datetime.now(timezone.utc).isoformat()
        record.status = ReservationStatus.RELEASED
        record.release_reason = reason
        record.shares_reserved_remaining = 0
        record.total_reserved_usd = Decimal("0.00")
        record.updated_at = now_ts

        self.save_reservation(record)
        return record

    def convert_reservation(
        self,
        order_id: str,
        fill_shares: int,
        fill_price: Decimal,
        fill_fee: Decimal = Decimal("1.00"),
        timestamp: Optional[str] = None,
    ) -> Tuple[CashReservationRecord, Decimal]:
        """Convert proportional reserved cash to actual ledger fill upon execution.
        
        Returns:
            Tuple of (updated CashReservationRecord, converted_amount_usd)
        """
        record = self.get_reservation(order_id)
        if record is None:
            raise ReservationNotFoundError(f"No cash reservation found for order '{order_id}'.")

        if record.status not in ACTIVE_RESERVATION_STATES:
            raise TerminalReservationStateError(
                f"Cannot convert reservation for order '{order_id}' in terminal state '{record.status.value}'."
            )

        shs = validate_positive_shares(fill_shares)
        if shs > record.shares_reserved_remaining:
            raise CashReservationError(
                f"Fill shares ({shs}) exceeds remaining reserved shares ({record.shares_reserved_remaining}) "
                f"for order '{order_id}'."
            )

        now_ts = timestamp or datetime.now(timezone.utc).isoformat()
        px = quantize_cent(to_decimal(fill_price))
        fee = quantize_cent(to_decimal(fill_fee))

        # Check if full fill or partial
        is_final_fill = (shs == record.shares_reserved_remaining)

        if is_final_fill:
            # Full conversion: release all remaining reserved cash to ledger deduction
            converted_amount = record.total_reserved_usd
            record.shares_reserved_remaining = 0
            record.total_reserved_usd = Decimal("0.00")
            record.status = ReservationStatus.CONVERTED_FULL
        else:
            # Proportional conversion: release proportional principal
            proportional_principal = quantize_cent(record.price_reserved * Decimal(shs))
            converted_amount = proportional_principal
            record.shares_reserved_remaining -= shs
            record.total_reserved_usd = quantize_cent(record.total_reserved_usd - converted_amount)
            record.status = ReservationStatus.CONVERTED_PARTIAL

        record.updated_at = now_ts
        record.conversion_history.append({
            "timestamp": now_ts,
            "fill_shares": shs,
            "fill_price": str(px),
            "fill_fee": str(fee),
            "converted_amount_usd": str(converted_amount),
            "remaining_reserved_usd": str(record.total_reserved_usd),
            "remaining_reserved_shares": record.shares_reserved_remaining,
            "status": record.status.value,
        })

        self.save_reservation(record)
        return record, converted_amount

    def reconcile_reservations(
        self,
        orders: Dict[str, PaperOrder],
        ledger: AccountingLedger,
    ) -> Dict[str, Any]:
        """Audit and reconcile active reservations against orders and ledger.
        
        Detects and resolves orphaned reservations for finalized orders.
        Verifies available cash arithmetic.
        """
        all_reservations = self.load_all_reservations()
        orphans_detected: List[Dict[str, Any]] = []
        modified = False

        for ord_id, res in all_reservations.items():
            if res.is_active:
                order = orders.get(ord_id)
                # Orphan Case 1: Order does not exist in registry
                if order is None:
                    orphans_detected.append({
                        "order_id": ord_id,
                        "reservation_id": res.reservation_id,
                        "reason": "ORDER_NOT_FOUND",
                        "unlocked_usd": str(res.total_reserved_usd),
                    })
                    res.status = ReservationStatus.RELEASED
                    res.release_reason = "RECONCILIATION_ORDER_NOT_FOUND"
                    res.total_reserved_usd = Decimal("0.00")
                    res.shares_reserved_remaining = 0
                    modified = True

                # Orphan Case 2: Order has reached terminal state
                elif order.is_terminal:
                    orphans_detected.append({
                        "order_id": ord_id,
                        "reservation_id": res.reservation_id,
                        "reason": f"TERMINAL_ORDER_STATE_{order.state.value}",
                        "unlocked_usd": str(res.total_reserved_usd),
                    })
                    if order.state == PaperOrderState.FILLED:
                        res.status = ReservationStatus.CONVERTED_FULL
                    else:
                        res.status = ReservationStatus.RELEASED
                        res.release_reason = f"RECONCILIATION_ORDER_{order.state.value}"
                    res.total_reserved_usd = Decimal("0.00")
                    res.shares_reserved_remaining = 0
                    modified = True

        if modified:
            data = self.storage.load_reservations()
            data["reservations"] = {k: v.to_dict() for k, v in all_reservations.items()}
            self.storage.save_reservations(data)

        # Compute reconciled numbers
        active = [r for r in all_reservations.values() if r.is_active]
        reconciled_reserved = sum((r.total_reserved_usd for r in active), Decimal("0.00"))
        reconciled_available = quantize_cent(ledger.cash_usd - reconciled_reserved)

        return {
            "status": "RECONCILED",
            "active_reservations_count": len(active),
            "total_reserved_usd": str(reconciled_reserved),
            "ledger_cash_usd": str(ledger.cash_usd),
            "available_cash_usd": str(reconciled_available),
            "orphans_resolved_count": len(orphans_detected),
            "orphans_resolved": orphans_detected,
        }
