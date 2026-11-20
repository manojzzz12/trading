"""Forward Paper Trading Evaluation Models for Trader-Swing Phase 9.

Defines data structures for observing live/forward market sessions, evaluating
the frozen trading strategy, recording candidate funnel metrics, and tracking
hypothetical trade performance without committing real or paper capital.

MANDATORY SAFETY MARKERS:
- All evaluation records marked: 'FORWARD_EVALUATION_ONLY'
- All hypothetical trades marked: 'SIMULATED_OBSERVATION — NOT EXECUTED'
- Zero connection to root paper_journal.json or real broker execution.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import json
from typing import Any, Dict, List, Optional, Sequence


MARKER_FORWARD_EVALUATION = "FORWARD_EVALUATION_ONLY"
MARKER_SIMULATED_TRADE = "SIMULATED_OBSERVATION — NOT EXECUTED"


class ForwardDataMode(str, Enum):
    """Execution provenance mode for forward evaluation records."""
    DAILY = "DAILY"
    LIVE = "LIVE"
    FIXTURE = "FIXTURE"
    HISTORICAL_PLAYBACK = "HISTORICAL_PLAYBACK"


class ForwardScreeningDecision(str, Enum):
    """Outcome of the 5-pillar screener evaluation."""
    ELIGIBLE = "ELIGIBLE"
    REJECTED = "REJECTED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    STALE_DATA = "STALE_DATA"


class HypotheticalTradeStatus(str, Enum):
    """Lifecycle states of a hypothetical forward trade."""
    PROPOSED = "PROPOSED"
    OPEN = "OPEN"
    CLOSED_TAKE_PROFIT = "CLOSED_TAKE_PROFIT"
    CLOSED_STOP_LOSS = "CLOSED_STOP_LOSS"
    CLOSED_MAX_HOLDING = "CLOSED_MAX_HOLDING"
    CLOSED_SESSION_END = "CLOSED_SESSION_END"


@dataclass(frozen=True)
class ForwardObservationRecord:
    """Detailed observation record captured for a single ticker in an evaluation session.
    
    Contains full data provenance, market regime, 5-pillar scores, rejection reasons,
    signal classifications, and Phase 2 deterministic risk sizing.
    """
    observation_id: str
    session_date: str
    observation_timestamp: str
    ticker: str
    data_mode: str
    data_provenance: Dict[str, Any]
    market_regime: str
    regime_allows_longs: bool
    screening_decision: str
    five_pillar_scores: Dict[str, Any]
    rejection_reasons: List[str]
    signal_type: Optional[str]
    proposed_entry: Optional[Decimal]
    proposed_stop: Optional[Decimal]
    proposed_target: Optional[Decimal]
    risk_sizing: Optional[Dict[str, Any]]
    is_eligible: bool
    schema_version: str = "1.0.0"
    record_marker: str = MARKER_FORWARD_EVALUATION
    disclaimer: str = MARKER_SIMULATED_TRADE

    def to_dict(self) -> Dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "session_date": self.session_date,
            "observation_timestamp": self.observation_timestamp,
            "ticker": self.ticker,
            "data_mode": self.data_mode,
            "data_provenance": self.data_provenance,
            "market_regime": self.market_regime,
            "regime_allows_longs": self.regime_allows_longs,
            "screening_decision": self.screening_decision,
            "five_pillar_scores": self.five_pillar_scores,
            "rejection_reasons": list(self.rejection_reasons),
            "signal_type": self.signal_type,
            "proposed_entry": str(self.proposed_entry) if self.proposed_entry is not None else None,
            "proposed_stop": str(self.proposed_stop) if self.proposed_stop is not None else None,
            "proposed_target": str(self.proposed_target) if self.proposed_target is not None else None,
            "risk_sizing": self.risk_sizing,
            "is_eligible": self.is_eligible,
            "schema_version": self.schema_version,
            "record_marker": self.record_marker,
            "disclaimer": self.disclaimer,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ForwardObservationRecord":
        return cls(
            observation_id=d["observation_id"],
            session_date=d["session_date"],
            observation_timestamp=d["observation_timestamp"],
            ticker=d["ticker"],
            data_mode=d.get("data_mode", ForwardDataMode.LIVE.value),
            data_provenance=d.get("data_provenance", {}),
            market_regime=d["market_regime"],
            regime_allows_longs=bool(d["regime_allows_longs"]),
            screening_decision=d["screening_decision"],
            five_pillar_scores=d.get("five_pillar_scores", {}),
            rejection_reasons=list(d.get("rejection_reasons", [])),
            signal_type=d.get("signal_type"),
            proposed_entry=Decimal(d["proposed_entry"]) if d.get("proposed_entry") is not None else None,
            proposed_stop=Decimal(d["proposed_stop"]) if d.get("proposed_stop") is not None else None,
            proposed_target=Decimal(d["proposed_target"]) if d.get("proposed_target") is not None else None,
            risk_sizing=d.get("risk_sizing"),
            is_eligible=bool(d["is_eligible"]),
            schema_version=d.get("schema_version", "1.0.0"),
            record_marker=d.get("record_marker", MARKER_FORWARD_EVALUATION),
            disclaimer=d.get("disclaimer", MARKER_SIMULATED_TRADE),
        )


@dataclass
class ForwardTradeProposal:
    """Hypothetical trade proposal generated strictly for an eligible candidate passing all gates."""
    proposal_id: str
    observation_id: str
    session_date: str
    ticker: str
    data_mode: str
    shares: int
    entry_reference_price: Decimal
    stop_loss_price: Decimal
    target_price: Decimal
    entry_fee: Decimal = Decimal("1.00")
    estimated_exit_fee: Decimal = Decimal("1.00")
    total_capital_required: Decimal = Decimal("0.00")
    risk_amount: Decimal = Decimal("0.00")
    status: str = HypotheticalTradeStatus.PROPOSED.value
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    marker: str = MARKER_SIMULATED_TRADE

    def to_dict(self) -> Dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "observation_id": self.observation_id,
            "session_date": self.session_date,
            "ticker": self.ticker,
            "data_mode": self.data_mode,
            "shares": self.shares,
            "entry_reference_price": str(self.entry_reference_price),
            "stop_loss_price": str(self.stop_loss_price),
            "target_price": str(self.target_price),
            "entry_fee": str(self.entry_fee),
            "estimated_exit_fee": str(self.estimated_exit_fee),
            "total_capital_required": str(self.total_capital_required),
            "risk_amount": str(self.risk_amount),
            "status": self.status,
            "created_at": self.created_at,
            "marker": self.marker,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ForwardTradeProposal":
        return cls(
            proposal_id=d["proposal_id"],
            observation_id=d["observation_id"],
            session_date=d["session_date"],
            ticker=d["ticker"],
            data_mode=d.get("data_mode", ForwardDataMode.LIVE.value),
            shares=int(d["shares"]),
            entry_reference_price=Decimal(d["entry_reference_price"]),
            stop_loss_price=Decimal(d["stop_loss_price"]),
            target_price=Decimal(d["target_price"]),
            entry_fee=Decimal(d.get("entry_fee", "1.00")),
            estimated_exit_fee=Decimal(d.get("estimated_exit_fee", "1.00")),
            total_capital_required=Decimal(d.get("total_capital_required", "0.00")),
            risk_amount=Decimal(d.get("risk_amount", "0.00")),
            status=d.get("status", HypotheticalTradeStatus.PROPOSED.value),
            created_at=d.get("created_at", datetime.now(timezone.utc).isoformat()),
            marker=d.get("marker", MARKER_SIMULATED_TRADE),
        )


@dataclass
class HypotheticalPosition:
    """An active hypothetical forward position tracked across subsequent completed daily bars."""
    position_id: str
    proposal_id: str
    session_date: str
    ticker: str
    data_mode: str
    shares: int
    entry_price: Decimal
    entry_fee: Decimal
    stop_loss: Decimal
    target: Decimal
    current_price: Decimal
    highest_high: Decimal
    lowest_low: Decimal
    mfe_pct: Decimal = Decimal("0.0000")
    mfe_usd: Decimal = Decimal("0.00")
    mae_pct: Decimal = Decimal("0.0000")
    mae_usd: Decimal = Decimal("0.00")
    bars_held: int = 0
    unrealized_pnl: Decimal = Decimal("0.00")
    status: str = HypotheticalTradeStatus.OPEN.value
    opened_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    marker: str = MARKER_SIMULATED_TRADE

    @property
    def cost_basis_usd(self) -> Decimal:
        return (Decimal(self.shares) * self.entry_price) + self.entry_fee

    @property
    def market_value_usd(self) -> Decimal:
        return Decimal(self.shares) * self.current_price

    def to_dict(self) -> Dict[str, Any]:
        return {
            "position_id": self.position_id,
            "proposal_id": self.proposal_id,
            "session_date": self.session_date,
            "ticker": self.ticker,
            "data_mode": self.data_mode,
            "shares": self.shares,
            "entry_price": str(self.entry_price),
            "entry_fee": str(self.entry_fee),
            "stop_loss": str(self.stop_loss),
            "target": str(self.target),
            "current_price": str(self.current_price),
            "highest_high": str(self.highest_high),
            "lowest_low": str(self.lowest_low),
            "mfe_pct": str(self.mfe_pct),
            "mfe_usd": str(self.mfe_usd),
            "mae_pct": str(self.mae_pct),
            "mae_usd": str(self.mae_usd),
            "bars_held": self.bars_held,
            "unrealized_pnl": str(self.unrealized_pnl),
            "status": self.status,
            "opened_at": self.opened_at,
            "last_updated_at": self.last_updated_at,
            "marker": self.marker,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "HypotheticalPosition":
        return cls(
            position_id=d["position_id"],
            proposal_id=d["proposal_id"],
            session_date=d["session_date"],
            ticker=d["ticker"],
            data_mode=d.get("data_mode", ForwardDataMode.LIVE.value),
            shares=int(d["shares"]),
            entry_price=Decimal(d["entry_price"]),
            entry_fee=Decimal(d["entry_fee"]),
            stop_loss=Decimal(d["stop_loss"]),
            target=Decimal(d["target"]),
            current_price=Decimal(d["current_price"]),
            highest_high=Decimal(d["highest_high"]),
            lowest_low=Decimal(d["lowest_low"]),
            mfe_pct=Decimal(d.get("mfe_pct", "0.0000")),
            mfe_usd=Decimal(d.get("mfe_usd", "0.00")),
            mae_pct=Decimal(d.get("mae_pct", "0.0000")),
            mae_usd=Decimal(d.get("mae_usd", "0.00")),
            bars_held=int(d.get("bars_held", 0)),
            unrealized_pnl=Decimal(d.get("unrealized_pnl", "0.00")),
            status=d.get("status", HypotheticalTradeStatus.OPEN.value),
            opened_at=d.get("opened_at", datetime.now(timezone.utc).isoformat()),
            last_updated_at=d.get("last_updated_at", datetime.now(timezone.utc).isoformat()),
            marker=d.get("marker", MARKER_SIMULATED_TRADE),
        )


@dataclass(frozen=True)
class HypotheticalClosedTrade:
    """Record of a hypothetical forward trade that reached an exit condition."""
    trade_id: str
    position_id: str
    proposal_id: str
    ticker: str
    data_mode: str
    shares: int
    entry_date: str
    entry_price: Decimal
    exit_date: str
    exit_price: Decimal
    entry_fee: Decimal
    exit_fee: Decimal
    gross_pnl: Decimal
    net_pnl: Decimal
    return_pct: Decimal
    exit_reason: str
    bars_held: int
    mfe_usd: Decimal
    mae_usd: Decimal
    closed_at: str
    marker: str = MARKER_SIMULATED_TRADE

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "position_id": self.position_id,
            "proposal_id": self.proposal_id,
            "ticker": self.ticker,
            "data_mode": self.data_mode,
            "shares": self.shares,
            "entry_date": self.entry_date,
            "entry_price": str(self.entry_price),
            "exit_date": self.exit_date,
            "exit_price": str(self.exit_price),
            "entry_fee": str(self.entry_fee),
            "exit_fee": str(self.exit_fee),
            "gross_pnl": str(self.gross_pnl),
            "net_pnl": str(self.net_pnl),
            "return_pct": str(self.return_pct),
            "exit_reason": self.exit_reason,
            "bars_held": self.bars_held,
            "mfe_usd": str(self.mfe_usd),
            "mae_usd": str(self.mae_usd),
            "closed_at": self.closed_at,
            "marker": self.marker,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "HypotheticalClosedTrade":
        return cls(
            trade_id=d["trade_id"],
            position_id=d["position_id"],
            proposal_id=d["proposal_id"],
            ticker=d["ticker"],
            data_mode=d.get("data_mode", ForwardDataMode.LIVE.value),
            shares=int(d["shares"]),
            entry_date=d["entry_date"],
            entry_price=Decimal(d["entry_price"]),
            exit_date=d["exit_date"],
            exit_price=Decimal(d["exit_price"]),
            entry_fee=Decimal(d["entry_fee"]),
            exit_fee=Decimal(d["exit_fee"]),
            gross_pnl=Decimal(d["gross_pnl"]),
            net_pnl=Decimal(d["net_pnl"]),
            return_pct=Decimal(d["return_pct"]),
            exit_reason=d["exit_reason"],
            bars_held=int(d["bars_held"]),
            mfe_usd=Decimal(d["mfe_usd"]),
            mae_usd=Decimal(d["mae_usd"]),
            closed_at=d["closed_at"],
            marker=d.get("marker", MARKER_SIMULATED_TRADE),
        )


@dataclass(frozen=True)
class EvaluationSessionSummary:
    """Auditable session-level summary of an evaluation run."""
    session_date: str
    data_mode: str
    market_regime: str
    tickers_screened: int
    eligible_candidates: int
    rejected_candidates: int
    proposals_created: int
    hypothetical_trades_opened: int
    hypothetical_trades_closed: int
    active_positions_count: int
    virtual_equity: Decimal
    virtual_cash: Decimal
    realized_net_pnl: Decimal
    unrealized_pnl: Decimal
    total_fees_paid: Decimal
    audit_timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    marker: str = MARKER_FORWARD_EVALUATION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_date": self.session_date,
            "data_mode": self.data_mode,
            "market_regime": self.market_regime,
            "tickers_screened": self.tickers_screened,
            "eligible_candidates": self.eligible_candidates,
            "rejected_candidates": self.rejected_candidates,
            "proposals_created": self.proposals_created,
            "hypothetical_trades_opened": self.hypothetical_trades_opened,
            "hypothetical_trades_closed": self.hypothetical_trades_closed,
            "active_positions_count": self.active_positions_count,
            "virtual_equity": str(self.virtual_equity),
            "virtual_cash": str(self.virtual_cash),
            "realized_net_pnl": str(self.realized_net_pnl),
            "unrealized_pnl": str(self.unrealized_pnl),
            "total_fees_paid": str(self.total_fees_paid),
            "audit_timestamp": self.audit_timestamp,
            "marker": self.marker,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "EvaluationSessionSummary":
        return cls(
            session_date=d["session_date"],
            data_mode=d.get("data_mode", ForwardDataMode.LIVE.value),
            market_regime=d["market_regime"],
            tickers_screened=int(d["tickers_screened"]),
            eligible_candidates=int(d["eligible_candidates"]),
            rejected_candidates=int(d["rejected_candidates"]),
            proposals_created=int(d["proposals_created"]),
            hypothetical_trades_opened=int(d["hypothetical_trades_opened"]),
            hypothetical_trades_closed=int(d["hypothetical_trades_closed"]),
            active_positions_count=int(d["active_positions_count"]),
            virtual_equity=Decimal(d["virtual_equity"]),
            virtual_cash=Decimal(d["virtual_cash"]),
            realized_net_pnl=Decimal(d["realized_net_pnl"]),
            unrealized_pnl=Decimal(d["unrealized_pnl"]),
            total_fees_paid=Decimal(d["total_fees_paid"]),
            audit_timestamp=d.get("audit_timestamp", datetime.now(timezone.utc).isoformat()),
            marker=d.get("marker", MARKER_FORWARD_EVALUATION),
        )
