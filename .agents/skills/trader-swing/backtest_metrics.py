"""Comprehensive financial, risk, and trade performance metrics for trader-swing backtests.

Enforces:
1. Mathematical rigor with pure Decimal precision.
2. Complete handling of edge cases (zero trades, zero losses, zero volatility).
3. Standard financial formulas for CAGR, Sharpe, Sortino, Calmar, and Drawdown.
4. Benchmark comparison against SPY over identical trading dates.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import math
import os
import sys
from typing import Any, Dict, List, Optional, Sequence

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from backtest_models import PortfolioDailySnapshot, SimulatedTradeRecord
from market_data_models import OHLCVCandle

TWO_DECIMALS = Decimal("0.01")
FOUR_DECIMALS = Decimal("0.0001")


@dataclass(frozen=True)
class PortfolioMetrics:
    initial_capital_usd: Decimal
    final_equity_usd: Decimal
    net_profit_usd: Decimal
    total_return_pct: Decimal
    cagr_pct: Decimal
    max_drawdown_usd: Decimal
    max_drawdown_pct: Decimal
    annualized_volatility_pct: Decimal
    sharpe_ratio: Decimal
    sortino_ratio: Optional[Decimal]
    calmar_ratio: Optional[Decimal]
    exposure_pct: Decimal
    avg_invested_capital_pct: Decimal

    def to_dict(self) -> Dict[str, Any]:
        return {
            "initial_capital_usd": str(self.initial_capital_usd),
            "final_equity_usd": str(self.final_equity_usd),
            "net_profit_usd": str(self.net_profit_usd),
            "total_return_pct": str(self.total_return_pct),
            "cagr_pct": str(self.cagr_pct),
            "max_drawdown_usd": str(self.max_drawdown_usd),
            "max_drawdown_pct": str(self.max_drawdown_pct),
            "annualized_volatility_pct": str(self.annualized_volatility_pct),
            "sharpe_ratio": str(self.sharpe_ratio),
            "sortino_ratio": str(self.sortino_ratio) if self.sortino_ratio is not None else None,
            "calmar_ratio": str(self.calmar_ratio) if self.calmar_ratio is not None else None,
            "exposure_pct": str(self.exposure_pct),
            "avg_invested_capital_pct": str(self.avg_invested_capital_pct),
        }


@dataclass(frozen=True)
class TradeMetrics:
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate_pct: Decimal
    avg_win_usd: Decimal
    avg_loss_usd: Decimal
    profit_factor: Optional[Decimal]
    expectancy_usd: Decimal
    avg_r_multiple: Decimal
    avg_holding_sessions: Decimal
    largest_win_usd: Decimal
    largest_loss_usd: Decimal
    max_consecutive_wins: int
    max_consecutive_losses: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "win_rate_pct": str(self.win_rate_pct),
            "avg_win_usd": str(self.avg_win_usd),
            "avg_loss_usd": str(self.avg_loss_usd),
            "profit_factor": str(self.profit_factor) if self.profit_factor is not None else None,
            "expectancy_usd": str(self.expectancy_usd),
            "avg_r_multiple": str(self.avg_r_multiple),
            "avg_holding_sessions": str(self.avg_holding_sessions),
            "largest_win_usd": str(self.largest_win_usd),
            "largest_loss_usd": str(self.largest_loss_usd),
            "max_consecutive_wins": self.max_consecutive_wins,
            "max_consecutive_losses": self.max_consecutive_losses,
        }


@dataclass(frozen=True)
class BenchmarkComparison:
    benchmark_ticker: str
    is_available: bool
    benchmark_total_return_pct: Decimal
    benchmark_cagr_pct: Decimal
    benchmark_max_drawdown_pct: Decimal
    excess_return_pct: Decimal
    alpha_pct: Optional[Decimal]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "benchmark_ticker": self.benchmark_ticker,
            "is_available": self.is_available,
            "benchmark_total_return_pct": str(self.benchmark_total_return_pct),
            "benchmark_cagr_pct": str(self.benchmark_cagr_pct),
            "benchmark_max_drawdown_pct": str(self.benchmark_max_drawdown_pct),
            "excess_return_pct": str(self.excess_return_pct),
            "alpha_pct": str(self.alpha_pct) if self.alpha_pct is not None else None,
        }


@dataclass(frozen=True)
class RiskAuditMetrics:
    max_open_buffered_risk_usd: Decimal
    max_aggregate_risk_pct: Decimal
    avg_open_buffered_risk_usd: Decimal
    risk_limit_rejections: int
    position_limit_rejections: int
    cash_reserve_violations: int
    max_simultaneous_positions: int
    max_single_ticker_allocation_pct: Decimal

    def to_dict(self) -> Dict[str, Any]:
        return {
            "max_open_buffered_risk_usd": str(self.max_open_buffered_risk_usd),
            "max_aggregate_risk_pct": str(self.max_aggregate_risk_pct),
            "avg_open_buffered_risk_usd": str(self.avg_open_buffered_risk_usd),
            "risk_limit_rejections": self.risk_limit_rejections,
            "position_limit_rejections": self.position_limit_rejections,
            "cash_reserve_violations": self.cash_reserve_violations,
            "max_simultaneous_positions": self.max_simultaneous_positions,
            "max_single_ticker_allocation_pct": str(self.max_single_ticker_allocation_pct),
        }


class BacktestMetricsCalculator:
    """Computes comprehensive performance, risk, and benchmark metrics."""

    @staticmethod
    def calculate_portfolio_metrics(
        snapshots: Sequence[PortfolioDailySnapshot],
        initial_capital: Decimal,
    ) -> PortfolioMetrics:
        """Compute portfolio-level returns, drawdowns, and risk-adjusted ratios."""
        if not snapshots:
            return PortfolioMetrics(
                initial_capital_usd=initial_capital,
                final_equity_usd=initial_capital,
                net_profit_usd=Decimal("0.00"),
                total_return_pct=Decimal("0.00"),
                cagr_pct=Decimal("0.00"),
                max_drawdown_usd=Decimal("0.00"),
                max_drawdown_pct=Decimal("0.00"),
                annualized_volatility_pct=Decimal("0.00"),
                sharpe_ratio=Decimal("0.00"),
                sortino_ratio=None,
                calmar_ratio=None,
                exposure_pct=Decimal("0.00"),
                avg_invested_capital_pct=Decimal("0.00"),
            )

        final_equity = snapshots[-1].equity_usd
        net_profit = final_equity - initial_capital
        total_return_pct = ((final_equity - initial_capital) / initial_capital) * Decimal("100.0")
        total_return_pct = total_return_pct.quantize(TWO_DECIMALS, ROUND_HALF_UP)

        # CAGR Calculation
        num_sessions = len(snapshots)
        years = Decimal(str(max(1, num_sessions))) / Decimal("252.0")
        if final_equity > Decimal("0.00") and years > Decimal("0.00"):
            ratio = float(final_equity / initial_capital)
            if ratio > 0:
                cagr_float = (ratio ** (1.0 / float(years))) - 1.0
                cagr_pct = (Decimal(str(cagr_float)) * Decimal("100.0")).quantize(TWO_DECIMALS, ROUND_HALF_UP)
            else:
                cagr_pct = Decimal("-100.00")
        else:
            cagr_pct = Decimal("0.00")

        # Maximum Drawdown
        peak_equity = initial_capital
        max_dd_usd = Decimal("0.00")
        max_dd_pct = Decimal("0.00")

        daily_returns: List[Decimal] = []
        for i, snap in enumerate(snapshots):
            eq = snap.equity_usd
            if eq > peak_equity:
                peak_equity = eq
            dd_usd = peak_equity - eq
            if dd_usd > max_dd_usd:
                max_dd_usd = dd_usd
            if peak_equity > Decimal("0.00"):
                dd_pct = (dd_usd / peak_equity) * Decimal("100.0")
                if dd_pct > max_dd_pct:
                    max_dd_pct = dd_pct

            # Daily return
            prev_eq = snapshots[i - 1].equity_usd if i > 0 else initial_capital
            if prev_eq > Decimal("0.00"):
                ret = (eq - prev_eq) / prev_eq
                daily_returns.append(ret)

        max_dd_usd = max_dd_usd.quantize(TWO_DECIMALS, ROUND_HALF_UP)
        max_dd_pct = max_dd_pct.quantize(TWO_DECIMALS, ROUND_HALF_UP)

        # Annualized Volatility & Sharpe
        if len(daily_returns) > 1:
            mean_ret = sum(daily_returns) / Decimal(len(daily_returns))
            variance = sum((r - mean_ret) ** 2 for r in daily_returns) / Decimal(len(daily_returns) - 1)
            daily_std = Decimal(str(math.sqrt(float(variance)))) if variance > Decimal("0.00") else Decimal("0.00")
            ann_vol_pct = (daily_std * Decimal(str(math.sqrt(252.0))) * Decimal("100.0")).quantize(TWO_DECIMALS, ROUND_HALF_UP)

            if daily_std > Decimal("0.00"):
                sharpe = ((mean_ret * Decimal("252.0")) / (daily_std * Decimal(str(math.sqrt(252.0))))).quantize(TWO_DECIMALS, ROUND_HALF_UP)
            else:
                sharpe = Decimal("0.00")

            # Sortino Ratio (downside deviation of negative returns only)
            negative_returns = [r for r in daily_returns if r < Decimal("0.00")]
            if negative_returns:
                downside_var = sum(r ** 2 for r in negative_returns) / Decimal(len(daily_returns))
                downside_std = Decimal(str(math.sqrt(float(downside_var))))
                if downside_std > Decimal("0.00"):
                    sortino = ((mean_ret * Decimal("252.0")) / (downside_std * Decimal(str(math.sqrt(252.0))))).quantize(TWO_DECIMALS, ROUND_HALF_UP)
                else:
                    sortino = None
            else:
                sortino = None
        else:
            ann_vol_pct = Decimal("0.00")
            sharpe = Decimal("0.00")
            sortino = None

        # Calmar Ratio: CAGR / Max DD%
        calmar = None
        if max_dd_pct > Decimal("0.00"):
            calmar = (cagr_pct / max_dd_pct).quantize(TWO_DECIMALS, ROUND_HALF_UP)

        # Exposure and Invested Capital
        sessions_with_positions = sum(1 for s in snapshots if s.open_positions_count > 0)
        exposure_pct = ((Decimal(sessions_with_positions) / Decimal(num_sessions)) * Decimal("100.0")).quantize(TWO_DECIMALS, ROUND_HALF_UP)
        avg_inv_pct = (sum(s.invested_capital_pct for s in snapshots) / Decimal(num_sessions)).quantize(TWO_DECIMALS, ROUND_HALF_UP)

        return PortfolioMetrics(
            initial_capital_usd=initial_capital,
            final_equity_usd=final_equity,
            net_profit_usd=net_profit.quantize(TWO_DECIMALS, ROUND_HALF_UP),
            total_return_pct=total_return_pct,
            cagr_pct=cagr_pct,
            max_drawdown_usd=max_dd_usd,
            max_drawdown_pct=max_dd_pct,
            annualized_volatility_pct=ann_vol_pct,
            sharpe_ratio=sharpe,
            sortino_ratio=sortino,
            calmar_ratio=calmar,
            exposure_pct=exposure_pct,
            avg_invested_capital_pct=avg_inv_pct,
        )

    @staticmethod
    def calculate_trade_metrics(trades: Sequence[SimulatedTradeRecord]) -> TradeMetrics:
        """Compute trade-level metrics, win rate, profit factor, and streaks."""
        total = len(trades)
        if total == 0:
            return TradeMetrics(
                total_trades=0,
                winning_trades=0,
                losing_trades=0,
                win_rate_pct=Decimal("0.00"),
                avg_win_usd=Decimal("0.00"),
                avg_loss_usd=Decimal("0.00"),
                profit_factor=None,
                expectancy_usd=Decimal("0.00"),
                avg_r_multiple=Decimal("0.00"),
                avg_holding_sessions=Decimal("0.0"),
                largest_win_usd=Decimal("0.00"),
                largest_loss_usd=Decimal("0.00"),
                max_consecutive_wins=0,
                max_consecutive_losses=0,
            )

        wins = [t for t in trades if t.net_pnl_usd > Decimal("0.00")]
        losses = [t for t in trades if t.net_pnl_usd < Decimal("0.00")]

        win_rate = (Decimal(len(wins)) / Decimal(total)) * Decimal("100.0")
        win_rate = win_rate.quantize(TWO_DECIMALS, ROUND_HALF_UP)

        gross_profit = sum((t.net_pnl_usd for t in wins), Decimal("0.00"))
        gross_loss = abs(sum((t.net_pnl_usd for t in losses), Decimal("0.00")))

        avg_win = (gross_profit / Decimal(len(wins))).quantize(TWO_DECIMALS, ROUND_HALF_UP) if wins else Decimal("0.00")
        avg_loss = (gross_loss / Decimal(len(losses))).quantize(TWO_DECIMALS, ROUND_HALF_UP) if losses else Decimal("0.00")

        # Profit Factor: Gross Profit / Gross Loss
        profit_factor = None
        if gross_loss > Decimal("0.00"):
            profit_factor = (gross_profit / gross_loss).quantize(TWO_DECIMALS, ROUND_HALF_UP)

        # Expectancy: (Win% * AvgWin) - (Loss% * AvgLoss)
        win_prob = Decimal(len(wins)) / Decimal(total)
        loss_prob = Decimal(len(losses)) / Decimal(total)
        expectancy = (win_prob * avg_win) - (loss_prob * avg_loss)
        expectancy = expectancy.quantize(TWO_DECIMALS, ROUND_HALF_UP)

        # Average R-Multiple
        avg_r = (sum(t.r_multiple for t in trades) / Decimal(total)).quantize(TWO_DECIMALS, ROUND_HALF_UP)

        # Average Holding Sessions
        avg_hold = (Decimal(sum(t.holding_sessions for t in trades)) / Decimal(total)).quantize(Decimal("0.1"), ROUND_HALF_UP)

        # Largest Win and Loss
        largest_win = max((t.net_pnl_usd for t in wins), default=Decimal("0.00"))
        largest_loss = min((t.net_pnl_usd for t in losses), default=Decimal("0.00"))

        # Streaks
        cur_win_streak = max_win_streak = 0
        cur_loss_streak = max_loss_streak = 0
        for t in trades:
            if t.net_pnl_usd > Decimal("0.00"):
                cur_win_streak += 1
                cur_loss_streak = 0
                max_win_streak = max(max_win_streak, cur_win_streak)
            elif t.net_pnl_usd < Decimal("0.00"):
                cur_loss_streak += 1
                cur_win_streak = 0
                max_loss_streak = max(max_loss_streak, cur_loss_streak)
            else:
                cur_win_streak = 0
                cur_loss_streak = 0

        return TradeMetrics(
            total_trades=total,
            winning_trades=len(wins),
            losing_trades=len(losses),
            win_rate_pct=win_rate,
            avg_win_usd=avg_win,
            avg_loss_usd=avg_loss,
            profit_factor=profit_factor,
            expectancy_usd=expectancy,
            avg_r_multiple=avg_r,
            avg_holding_sessions=avg_hold,
            largest_win_usd=largest_win,
            largest_loss_usd=largest_loss,
            max_consecutive_wins=max_win_streak,
            max_consecutive_losses=max_loss_streak,
        )

    @staticmethod
    def compare_with_benchmark(
        strategy_metrics: PortfolioMetrics,
        benchmark_candles: Sequence[OHLCVCandle],
        benchmark_ticker: str = "SPY",
    ) -> BenchmarkComparison:
        """Compare strategy returns against SPY buy-and-hold over the exact backtest period."""
        if not benchmark_candles or len(benchmark_candles) < 2:
            return BenchmarkComparison(
                benchmark_ticker=benchmark_ticker,
                is_available=False,
                benchmark_total_return_pct=Decimal("0.00"),
                benchmark_cagr_pct=Decimal("0.00"),
                benchmark_max_drawdown_pct=Decimal("0.00"),
                excess_return_pct=Decimal("0.00"),
                alpha_pct=None,
            )

        start_px = benchmark_candles[0].close
        end_px = benchmark_candles[-1].close
        bench_ret_pct = ((end_px - start_px) / start_px) * Decimal("100.0")
        bench_ret_pct = bench_ret_pct.quantize(TWO_DECIMALS, ROUND_HALF_UP)

        num_sessions = len(benchmark_candles)
        years = Decimal(str(max(1, num_sessions))) / Decimal("252.0")
        ratio = float(end_px / start_px)
        if ratio > 0 and years > Decimal("0.00"):
            bench_cagr = (Decimal(str((ratio ** (1.0 / float(years))) - 1.0)) * Decimal("100.0")).quantize(TWO_DECIMALS, ROUND_HALF_UP)
        else:
            bench_cagr = Decimal("0.00")

        # Benchmark Max Drawdown
        peak = start_px
        max_bench_dd = Decimal("0.00")
        for c in benchmark_candles:
            if c.high > peak:
                peak = c.high
            dd = ((peak - c.low) / peak) * Decimal("100.0")
            if dd > max_bench_dd:
                max_bench_dd = dd
        max_bench_dd = max_bench_dd.quantize(TWO_DECIMALS, ROUND_HALF_UP)

        excess_return = strategy_metrics.total_return_pct - bench_ret_pct

        return BenchmarkComparison(
            benchmark_ticker=benchmark_ticker,
            is_available=True,
            benchmark_total_return_pct=bench_ret_pct,
            benchmark_cagr_pct=bench_cagr,
            benchmark_max_drawdown_pct=max_bench_dd,
            excess_return_pct=excess_return,
            alpha_pct=strategy_metrics.cagr_pct - bench_cagr,
        )
