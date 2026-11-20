"""Data models and lifecycle states for Trader-Swing Phase 8.1 Multi-Gate Human Authorization Subsystem.

Enforces:
1. Strict 3-gate authorization sequence:
   PROPOSED -> RISK_VALIDATED -> PENDING_HUMAN_APPROVAL -> HUMAN_APPROVED -> PLATFORM_PERMISSION_APPROVED -> AUTHORIZED
2. Terminal rejection/failure states:
   RISK_REJECTED, HUMAN_REJECTED, EXPIRED, CANCELLED, AUTHORIZATION_FAILED
3. Cryptographically signed, time-limited HMAC approval tokens bound to canonical order intent.
4. Single-use token enforcement and replay protection.
5. Invariant: An authorized order is NOT an executed order (zero fills, zero cash mutations).
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

from accounting import quantize_cent, to_decimal
from paper_models import PaperOrderSide


class AuthorizationState(str, Enum):
    """Authoritative lifecycle states for multi-gate human authorization."""
    PROPOSED = "PROPOSED"
    RISK_VALIDATED = "RISK_VALIDATED"
    PENDING_HUMAN_APPROVAL = "PENDING_HUMAN_APPROVAL"
    HUMAN_APPROVED = "HUMAN_APPROVED"
    PLATFORM_PERMISSION_APPROVED = "PLATFORM_PERMISSION_APPROVED"
    AUTHORIZED = "AUTHORIZED"

    # Terminal rejection and cancellation states
    RISK_REJECTED = "RISK_REJECTED"
    HUMAN_REJECTED = "HUMAN_REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    AUTHORIZATION_FAILED = "AUTHORIZATION_FAILED"


TERMINAL_AUTH_STATES: Set[AuthorizationState] = {
    AuthorizationState.AUTHORIZED,
    AuthorizationState.RISK_REJECTED,
    AuthorizationState.HUMAN_REJECTED,
    AuthorizationState.EXPIRED,
    AuthorizationState.CANCELLED,
    AuthorizationState.AUTHORIZATION_FAILED,
}

LEGAL_AUTH_TRANSITIONS: Dict[AuthorizationState, Set[AuthorizationState]] = {
    AuthorizationState.PROPOSED: {
        AuthorizationState.RISK_VALIDATED,
        AuthorizationState.RISK_REJECTED,
        AuthorizationState.CANCELLED,
    },
    AuthorizationState.RISK_VALIDATED: {
        AuthorizationState.PENDING_HUMAN_APPROVAL,
        AuthorizationState.CANCELLED,
        AuthorizationState.EXPIRED,
    },
    AuthorizationState.PENDING_HUMAN_APPROVAL: {
        AuthorizationState.HUMAN_APPROVED,
        AuthorizationState.HUMAN_REJECTED,
        AuthorizationState.CANCELLED,
        AuthorizationState.EXPIRED,
    },
    AuthorizationState.HUMAN_APPROVED: {
        AuthorizationState.PLATFORM_PERMISSION_APPROVED,
        AuthorizationState.AUTHORIZATION_FAILED,
        AuthorizationState.CANCELLED,
        AuthorizationState.EXPIRED,
    },
    AuthorizationState.PLATFORM_PERMISSION_APPROVED: {
        AuthorizationState.AUTHORIZED,
        AuthorizationState.AUTHORIZATION_FAILED,
        AuthorizationState.CANCELLED,
    },
    AuthorizationState.AUTHORIZED: set(),
    AuthorizationState.RISK_REJECTED: set(),
    AuthorizationState.HUMAN_REJECTED: set(),
    AuthorizationState.EXPIRED: set(),
    AuthorizationState.CANCELLED: set(),
    AuthorizationState.AUTHORIZATION_FAILED: set(),
}


# -----------------------------------------------------------------------------
# Exception Hierarchy
# -----------------------------------------------------------------------------

class AuthorizationError(ValueError):
    """Base exception for authorization subsystem failures."""
    pass


class RiskGateRejectionError(AuthorizationError):
    """Raised when Gate 1 (deterministic risk check) rejects an order."""
    pass


class HumanGateRejectionError(AuthorizationError):
    """Raised when Gate 2 (explicit human confirmation) rejects or cancels an order."""
    pass


class PlatformPermissionError(AuthorizationError):
    """Raised when Gate 3 (Antigravity execution permission) is denied or unavailable."""
    pass


class TokenExpiredError(AuthorizationError):
    """Raised when an approval token has passed its time-to-live expiration."""
    pass


class TokenTamperedError(AuthorizationError):
    """Raised when an HMAC signature fails cryptographic verification."""
    pass


class TokenAlreadyUsedError(AuthorizationError):
    """Raised when a single-use approval token is submitted more than once."""
    pass


class TokenBindingMismatchError(AuthorizationError):
    """Raised when an approval token attributes mismatch the target order."""
    pass


class SecretKeyUnavailableError(AuthorizationError):
    """Raised when the cryptographic signing secret is missing or unreadable."""
    pass


class TerminalAuthorizationStateError(AuthorizationError):
    """Raised when an order in a terminal authorization state attempts further transition."""
    pass


class IllegalAuthorizationTransitionError(AuthorizationError):
    """Raised when an invalid state transition is attempted."""
    pass


# -----------------------------------------------------------------------------
# Data Models
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class GateVerdict:
    """Audit record of an individual gate evaluation."""
    gate_name: str
    passed: bool
    reason: str
    timestamp: str
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "gate_name": self.gate_name,
            "passed": self.passed,
            "reason": self.reason,
            "timestamp": self.timestamp,
            "details": dict(self.details),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GateVerdict":
        return cls(
            gate_name=data["gate_name"],
            passed=bool(data["passed"]),
            reason=data.get("reason", ""),
            timestamp=data["timestamp"],
            details=dict(data.get("details", {})),
        )


@dataclass
class HMACApprovalToken:
    """Cryptographically signed, time-limited approval token bound to exact order intent."""
    token_id: str
    order_id: str
    idempotency_key: str
    intent_hash: str
    session_date: str
    ticker: str
    side: PaperOrderSide
    shares: int
    limit_price: Optional[Decimal]
    stop_loss: Decimal
    target: Optional[Decimal]
    issued_at: str
    expires_at: str
    nonce: str
    signature: str
    used: bool = False
    used_at: Optional[str] = None

    def is_expired(self, current_time: Optional[datetime] = None) -> bool:
        """Return True if token has elapsed its time-to-live."""
        now_dt = current_time or datetime.now(timezone.utc)
        exp_dt = datetime.fromisoformat(self.expires_at)
        return now_dt > exp_dt

    def to_dict(self) -> Dict[str, Any]:
        return {
            "token_id": self.token_id,
            "order_id": self.order_id,
            "idempotency_key": self.idempotency_key,
            "intent_hash": self.intent_hash,
            "session_date": self.session_date,
            "ticker": self.ticker,
            "side": self.side.value if isinstance(self.side, PaperOrderSide) else str(self.side),
            "shares": self.shares,
            "limit_price": str(self.limit_price) if self.limit_price is not None else None,
            "stop_loss": str(self.stop_loss),
            "target": str(self.target) if self.target is not None else None,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "nonce": self.nonce,
            "signature": self.signature,
            "used": self.used,
            "used_at": self.used_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HMACApprovalToken":
        return cls(
            token_id=data["token_id"],
            order_id=data["order_id"],
            idempotency_key=data["idempotency_key"],
            intent_hash=data["intent_hash"],
            session_date=data["session_date"],
            ticker=data["ticker"],
            side=PaperOrderSide(data["side"]),
            shares=int(data["shares"]),
            limit_price=to_decimal(data["limit_price"]) if data.get("limit_price") is not None else None,
            stop_loss=to_decimal(data["stop_loss"]),
            target=to_decimal(data["target"]) if data.get("target") is not None else None,
            issued_at=data["issued_at"],
            expires_at=data["expires_at"],
            nonce=data["nonce"],
            signature=data["signature"],
            used=bool(data.get("used", False)),
            used_at=data.get("used_at"),
        )


@dataclass
class AuthorizationRecord:
    """Comprehensive record of an order's authorization progress across all 3 gates."""
    order_id: str
    idempotency_key: str
    ticker: str
    shares: int
    state: AuthorizationState
    created_at: str
    updated_at: str
    gate_verdicts: List[GateVerdict] = field(default_factory=list)
    token: Optional[HMACApprovalToken] = None
    rejection_reason: Optional[str] = None
    cancellation_reason: Optional[str] = None
    failure_reason: Optional[str] = None

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_AUTH_STATES

    @property
    def is_authorized(self) -> bool:
        return self.state == AuthorizationState.AUTHORIZED

    def transition_to(
        self,
        new_state: AuthorizationState,
        timestamp: Optional[str] = None,
        reason: str = "",
        verdict: Optional[GateVerdict] = None,
    ) -> None:
        """Execute and record an authorization state transition with gate verification."""
        ts = timestamp or datetime.now(timezone.utc).isoformat()

        # Terminal lockout
        if self.state in TERMINAL_AUTH_STATES:
            raise TerminalAuthorizationStateError(
                f"Cannot transition order '{self.order_id}' from terminal state "
                f"'{self.state.value}' to '{new_state.value}'."
            )

        # Transition table check
        allowed = LEGAL_AUTH_TRANSITIONS.get(self.state, set())
        if new_state not in allowed:
            raise IllegalAuthorizationTransitionError(
                f"Illegal authorization transition from '{self.state.value}' to '{new_state.value}' "
                f"for order '{self.order_id}'. Permitted: {[s.value for s in allowed]}"
            )

        if verdict is not None:
            self.gate_verdicts.append(verdict)

        self.state = new_state
        self.updated_at = ts

        if new_state in (AuthorizationState.RISK_REJECTED, AuthorizationState.HUMAN_REJECTED):
            self.rejection_reason = reason
        elif new_state == AuthorizationState.CANCELLED:
            self.cancellation_reason = reason
        elif new_state == AuthorizationState.AUTHORIZATION_FAILED:
            self.failure_reason = reason

    def to_dict(self) -> Dict[str, Any]:
        return {
            "order_id": self.order_id,
            "idempotency_key": self.idempotency_key,
            "ticker": self.ticker,
            "shares": self.shares,
            "state": self.state.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "gate_verdicts": [v.to_dict() for v in self.gate_verdicts],
            "token": self.token.to_dict() if self.token is not None else None,
            "rejection_reason": self.rejection_reason,
            "cancellation_reason": self.cancellation_reason,
            "failure_reason": self.failure_reason,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AuthorizationRecord":
        verdicts = [GateVerdict.from_dict(v) for v in data.get("gate_verdicts", [])]
        tok = HMACApprovalToken.from_dict(data["token"]) if data.get("token") is not None else None
        return cls(
            order_id=data["order_id"],
            idempotency_key=data["idempotency_key"],
            ticker=data["ticker"],
            shares=int(data["shares"]),
            state=AuthorizationState(data["state"]),
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            gate_verdicts=verdicts,
            token=tok,
            rejection_reason=data.get("rejection_reason"),
            cancellation_reason=data.get("cancellation_reason"),
            failure_reason=data.get("failure_reason"),
        )
