"""Order lifecycle management engine for Trader-Swing Phase 8.0.

Enforces:
1. Deterministic order creation with idempotency key deduplication.
2. Rejection of conflicting order intent for an existing idempotency key.
3. Strict state machine transitions according to the authoritative transition table.
4. Terminal state protection (no transition out of terminal states).
5. Accurate execution fill application with share decrementing and state updates.
6. Audit trail generation for all order creations, transitions, and fills.
"""

from datetime import datetime, timezone
from decimal import Decimal
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

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
    ConflictingIdempotencyKeyError,
    ConflictingFillIdentityError,
    compute_fill_identity,
    compute_order_id,
)
from paper_models import PaperTradeAuditEntry
from paper_storage import PaperStorageManager


class PaperOrderEngine:
    """Coordinates lifecycle transitions, persistence, and idempotency for paper orders."""

    def __init__(self, storage: Optional[PaperStorageManager] = None):
        self.storage = storage or PaperStorageManager()

    def create_order(
        self,
        intent: CanonicalOrderIntent,
        reference_now: Optional[datetime] = None,
        order_notes: str = "",
    ) -> PaperOrder:
        """Create a new proposed order or return existing order if idempotency key matches.

        Raises:
            ConflictingIdempotencyKeyError: If idempotency key exists with conflicting intent.
        """
        intent.validate()
        idemp_key = intent.compute_idempotency_key()
        intent_hash = intent.compute_intent_hash()

        # 1. Check for existing order by idempotency key
        existing = self.storage.get_order_by_idempotency_key(idemp_key)
        if existing is not None:
            # Check intent hash
            if existing.intent_hash == intent_hash:
                # Idempotent match: return existing order without mutation
                return existing
            else:
                raise ConflictingIdempotencyKeyError(
                    f"Conflicting order intent for idempotency key '{idemp_key}'. "
                    f"Existing intent hash: {existing.intent_hash}, new intent hash: {intent_hash}."
                )

        now_utc = reference_now or datetime.now(timezone.utc)
        ts_str = now_utc.isoformat()
        order_id = compute_order_id(idemp_key)

        # 2. Construct initial proposed order
        order = PaperOrder(
            order_id=order_id,
            idempotency_key=idemp_key,
            ticker=intent.ticker.strip().upper(),
            side=intent.side,
            order_type=intent.order_type.strip().upper(),
            shares_initial=intent.shares,
            shares_filled=0,
            shares_remaining=intent.shares,
            stop_loss=intent.stop_loss,
            limit_price=intent.limit_price,
            target=intent.target,
            state=PaperOrderState.PROPOSED,
            created_at=ts_str,
            updated_at=ts_str,
            intent_hash=intent_hash,
            client_order_id=intent.client_tag or None,
            session_date=intent.canonical_session_date(),
            strategy_name=intent.strategy_name,
            strategy_version=intent.strategy_version,
            order_notes=order_notes,
            transition_history=[],
            fill_ids=[],
        )

        # Record initial creation transition
        initial_trans = OrderStateTransition(
            from_state=PaperOrderState.PROPOSED,
            to_state=PaperOrderState.PROPOSED,
            timestamp=ts_str,
            reason="ORDER_PROPOSED",
            metadata={"idempotency_key": idemp_key, "intent_hash": intent_hash},
        )
        order.transition_history.append(initial_trans)

        # 3. Persist atomically
        self.storage.save_order(order)

        # 4. Append audit event
        audit_entry = PaperTradeAuditEntry(
            event_id=f"EVT-ORD-{idemp_key[-8:]}",
            timestamp=ts_str,
            event_type="ORDER_PROPOSED",
            ticker=order.ticker,
            shares=order.shares_initial,
            price=str(order.limit_price) if order.limit_price is not None else None,
            fee=None,
            equity_after="",
            cash_after="",
            details={
                "order_id": order.order_id,
                "idempotency_key": idemp_key,
                "state": order.state.value,
                "stop_loss": str(order.stop_loss),
                "target": str(order.target) if order.target is not None else None,
            },
        )
        self.storage.append_audit_event(audit_entry)

        return order

    def transition_order(
        self,
        order_id: str,
        new_state: PaperOrderState,
        reason: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        timestamp: Optional[str] = None,
    ) -> PaperOrder:
        """Transition an existing order to a new state with full validation."""
        order = self.storage.get_order(order_id)
        if order is None:
            raise KeyError(f"Order '{order_id}' not found in paper orders registry.")

        ts = timestamp or datetime.now(timezone.utc).isoformat()
        old_state = order.state

        # Execute state transition (enforces transition table and terminal protection)
        trans = order.transition_to(
            new_state=new_state,
            timestamp=ts,
            reason=reason,
            metadata=metadata,
        )

        # Persist atomically
        self.storage.save_order(order)

        # Append audit event
        audit_entry = PaperTradeAuditEntry(
            event_id=f"EVT-TRN-{order.order_id[-8:]}-{len(order.transition_history):02d}",
            timestamp=ts,
            event_type=f"ORDER_{new_state.value}",
            ticker=order.ticker,
            shares=order.shares_remaining,
            price=str(order.limit_price) if order.limit_price is not None else None,
            fee=None,
            equity_after="",
            cash_after="",
            details={
                "order_id": order.order_id,
                "from_state": old_state.value,
                "to_state": new_state.value,
                "reason": reason,
                "metadata": metadata or {},
            },
        )
        self.storage.append_audit_event(audit_entry)

        return order

    def apply_fill_to_order(
        self,
        order_id: str,
        shares: int,
        price: Decimal,
        timestamp: Optional[str] = None,
        fee: Decimal = Decimal("0.00"),
        fill_id: Optional[str] = None,
    ) -> Tuple[PaperOrder, str]:
        """Apply an execution fill to an order, tracking shares and transitioning state.

        Returns:
            Tuple of (updated PaperOrder, effective fill_id)
        """
        order = self.storage.get_order(order_id)
        if order is None:
            raise KeyError(f"Order '{order_id}' not found in paper orders registry.")

        ts = timestamp or datetime.now(timezone.utc).isoformat()
        eff_fill_id = fill_id or compute_fill_identity(
            order_id=order_id,
            fill_index=len(order.fill_ids) + 1,
            shares=shares,
            price=price,
            timestamp=ts,
        )

        # Apply fill on order
        trans = order.apply_fill(
            fill_id=eff_fill_id,
            shares=shares,
            price=price,
            timestamp=ts,
            fee=fee,
        )

        # Persist atomically
        self.storage.save_order(order)

        # Append audit event
        audit_entry = PaperTradeAuditEntry(
            event_id=f"EVT-FILL-{eff_fill_id[-8:]}",
            timestamp=ts,
            event_type=f"ORDER_{order.state.value}",
            ticker=order.ticker,
            shares=shares,
            price=str(price),
            fee=str(fee),
            equity_after="",
            cash_after="",
            details={
                "order_id": order.order_id,
                "fill_id": eff_fill_id,
                "shares_filled": shares,
                "shares_remaining": order.shares_remaining,
                "state": order.state.value,
            },
        )
        self.storage.append_audit_event(audit_entry)

        return order, eff_fill_id

    def get_order(self, order_id: str) -> Optional[PaperOrder]:
        """Retrieve order by order ID."""
        return self.storage.get_order(order_id)

    def get_order_by_idempotency_key(self, idempotency_key: str) -> Optional[PaperOrder]:
        """Retrieve order by idempotency key."""
        return self.storage.get_order_by_idempotency_key(idempotency_key)

    def list_orders(
        self,
        state: Optional[PaperOrderState] = None,
        ticker: Optional[str] = None,
    ) -> List[PaperOrder]:
        """List orders, optionally filtered by state and/or ticker."""
        orders_dict = self.storage.load_all_orders()
        result: List[PaperOrder] = []
        for order in orders_dict.values():
            if state is not None and order.state != state:
                continue
            if ticker is not None and order.ticker != ticker.strip().upper():
                continue
            result.append(order)
        return result
