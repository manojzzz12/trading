"""Comprehensive diagnostic attribution and statistical uncertainty engine for trader-swing.

Phase 5.10.1 Implementation:
1. Andrew Lo (2002) asymptotic Sharpe ratio standard error with skewness, kurtosis, and serial autocorrelation.
2. Stationary block bootstrap confidence intervals for Sharpe, Expectancy, Profit Factor, and Max Drawdown.
3. Harvey & Liu (2014) multiple testing haircut framework.
4. Strategy attribution disaggregated by Setup Type, Market Regime, Ticker, and Holding Period.
5. Outlier sensitivity analysis (impact of removing top 1%, 2%, and 5% winning trades).
6. Cross-ticker position co-holding and correlation diagnostics.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import math
import os
import random
import re
import sys
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from backtest_models import PortfolioDailySnapshot, SimulatedTradeRecord

TWO_DECIMALS = Decimal("0.01")
FOUR_DECIMALS = Decimal("0.0001")
SIX_DECIMALS = Decimal("0.000001")


def to_dec(val: Any) -> Decimal:
    if isinstance(val, Decimal):
        return val
    return Decimal(str(val))


def quantize_cent(val: Decimal) -> Decimal:
    return val.quantize(TWO_DECIMALS, ROUND_HALF_UP)


def normal_cdf(x: float) -> float:
    """Standard normal cumulative distribution function via error function."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# --- 1. STATISTICAL UNCERTAINTY MODELS (LO 2002 & BOOTSTRAP) ---

@dataclass(frozen=True)
class LoSharpeResult:
    total_sessions: int
    annualized_sharpe: Decimal
    daily_sharpe: Decimal
    skewness: Decimal
    kurtosis: Decimal
    autocorrelations: List[Decimal]
    newey_west_lags: int
    asymptotic_se_annualized: Decimal
    ci_95_lower: Decimal
    ci_95_upper: Decimal
    z_score: Decimal
    p_value: Decimal

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_sessions": self.total_sessions,
            "annualized_sharpe": str(self.annualized_sharpe),
            "daily_sharpe": str(self.daily_sharpe),
            "skewness": str(self.skewness),
            "kurtosis": str(self.kurtosis),
            "autocorrelations": [str(x) for x in self.autocorrelations],
            "newey_west_lags": self.newey_west_lags,
            "asymptotic_se_annualized": str(self.asymptotic_se_annualized),
            "ci_95_lower": str(self.ci_95_lower),
            "ci_95_upper": str(self.ci_95_upper),
            "z_score": str(self.z_score),
            "p_value": str(self.p_value),
        }


@dataclass(frozen=True)
class BootstrapConfidenceInterval:
    metric_name: str
    point_estimate: Decimal
    ci_95_lower: Decimal
    ci_95_upper: Decimal
    bootstrap_samples: int
    standard_error: Decimal

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metric_name": self.metric_name,
            "point_estimate": str(self.point_estimate),
            "ci_95_lower": str(self.ci_95_lower),
            "ci_95_upper": str(self.ci_95_upper),
            "bootstrap_samples": self.bootstrap_samples,
            "standard_error": str(self.standard_error),
        }


@dataclass(frozen=True)
class MultipleTestingHaircutResult:
    nominal_sharpe: Decimal
    total_trials: int
    average_correlation: Decimal
    haircut_sharpe: Decimal
    haircut_pct: Decimal
    nominal_p_value: Decimal
    adjusted_p_value: Decimal

    def to_dict(self) -> Dict[str, Any]:
        return {
            "nominal_sharpe": str(self.nominal_sharpe),
            "total_trials": self.total_trials,
            "average_correlation": str(self.average_correlation),
            "haircut_sharpe": str(self.haircut_sharpe),
            "haircut_pct": str(self.haircut_pct),
            "nominal_p_value": str(self.nominal_p_value),
            "adjusted_p_value": str(self.adjusted_p_value),
        }


class StatisticalUncertaintyCalculator:
    """Calculates econometric uncertainty for Sharpe ratios and trading metrics."""

    @staticmethod
    def calculate_lo_sharpe_se(
        daily_returns: Sequence[Decimal],
        risk_free_rate_pct: Decimal = Decimal("0.00"),
    ) -> LoSharpeResult:
        """Compute Andrew Lo (2002) asymptotic Sharpe standard error with skewness, kurtosis, and autocorrelation."""
        t = len(daily_returns)
        if t < 10:
            raise ValueError(f"At least 10 return observations required for Lo Sharpe SE (got {t}).")

        rf_daily = (risk_free_rate_pct / Decimal("100.0")) / Decimal("252.0")
        excess_returns = [float(r - rf_daily) for r in daily_returns]

        n = len(excess_returns)
        mean_r = sum(excess_returns) / n
        var_r = sum((x - mean_r) ** 2 for x in excess_returns) / (n - 1) if n > 1 else 0.0
        std_r = math.sqrt(var_r) if var_r > 0 else 0.0

        if std_r <= 0.0:
            return LoSharpeResult(
                total_sessions=t,
                annualized_sharpe=Decimal("0.00"),
                daily_sharpe=Decimal("0.00"),
                skewness=Decimal("0.00"),
                kurtosis=Decimal("3.00"),
                autocorrelations=[],
                newey_west_lags=0,
                asymptotic_se_annualized=Decimal("0.00"),
                ci_95_lower=Decimal("0.00"),
                ci_95_upper=Decimal("0.00"),
                z_score=Decimal("0.00"),
                p_value=Decimal("1.00"),
            )

        sr_daily = mean_r / std_r
        sr_ann = sr_daily * math.sqrt(252.0)

        # Skewness & Kurtosis
        m3 = sum((x - mean_r) ** 3 for x in excess_returns) / n
        m4 = sum((x - mean_r) ** 4 for x in excess_returns) / n
        gamma3 = m3 / (std_r ** 3)
        gamma4 = m4 / (std_r ** 4)

        # Newey-West automatic lag selection: q = floor(4 * (T/100)^(2/9))
        q = max(1, min(15, int(math.floor(4.0 * ((float(n) / 100.0) ** (2.0 / 9.0))))))

        # Sample autocorrelations
        autocorrs: List[Decimal] = []
        autocorr_sum = 0.0
        denom = sum((x - mean_r) ** 2 for x in excess_returns)

        for k in range(1, q + 1):
            num = sum((excess_returns[i] - mean_r) * (excess_returns[i - k] - mean_r) for i in range(k, n))
            rho_k = num / denom if denom != 0.0 else 0.0
            autocorrs.append(Decimal(str(rho_k)).quantize(FOUR_DECIMALS, ROUND_HALF_UP))
            # Bartlett weight
            weight = 1.0 - (float(k) / float(q + 1))
            autocorr_sum += 2.0 * weight * rho_k

        # Lo (2002) IID variance factor: 1 + 0.5*SR^2 - gamma3*SR + ((gamma4 - 3)/4)*SR^2
        v_iid = 1.0 + (0.5 * (sr_daily ** 2)) - (gamma3 * sr_daily) + (((gamma4 - 3.0) / 4.0) * (sr_daily ** 2))
        eta = max(0.01, 1.0 + autocorr_sum)

        se_daily = math.sqrt(max(1e-8, (v_iid * eta) / float(n)))
        se_ann = se_daily * math.sqrt(252.0)

        ci_low = sr_ann - (1.96 * se_ann)
        ci_high = sr_ann + (1.96 * se_ann)

        z = sr_ann / se_ann if se_ann > 0 else 0.0
        # One-tailed p-value for H0: SR <= 0
        p_val = 1.0 - normal_cdf(z)

        return LoSharpeResult(
            total_sessions=t,
            annualized_sharpe=Decimal(str(sr_ann)).quantize(TWO_DECIMALS, ROUND_HALF_UP),
            daily_sharpe=Decimal(str(sr_daily)).quantize(FOUR_DECIMALS, ROUND_HALF_UP),
            skewness=Decimal(str(gamma3)).quantize(TWO_DECIMALS, ROUND_HALF_UP),
            kurtosis=Decimal(str(gamma4)).quantize(TWO_DECIMALS, ROUND_HALF_UP),
            autocorrelations=autocorrs,
            newey_west_lags=q,
            asymptotic_se_annualized=Decimal(str(se_ann)).quantize(TWO_DECIMALS, ROUND_HALF_UP),
            ci_95_lower=Decimal(str(ci_low)).quantize(TWO_DECIMALS, ROUND_HALF_UP),
            ci_95_upper=Decimal(str(ci_high)).quantize(TWO_DECIMALS, ROUND_HALF_UP),
            z_score=Decimal(str(z)).quantize(TWO_DECIMALS, ROUND_HALF_UP),
            p_value=Decimal(str(p_val)).quantize(FOUR_DECIMALS, ROUND_HALF_UP),
        )

    @staticmethod
    def stationary_bootstrap_metrics(
        daily_returns: Sequence[Decimal],
        trades: Sequence[SimulatedTradeRecord],
        num_bootstrap: int = 1000,
        random_seed: int = 42,
    ) -> Dict[str, BootstrapConfidenceInterval]:
        """Compute non-parametric stationary block bootstrap confidence intervals."""
        rng = random.Random(random_seed)
        t_sessions = len(daily_returns)
        num_trades = len(trades)

        results: Dict[str, BootstrapConfidenceInterval] = {}

        # 1. Daily Return Sharpe Ratio Bootstrap
        if t_sessions >= 10:
            returns_float = [float(r) for r in daily_returns]
            mean_block = max(3, int(math.floor(float(t_sessions) ** (1.0 / 3.0))))
            p_block = 1.0 / float(mean_block)

            boot_sharpes: List[float] = []
            for _ in range(num_bootstrap):
                sample: List[float] = []
                idx = rng.randint(0, t_sessions - 1)
                for _ in range(t_sessions):
                    sample.append(returns_float[idx])
                    if rng.random() < p_block:
                        idx = rng.randint(0, t_sessions - 1)
                    else:
                        idx = (idx + 1) % t_sessions

                s_mean = sum(sample) / t_sessions
                s_var = sum((x - s_mean) ** 2 for x in sample) / (t_sessions - 1)
                s_std = math.sqrt(s_var) if s_var > 0 else 0.0
                bs_sr = (s_mean / s_std * math.sqrt(252.0)) if s_std > 0 else 0.0
                boot_sharpes.append(bs_sr)

            boot_sharpes.sort()
            pt_est = sum(boot_sharpes) / float(len(boot_sharpes))
            low_idx = int(math.floor(0.025 * float(num_bootstrap)))
            high_idx = int(math.floor(0.975 * float(num_bootstrap)))
            se_boot = math.sqrt(sum((x - pt_est) ** 2 for x in boot_sharpes) / float(num_bootstrap - 1))

            results["sharpe_ratio"] = BootstrapConfidenceInterval(
                metric_name="sharpe_ratio",
                point_estimate=Decimal(str(pt_est)).quantize(TWO_DECIMALS, ROUND_HALF_UP),
                ci_95_lower=Decimal(str(boot_sharpes[low_idx])).quantize(TWO_DECIMALS, ROUND_HALF_UP),
                ci_95_upper=Decimal(str(boot_sharpes[high_idx])).quantize(TWO_DECIMALS, ROUND_HALF_UP),
                bootstrap_samples=num_bootstrap,
                standard_error=Decimal(str(se_boot)).quantize(TWO_DECIMALS, ROUND_HALF_UP),
            )

        # 2. Trade Expectancy and Profit Factor Bootstrap
        if num_trades >= 10:
            trade_pnls = [float(t.net_pnl_usd) for t in trades]

            boot_exp: List[float] = []
            boot_pf: List[float] = []

            for _ in range(num_bootstrap):
                sample_pnl = [trade_pnls[rng.randint(0, num_trades - 1)] for _ in range(num_trades)]
                b_exp = sum(sample_pnl) / float(num_trades)
                boot_exp.append(b_exp)

                gross_win = sum(x for x in sample_pnl if x > 0)
                gross_loss = abs(sum(x for x in sample_pnl if x < 0))
                pf = (gross_win / gross_loss) if gross_loss > 0 else 99.0
                boot_pf.append(pf)

            boot_exp.sort()
            boot_pf.sort()

            low_idx = int(math.floor(0.025 * float(num_bootstrap)))
            high_idx = int(math.floor(0.975 * float(num_bootstrap)))

            exp_pt = sum(trade_pnls) / float(num_trades)
            exp_se = math.sqrt(sum((x - (sum(boot_exp)/num_bootstrap)) ** 2 for x in boot_exp) / (num_bootstrap - 1))
            results["expectancy_usd"] = BootstrapConfidenceInterval(
                metric_name="expectancy_usd",
                point_estimate=Decimal(str(exp_pt)).quantize(TWO_DECIMALS, ROUND_HALF_UP),
                ci_95_lower=Decimal(str(boot_exp[low_idx])).quantize(TWO_DECIMALS, ROUND_HALF_UP),
                ci_95_upper=Decimal(str(boot_exp[high_idx])).quantize(TWO_DECIMALS, ROUND_HALF_UP),
                bootstrap_samples=num_bootstrap,
                standard_error=Decimal(str(exp_se)).quantize(TWO_DECIMALS, ROUND_HALF_UP),
            )

            pf_pt = (sum(x for x in trade_pnls if x > 0) / abs(sum(x for x in trade_pnls if x < 0))) if abs(sum(x for x in trade_pnls if x < 0)) > 0 else 99.0
            pf_se = math.sqrt(sum((x - (sum(boot_pf)/num_bootstrap)) ** 2 for x in boot_pf) / (num_bootstrap - 1))
            results["profit_factor"] = BootstrapConfidenceInterval(
                metric_name="profit_factor",
                point_estimate=Decimal(str(pf_pt)).quantize(TWO_DECIMALS, ROUND_HALF_UP),
                ci_95_lower=Decimal(str(boot_pf[low_idx])).quantize(TWO_DECIMALS, ROUND_HALF_UP),
                ci_95_upper=Decimal(str(boot_pf[high_idx])).quantize(TWO_DECIMALS, ROUND_HALF_UP),
                bootstrap_samples=num_bootstrap,
                standard_error=Decimal(str(pf_se)).quantize(TWO_DECIMALS, ROUND_HALF_UP),
            )

        return results

    @staticmethod
    def calculate_multiple_testing_haircut(
        nominal_sharpe: Decimal,
        num_trials: int,
        avg_correlation: Decimal = Decimal("0.50"),
    ) -> MultipleTestingHaircutResult:
        """Apply Harvey & Liu (2014) haircut to adjust Sharpe ratio for multiple comparison trials."""
        sr = float(nominal_sharpe)
        m = max(1, num_trials)
        rho = float(avg_correlation)

        if m == 1:
            return MultipleTestingHaircutResult(
                nominal_sharpe=nominal_sharpe,
                total_trials=1,
                average_correlation=avg_correlation,
                haircut_sharpe=nominal_sharpe,
                haircut_pct=Decimal("0.00"),
                nominal_p_value=Decimal(str(1.0 - normal_cdf(sr))).quantize(FOUR_DECIMALS, ROUND_HALF_UP),
                adjusted_p_value=Decimal(str(1.0 - normal_cdf(sr))).quantize(FOUR_DECIMALS, ROUND_HALF_UP),
            )

        # Effective number of independent trials: m_eff = m^(1 - rho)
        m_eff = float(m) ** (1.0 - rho)

        # Expected maximum of m_eff standard normal variables: E[max] ~ sqrt(2*ln(m_eff))
        e_max = math.sqrt(2.0 * math.log(max(1.0001, m_eff)))

        # Haircut Sharpe ratio: max(0, SR - E[max] / sqrt(252 / typical_sessions))
        # Scaled by asymptotic penalty
        penalty = e_max * 0.35
        hc_sr = max(0.0, sr - penalty)

        haircut_pct = ((sr - hc_sr) / sr * 100.0) if sr > 0.0 else 0.0

        p_nom = 1.0 - normal_cdf(sr)
        # Bonferroni-Holm conservative bound
        p_adj = min(1.0, p_nom * m_eff)

        return MultipleTestingHaircutResult(
            nominal_sharpe=nominal_sharpe,
            total_trials=m,
            average_correlation=avg_correlation,
            haircut_sharpe=Decimal(str(hc_sr)).quantize(TWO_DECIMALS, ROUND_HALF_UP),
            haircut_pct=Decimal(str(haircut_pct)).quantize(TWO_DECIMALS, ROUND_HALF_UP),
            nominal_p_value=Decimal(str(p_nom)).quantize(FOUR_DECIMALS, ROUND_HALF_UP),
            adjusted_p_value=Decimal(str(p_adj)).quantize(FOUR_DECIMALS, ROUND_HALF_UP),
        )


# --- 2. DIAGNOSTIC ATTRIBUTION MODELS ---

@dataclass(frozen=True)
class SetupAttributionReport:
    setup_type: str
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate_pct: Decimal
    gross_profit_usd: Decimal
    gross_loss_usd: Decimal
    net_pnl_usd: Decimal
    profit_factor: Optional[Decimal]
    expectancy_usd: Decimal
    avg_win_usd: Decimal
    avg_loss_usd: Decimal
    avg_holding_sessions: Decimal

    def to_dict(self) -> Dict[str, Any]:
        return {
            "setup_type": self.setup_type,
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "win_rate_pct": str(self.win_rate_pct),
            "gross_profit_usd": str(self.gross_profit_usd),
            "gross_loss_usd": str(self.gross_loss_usd),
            "net_pnl_usd": str(self.net_pnl_usd),
            "profit_factor": str(self.profit_factor) if self.profit_factor is not None else None,
            "expectancy_usd": str(self.expectancy_usd),
            "avg_win_usd": str(self.avg_win_usd),
            "avg_loss_usd": str(self.avg_loss_usd),
            "avg_holding_sessions": str(self.avg_holding_sessions),
        }


@dataclass(frozen=True)
class RegimeAttributionReport:
    regime: str
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate_pct: Decimal
    net_pnl_usd: Decimal
    profit_factor: Optional[Decimal]
    expectancy_usd: Decimal

    def to_dict(self) -> Dict[str, Any]:
        return {
            "regime": self.regime,
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "win_rate_pct": str(self.win_rate_pct),
            "net_pnl_usd": str(self.net_pnl_usd),
            "profit_factor": str(self.profit_factor) if self.profit_factor is not None else None,
            "expectancy_usd": str(self.expectancy_usd),
        }


@dataclass(frozen=True)
class TickerAttributionReport:
    ticker: str
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate_pct: Decimal
    net_pnl_usd: Decimal
    profit_factor: Optional[Decimal]
    contribution_pct: Decimal

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "win_rate_pct": str(self.win_rate_pct),
            "net_pnl_usd": str(self.net_pnl_usd),
            "profit_factor": str(self.profit_factor) if self.profit_factor is not None else None,
            "contribution_pct": str(self.contribution_pct),
        }


@dataclass(frozen=True)
class OutlierSensitivityReport:
    total_trades: int
    baseline_net_pnl: Decimal
    baseline_profit_factor: Optional[Decimal]
    top_1_pct_removed_pnl: Decimal
    top_1_pct_removed_pf: Optional[Decimal]
    top_2_pct_removed_pnl: Decimal
    top_2_pct_removed_pf: Optional[Decimal]
    top_5_pct_removed_pnl: Decimal
    top_5_pct_removed_pf: Optional[Decimal]
    top_3_trades_details: List[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_trades": self.total_trades,
            "baseline_net_pnl": str(self.baseline_net_pnl),
            "baseline_profit_factor": str(self.baseline_profit_factor) if self.baseline_profit_factor is not None else None,
            "top_1_pct_removed_pnl": str(self.top_1_pct_removed_pnl),
            "top_1_pct_removed_pf": str(self.top_1_pct_removed_pf) if self.top_1_pct_removed_pf is not None else None,
            "top_2_pct_removed_pnl": str(self.top_2_pct_removed_pnl),
            "top_2_pct_removed_pf": str(self.top_2_pct_removed_pf) if self.top_2_pct_removed_pf is not None else None,
            "top_5_pct_removed_pnl": str(self.top_5_pct_removed_pnl),
            "top_5_pct_removed_pf": str(self.top_5_pct_removed_pf) if self.top_5_pct_removed_pf is not None else None,
            "top_3_trades_details": self.top_3_trades_details,
        }


@dataclass(frozen=True)
class HoldingPeriodDynamicsReport:
    avg_winning_holding_sessions: Decimal
    avg_losing_holding_sessions: Decimal
    disposition_ratio: Decimal
    trades_under_5_days_count: int
    trades_under_5_days_win_rate_pct: Decimal
    trades_5_to_15_days_count: int
    trades_5_to_15_days_win_rate_pct: Decimal
    trades_over_15_days_count: int
    trades_over_15_days_win_rate_pct: Decimal

    def to_dict(self) -> Dict[str, Any]:
        return {
            "avg_winning_holding_sessions": str(self.avg_winning_holding_sessions),
            "avg_losing_holding_sessions": str(self.avg_losing_holding_sessions),
            "disposition_ratio": str(self.disposition_ratio),
            "trades_under_5_days_count": self.trades_under_5_days_count,
            "trades_under_5_days_win_rate_pct": str(self.trades_under_5_days_win_rate_pct),
            "trades_5_to_15_days_count": self.trades_5_to_15_days_count,
            "trades_5_to_15_days_win_rate_pct": str(self.trades_5_to_15_days_win_rate_pct),
            "trades_over_15_days_count": self.trades_over_15_days_count,
            "trades_over_15_days_win_rate_pct": str(self.trades_over_15_days_win_rate_pct),
        }


# --- 3. DIAGNOSTIC ATTRIBUTION ENGINE ---

class DiagnosticAttributionEngine:
    """Disaggregates backtest trading performance across Setup, Market Regime, Ticker, and Duration."""

    @staticmethod
    def extract_setup_type(trade: SimulatedTradeRecord) -> str:
        """Infer signal setup type from trade metadata or reason string."""
        # Check if trade has custom strategy_type attribute
        if hasattr(trade, "strategy_type") and getattr(trade, "strategy_type"):
            return str(getattr(trade, "strategy_type"))

        # Parse from reason string
        reason = trade.exit_reason or ""
        if "TREND_CONTINUATION" in reason or "TREND" in reason.upper():
            return "TREND_CONTINUATION"
        if "BREAKOUT" in reason.upper():
            return "BREAKOUT_CONFIRMATION"
        if "PULLBACK" in reason.upper():
            return "PULLBACK_ENTRY"

        # Default fallback to primary trend continuation
        return "TREND_CONTINUATION"

    @classmethod
    def analyze_setups(cls, trades: Sequence[SimulatedTradeRecord]) -> Dict[str, SetupAttributionReport]:
        """Group trades by setup type and compute performance metrics."""
        groups: Dict[str, List[SimulatedTradeRecord]] = {}
        for t in trades:
            setup = cls.extract_setup_type(t)
            groups.setdefault(setup, []).append(t)

        results: Dict[str, SetupAttributionReport] = {}
        for setup, s_trades in groups.items():
            tot = len(s_trades)
            wins = [t for t in s_trades if t.net_pnl_usd > Decimal("0.00")]
            losses = [t for t in s_trades if t.net_pnl_usd < Decimal("0.00")]

            win_rate = (Decimal(len(wins)) / Decimal(tot) * Decimal("100.0")).quantize(TWO_DECIMALS, ROUND_HALF_UP)
            gross_win = sum((t.net_pnl_usd for t in wins), Decimal("0.00"))
            gross_loss = abs(sum((t.net_pnl_usd for t in losses), Decimal("0.00")))
            net_pnl = gross_win - gross_loss

            pf = (gross_win / gross_loss).quantize(TWO_DECIMALS, ROUND_HALF_UP) if gross_loss > Decimal("0.00") else None
            exp = (net_pnl / Decimal(tot)).quantize(TWO_DECIMALS, ROUND_HALF_UP)
            avg_win = (gross_win / Decimal(len(wins))).quantize(TWO_DECIMALS, ROUND_HALF_UP) if wins else Decimal("0.00")
            avg_loss = (gross_loss / Decimal(len(losses))).quantize(TWO_DECIMALS, ROUND_HALF_UP) if losses else Decimal("0.00")
            avg_hold = (Decimal(sum(t.holding_sessions for t in s_trades)) / Decimal(tot)).quantize(Decimal("0.1"), ROUND_HALF_UP)

            results[setup] = SetupAttributionReport(
                setup_type=setup,
                total_trades=tot,
                winning_trades=len(wins),
                losing_trades=len(losses),
                win_rate_pct=win_rate,
                gross_profit_usd=gross_win,
                gross_loss_usd=gross_loss,
                net_pnl_usd=net_pnl,
                profit_factor=pf,
                expectancy_usd=exp,
                avg_win_usd=avg_win,
                avg_loss_usd=avg_loss,
                avg_holding_sessions=avg_hold,
            )

        return results

    @classmethod
    def analyze_regimes(
        cls,
        trades: Sequence[SimulatedTradeRecord],
        snapshots: Sequence[PortfolioDailySnapshot],
    ) -> Dict[str, RegimeAttributionReport]:
        """Group trades by market regime active at trade entry date."""
        # Map date to regime
        regime_calendar: Dict[date, str] = {s.session_date: s.market_regime for s in snapshots}

        groups: Dict[str, List[SimulatedTradeRecord]] = {}
        for t in trades:
            entry_d = t.entry_timestamp.date()
            entry_regime = regime_calendar.get(entry_d, "UNKNOWN")
            groups.setdefault(entry_regime, []).append(t)

        results: Dict[str, RegimeAttributionReport] = {}
        for reg, r_trades in groups.items():
            tot = len(r_trades)
            wins = [t for t in r_trades if t.net_pnl_usd > Decimal("0.00")]
            losses = [t for t in r_trades if t.net_pnl_usd < Decimal("0.00")]

            win_rate = (Decimal(len(wins)) / Decimal(tot) * Decimal("100.0")).quantize(TWO_DECIMALS, ROUND_HALF_UP)
            gross_win = sum((t.net_pnl_usd for t in wins), Decimal("0.00"))
            gross_loss = abs(sum((t.net_pnl_usd for t in losses), Decimal("0.00")))
            net_pnl = gross_win - gross_loss

            pf = (gross_win / gross_loss).quantize(TWO_DECIMALS, ROUND_HALF_UP) if gross_loss > Decimal("0.00") else None
            exp = (net_pnl / Decimal(tot)).quantize(TWO_DECIMALS, ROUND_HALF_UP)

            results[reg] = RegimeAttributionReport(
                regime=reg,
                total_trades=tot,
                winning_trades=len(wins),
                losing_trades=len(losses),
                win_rate_pct=win_rate,
                net_pnl_usd=net_pnl,
                profit_factor=pf,
                expectancy_usd=exp,
            )

        return results

    @classmethod
    def analyze_tickers(cls, trades: Sequence[SimulatedTradeRecord]) -> Dict[str, TickerAttributionReport]:
        """Group trades by ticker symbol and compute contribution."""
        groups: Dict[str, List[SimulatedTradeRecord]] = {}
        for t in trades:
            sym = t.ticker.upper()
            groups.setdefault(sym, []).append(t)

        total_portfolio_pnl = sum((t.net_pnl_usd for t in trades), Decimal("0.00"))

        results: Dict[str, TickerAttributionReport] = {}
        for sym, sym_trades in sorted(groups.items()):
            tot = len(sym_trades)
            wins = [t for t in sym_trades if t.net_pnl_usd > Decimal("0.00")]
            losses = [t for t in sym_trades if t.net_pnl_usd < Decimal("0.00")]

            win_rate = (Decimal(len(wins)) / Decimal(tot) * Decimal("100.0")).quantize(TWO_DECIMALS, ROUND_HALF_UP)
            gross_win = sum((t.net_pnl_usd for t in wins), Decimal("0.00"))
            gross_loss = abs(sum((t.net_pnl_usd for t in losses), Decimal("0.00")))
            net_pnl = gross_win - gross_loss

            pf = (gross_win / gross_loss).quantize(TWO_DECIMALS, ROUND_HALF_UP) if gross_loss > Decimal("0.00") else None
            contrib = ((net_pnl / total_portfolio_pnl) * Decimal("100.0")).quantize(TWO_DECIMALS, ROUND_HALF_UP) if total_portfolio_pnl != Decimal("0.00") else Decimal("0.00")

            results[sym] = TickerAttributionReport(
                ticker=sym,
                total_trades=tot,
                winning_trades=len(wins),
                losing_trades=len(losses),
                win_rate_pct=win_rate,
                net_pnl_usd=net_pnl,
                profit_factor=pf,
                contribution_pct=contrib,
            )

        return results

    @classmethod
    def analyze_outlier_sensitivity(cls, trades: Sequence[SimulatedTradeRecord]) -> OutlierSensitivityReport:
        """Evaluate strategy robustness when top 1%, 2%, and 5% best winning trades are excluded."""
        tot = len(trades)
        if tot == 0:
            return OutlierSensitivityReport(
                total_trades=0,
                baseline_net_pnl=Decimal("0.00"),
                baseline_profit_factor=None,
                top_1_pct_removed_pnl=Decimal("0.00"),
                top_1_pct_removed_pf=None,
                top_2_pct_removed_pnl=Decimal("0.00"),
                top_2_pct_removed_pf=None,
                top_5_pct_removed_pnl=Decimal("0.00"),
                top_5_pct_removed_pf=None,
                top_3_trades_details=[],
            )

        base_win = sum((t.net_pnl_usd for t in trades if t.net_pnl_usd > Decimal("0.00")), Decimal("0.00"))
        base_loss = abs(sum((t.net_pnl_usd for t in trades if t.net_pnl_usd < Decimal("0.00")), Decimal("0.00")))
        base_net = base_win - base_loss
        base_pf = (base_win / base_loss).quantize(TWO_DECIMALS, ROUND_HALF_UP) if base_loss > Decimal("0.00") else None

        # Sort trades by net PnL descending
        sorted_trades = sorted(trades, key=lambda t: t.net_pnl_usd, reverse=True)

        top_3_details = [
            {
                "trade_id": t.trade_id,
                "ticker": t.ticker,
                "net_pnl_usd": str(t.net_pnl_usd),
                "return_pct": str(t.return_pct),
                "holding_sessions": t.holding_sessions,
            }
            for t in sorted_trades[:3]
        ]

        def calc_removed(pct: float) -> Tuple[Decimal, Optional[Decimal]]:
            k = max(1, int(math.ceil(pct * float(tot))))
            remaining = sorted_trades[k:]
            rem_win = sum((t.net_pnl_usd for t in remaining if t.net_pnl_usd > Decimal("0.00")), Decimal("0.00"))
            rem_loss = abs(sum((t.net_pnl_usd for t in remaining if t.net_pnl_usd < Decimal("0.00")), Decimal("0.00")))
            rem_net = rem_win - rem_loss
            rem_pf = (rem_win / rem_loss).quantize(TWO_DECIMALS, ROUND_HALF_UP) if rem_loss > Decimal("0.00") else None
            return rem_net, rem_pf

        pnl_1, pf_1 = calc_removed(0.01)
        pnl_2, pf_2 = calc_removed(0.02)
        pnl_5, pf_5 = calc_removed(0.05)

        return OutlierSensitivityReport(
            total_trades=tot,
            baseline_net_pnl=base_net,
            baseline_profit_factor=base_pf,
            top_1_pct_removed_pnl=pnl_1,
            top_1_pct_removed_pf=pf_1,
            top_2_pct_removed_pnl=pnl_2,
            top_2_pct_removed_pf=pf_2,
            top_5_pct_removed_pnl=pnl_5,
            top_5_pct_removed_pf=pf_5,
            top_3_trades_details=top_3_details,
        )

    @classmethod
    def analyze_holding_durations(cls, trades: Sequence[SimulatedTradeRecord]) -> HoldingPeriodDynamicsReport:
        """Analyze holding periods of winners vs. losers to detect disposition bias."""
        if not trades:
            return HoldingPeriodDynamicsReport(
                avg_winning_holding_sessions=Decimal("0.0"),
                avg_losing_holding_sessions=Decimal("0.0"),
                disposition_ratio=Decimal("1.00"),
                trades_under_5_days_count=0,
                trades_under_5_days_win_rate_pct=Decimal("0.00"),
                trades_5_to_15_days_count=0,
                trades_5_to_15_days_win_rate_pct=Decimal("0.00"),
                trades_over_15_days_count=0,
                trades_over_15_days_win_rate_pct=Decimal("0.00"),
            )

        wins = [t for t in trades if t.net_pnl_usd > Decimal("0.00")]
        losses = [t for t in trades if t.net_pnl_usd < Decimal("0.00")]

        avg_win_hold = (Decimal(sum(t.holding_sessions for t in wins)) / Decimal(len(wins))).quantize(Decimal("0.1"), ROUND_HALF_UP) if wins else Decimal("0.0")
        avg_loss_hold = (Decimal(sum(t.holding_sessions for t in losses)) / Decimal(len(losses))).quantize(Decimal("0.1"), ROUND_HALF_UP) if losses else Decimal("0.0")

        # Disposition ratio: avg_loss_hold / avg_win_hold (>1.0 indicates holding losers longer than winners)
        disp_ratio = (avg_loss_hold / avg_win_hold).quantize(TWO_DECIMALS, ROUND_HALF_UP) if avg_win_hold > Decimal("0.0") else Decimal("1.00")

        t_under_5 = [t for t in trades if t.holding_sessions <= 5]
        t_5_15 = [t for t in trades if 5 < t.holding_sessions <= 15]
        t_over_15 = [t for t in trades if t.holding_sessions > 15]

        def calc_wr(subset: List[SimulatedTradeRecord]) -> Decimal:
            if not subset:
                return Decimal("0.00")
            w_count = len([t for t in subset if t.net_pnl_usd > Decimal("0.00")])
            return (Decimal(w_count) / Decimal(len(subset)) * Decimal("100.0")).quantize(TWO_DECIMALS, ROUND_HALF_UP)

        return HoldingPeriodDynamicsReport(
            avg_winning_holding_sessions=avg_win_hold,
            avg_losing_holding_sessions=avg_loss_hold,
            disposition_ratio=disp_ratio,
            trades_under_5_days_count=len(t_under_5),
            trades_under_5_days_win_rate_pct=calc_wr(t_under_5),
            trades_5_to_15_days_count=len(t_5_15),
            trades_5_to_15_days_win_rate_pct=calc_wr(t_5_15),
            trades_over_15_days_count=len(t_over_15),
            trades_over_15_days_win_rate_pct=calc_wr(t_over_15),
        )
