"""Data models and configurations for historical backtesting in trader-swing.

Enforces:
1. Pure Decimal arithmetic for all monetary amounts, returns, and ratios.
2. Complete audit trail for every simulated order, fill, and daily portfolio snapshot.
3. Configurable execution and transaction cost models.
4. Strict separation between strategy signal generation and execution fills.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence

from risk import FeeConfig, RiskConfig


class SimulatedOrderSide(str, Enum):
    """Direction of a simulated order."""
    BUY = "BUY"
    SELL = "SELL"


class SimulatedOrderType(str, Enum):
    """Execution type for a simulated order."""
    MARKET_NEXT_OPEN = "MARKET_NEXT_OPEN"
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    TRAILING_STOP = "TRAILING_STOP"
    REGIME_EXIT = "REGIME_EXIT"
    TIME_EXIT = "TIME_EXIT"


class SimulatedOrderStatus(str, Enum):
    """Lifecycle status of a simulated order."""
    PENDING = "PENDING"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class PriceAdjustmentType(str, Enum):
    """Explicit corporate-action price adjustment semantics.

    - RAW_UNADJUSTED: Raw unadjusted exchange prices as printed on the tape.
      Pre-split prices reflect unadjusted pre-split levels. Cash dividends cause
      overnight price drops. In this mode, active portfolio lots MUST be adjusted
      on split dates, and cash dividends are accrued/settled into the ledger.
    - SPLIT_ADJUSTED: Historical OHLC prices are backward split-adjusted to post-split
      dollar equivalents (standard Yahoo Finance chart output). Pre-split candles
      are already divided by the split ratio. In this mode, active portfolio lots
      MUST NOT be split again (prevents double split). Cash dividends are accrued/settled.
    - SPLIT_AND_DIVIDEND_ADJUSTED: Total-return series where historical prices are
      adjusted for both splits and cash dividend distributions (e.g. Yahoo adjclose).
      Capital gains in the price series already reflect dividend reinvestment. In this
      mode, cash dividends MUST NOT be accrued to the ledger (prevents double-counting).
    """
    RAW_UNADJUSTED = "RAW_UNADJUSTED"
    SPLIT_ADJUSTED = "SPLIT_ADJUSTED"
    SPLIT_AND_DIVIDEND_ADJUSTED = "SPLIT_AND_DIVIDEND_ADJUSTED"


@dataclass(frozen=True)
class StockSplitEvent:
    """Historical stock split event.

    Attributes:
        ticker: Uppercase US equity ticker.
        split_date: Effective date of the split.
        ratio: Split ratio (e.g., 2.0 for 2:1 forward split, 0.10 for 1:10 reverse split).
        split_numerator: Optional integer numerator for reverse splits (e.g. 1 for 1-for-10).
        split_denominator: Optional integer denominator for reverse splits (e.g. 10 for 1-for-10).
        description: Informational description.
    """
    ticker: str
    split_date: date
    ratio: Decimal
    split_numerator: Optional[int] = None
    split_denominator: Optional[int] = None
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "split_date": self.split_date.isoformat(),
            "ratio": str(self.ratio),
            "split_numerator": self.split_numerator,
            "split_denominator": self.split_denominator,
            "description": self.description,
        }


@dataclass(frozen=True)
class DividendEvent:
    """Historical cash dividend event.

    Attributes:
        ticker: Uppercase US equity ticker.
        ex_date: Ex-dividend date.
        amount_per_share: Cash dividend per share in USD.
        payable_date: Official distribution date when cash is credited to account.
        is_estimated_payable_date: Flag indicating whether payable_date was estimated.
    """
    ticker: str
    ex_date: date
    amount_per_share: Decimal
    payable_date: Optional[date] = None
    is_estimated_payable_date: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "ex_date": self.ex_date.isoformat(),
            "amount_per_share": str(self.amount_per_share),
            "payable_date": self.payable_date.isoformat() if self.payable_date else None,
            "is_estimated_payable_date": self.is_estimated_payable_date,
        }


@dataclass(frozen=True)
class DatasetIntegrityReport:
    """Audit report for historical candle series quality and calendar coverage."""
    ticker: str
    total_candles: int
    start_date: Optional[date]
    end_date: Optional[date]
    expected_calendar_sessions: int
    missing_sessions: List[date]
    duplicate_dates_count: int
    detected_unadjusted_splits: List[StockSplitEvent]
    adjustment_type: PriceAdjustmentType
    quality_status: str  # "CLEAN", "WARNINGS", "CORRUPTED"
    warnings: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "total_candles": self.total_candles,
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "end_date": self.end_date.isoformat() if self.end_date else None,
            "expected_calendar_sessions": self.expected_calendar_sessions,
            "missing_sessions": [d.isoformat() for d in self.missing_sessions],
            "duplicate_dates_count": self.duplicate_dates_count,
            "detected_unadjusted_splits": [s.to_dict() for s in self.detected_unadjusted_splits],
            "adjustment_type": self.adjustment_type.value,
            "quality_status": self.quality_status,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class SlippageConfig:
    """Configurable slippage assumptions for simulated order fills."""
    entry_bps: Decimal = Decimal("5.0")       # Basis points applied adversely to entry (0.05%)
    exit_bps: Decimal = Decimal("5.0")        # Basis points applied adversely to exit (0.05%)
    fixed_per_share: Decimal = Decimal("0.00") # Additional fixed per-share adverse slippage in USD
    atr_multiplier: Decimal = Decimal("0.00")  # Multiplier on ATR (e.g., 0.010 for 1% of ATR)
    min_cents: Decimal = Decimal("0.00")       # Minimum adverse slippage floor in USD

    def calculate_entry_fill_price(
        self,
        reference_price: Decimal,
        atr_value: Optional[Decimal] = None,
    ) -> Decimal:
        """Calculate adverse entry fill price (buyer pays higher)."""
        if atr_value is not None and (self.atr_multiplier > Decimal("0.00") or self.min_cents > Decimal("0.00")):
            slippage_amt = max(self.min_cents, (self.atr_multiplier * atr_value).quantize(Decimal("0.01"), ROUND_HALF_UP))
            return (reference_price + slippage_amt).quantize(Decimal("0.01"))
        factor = Decimal("1.0") + (self.entry_bps / Decimal("10000.0"))
        return (reference_price * factor + self.fixed_per_share).quantize(Decimal("0.01"))

    def calculate_exit_fill_price(
        self,
        reference_price: Decimal,
        atr_value: Optional[Decimal] = None,
    ) -> Decimal:
        """Calculate adverse exit fill price (seller receives lower)."""
        if atr_value is not None and (self.atr_multiplier > Decimal("0.00") or self.min_cents > Decimal("0.00")):
            slippage_amt = max(self.min_cents, (self.atr_multiplier * atr_value).quantize(Decimal("0.01"), ROUND_HALF_UP))
            raw = reference_price - slippage_amt
            return max(Decimal("0.01"), raw.quantize(Decimal("0.01")))
        factor = Decimal("1.0") - (self.exit_bps / Decimal("10000.0"))
        raw = reference_price * factor - self.fixed_per_share
        return max(Decimal("0.01"), raw.quantize(Decimal("0.01")))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entry_bps": str(self.entry_bps),
            "exit_bps": str(self.exit_bps),
            "fixed_per_share": str(self.fixed_per_share),
            "atr_multiplier": str(self.atr_multiplier),
            "min_cents": str(self.min_cents),
        }


@dataclass(frozen=True)
class ExecutionAssumptions:
    """Documented execution mechanics for the backtest engine."""
    fill_timing: str = "NEXT_SESSION_OPEN"
    conservative_same_bar_tie_break: bool = True  # Stop triggers first if both stop & target touched
    gap_down_stop_fill: str = "OPEN_PRICE"         # Fill at open price when gap down breaches stop
    allow_partial_fills: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fill_timing": self.fill_timing,
            "conservative_same_bar_tie_break": self.conservative_same_bar_tie_break,
            "gap_down_stop_fill": self.gap_down_stop_fill,
            "allow_partial_fills": self.allow_partial_fills,
        }


@dataclass
class SimulatedOrder:
    """Audit record for a simulated trading order."""
    order_id: str
    ticker: str
    side: SimulatedOrderSide
    order_type: SimulatedOrderType
    quantity: int
    signal_timestamp: datetime
    submission_timestamp: datetime
    reference_price: Decimal
    stop_loss_price: Optional[Decimal] = None
    target_price: Optional[Decimal] = None
    limit_price: Optional[Decimal] = None
    lot_id: Optional[str] = None
    fill_timestamp: Optional[datetime] = None
    fill_price: Optional[Decimal] = None
    slippage_usd: Decimal = Decimal("0.00")
    fee_usd: Decimal = Decimal("0.00")
    status: SimulatedOrderStatus = SimulatedOrderStatus.PENDING
    reason: str = ""
    rejection_reason: Optional[str] = None
    residual_cancelled_shares: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "order_id": self.order_id,
            "ticker": self.ticker,
            "side": self.side.value,
            "order_type": self.order_type.value,
            "quantity": self.quantity,
            "residual_cancelled_shares": self.residual_cancelled_shares,
            "signal_timestamp": self.signal_timestamp.isoformat(),
            "submission_timestamp": self.submission_timestamp.isoformat(),
            "reference_price": str(self.reference_price),
            "stop_loss_price": str(self.stop_loss_price) if self.stop_loss_price is not None else None,
            "target_price": str(self.target_price) if self.target_price is not None else None,
            "limit_price": str(self.limit_price) if self.limit_price is not None else None,
            "lot_id": self.lot_id,
            "fill_timestamp": self.fill_timestamp.isoformat() if self.fill_timestamp else None,
            "fill_price": str(self.fill_price) if self.fill_price is not None else None,
            "slippage_usd": str(self.slippage_usd),
            "fee_usd": str(self.fee_usd),
            "status": self.status.value,
            "reason": self.reason,
            "rejection_reason": self.rejection_reason,
        }

    def adjust_for_stock_split(
        self,
        ratio: Decimal,
        split_numerator: Optional[int] = None,
        split_denominator: Optional[int] = None,
    ) -> bool:
        """Adjust pending order shares and price-based fields across a stock split.

        In RAW_UNADJUSTED mode, adjusts reference entry, limit, stop loss, and target prices,
        and scales order quantity to maintain identical economic exposure and risk.

        Returns:
            True if order remains valid with whole-share quantity.
            False if order was cancelled due to fractional shares or non-positive quantity.
        """
        if split_numerator is not None and split_denominator is not None:
            num = int(split_numerator)
            denom = int(split_denominator)
        else:
            from fractions import Fraction
            frac = Fraction(str(ratio)).limit_denominator(1000)
            num, denom = frac.numerator, frac.denominator

        if (self.quantity * num) % denom != 0:
            self.status = SimulatedOrderStatus.CANCELLED
            frac_qty = (Decimal(self.quantity) * Decimal(num)) / Decimal(denom)
            self.rejection_reason = (
                f"Pending order cancelled: split ratio {ratio} produced non-integer share quantity "
                f"({frac_qty}) from {self.quantity} shares."
            )
            return False

        new_qty = (self.quantity * num) // denom
        if new_qty <= 0:
            self.status = SimulatedOrderStatus.CANCELLED
            self.rejection_reason = (
                f"Pending order cancelled: split ratio {ratio} produced zero shares from {self.quantity} shares."
            )
            return False

        factor = Decimal(num) / Decimal(denom)
        self.quantity = new_qty
        self.reference_price = (self.reference_price / factor).quantize(Decimal("0.01"))
        if self.stop_loss_price is not None:
            self.stop_loss_price = (self.stop_loss_price / factor).quantize(Decimal("0.01"))
        if self.target_price is not None:
            self.target_price = (self.target_price / factor).quantize(Decimal("0.01"))
        if self.limit_price is not None:
            self.limit_price = (self.limit_price / factor).quantize(Decimal("0.01"))

        return True


@dataclass(frozen=True)
class PortfolioDailySnapshot:
    """End-of-session financial and risk state of the simulated portfolio."""
    session_date: date
    timestamp: datetime
    equity_usd: Decimal
    cash_usd: Decimal
    gross_market_value_usd: Decimal
    invested_capital_pct: Decimal
    unrealized_pnl_usd: Decimal
    cumulative_realized_pnl_usd: Decimal
    open_positions_count: int
    open_lots_count: int
    buffered_open_risk_usd: Decimal
    aggregate_risk_pct: Decimal
    market_regime: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_date": self.session_date.isoformat(),
            "timestamp": self.timestamp.isoformat(),
            "equity_usd": str(self.equity_usd),
            "cash_usd": str(self.cash_usd),
            "gross_market_value_usd": str(self.gross_market_value_usd),
            "invested_capital_pct": str(self.invested_capital_pct),
            "unrealized_pnl_usd": str(self.unrealized_pnl_usd),
            "cumulative_realized_pnl_usd": str(self.cumulative_realized_pnl_usd),
            "open_positions_count": self.open_positions_count,
            "open_lots_count": self.open_lots_count,
            "buffered_open_risk_usd": str(self.buffered_open_risk_usd),
            "aggregate_risk_pct": str(self.aggregate_risk_pct),
            "market_regime": self.market_regime,
        }


@dataclass(frozen=True)
class SimulatedTradeRecord:
    """Complete round-trip closed trade record for performance evaluation."""
    trade_id: str
    ticker: str
    lot_id: str
    entry_timestamp: datetime
    exit_timestamp: datetime
    holding_sessions: int
    shares: int
    entry_price: Decimal
    exit_price: Decimal
    initial_stop_price: Decimal
    initial_target_price: Decimal
    gross_pnl_usd: Decimal
    total_fees_usd: Decimal
    total_slippage_usd: Decimal
    net_pnl_usd: Decimal
    return_pct: Decimal
    r_multiple: Decimal
    exit_reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "ticker": self.ticker,
            "lot_id": self.lot_id,
            "entry_timestamp": self.entry_timestamp.isoformat(),
            "exit_timestamp": self.exit_timestamp.isoformat(),
            "holding_sessions": self.holding_sessions,
            "shares": self.shares,
            "entry_price": str(self.entry_price),
            "exit_price": str(self.exit_price),
            "initial_stop_price": str(self.initial_stop_price),
            "initial_target_price": str(self.initial_target_price),
            "gross_pnl_usd": str(self.gross_pnl_usd),
            "total_fees_usd": str(self.total_fees_usd),
            "total_slippage_usd": str(self.total_slippage_usd),
            "net_pnl_usd": str(self.net_pnl_usd),
            "return_pct": str(self.return_pct),
            "r_multiple": str(self.r_multiple),
            "exit_reason": self.exit_reason,
        }


@dataclass(frozen=True)
class BacktestConfig:
    """Master configuration for a historical backtesting run."""
    start_date: date
    end_date: date
    universe: List[str]
    initial_capital_usd: Decimal = Decimal("5000.00")
    risk_config: RiskConfig = field(default_factory=RiskConfig)
    fee_config: FeeConfig = field(default_factory=FeeConfig)
    slippage_config: SlippageConfig = field(default_factory=SlippageConfig)
    execution_assumptions: ExecutionAssumptions = field(default_factory=ExecutionAssumptions)
    benchmark_ticker: str = "SPY"
    top_candidates_per_session: int = 5
    price_adjustment_type: PriceAdjustmentType = PriceAdjustmentType.SPLIT_ADJUSTED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "universe": list(self.universe),
            "initial_capital_usd": str(self.initial_capital_usd),
            "price_adjustment_type": self.price_adjustment_type.value,
            "risk_config": {
                "risk_per_trade_pct": str(self.risk_config.risk_per_trade_pct),
                "max_aggregate_buffered_risk_pct": str(self.risk_config.max_aggregate_buffered_risk_pct),
                "max_allocation_per_ticker_pct": str(self.risk_config.max_allocation_per_ticker_pct),
                "max_invested_capital_pct": str(self.risk_config.max_invested_capital_pct),
                "min_cash_reserve_pct": str(self.risk_config.min_cash_reserve_pct),
                "max_simultaneous_positions": self.risk_config.max_simultaneous_positions,
                "gap_risk_multiplier": str(self.risk_config.gap_risk_multiplier),
            },
            "fee_config": {
                "entry_fixed": str(self.fee_config.entry.fixed),
                "exit_fixed": str(self.fee_config.exit.fixed),
            },
            "slippage_config": self.slippage_config.to_dict(),
            "execution_assumptions": self.execution_assumptions.to_dict(),
            "benchmark_ticker": self.benchmark_ticker,
            "top_candidates_per_session": self.top_candidates_per_session,
        }
