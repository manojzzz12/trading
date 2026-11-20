"""Data models and structures for Phase 6 manual paper trading in trader-swing.

Enforces:
1. Strict type safety and Decimal representations for all monetary fields.
2. Long-only, stock-only definitions for paper order side and exit reasons.
3. Structured representations for fills, exits, positions, and append-only audit events.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
import os
import sys
from typing import Any, Dict, List, Optional


class PaperOrderSide(str, Enum):
    """Permitted side for paper trading (long-only)."""
    BUY = "BUY"
    SELL = "SELL"


class PaperOrderType(str, Enum):
    """Execution type for paper orders."""
    MANUAL_FILL = "MANUAL_FILL"
    LIMIT = "LIMIT"
    MARKET = "MARKET"
    STOP = "STOP"
    BRACKET = "BRACKET"


class PaperExitReason(str, Enum):
    """Authoritative reasons for closing or writing off a paper position."""
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    TECHNICAL_EXIT = "TECHNICAL_EXIT"
    MANUAL = "MANUAL"
    DELISTING_ZERO_RECOVERY = "DELISTING_ZERO_RECOVERY"
    DELISTING_POSITIVE = "DELISTING_POSITIVE"
    EMERGENCY_FLATTEN = "EMERGENCY_FLATTEN"


@dataclass(frozen=True)
class PaperPositionMetadata:
    """Lot-level protective parameters and metadata tracked outside the ledger."""
    ticker: str
    lot_id: str
    shares: int
    entry_price: Decimal
    entry_fee: Decimal
    entry_timestamp: str
    stop_loss: Decimal
    target: Optional[Decimal] = None
    order_notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "lot_id": self.lot_id,
            "shares": self.shares,
            "entry_price": str(self.entry_price),
            "entry_fee": str(self.entry_fee),
            "entry_timestamp": self.entry_timestamp,
            "stop_loss": str(self.stop_loss),
            "target": str(self.target) if self.target is not None else None,
            "order_notes": self.order_notes,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PaperPositionMetadata":
        return cls(
            ticker=data["ticker"],
            lot_id=data["lot_id"],
            shares=int(data["shares"]),
            entry_price=Decimal(str(data["entry_price"])),
            entry_fee=Decimal(str(data["entry_fee"])),
            entry_timestamp=data["entry_timestamp"],
            stop_loss=Decimal(str(data["stop_loss"])),
            target=Decimal(str(data["target"])) if data.get("target") is not None else None,
            order_notes=data.get("order_notes", ""),
        )


@dataclass(frozen=True)
class PaperFillRecord:
    """Immutable record of an executed paper entry fill."""
    fill_id: str
    ticker: str
    side: PaperOrderSide
    shares: int
    price: Decimal
    fee: Decimal
    timestamp: str
    stop_loss: Decimal
    target: Optional[Decimal] = None
    order_notes: str = ""
    lot_id: Optional[str] = None
    order_id: Optional[str] = None
    idempotency_key: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fill_id": self.fill_id,
            "ticker": self.ticker,
            "side": self.side.value,
            "shares": self.shares,
            "price": str(self.price),
            "fee": str(self.fee),
            "timestamp": self.timestamp,
            "stop_loss": str(self.stop_loss),
            "target": str(self.target) if self.target is not None else None,
            "order_notes": self.order_notes,
            "lot_id": self.lot_id,
            "order_id": self.order_id,
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True)
class PaperExitRecord:
    """Immutable record of an executed paper exit or write-off."""
    exit_id: str
    ticker: str
    shares: int
    price: Decimal
    fee: Decimal
    timestamp: str
    reason: PaperExitReason
    realized_pnl: Decimal
    lot_id: Optional[str] = None
    order_notes: str = ""
    order_id: Optional[str] = None
    idempotency_key: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "exit_id": self.exit_id,
            "ticker": self.ticker,
            "shares": self.shares,
            "price": str(self.price),
            "fee": str(self.fee),
            "timestamp": self.timestamp,
            "reason": self.reason.value,
            "realized_pnl": str(self.realized_pnl),
            "lot_id": self.lot_id,
            "order_notes": self.order_notes,
            "order_id": self.order_id,
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True)
class PaperTradeAuditEntry:
    """Structured audit trail record logged for every paper state transition."""
    event_id: str
    timestamp: str
    event_type: str
    ticker: Optional[str]
    shares: Optional[int]
    price: Optional[str]
    fee: Optional[str]
    equity_after: str
    cash_after: str
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "ticker": self.ticker,
            "shares": self.shares,
            "price": self.price,
            "fee": self.fee,
            "equity_after": self.equity_after,
            "cash_after": self.cash_after,
            "details": self.details,
        }


# Re-export Phase 8.0 Order Lifecycle & Idempotency types for clean unified imports
try:
    from order_models import (
        IllegalStateTransitionError,
        LEGAL_ORDER_TRANSITIONS,
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
    from authorization_models import (
        AuthorizationError,
        AuthorizationRecord,
        AuthorizationState,
        GateVerdict,
        HMACApprovalToken,
        HumanGateRejectionError,
        IllegalAuthorizationTransitionError,
        PlatformPermissionError,
        RiskGateRejectionError,
        SecretKeyUnavailableError,
        TerminalAuthorizationStateError,
        TokenAlreadyUsedError,
        TokenBindingMismatchError,
        TokenExpiredError,
        TokenTamperedError,
    )
    from authorization_token import (
        DEFAULT_TOKEN_TTL_SECONDS,
        HMACApprovalTokenManager,
        SecretKeyManager,
    )
    from authorization_service import HumanAuthorizationService
except ImportError:
    pass
