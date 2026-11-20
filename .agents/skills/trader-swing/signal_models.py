"""Data models for swing trading entry and exit signals in trader-swing.

Enforces:
1. Pure Decimal arithmetic for price levels, stops, targets, and reward-to-risk ratios.
2. Clear distinction between technically valid signals, risk-approved trade plans, and risk-rejected candidates.
3. Informational exit signals for portfolio lots with zero mutation of actual positions.
4. Comprehensive evidence tracking and invalidating conditions.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional


class EntryStrategyType(str, Enum):
    """Specific technical swing-entry setup type."""
    TREND_CONTINUATION = "TREND_CONTINUATION"
    BREAKOUT_CONFIRMATION = "BREAKOUT_CONFIRMATION"
    PULLBACK_ENTRY = "PULLBACK_ENTRY"


class ExitSignalType(str, Enum):
    """Reason code for an informational exit recommendation."""
    STOP_LOSS_BREACHED = "STOP_LOSS_BREACHED"
    TARGET_REACHED = "TARGET_REACHED"
    TREND_DETERIORATION = "TREND_DETERIORATION"
    MOMENTUM_REVERSAL = "MOMENTUM_REVERSAL"
    TRAILING_STOP_UPDATE = "TRAILING_STOP_UPDATE"
    TIME_BASED_REEVALUATION = "TIME_BASED_REEVALUATION"
    REGIME_DETERIORATION = "REGIME_DETERIORATION"


@dataclass(frozen=True)
class ProposedTradePlan:
    """Deterministic trade plan evaluated by the Phase 2 risk engine."""
    ticker: str
    strategy_type: EntryStrategyType
    entry_price: Decimal
    entry_zone_low: Decimal
    entry_zone_high: Decimal
    stop_loss_price: Decimal
    target_price: Decimal
    reward_to_risk: Decimal
    shares: int
    total_capital_required: Decimal
    total_buffered_risk: Decimal
    risk_approved: bool
    binding_constraint: Optional[str]
    risk_rejection_reasons: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "strategy_type": self.strategy_type.value,
            "entry_price": str(self.entry_price),
            "entry_zone_low": str(self.entry_zone_low),
            "entry_zone_high": str(self.entry_zone_high),
            "stop_loss_price": str(self.stop_loss_price),
            "target_price": str(self.target_price),
            "reward_to_risk": str(self.reward_to_risk),
            "shares": self.shares,
            "total_capital_required": str(self.total_capital_required),
            "total_buffered_risk": str(self.total_buffered_risk),
            "risk_approved": self.risk_approved,
            "binding_constraint": self.binding_constraint,
            "risk_rejection_reasons": list(self.risk_rejection_reasons),
        }


@dataclass(frozen=True)
class EntrySignal:
    """Explainable swing entry signal."""
    ticker: str
    strategy: EntryStrategyType
    signal_timestamp: datetime
    reference_price: Decimal
    entry_zone_low: Decimal
    entry_zone_high: Decimal
    suggested_stop_loss: Decimal
    suggested_target: Decimal
    reward_to_risk: Decimal
    confidence_score: Decimal
    supporting_evidence: List[str]
    contradicting_evidence: List[str]
    invalidating_conditions: List[str]
    data_quality_status: str
    expiration_sessions: int
    is_technically_valid: bool
    trade_plan: Optional[ProposedTradePlan] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "strategy": self.strategy.value,
            "signal_timestamp": self.signal_timestamp.isoformat(),
            "reference_price": str(self.reference_price),
            "entry_zone_low": str(self.entry_zone_low),
            "entry_zone_high": str(self.entry_zone_high),
            "suggested_stop_loss": str(self.suggested_stop_loss),
            "suggested_target": str(self.suggested_target),
            "reward_to_risk": str(self.reward_to_risk),
            "confidence_score": str(self.confidence_score),
            "supporting_evidence": list(self.supporting_evidence),
            "contradicting_evidence": list(self.contradicting_evidence),
            "invalidating_conditions": list(self.invalidating_conditions),
            "data_quality_status": self.data_quality_status,
            "expiration_sessions": self.expiration_sessions,
            "is_technically_valid": self.is_technically_valid,
            "trade_plan": self.trade_plan.to_dict() if self.trade_plan else None,
        }


@dataclass(frozen=True)
class ExitSignal:
    """Informational exit recommendation for an existing open lot/position."""
    ticker: str
    lot_id: Optional[str]
    exit_type: ExitSignalType
    signal_timestamp: datetime
    current_price: Decimal
    entry_price: Decimal
    stop_loss_price: Decimal
    target_price: Decimal
    recommended_action: str  # e.g. "EXIT_FULL", "UPDATE_STOP"
    new_stop_price: Optional[Decimal]
    unrealized_pnl_usd: Decimal
    unrealized_return_pct: Decimal
    holding_sessions: int
    rationale: str
    evidence: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "lot_id": self.lot_id,
            "exit_type": self.exit_type.value,
            "signal_timestamp": self.signal_timestamp.isoformat(),
            "current_price": str(self.current_price),
            "entry_price": str(self.entry_price),
            "stop_loss_price": str(self.stop_loss_price),
            "target_price": str(self.target_price),
            "recommended_action": self.recommended_action,
            "new_stop_price": str(self.new_stop_price) if self.new_stop_price is not None else None,
            "unrealized_pnl_usd": str(self.unrealized_pnl_usd),
            "unrealized_return_pct": str(self.unrealized_return_pct),
            "holding_sessions": self.holding_sessions,
            "rationale": self.rationale,
            "evidence": list(self.evidence),
        }
