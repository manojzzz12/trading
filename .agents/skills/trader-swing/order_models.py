"""Order lifecycle data models and state machine for Trader-Swing Phase 8.0.

Enforces:
1. Typed order model with explicit states:
   PROPOSED, PENDING_APPROVAL, APPROVED, SUBMITTED, PARTIALLY_FILLED,
   FILLED, REJECTED, CANCELLED, EXPIRED, FAILED.
2. Authoritative legal state-transition table with terminal-state protection.
3. Separation of order intent/lifecycle from ledger fills and portfolio positions.
4. Traceable transition records with timestamps, reasons, and metadata.
5. Strict validation of positive quantities, remaining shares, and valid prices.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import os
import sys
from typing import Any, Dict, List, Optional, Set

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from accounting import quantize_cent, to_decimal, validate_positive_shares
from paper_models import PaperOrderSide


class PaperOrderState(str, Enum):
    """Authoritative states in the Trader-Swing order lifecycle."""
    PROPOSED = "PROPOSED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


# Terminal states: orders in these states can NEVER transition out
TERMINAL_ORDER_STATES: Set[PaperOrderState] = {
    PaperOrderState.FILLED,
    PaperOrderState.REJECTED,
    PaperOrderState.CANCELLED,
    PaperOrderState.EXPIRED,
    PaperOrderState.FAILED,
}

# Authoritative State-Transition Table
LEGAL_ORDER_TRANSITIONS: Dict[PaperOrderState, Set[PaperOrderState]] = {
    PaperOrderState.PROPOSED: {
        PaperOrderState.PENDING_APPROVAL,
        PaperOrderState.REJECTED,
        PaperOrderState.CANCELLED,
    },
    PaperOrderState.PENDING_APPROVAL: {
        PaperOrderState.APPROVED,
        PaperOrderState.REJECTED,
        PaperOrderState.CANCELLED,
        PaperOrderState.EXPIRED,
    },
    PaperOrderState.APPROVED: {
        PaperOrderState.SUBMITTED,
        PaperOrderState.CANCELLED,
        PaperOrderState.EXPIRED,
    },
    PaperOrderState.SUBMITTED: {
        PaperOrderState.PARTIALLY_FILLED,
        PaperOrderState.FILLED,
        PaperOrderState.CANCELLED,
        PaperOrderState.EXPIRED,
        PaperOrderState.FAILED,
    },
    PaperOrderState.PARTIALLY_FILLED: {
        PaperOrderState.PARTIALLY_FILLED,
        PaperOrderState.FILLED,
        PaperOrderState.CANCELLED,
        PaperOrderState.EXPIRED,
        PaperOrderState.FAILED,
    },
    PaperOrderState.FILLED: set(),
    PaperOrderState.REJECTED: set(),
    PaperOrderState.CANCELLED: set(),
    PaperOrderState.EXPIRED: set(),
    PaperOrderState.FAILED: set(),
}


class IllegalStateTransitionError(ValueError):
    """Raised when an order attempts an unauthorized state transition."""
    pass


class TerminalOrderStateError(IllegalStateTransitionError):
    """Raised when an order in a terminal state attempts any further transition."""
    pass


@dataclass(frozen=True)
class OrderStateTransition:
    """Immutable audit record of an individual order state transition."""
    from_state: PaperOrderState
    to_state: PaperOrderState
    timestamp: str
    reason: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "from_state": self.from_state.value,
            "to_state": self.to_state.value,
            "timestamp": self.timestamp,
            "reason": self.reason,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OrderStateTransition":
        return cls(
            from_state=PaperOrderState(data["from_state"]),
            to_state=PaperOrderState(data["to_state"]),
            timestamp=data["timestamp"],
            reason=data.get("reason", ""),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass
class PaperOrder:
    """Authoritative order record tracking full lifecycle state and fill progress."""
    order_id: str
    idempotency_key: str
    ticker: str
    side: PaperOrderSide
    order_type: str
    shares_initial: int
    shares_filled: int
    shares_remaining: int
    stop_loss: Decimal
    limit_price: Optional[Decimal] = None
    target: Optional[Decimal] = None
    state: PaperOrderState = PaperOrderState.PROPOSED
    created_at: str = ""
    updated_at: str = ""
    intent_hash: str = ""
    client_order_id: Optional[str] = None
    session_date: str = ""
    strategy_name: str = "TRADER_SWING"
    strategy_version: str = "1.0.0"
    order_notes: str = ""
    rejection_reason: Optional[str] = None
    cancellation_reason: Optional[str] = None
    failure_reason: Optional[str] = None
    transition_history: List[OrderStateTransition] = field(default_factory=list)
    fill_ids: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.ticker = self.ticker.strip().upper()
        self.shares_initial = validate_positive_shares(self.shares_initial)
        if self.shares_filled < 0:
            raise ValueError(f"shares_filled cannot be negative, got {self.shares_filled}")
        if self.shares_remaining < 0:
            raise ValueError(f"shares_remaining cannot be negative, got {self.shares_remaining}")
        if self.shares_filled + self.shares_remaining != self.shares_initial:
            raise ValueError(
                f"Share quantity mismatch: filled ({self.shares_filled}) + "
                f"remaining ({self.shares_remaining}) != initial ({self.shares_initial})"
            )
        self.stop_loss = quantize_cent(to_decimal(self.stop_loss))
        if self.stop_loss <= Decimal("0.00"):
            raise ValueError(f"stop_loss must be positive, got {self.stop_loss}")
        if self.limit_price is not None:
            self.limit_price = quantize_cent(to_decimal(self.limit_price))
            if self.limit_price <= Decimal("0.00"):
                raise ValueError(f"limit_price must be positive, got {self.limit_price}")
        if self.target is not None:
            self.target = quantize_cent(to_decimal(self.target))
            if self.target <= Decimal("0.00"):
                raise ValueError(f"target must be positive, got {self.target}")
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()
        if not self.updated_at:
            self.updated_at = self.created_at

    @property
    def is_terminal(self) -> bool:
        """Return True if the order has reached an irreversible terminal state."""
        return self.state in TERMINAL_ORDER_STATES

    @property
    def is_active(self) -> bool:
        """Return True if order is active and awaiting fill or approval."""
        return not self.is_terminal

    def transition_to(
        self,
        new_state: PaperOrderState,
        timestamp: Optional[str] = None,
        reason: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> OrderStateTransition:
        """Execute and record an authoritative state transition.

        Raises:
            TerminalOrderStateError: If current state is terminal.
            IllegalStateTransitionError: If the transition is not in the legal table,
                or if quantity invariants for PARTIALLY_FILLED/FILLED are violated.
        """
        ts = timestamp or datetime.now(timezone.utc).isoformat()

        # 1. Guard terminal state
        if self.state in TERMINAL_ORDER_STATES:
            raise TerminalOrderStateError(
                f"Cannot transition order '{self.order_id}' from terminal state "
                f"'{self.state.value}' to '{new_state.value}'."
            )

        # 2. Guard legal transition table
        allowed = LEGAL_ORDER_TRANSITIONS.get(self.state, set())
        if new_state not in allowed:
            raise IllegalStateTransitionError(
                f"Illegal state transition from '{self.state.value}' to '{new_state.value}' "
                f"for order '{self.order_id}'. Permitted: {[s.value for s in allowed]}"
            )

        # 3. Guard fill-state quantity preconditions
        if new_state == PaperOrderState.FILLED and self.shares_remaining != 0:
            raise IllegalStateTransitionError(
                f"Cannot transition order '{self.order_id}' to FILLED: "
                f"remaining shares ({self.shares_remaining}) must be 0."
            )
        if new_state == PaperOrderState.PARTIALLY_FILLED and self.shares_filled == 0:
            raise IllegalStateTransitionError(
                f"Cannot transition order '{self.order_id}' to PARTIALLY_FILLED: "
                f"filled shares must be > 0."
            )

        # 4. Record transition
        transition = OrderStateTransition(
            from_state=self.state,
            to_state=new_state,
            timestamp=ts,
            reason=reason,
            metadata=metadata or {},
        )
        self.transition_history.append(transition)
        self.state = new_state
        self.updated_at = ts

        # Update specific terminal reason fields
        if new_state == PaperOrderState.REJECTED:
            self.rejection_reason = reason
        elif new_state == PaperOrderState.CANCELLED:
            self.cancellation_reason = reason
        elif new_state == PaperOrderState.FAILED:
            self.failure_reason = reason

        return transition

    def apply_fill(
        self,
        fill_id: str,
        shares: int,
        price: Decimal,
        timestamp: Optional[str] = None,
        fee: Decimal = Decimal("0.00"),
    ) -> OrderStateTransition:
        """Apply an execution fill to this order, reducing remaining quantity.

        Transitions order to PARTIALLY_FILLED or FILLED.
        """
        ts = timestamp or datetime.now(timezone.utc).isoformat()
        fill_shares = validate_positive_shares(shares)
        fill_px = quantize_cent(to_decimal(price))
        fill_fee = quantize_cent(to_decimal(fee))

        # 1. State check: order must be SUBMITTED or PARTIALLY_FILLED
        if self.state not in (PaperOrderState.SUBMITTED, PaperOrderState.PARTIALLY_FILLED):
            raise IllegalStateTransitionError(
                f"Cannot fill order '{self.order_id}' in state '{self.state.value}'. "
                f"Order must be in SUBMITTED or PARTIALLY_FILLED."
            )

        # 2. Quantity check
        if fill_shares > self.shares_remaining:
            raise ValueError(
                f"Fill quantity ({fill_shares} shares) exceeds order remaining quantity "
                f"({self.shares_remaining} shares) for order '{self.order_id}'."
            )

        # 3. Duplicate fill ID check on this order
        if fill_id in self.fill_ids:
            raise ValueError(
                f"Duplicate fill ID '{fill_id}' has already been applied to order '{self.order_id}'."
            )

        self.fill_ids.append(fill_id)
        self.shares_filled += fill_shares
        self.shares_remaining = self.shares_initial - self.shares_filled

        # 4. Determine next state
        target_state = PaperOrderState.FILLED if self.shares_remaining == 0 else PaperOrderState.PARTIALLY_FILLED
        reason_msg = (
            f"Execution fill {fill_id}: {fill_shares} shares @ ${fill_px} "
            f"({self.shares_remaining} remaining)"
        )
        return self.transition_to(
            new_state=target_state,
            timestamp=ts,
            reason=reason_msg,
            metadata={
                "fill_id": fill_id,
                "shares": fill_shares,
                "price": str(fill_px),
                "fee": str(fill_fee),
                "shares_filled_total": self.shares_filled,
                "shares_remaining": self.shares_remaining,
            },
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize order to JSON-compatible dictionary."""
        return {
            "order_id": self.order_id,
            "idempotency_key": self.idempotency_key,
            "ticker": self.ticker,
            "side": self.side.value if isinstance(self.side, PaperOrderSide) else str(self.side),
            "order_type": self.order_type,
            "shares_initial": self.shares_initial,
            "shares_filled": self.shares_filled,
            "shares_remaining": self.shares_remaining,
            "stop_loss": str(self.stop_loss),
            "limit_price": str(self.limit_price) if self.limit_price is not None else None,
            "target": str(self.target) if self.target is not None else None,
            "state": self.state.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "intent_hash": self.intent_hash,
            "client_order_id": self.client_order_id,
            "session_date": self.session_date,
            "strategy_name": self.strategy_name,
            "strategy_version": self.strategy_version,
            "order_notes": self.order_notes,
            "rejection_reason": self.rejection_reason,
            "cancellation_reason": self.cancellation_reason,
            "failure_reason": self.failure_reason,
            "transition_history": [t.to_dict() for t in self.transition_history],
            "fill_ids": list(self.fill_ids),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PaperOrder":
        """Deserialize order from dictionary."""
        transitions = [
            OrderStateTransition.from_dict(t)
            for t in data.get("transition_history", [])
        ]
        return cls(
            order_id=data["order_id"],
            idempotency_key=data["idempotency_key"],
            ticker=data["ticker"],
            side=PaperOrderSide(data["side"]),
            order_type=data["order_type"],
            shares_initial=int(data["shares_initial"]),
            shares_filled=int(data.get("shares_filled", 0)),
            shares_remaining=int(data.get("shares_remaining", data["shares_initial"])),
            stop_loss=to_decimal(data["stop_loss"]),
            limit_price=to_decimal(data["limit_price"]) if data.get("limit_price") is not None else None,
            target=to_decimal(data["target"]) if data.get("target") is not None else None,
            state=PaperOrderState(data["state"]),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            intent_hash=data.get("intent_hash", ""),
            client_order_id=data.get("client_order_id"),
            session_date=data.get("session_date", ""),
            strategy_name=data.get("strategy_name", "TRADER_SWING"),
            strategy_version=data.get("strategy_version", "1.0.0"),
            order_notes=data.get("order_notes", ""),
            rejection_reason=data.get("rejection_reason"),
            cancellation_reason=data.get("cancellation_reason"),
            failure_reason=data.get("failure_reason"),
            transition_history=transitions,
            fill_ids=list(data.get("fill_ids", [])),
        )
