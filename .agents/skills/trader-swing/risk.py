"""Deterministic Risk Management and Position Sizing Engine for trader-swing.

Enforces:
1. Per-trade risk limit (0.5% default, gap-buffered at 1.25x on price downside).
2. Aggregate portfolio buffered risk limit (2.0% default).
3. Maximum allocation per ticker (25.0% default, including existing lots).
4. Maximum invested capital (80.0% default).
5. Minimum cash reserve (20.0% default).
6. Maximum simultaneous open positions (4 unique tickers default).
7. Pure Decimal precision with iterative binary-search position sizing.
"""

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Dict, List, Optional, Set, Any, Tuple
import os
import sys

# Ensure accounting imports work regardless of execution context
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from accounting import (
    AccountingLedger,
    AccountingError,
    InvalidFinancialValue,
    to_decimal,
    quantize_cent,
    validate_positive_shares,
    validate_non_negative_amount,
)


CENT = Decimal("0.01")


class RiskError(AccountingError):
    """Base exception for risk management violations."""
    pass


class ConfigurationError(RiskError):
    """Raised when risk or fee configuration parameters are invalid."""
    pass


@dataclass
class FeeSchedule:
    """Configurable execution fee schedule for one order side."""
    per_share: Decimal = Decimal("0.00")
    basis_points: Decimal = Decimal("0.00")
    minimum: Decimal = Decimal("0.00")
    fixed: Decimal = Decimal("0.00")

    def __post_init__(self):
        self.per_share = to_decimal(self.per_share)
        self.basis_points = to_decimal(self.basis_points)
        self.minimum = to_decimal(self.minimum)
        self.fixed = to_decimal(self.fixed)
        if self.per_share < Decimal("0.00") or self.basis_points < Decimal("0.00"):
            raise ConfigurationError("Fee schedule rates cannot be negative.")
        if self.minimum < Decimal("0.00") or self.fixed < Decimal("0.00"):
            raise ConfigurationError("Fee minimum and fixed charges cannot be negative.")

    def calculate_fee(self, shares: int, price: Decimal) -> Decimal:
        """Calculate exact transaction fee with ROUND_HALF_UP cent quantization."""
        if shares <= 0:
            return Decimal("0.00")
        px = to_decimal(price)
        principal = Decimal(shares) * px
        var_shares = Decimal(shares) * self.per_share
        var_bps = principal * (self.basis_points / Decimal("10000"))
        variable_fee = var_shares + var_bps
        applied_min = max(self.minimum, variable_fee)
        total = applied_min + self.fixed
        return quantize_cent(total)


@dataclass
class FeeConfig:
    """Separate fee schedules for entry (buy) and exit (sell) orders."""
    entry: FeeSchedule = field(default_factory=FeeSchedule)
    exit: FeeSchedule = field(default_factory=FeeSchedule)


@dataclass
class RiskConfig:
    """Validated portfolio-level risk limits and multipliers."""
    starting_virtual_capital_usd: Decimal = Decimal("5000.00")
    risk_per_trade_pct: Decimal = Decimal("0.005")  # 0.5%
    max_aggregate_buffered_risk_pct: Decimal = Decimal("0.02")  # 2.0%
    max_allocation_per_ticker_pct: Decimal = Decimal("0.25")  # 25.0%
    max_invested_capital_pct: Decimal = Decimal("0.80")  # 80.0%
    min_cash_reserve_pct: Decimal = Decimal("0.20")  # 20.0%
    max_simultaneous_positions: int = 4
    gap_risk_multiplier: Decimal = Decimal("1.25")

    def __post_init__(self):
        self.starting_virtual_capital_usd = to_decimal(self.starting_virtual_capital_usd)
        self.risk_per_trade_pct = to_decimal(self.risk_per_trade_pct)
        self.max_aggregate_buffered_risk_pct = to_decimal(self.max_aggregate_buffered_risk_pct)
        self.max_allocation_per_ticker_pct = to_decimal(self.max_allocation_per_ticker_pct)
        self.max_invested_capital_pct = to_decimal(self.max_invested_capital_pct)
        self.min_cash_reserve_pct = to_decimal(self.min_cash_reserve_pct)
        self.gap_risk_multiplier = to_decimal(self.gap_risk_multiplier)

        if self.starting_virtual_capital_usd <= Decimal("0.00"):
            raise ConfigurationError("Starting virtual capital must be positive.")
        if not (Decimal("0.00") < self.risk_per_trade_pct < Decimal("1.00")):
            raise ConfigurationError("Risk per trade percentage must be between 0 and 1.")
        if not (Decimal("0.00") < self.max_aggregate_buffered_risk_pct < Decimal("1.00")):
            raise ConfigurationError("Max aggregate buffered risk percentage must be between 0 and 1.")
        if not (Decimal("0.00") < self.max_allocation_per_ticker_pct <= Decimal("1.00")):
            raise ConfigurationError("Max allocation per ticker percentage must be between 0 and 1.")
        if not (Decimal("0.00") < self.max_invested_capital_pct <= Decimal("1.00")):
            raise ConfigurationError("Max invested capital percentage must be between 0 and 1.")
        if not (Decimal("0.00") < self.min_cash_reserve_pct < Decimal("1.00")):
            raise ConfigurationError("Min cash reserve percentage must be between 0 and 1.")
        if self.max_invested_capital_pct + self.min_cash_reserve_pct > Decimal("1.00"):
            raise ConfigurationError("Sum of max invested capital and min cash reserve cannot exceed 100%.")
        if not isinstance(self.max_simultaneous_positions, int) or self.max_simultaneous_positions <= 0:
            raise ConfigurationError("Max simultaneous positions must be a positive integer.")
        if self.gap_risk_multiplier < Decimal("1.00"):
            raise ConfigurationError("Gap risk multiplier must be at least 1.00x.")


@dataclass
class SingleTradeRisk:
    """Breakdown of calculated risk for an individual trade."""
    shares: int
    entry_price: Decimal
    stop_loss_price: Decimal
    unbuffered_price_risk_usd: Decimal
    buffered_price_risk_usd: Decimal
    estimated_entry_fee_usd: Decimal
    estimated_exit_fee_usd: Decimal
    total_buffered_trade_risk_usd: Decimal


@dataclass
class PortfolioRiskMetrics:
    """Current risk and capital deployment metrics across the entire portfolio."""
    current_equity_usd: Decimal
    current_cash_usd: Decimal
    gross_market_value_usd: Decimal
    invested_capital_pct: Decimal
    open_positions_count: int
    total_open_lots_count: int
    total_remaining_entry_fees_usd: Decimal
    total_estimated_exit_fees_usd: Decimal
    total_unbuffered_open_risk_usd: Decimal
    total_buffered_open_risk_usd: Decimal
    aggregate_risk_pct: Decimal
    available_risk_capacity_usd: Decimal


@dataclass
class RiskVerdict:
    """Approval status and detailed diagnostic for a proposed trade."""
    approved: bool
    reason: str
    binding_constraint: Optional[str]
    details: Dict[str, Any]


@dataclass
class SizingResult:
    """Outcome of deterministic iterative position sizing."""
    ticker: str
    shares: int
    approved: bool
    entry_price: Decimal
    stop_loss_price: Decimal
    estimated_entry_fee: Decimal
    estimated_exit_fee: Decimal
    total_capital_required: Decimal
    total_buffered_risk: Decimal
    rejection_reasons: List[str]
    binding_constraint: Optional[str]


class RiskEngine:
    """Deterministic risk management and position sizing engine."""

    def __init__(self, risk_config: Optional[RiskConfig] = None, fee_config: Optional[FeeConfig] = None):
        self.risk_config = risk_config or RiskConfig()
        self.fee_config = fee_config or FeeConfig()

    def calculate_single_trade_risk(
        self,
        entry_price: Any,
        stop_loss_price: Any,
        shares: int,
        estimated_entry_fee: Any = Decimal("0.00"),
        estimated_exit_fee: Any = Decimal("0.00"),
        gap_risk_multiplier: Optional[Any] = None,
    ) -> SingleTradeRisk:
        """Calculate trade risk with 1.25x gap-risk multiplier applied ONLY to price downside.

        Formula:
          unbuffered_price_risk = max(0, entry_price - stop_loss_price) * shares
          buffered_price_risk = unbuffered_price_risk * 1.25
          total_buffered_trade_risk = buffered_price_risk + entry_fee + exit_fee
        """
        px_entry = to_decimal(entry_price)
        px_stop = to_decimal(stop_loss_price)
        shs = validate_positive_shares(shares)
        fee_in = validate_non_negative_amount(estimated_entry_fee, "Estimated entry fee")
        fee_out = validate_non_negative_amount(estimated_exit_fee, "Estimated exit fee")
        multiplier = to_decimal(gap_risk_multiplier) if gap_risk_multiplier is not None else self.risk_config.gap_risk_multiplier

        if px_entry <= Decimal("0.00"):
            raise InvalidFinancialValue(f"Entry price must be positive, got: {px_entry}")
        if px_stop <= Decimal("0.00"):
            raise InvalidFinancialValue(f"Stop loss price must be positive, got: {px_stop}")

        # Downside price risk per share: max(0, entry - stop)
        # When trailing stop is above entry (stop >= entry), price risk is strictly 0.00
        unbuffered_per_share = max(Decimal("0.00"), px_entry - px_stop)
        unbuffered_price_risk = quantize_cent(Decimal(shs) * unbuffered_per_share)
        buffered_price_risk = quantize_cent(unbuffered_price_risk * multiplier)

        # Fees are strictly additive and NEVER multiplied by the gap multiplier
        total_buffered_risk = quantize_cent(buffered_price_risk + fee_in + fee_out)

        return SingleTradeRisk(
            shares=shs,
            entry_price=px_entry,
            stop_loss_price=px_stop,
            unbuffered_price_risk_usd=unbuffered_price_risk,
            buffered_price_risk_usd=buffered_price_risk,
            estimated_entry_fee_usd=fee_in,
            estimated_exit_fee_usd=fee_out,
            total_buffered_trade_risk_usd=total_buffered_risk,
        )

    def calculate_portfolio_risk(
        self,
        ledger: AccountingLedger,
        current_stops: Optional[Dict[str, Decimal]] = None,
    ) -> PortfolioRiskMetrics:
        """Calculate aggregate portfolio risk metrics based on open lots and protective stops."""
        ledger.validate_equity_invariant()
        equity = ledger.total_equity_usd
        cash = ledger.cash_usd
        gmv = ledger.gross_market_value_usd
        stops = current_stops or {}

        invested_pct = (gmv / equity).quantize(Decimal("0.0001"), ROUND_HALF_UP) if equity > Decimal("0.00") else Decimal("0.00")
        unique_open_tickers = set(t for t, p in ledger.positions.items() if p.total_shares > 0)
        open_positions_count = len(unique_open_tickers)

        total_open_lots = 0
        total_remaining_entry_fees = Decimal("0.00")
        total_estimated_exit_fees = Decimal("0.00")
        total_unbuffered_open_risk = Decimal("0.00")
        total_buffered_open_risk = Decimal("0.00")

        for ticker, pos in ledger.positions.items():
            for lot in pos.lots:
                if lot.status != "OPEN":
                    continue

                total_open_lots += 1
                total_remaining_entry_fees += lot.entry_fee_remaining

                # Resolve protective stop for this lot
                stop_px = stops.get(lot.lot_id) or stops.get(ticker) or lot.entry_price
                stop_px = to_decimal(stop_px)

                # Exit fee estimated at the stop price
                exit_fee = self.fee_config.exit.calculate_fee(lot.shares_remaining, stop_px)
                total_estimated_exit_fees += exit_fee

                # Downside risk
                unbuf_per_sh = max(Decimal("0.00"), lot.entry_price - stop_px)
                lot_unbuf_risk = quantize_cent(Decimal(lot.shares_remaining) * unbuf_per_sh)
                lot_buf_risk = quantize_cent(lot_unbuf_risk * self.risk_config.gap_risk_multiplier)

                # Total risk for this open lot includes unamortized entry fees + exit fee at stop
                total_unbuffered_open_risk += quantize_cent(lot_unbuf_risk + lot.entry_fee_remaining + exit_fee)
                total_buffered_open_risk += quantize_cent(lot_buf_risk + lot.entry_fee_remaining + exit_fee)

        agg_risk_pct = (total_buffered_open_risk / equity).quantize(Decimal("0.0001"), ROUND_HALF_UP) if equity > Decimal("0.00") else Decimal("0.00")
        max_buffered_risk_allowed = quantize_cent(equity * self.risk_config.max_aggregate_buffered_risk_pct)
        available_risk_capacity = max(Decimal("0.00"), max_buffered_risk_allowed - total_buffered_open_risk)

        return PortfolioRiskMetrics(
            current_equity_usd=equity,
            current_cash_usd=cash,
            gross_market_value_usd=gmv,
            invested_capital_pct=invested_pct,
            open_positions_count=open_positions_count,
            total_open_lots_count=total_open_lots,
            total_remaining_entry_fees_usd=total_remaining_entry_fees,
            total_estimated_exit_fees_usd=total_estimated_exit_fees,
            total_unbuffered_open_risk_usd=total_unbuffered_open_risk,
            total_buffered_open_risk_usd=total_buffered_open_risk,
            aggregate_risk_pct=agg_risk_pct,
            available_risk_capacity_usd=available_risk_capacity,
        )

    def evaluate_proposed_trade(
        self,
        ledger: AccountingLedger,
        ticker: str,
        shares: int,
        entry_price: Any,
        stop_loss_price: Any,
        current_stops: Optional[Dict[str, Decimal]] = None,
    ) -> RiskVerdict:
        """Evaluate a proposed trade against all 5 hard portfolio risk and capital rules."""
        sym = ticker.upper()
        shs = validate_positive_shares(shares)
        px_entry = to_decimal(entry_price)
        px_stop = to_decimal(stop_loss_price)

        if px_entry <= Decimal("0.00"):
            return RiskVerdict(False, f"Entry price must be positive, got {px_entry}", "INVALID_PRICE", {})
        if px_stop <= Decimal("0.00"):
            return RiskVerdict(False, f"Stop loss price must be positive, got {px_stop}", "INVALID_STOP", {})
        if px_stop >= px_entry:
            return RiskVerdict(
                False,
                f"Stop loss price ({px_stop}) must be strictly below entry price ({px_entry}) for a new long trade",
                "INVALID_STOP_DISTANCE",
                {},
            )

        port_metrics = self.calculate_portfolio_risk(ledger, current_stops=current_stops)
        equity = port_metrics.current_equity_usd

        # 1. Unique position count constraint (max 4 unique tickers)
        current_open_tickers = set(t for t, p in ledger.positions.items() if p.total_shares > 0)
        is_scale_in = (sym in current_open_tickers)
        if not is_scale_in and len(current_open_tickers) >= self.risk_config.max_simultaneous_positions:
            return RiskVerdict(
                approved=False,
                reason=f"Position count limit reached: currently holding {len(current_open_tickers)} unique tickers (max: {self.risk_config.max_simultaneous_positions})",
                binding_constraint="MAX_POSITIONS_EXCEEDED",
                details={"open_tickers": list(current_open_tickers), "max_allowed": self.risk_config.max_simultaneous_positions},
            )

        # Calculate exact fees for the proposed trade
        entry_fee = self.fee_config.entry.calculate_fee(shs, px_entry)
        exit_fee = self.fee_config.exit.calculate_fee(shs, px_stop)
        principal = quantize_cent(Decimal(shs) * px_entry)
        total_capital_required = principal + entry_fee

        # 2. Cash availability constraint
        if total_capital_required > ledger.cash_usd:
            return RiskVerdict(
                approved=False,
                reason=f"Insufficient cash: trade requires {total_capital_required} (principal {principal} + fee {entry_fee}), but cash balance is {ledger.cash_usd}",
                binding_constraint="INSUFFICIENT_CASH",
                details={"required_cash": str(total_capital_required), "available_cash": str(ledger.cash_usd)},
            )

        # 3. Minimum cash reserve constraint (20% of equity)
        min_cash_required = quantize_cent(equity * self.risk_config.min_cash_reserve_pct)
        post_trade_cash = ledger.cash_usd - total_capital_required
        if post_trade_cash < min_cash_required:
            return RiskVerdict(
                approved=False,
                reason=f"Minimum cash reserve violated: post-trade cash would be {post_trade_cash}, below required reserve of {min_cash_required} (20% of equity {equity})",
                binding_constraint="CASH_RESERVE_VIOLATED",
                details={"post_trade_cash": str(post_trade_cash), "min_cash_reserve": str(min_cash_required)},
            )

        # 4. Maximum invested capital constraint (80% of equity)
        max_invested_allowed = quantize_cent(equity * self.risk_config.max_invested_capital_pct)
        post_trade_gmv = port_metrics.gross_market_value_usd + principal
        if post_trade_gmv > max_invested_allowed:
            return RiskVerdict(
                approved=False,
                reason=f"Maximum invested capital exceeded: post-trade invested capital would be {post_trade_gmv}, exceeding limit of {max_invested_allowed} (80% of equity {equity})",
                binding_constraint="MAX_INVESTED_CAPITAL_EXCEEDED",
                details={"post_trade_gmv": str(post_trade_gmv), "max_invested_allowed": str(max_invested_allowed)},
            )

        # 5. Maximum allocation per ticker constraint (25% of equity, includes all existing lots)
        max_ticker_allocation = quantize_cent(equity * self.risk_config.max_allocation_per_ticker_pct)
        existing_ticker_mv = ledger.positions[sym].gross_market_value_usd if sym in ledger.positions else Decimal("0.00")
        post_trade_ticker_exposure = existing_ticker_mv + principal
        if post_trade_ticker_exposure > max_ticker_allocation:
            return RiskVerdict(
                approved=False,
                reason=f"Ticker allocation limit exceeded: post-trade allocation for {sym} would be {post_trade_ticker_exposure} (existing {existing_ticker_mv} + proposed {principal}), exceeding limit of {max_ticker_allocation} (25% of equity {equity})",
                binding_constraint="TICKER_ALLOCATION_LIMIT_EXCEEDED",
                details={"post_trade_exposure": str(post_trade_ticker_exposure), "max_allowed": str(max_ticker_allocation)},
            )

        # 6. Single-trade risk limit constraint (0.5% of equity)
        trade_risk = self.calculate_single_trade_risk(
            entry_price=px_entry,
            stop_loss_price=px_stop,
            shares=shs,
            estimated_entry_fee=entry_fee,
            estimated_exit_fee=exit_fee,
        )
        max_trade_risk_allowed = quantize_cent(equity * self.risk_config.risk_per_trade_pct)
        if trade_risk.total_buffered_trade_risk_usd > max_trade_risk_allowed:
            return RiskVerdict(
                approved=False,
                reason=f"Per-trade risk limit exceeded: buffered trade risk of {trade_risk.total_buffered_trade_risk_usd} exceeds limit of {max_trade_risk_allowed} (0.5% of equity {equity})",
                binding_constraint="TRADE_RISK_LIMIT_EXCEEDED",
                details={"trade_risk": str(trade_risk.total_buffered_trade_risk_usd), "max_trade_risk": str(max_trade_risk_allowed)},
            )

        # 7. Aggregate portfolio buffered risk limit constraint (2.0% of equity)
        max_aggregate_risk_allowed = quantize_cent(equity * self.risk_config.max_aggregate_buffered_risk_pct)
        post_trade_aggregate_risk = port_metrics.total_buffered_open_risk_usd + trade_risk.total_buffered_trade_risk_usd
        if post_trade_aggregate_risk > max_aggregate_risk_allowed:
            return RiskVerdict(
                approved=False,
                reason=f"Aggregate portfolio risk limit exceeded: post-trade buffered risk would be {post_trade_aggregate_risk} (existing {port_metrics.total_buffered_open_risk_usd} + trade {trade_risk.total_buffered_trade_risk_usd}), exceeding limit of {max_aggregate_risk_allowed} (2.0% of equity {equity})",
                binding_constraint="AGGREGATE_RISK_LIMIT_EXCEEDED",
                details={"post_trade_risk": str(post_trade_aggregate_risk), "max_aggregate_risk": str(max_aggregate_risk_allowed)},
            )

        return RiskVerdict(
            approved=True,
            reason="All risk, allocation, cash reserve, and portfolio limits satisfied",
            binding_constraint=None,
            details={
                "shares": shs,
                "entry_price": str(px_entry),
                "stop_price": str(px_stop),
                "principal": str(principal),
                "entry_fee": str(entry_fee),
                "exit_fee": str(exit_fee),
                "buffered_trade_risk": str(trade_risk.total_buffered_trade_risk_usd),
                "post_trade_ticker_exposure": str(post_trade_ticker_exposure),
                "post_trade_cash": str(post_trade_cash),
                "post_trade_aggregate_risk": str(post_trade_aggregate_risk),
            },
        )

    def size_position(
        self,
        ledger: AccountingLedger,
        ticker: str,
        entry_price: Any,
        stop_loss_price: Any,
        current_stops: Optional[Dict[str, Decimal]] = None,
    ) -> SizingResult:
        """Calculate the largest whole-share integer quantity satisfying every portfolio and risk constraint.

        Uses deterministic binary search. Accounts for non-linear fee structures (minimums and fixed tickets).
        Verifies that Q + 1 violates at least one constraint.
        """
        sym = ticker.upper()
        px_entry = to_decimal(entry_price)
        px_stop = to_decimal(stop_loss_price)

        if px_entry <= Decimal("0.00") or px_stop <= Decimal("0.00") or px_stop >= px_entry:
            verdict = self.evaluate_proposed_trade(ledger, sym, 1, px_entry, px_stop, current_stops)
            return SizingResult(
                ticker=sym,
                shares=0,
                approved=False,
                entry_price=px_entry,
                stop_loss_price=px_stop,
                estimated_entry_fee=Decimal("0.00"),
                estimated_exit_fee=Decimal("0.00"),
                total_capital_required=Decimal("0.00"),
                total_buffered_risk=Decimal("0.00"),
                rejection_reasons=[verdict.reason],
                binding_constraint=verdict.binding_constraint,
            )

        equity = ledger.total_equity_usd
        port_metrics = self.calculate_portfolio_risk(ledger, current_stops=current_stops)

        # Check unique ticker limit upfront
        current_open_tickers = set(t for t, p in ledger.positions.items() if p.total_shares > 0)
        if sym not in current_open_tickers and len(current_open_tickers) >= self.risk_config.max_simultaneous_positions:
            verdict = self.evaluate_proposed_trade(ledger, sym, 1, px_entry, px_stop, current_stops)
            return SizingResult(
                ticker=sym,
                shares=0,
                approved=False,
                entry_price=px_entry,
                stop_loss_price=px_stop,
                estimated_entry_fee=Decimal("0.00"),
                estimated_exit_fee=Decimal("0.00"),
                total_capital_required=Decimal("0.00"),
                total_buffered_risk=Decimal("0.00"),
                rejection_reasons=[verdict.reason],
                binding_constraint=verdict.binding_constraint,
            )

        # Determine theoretical upper bound for binary search
        price_risk_per_sh = (px_entry - px_stop) * self.risk_config.gap_risk_multiplier
        trade_risk_budget = equity * self.risk_config.risk_per_trade_pct
        port_risk_budget = port_metrics.available_risk_capacity_usd
        effective_risk_budget = min(trade_risk_budget, port_risk_budget)
        max_by_risk = int(effective_risk_budget / price_risk_per_sh) if price_risk_per_sh > Decimal("0.00") else 0

        existing_ticker_mv = ledger.positions[sym].gross_market_value_usd if sym in ledger.positions else Decimal("0.00")
        max_ticker_cap = equity * self.risk_config.max_allocation_per_ticker_pct
        avail_ticker_cap = max(Decimal("0.00"), max_ticker_cap - existing_ticker_mv)
        max_by_alloc = int(avail_ticker_cap / px_entry) if px_entry > Decimal("0.00") else 0

        max_invested_cap = equity * self.risk_config.max_invested_capital_pct
        avail_invested_cap = max(Decimal("0.00"), max_invested_cap - port_metrics.gross_market_value_usd)
        max_by_invested = int(avail_invested_cap / px_entry) if px_entry > Decimal("0.00") else 0

        min_cash_reserve = equity * self.risk_config.min_cash_reserve_pct
        avail_cash = max(Decimal("0.00"), ledger.cash_usd - min_cash_reserve)
        max_by_cash = int(avail_cash / px_entry) if px_entry > Decimal("0.00") else 0

        upper_bound = max(0, min(max_by_risk, max_by_alloc, max_by_invested, max_by_cash))

        # Binary search for the largest valid integer share quantity
        low = 1
        high = upper_bound
        best_q = 0

        while low <= high:
            mid = (low + high) // 2
            v = self.evaluate_proposed_trade(ledger, sym, mid, px_entry, px_stop, current_stops)
            if v.approved:
                best_q = mid
                low = mid + 1  # Try higher
            else:
                high = mid - 1  # Try lower

        if best_q > 0:
            # Sizing succeeded with best_q shares
            entry_fee = self.fee_config.entry.calculate_fee(best_q, px_entry)
            exit_fee = self.fee_config.exit.calculate_fee(best_q, px_stop)
            capital_req = quantize_cent(Decimal(best_q) * px_entry + entry_fee)
            trade_risk = self.calculate_single_trade_risk(px_entry, px_stop, best_q, entry_fee, exit_fee)

            # Determine which constraint binds at Q + 1
            next_v = self.evaluate_proposed_trade(ledger, sym, best_q + 1, px_entry, px_stop, current_stops)
            binding = next_v.binding_constraint

            return SizingResult(
                ticker=sym,
                shares=best_q,
                approved=True,
                entry_price=px_entry,
                stop_loss_price=px_stop,
                estimated_entry_fee=entry_fee,
                estimated_exit_fee=exit_fee,
                total_capital_required=capital_req,
                total_buffered_risk=trade_risk.total_buffered_trade_risk_usd,
                rejection_reasons=[],
                binding_constraint=binding,
            )
        else:
            # best_q == 0: evaluate Q = 1 to diagnose the exact binding constraint
            v1 = self.evaluate_proposed_trade(ledger, sym, 1, px_entry, px_stop, current_stops)
            return SizingResult(
                ticker=sym,
                shares=0,
                approved=False,
                entry_price=px_entry,
                stop_loss_price=px_stop,
                estimated_entry_fee=Decimal("0.00"),
                estimated_exit_fee=Decimal("0.00"),
                total_capital_required=Decimal("0.00"),
                total_buffered_risk=Decimal("0.00"),
                rejection_reasons=[v1.reason],
                binding_constraint=v1.binding_constraint,
            )
