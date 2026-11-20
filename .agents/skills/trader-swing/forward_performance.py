"""Statistical Performance Evaluation Engine for Trader-Swing Phase 9.

Computes mathematically honest, auditable forward performance metrics from
recorded forward observations and closed hypothetical trades.

STATISTICAL HONESTY MANDATE:
- Distinguishes closed-trade statistics from unrealized open positions.
- Computes Wilson score 95% confidence intervals on win rate.
- Flags sample sizes under N < 30 as statistically insufficient.
- Zero backtest cherry-picking: relies exclusively on recorded forward observations.
- Benchmarks equity trajectory against SPY over the identical trading window.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
import math
import os
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, "..", "..", ".."))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from accounting import quantize_cent, to_decimal
from forward_evaluation_models import (
    EvaluationSessionSummary,
    ForwardObservationRecord,
    HypotheticalClosedTrade,
    HypotheticalPosition,
)
from forward_evaluation_storage import ForwardEvaluationStorage, is_genuine_daily_mode


def compute_wilson_score_interval(successes: int, total: int, z: float = 1.96) -> Tuple[Decimal, Decimal]:
    """Calculate the Wilson score confidence interval for a binomial proportion at 95% confidence."""
    if total <= 0:
        return Decimal("0.0000"), Decimal("0.0000")
    p = float(successes) / float(total)
    denominator = 1.0 + (z**2 / float(total))
    centre = (p + (z**2 / (2.0 * float(total)))) / denominator
    spread = (z / denominator) * math.sqrt((p * (1.0 - p) / float(total)) + (z**2 / (4.0 * float(total)**2)))
    low = max(0.0, centre - spread)
    high = min(1.0, centre + spread)
    return quantize_cent(Decimal(str(round(low, 4)))), quantize_cent(Decimal(str(round(high, 4))))


@dataclass(frozen=True)
class ForwardPerformanceReport:
    """Comprehensive performance assessment based strictly on forward evaluation data."""
    evaluation_start_date: str
    evaluation_end_date: str
    total_sessions_evaluated: int
    total_symbols_screened: int
    total_eligible_candidates: int
    total_rejected_candidates: int
    candidate_conversion_pct: Decimal
    open_positions_count: int
    closed_trades_count: int
    winning_trades_count: int
    losing_trades_count: int
    break_even_trades_count: int
    win_rate_pct: Decimal
    win_rate_ci_lower_pct: Decimal
    win_rate_ci_upper_pct: Decimal
    profit_factor: Optional[Decimal]
    gross_realized_pnl: Decimal
    total_fees_incurred: Decimal
    net_realized_pnl: Decimal
    current_unrealized_pnl: Decimal
    total_net_pnl: Decimal
    average_trade_net_pnl: Decimal
    average_win_usd: Decimal
    average_loss_usd: Decimal
    win_loss_ratio: Optional[Decimal]
    expectancy_per_trade: Decimal
    max_drawdown_usd: Decimal
    max_drawdown_pct: Decimal
    average_holding_days: Decimal
    average_mfe_usd: Decimal
    average_mae_usd: Decimal
    regime_breakdown: Dict[str, Any]
    ticker_breakdown: Dict[str, Any]
    benchmark_spy_return_pct: Optional[Decimal]
    is_sample_statistically_significant: bool
    statistical_caveat: str
    data_mode_filter: str = "GENUINE_FORWARD_ONLY"
    as_of_timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "evaluation_start_date": self.evaluation_start_date,
            "evaluation_end_date": self.evaluation_end_date,
            "total_sessions_evaluated": self.total_sessions_evaluated,
            "total_symbols_screened": self.total_symbols_screened,
            "total_eligible_candidates": self.total_eligible_candidates,
            "total_rejected_candidates": self.total_rejected_candidates,
            "candidate_conversion_pct": str(self.candidate_conversion_pct),
            "open_positions_count": self.open_positions_count,
            "closed_trades_count": self.closed_trades_count,
            "winning_trades_count": self.winning_trades_count,
            "losing_trades_count": self.losing_trades_count,
            "break_even_trades_count": self.break_even_trades_count,
            "win_rate_pct": str(self.win_rate_pct),
            "win_rate_ci_lower_pct": str(self.win_rate_ci_lower_pct),
            "win_rate_ci_upper_pct": str(self.win_rate_ci_upper_pct),
            "profit_factor": str(self.profit_factor) if self.profit_factor is not None else None,
            "gross_realized_pnl": str(self.gross_realized_pnl),
            "total_fees_incurred": str(self.total_fees_incurred),
            "net_realized_pnl": str(self.net_realized_pnl),
            "current_unrealized_pnl": str(self.current_unrealized_pnl),
            "total_net_pnl": str(self.total_net_pnl),
            "average_trade_net_pnl": str(self.average_trade_net_pnl),
            "average_win_usd": str(self.average_win_usd),
            "average_loss_usd": str(self.average_loss_usd),
            "win_loss_ratio": str(self.win_loss_ratio) if self.win_loss_ratio is not None else None,
            "expectancy_per_trade": str(self.expectancy_per_trade),
            "max_drawdown_usd": str(self.max_drawdown_usd),
            "max_drawdown_pct": str(self.max_drawdown_pct),
            "average_holding_days": str(self.average_holding_days),
            "average_mfe_usd": str(self.average_mfe_usd),
            "average_mae_usd": str(self.average_mae_usd),
            "regime_breakdown": self.regime_breakdown,
            "ticker_breakdown": self.ticker_breakdown,
            "benchmark_spy_return_pct": str(self.benchmark_spy_return_pct) if self.benchmark_spy_return_pct is not None else None,
            "is_sample_statistically_significant": self.is_sample_statistically_significant,
            "statistical_caveat": self.statistical_caveat,
            "data_mode_filter": self.data_mode_filter,
            "as_of_timestamp": self.as_of_timestamp,
        }


class ForwardPerformanceEngine:
    """Calculates forward performance metrics from storage."""

    def __init__(self, storage: ForwardEvaluationStorage, virtual_starting_capital: Decimal = Decimal("5000.00")):
        self.storage = storage
        self.starting_capital = quantize_cent(to_decimal(virtual_starting_capital))

    def evaluate_performance(
        self,
        session_summaries: Optional[List[EvaluationSessionSummary]] = None,
        closed_trades: Optional[List[HypotheticalClosedTrade]] = None,
        active_positions: Optional[List[HypotheticalPosition]] = None,
        spy_benchmark_candles: Optional[Sequence[Any]] = None,
        data_mode: Optional[str] = None,
        genuine_only: bool = True,
    ) -> ForwardPerformanceReport:
        """Compute comprehensive performance report across forward records.
        
        By default (genuine_only=True), strictly filters for genuine DAILY/LIVE records,
        excluding FIXTURE, UNKNOWN, or synthetic data.
        """
        if session_summaries is not None:
            if genuine_only:
                summaries = [s for s in session_summaries if is_genuine_daily_mode(s.data_mode)]
            elif data_mode:
                summaries = [s for s in session_summaries if s.data_mode and s.data_mode.strip().upper() == data_mode.strip().upper()]
            else:
                summaries = list(session_summaries)
        else:
            summaries = self.storage.load_session_summaries(data_mode=data_mode, genuine_only=genuine_only)

        if closed_trades is not None:
            if genuine_only:
                trades = [t for t in closed_trades if is_genuine_daily_mode(t.data_mode)]
            elif data_mode:
                trades = [t for t in closed_trades if t.data_mode and t.data_mode.strip().upper() == data_mode.strip().upper()]
            else:
                trades = list(closed_trades)
        else:
            trades = self.storage.load_closed_trades(data_mode=data_mode, genuine_only=genuine_only)

        if active_positions is not None:
            if genuine_only:
                positions = [p for p in active_positions if is_genuine_daily_mode(p.data_mode)]
            elif data_mode:
                positions = [p for p in active_positions if p.data_mode and p.data_mode.strip().upper() == data_mode.strip().upper()]
            else:
                positions = list(active_positions)
        else:
            positions = self.storage.load_active_positions(data_mode=data_mode, genuine_only=genuine_only)

        # Dates & Counts
        start_date = summaries[0].session_date if summaries else "N/A"
        end_date = summaries[-1].session_date if summaries else "N/A"
        total_sessions = len(summaries)

        screened = sum(s.tickers_screened for s in summaries)
        eligible = sum(s.eligible_candidates for s in summaries)
        rejected = sum(s.rejected_candidates for s in summaries)
        conversion_pct = quantize_cent((Decimal(eligible) / Decimal(screened)) * Decimal("100.00")) if screened > 0 else Decimal("0.00")

        # Closed trade metrics
        n_trades = len(trades)
        wins = [t for t in trades if t.net_pnl > Decimal("0.00")]
        losses = [t for t in trades if t.net_pnl < Decimal("0.00")]
        evens = [t for t in trades if t.net_pnl == Decimal("0.00")]

        win_count = len(wins)
        loss_count = len(losses)
        even_count = len(evens)

        win_rate = quantize_cent((Decimal(win_count) / Decimal(n_trades)) * Decimal("100.00")) if n_trades > 0 else Decimal("0.00")
        ci_low, ci_high = compute_wilson_score_interval(win_count, n_trades)
        ci_low_pct = quantize_cent(ci_low * Decimal("100.00"))
        ci_high_pct = quantize_cent(ci_high * Decimal("100.00"))

        tot_win_usd = sum((w.net_pnl for w in wins), Decimal("0.00"))
        tot_loss_usd = abs(sum((l.net_pnl for l in losses), Decimal("0.00")))
        tot_gross_pnl = sum((t.gross_pnl for t in trades), Decimal("0.00"))
        tot_fees = sum((t.entry_fee + t.exit_fee for t in trades), Decimal("0.00"))
        tot_net_pnl = sum((t.net_pnl for t in trades), Decimal("0.00"))

        avg_win = quantize_cent(tot_win_usd / Decimal(win_count)) if win_count > 0 else Decimal("0.00")
        avg_loss = quantize_cent(tot_loss_usd / Decimal(loss_count)) if loss_count > 0 else Decimal("0.00")
        avg_net_trade = quantize_cent(tot_net_pnl / Decimal(n_trades)) if n_trades > 0 else Decimal("0.00")

        profit_factor: Optional[Decimal] = None
        if tot_loss_usd > Decimal("0.00"):
            profit_factor = quantize_cent(tot_win_usd / tot_loss_usd)
        elif tot_win_usd > Decimal("0.00") and loss_count == 0:
            profit_factor = Decimal("99.99")  # Infinite / zero loss floor

        win_loss_ratio: Optional[Decimal] = None
        if avg_loss > Decimal("0.00"):
            win_loss_ratio = quantize_cent(avg_win / avg_loss)

        # Expectancy: (P(Win) * AvgWin) - (P(Loss) * AvgLoss) - AvgFee
        avg_fee_per_trade = quantize_cent(tot_fees / Decimal(n_trades)) if n_trades > 0 else Decimal("0.00")
        p_win = Decimal(win_count) / Decimal(n_trades) if n_trades > 0 else Decimal("0.00")
        p_loss = Decimal(loss_count) / Decimal(n_trades) if n_trades > 0 else Decimal("0.00")
        expectancy = quantize_cent((p_win * avg_win) - (p_loss * avg_loss))

        # Excursions & Holding duration
        tot_mfe = sum((t.mfe_usd for t in trades), Decimal("0.00"))
        tot_mae = sum((t.mae_usd for t in trades), Decimal("0.00"))
        avg_mfe = quantize_cent(tot_mfe / Decimal(n_trades)) if n_trades > 0 else Decimal("0.00")
        avg_mae = quantize_cent(tot_mae / Decimal(n_trades)) if n_trades > 0 else Decimal("0.00")
        avg_hold_days = quantize_cent(Decimal(sum(t.bars_held for t in trades)) / Decimal(n_trades)) if n_trades > 0 else Decimal("0.00")

        # Open Position metrics
        curr_unrealized = sum((p.unrealized_pnl for p in positions), Decimal("0.00"))
        total_pnl = tot_net_pnl + curr_unrealized

        # Drawdown calculation along equity curve
        peak_equity = self.starting_capital
        running_equity = self.starting_capital
        max_dd_usd = Decimal("0.00")
        max_dd_pct = Decimal("0.00")

        for t in trades:
            running_equity += t.net_pnl
            if running_equity > peak_equity:
                peak_equity = running_equity
            dd_usd = peak_equity - running_equity
            if dd_usd > max_dd_usd:
                max_dd_usd = dd_usd
                max_dd_pct = quantize_cent((dd_usd / peak_equity) * Decimal("100.00")) if peak_equity > 0 else Decimal("0.00")

        # Regime Breakdown
        regime_stats: Dict[str, Dict[str, Any]] = {}
        for s in summaries:
            r = s.market_regime
            if r not in regime_stats:
                regime_stats[r] = {"sessions": 0, "screened": 0, "eligible": 0}
            regime_stats[r]["sessions"] += 1
            regime_stats[r]["screened"] += s.tickers_screened
            regime_stats[r]["eligible"] += s.eligible_candidates

        # Ticker Breakdown
        ticker_stats: Dict[str, Dict[str, Any]] = {}
        for t in trades:
            sym = t.ticker
            if sym not in ticker_stats:
                ticker_stats[sym] = {"trades": 0, "wins": 0, "losses": 0, "net_pnl": Decimal("0.00")}
            ticker_stats[sym]["trades"] += 1
            if t.net_pnl > 0:
                ticker_stats[sym]["wins"] += 1
            elif t.net_pnl < 0:
                ticker_stats[sym]["losses"] += 1
            ticker_stats[sym]["net_pnl"] += t.net_pnl

        # Serialize ticker pnl
        serializable_ticker_stats = {
            sym: {
                "trades": data["trades"],
                "wins": data["wins"],
                "losses": data["losses"],
                "net_pnl": str(data["net_pnl"]),
            }
            for sym, data in ticker_stats.items()
        }

        # SPY Benchmark comparison if available
        spy_return_pct: Optional[Decimal] = None
        if spy_benchmark_candles and len(spy_benchmark_candles) >= 2:
            first_c = spy_benchmark_candles[0].close
            last_c = spy_benchmark_candles[-1].close
            if first_c > 0:
                spy_return_pct = quantize_cent(((last_c - first_c) / first_c) * Decimal("100.00"))

        # Mode filter labeling & statistical significance check
        filter_label = (
            "GENUINE_FORWARD_ONLY (DAILY/LIVE)"
            if genuine_only
            else (f"DIAGNOSTIC ({data_mode.upper()})" if data_mode else "DIAGNOSTIC_ALL_COMBINED")
        )
        is_stat_sig = (n_trades >= 30)
        caveat = (
            "STATISTICALLY ROBUST SAMPLE: N >= 30 closed trades observed."
            if is_stat_sig
            else f"SAMPLE INSUFFICIENT: Only {n_trades} closed trades observed. A minimum of N=30 independent trades is required before inferring statistical expectancy."
        )
        if not genuine_only or (data_mode and data_mode.upper() == "FIXTURE"):
            caveat = f"⚠️ DIAGNOSTIC DATA ONLY ({filter_label}). Does not constitute forward performance evidence. " + caveat

        return ForwardPerformanceReport(
            evaluation_start_date=start_date,
            evaluation_end_date=end_date,
            total_sessions_evaluated=total_sessions,
            total_symbols_screened=screened,
            total_eligible_candidates=eligible,
            total_rejected_candidates=rejected,
            candidate_conversion_pct=conversion_pct,
            open_positions_count=len(positions),
            closed_trades_count=n_trades,
            winning_trades_count=win_count,
            losing_trades_count=loss_count,
            break_even_trades_count=even_count,
            win_rate_pct=win_rate,
            win_rate_ci_lower_pct=ci_low_pct,
            win_rate_ci_upper_pct=ci_high_pct,
            profit_factor=profit_factor,
            gross_realized_pnl=tot_gross_pnl,
            total_fees_incurred=tot_fees,
            net_realized_pnl=tot_net_pnl,
            current_unrealized_pnl=curr_unrealized,
            total_net_pnl=total_pnl,
            average_trade_net_pnl=avg_net_trade,
            average_win_usd=avg_win,
            average_loss_usd=avg_loss,
            win_loss_ratio=win_loss_ratio,
            expectancy_per_trade=expectancy,
            max_drawdown_usd=quantize_cent(max_dd_usd),
            max_drawdown_pct=quantize_cent(max_dd_pct),
            average_holding_days=avg_hold_days,
            average_mfe_usd=avg_mfe,
            average_mae_usd=avg_mae,
            regime_breakdown=regime_stats,
            ticker_breakdown=serializable_ticker_stats,
            benchmark_spy_return_pct=spy_return_pct,
            is_sample_statistically_significant=is_stat_sig,
            statistical_caveat=caveat,
            data_mode_filter=filter_label,
        )
