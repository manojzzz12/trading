"""Benchmark validation and cash yield accrual engine for trader-swing.

Phase 5.10.1 Implementation:
1. SPY Total Return Index (TRI) construction with ex-date cash dividend reinvestment.
2. Prevention of dividend double-counting via explicit split-adjusted candle + cash dividend modeling.
3. FRED DTB3 3-Month Treasury Bill yield conversion (discount basis to bond-equivalent yield).
4. Calendar-day cash yield accrual across trading sessions, weekends, and holidays.
5. Risk-adjusted benchmark comparisons (Beta, Jensen's Alpha, Information Ratio, Treynor, Calmar, Sortino).
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import math
import os
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from backtest_models import DividendEvent, PortfolioDailySnapshot
from market_data_models import OHLCVCandle

TWO_DECIMALS = Decimal("0.01")
FOUR_DECIMALS = Decimal("0.0001")
SIX_DECIMALS = Decimal("0.000001")


def to_dec(val: Any) -> Decimal:
    if isinstance(val, Decimal):
        return val
    return Decimal(str(val))


def quantize_cent(val: Decimal) -> Decimal:
    return val.quantize(TWO_DECIMALS, ROUND_HALF_UP)


# --- 1. SPY TOTAL RETURN BENCHMARK ---

@dataclass(frozen=True)
class SPYTotalReturnPoint:
    session_date: date
    price: Decimal
    dividend_amount: Decimal
    daily_price_return: Decimal
    daily_total_return: Decimal
    tri_value: Decimal


@dataclass(frozen=True)
class SPYTotalReturnResult:
    benchmark_ticker: str
    start_date: date
    end_date: date
    total_sessions: int
    start_price: Decimal
    end_price: Decimal
    price_return_pct: Decimal
    total_dividends_paid: Decimal
    dividend_count: int
    total_return_pct: Decimal
    cagr_pct: Decimal
    max_drawdown_pct: Decimal
    annualized_volatility_pct: Decimal
    daily_total_returns: List[Decimal]
    tri_series: List[SPYTotalReturnPoint]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "benchmark_ticker": self.benchmark_ticker,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "total_sessions": self.total_sessions,
            "start_price": str(self.start_price),
            "end_price": str(self.end_price),
            "price_return_pct": str(self.price_return_pct),
            "total_dividends_paid": str(self.total_dividends_paid),
            "dividend_count": self.dividend_count,
            "total_return_pct": str(self.total_return_pct),
            "cagr_pct": str(self.cagr_pct),
            "max_drawdown_pct": str(self.max_drawdown_pct),
            "annualized_volatility_pct": str(self.annualized_volatility_pct),
            "tri_points_count": len(self.tri_series),
        }


class SPYTotalReturnCalculator:
    """Calculates SPY buy-and-hold returns on both a price and total-return (dividend-reinvested) basis."""

    @staticmethod
    def calculate(
        benchmark_candles: Sequence[OHLCVCandle],
        dividends: Optional[Sequence[DividendEvent]] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        benchmark_ticker: str = "SPY",
    ) -> SPYTotalReturnResult:
        if not benchmark_candles or len(benchmark_candles) < 2:
            raise ValueError("At least 2 benchmark candles are required to calculate benchmark returns.")

        # Sort candles chronologically
        sorted_candles = sorted(benchmark_candles, key=lambda c: c.timestamp)

        # Filter by date range if provided
        filtered = []
        for c in sorted_candles:
            c_date = c.timestamp.date()
            if start_date and c_date < start_date:
                continue
            if end_date and c_date > end_date:
                continue
            filtered.append(c)

        if len(filtered) < 2:
            raise ValueError(f"Fewer than 2 benchmark candles remain after date filtering ({start_date} to {end_date}).")

        # Map dividends to ex-date
        div_map: Dict[date, Decimal] = {}
        if dividends:
            for d in dividends:
                if d.ticker.upper() == benchmark_ticker.upper():
                    div_map[d.ex_date] = div_map.get(d.ex_date, Decimal("0.00")) + d.amount_per_share

        start_px = filtered[0].close
        end_px = filtered[-1].close
        price_ret_pct = ((end_px - start_px) / start_px) * Decimal("100.0")
        price_ret_pct = price_ret_pct.quantize(TWO_DECIMALS, ROUND_HALF_UP)

        # Build Total Return Index (TRI) series
        tri_series: List[SPYTotalReturnPoint] = []
        daily_total_returns: List[Decimal] = []
        current_tri = Decimal("100.000000")

        # Initial point
        first_date = filtered[0].timestamp.date()
        first_div = div_map.get(first_date, Decimal("0.00"))
        tri_series.append(
            SPYTotalReturnPoint(
                session_date=first_date,
                price=filtered[0].close,
                dividend_amount=first_div,
                daily_price_return=Decimal("0.000000"),
                daily_total_return=Decimal("0.000000"),
                tri_value=current_tri,
            )
        )

        total_divs = Decimal("0.00")
        div_count = 0
        if first_div > Decimal("0.00"):
            total_divs += first_div
            div_count += 1

        for i in range(1, len(filtered)):
            prev_c = filtered[i - 1]
            curr_c = filtered[i]
            c_date = curr_c.timestamp.date()

            div_amt = div_map.get(c_date, Decimal("0.00"))
            if div_amt > Decimal("0.00"):
                total_divs += div_amt
                div_count += 1

            # Price return
            p_prev = prev_c.close
            p_curr = curr_c.close
            daily_px_ret = (p_curr - p_prev) / p_prev

            # Total return: (Price_t + Div_t) / Price_{t-1} - 1
            daily_tot_ret = (p_curr + div_amt - p_prev) / p_prev
            daily_total_returns.append(daily_tot_ret)

            # Reinvest dividend into synthetic TRI
            current_tri = current_tri * (Decimal("1.0") + daily_tot_ret)

            tri_series.append(
                SPYTotalReturnPoint(
                    session_date=c_date,
                    price=p_curr,
                    dividend_amount=div_amt,
                    daily_price_return=daily_px_ret.quantize(SIX_DECIMALS, ROUND_HALF_UP),
                    daily_total_return=daily_tot_ret.quantize(SIX_DECIMALS, ROUND_HALF_UP),
                    tri_value=current_tri.quantize(SIX_DECIMALS, ROUND_HALF_UP),
                )
            )

        # Final TRI metrics
        start_tri = tri_series[0].tri_value
        end_tri = tri_series[-1].tri_value
        total_ret_pct = ((end_tri - start_tri) / start_tri) * Decimal("100.0")
        total_ret_pct = total_ret_pct.quantize(TWO_DECIMALS, ROUND_HALF_UP)

        num_sessions = len(filtered)
        years = Decimal(str(max(1, num_sessions))) / Decimal("252.0")
        if end_tri > Decimal("0.00") and years > Decimal("0.00"):
            ratio = float(end_tri / start_tri)
            cagr_float = (ratio ** (1.0 / float(years))) - 1.0 if ratio > 0 else -1.0
            cagr_pct = (Decimal(str(cagr_float)) * Decimal("100.0")).quantize(TWO_DECIMALS, ROUND_HALF_UP)
        else:
            cagr_pct = Decimal("0.00")

        # Max Drawdown of TRI
        peak_tri = start_tri
        max_dd_pct = Decimal("0.00")
        for pt in tri_series:
            val = pt.tri_value
            if val > peak_tri:
                peak_tri = val
            dd = ((peak_tri - val) / peak_tri) * Decimal("100.0")
            if dd > max_dd_pct:
                max_dd_pct = dd
        max_dd_pct = max_dd_pct.quantize(TWO_DECIMALS, ROUND_HALF_UP)

        # Annualized Volatility
        if daily_total_returns:
            mean_ret = sum(daily_total_returns) / Decimal(len(daily_total_returns))
            var = sum((r - mean_ret) ** 2 for r in daily_total_returns) / Decimal(max(1, len(daily_total_returns) - 1))
            daily_std = Decimal(str(math.sqrt(float(var))))
            ann_vol = (daily_std * Decimal(str(math.sqrt(252))) * Decimal("100.0")).quantize(TWO_DECIMALS, ROUND_HALF_UP)
        else:
            ann_vol = Decimal("0.00")

        return SPYTotalReturnResult(
            benchmark_ticker=benchmark_ticker,
            start_date=filtered[0].timestamp.date(),
            end_date=filtered[-1].timestamp.date(),
            total_sessions=num_sessions,
            start_price=start_px,
            end_price=end_px,
            price_return_pct=price_ret_pct,
            total_dividends_paid=total_divs,
            dividend_count=div_count,
            total_return_pct=total_ret_pct,
            cagr_pct=cagr_pct,
            max_drawdown_pct=max_dd_pct,
            annualized_volatility_pct=ann_vol,
            daily_total_returns=daily_total_returns,
            tri_series=tri_series,
        )


# --- 2. T-BILL CASH YIELD ACCRUAL ENGINE ---

# Representative historical 3-Month US Treasury Bill Secondary Market Rates (FRED: DTB3)
# Rates in percentage points (e.g. 5.25 means 5.25% discount rate)
# Key inflection regimes: Near-zero (2020-2021), rapid tightening (2022), peak plateau (2023-2024), gradual easing (2025)
DEFAULT_FRED_DTB3_REGIME_TABLE: Dict[Tuple[int, int], Decimal] = {
    # 2020: COVID-19 zero-rate regime
    (2020, 1): Decimal("1.52"), (2020, 2): Decimal("1.50"), (2020, 3): Decimal("0.29"),
    (2020, 4): Decimal("0.14"), (2020, 5): Decimal("0.13"), (2020, 6): Decimal("0.16"),
    (2020, 7): Decimal("0.13"), (2020, 8): Decimal("0.10"), (2020, 9): Decimal("0.11"),
    (2020, 10): Decimal("0.10"), (2020, 11): Decimal("0.09"), (2020, 12): Decimal("0.09"),
    # 2021: Persistent near-zero policy
    (2021, 1): Decimal("0.08"), (2021, 2): Decimal("0.04"), (2021, 3): Decimal("0.03"),
    (2021, 4): Decimal("0.02"), (2021, 5): Decimal("0.02"), (2021, 6): Decimal("0.04"),
    (2021, 7): Decimal("0.05"), (2021, 8): Decimal("0.05"), (2021, 9): Decimal("0.04"),
    (2021, 10): Decimal("0.05"), (2021, 11): Decimal("0.05"), (2021, 12): Decimal("0.06"),
    # 2022: Aggressive Fed rate hiking
    (2022, 1): Decimal("0.15"), (2022, 2): Decimal("0.33"), (2022, 3): Decimal("0.45"),
    (2022, 4): Decimal("0.76"), (2022, 5): Decimal("0.98"), (2022, 6): Decimal("1.49"),
    (2022, 7): Decimal("2.13"), (2022, 8): Decimal("2.57"), (2022, 9): Decimal("3.02"),
    (2022, 10): Decimal("3.59"), (2022, 11): Decimal("4.15"), (2022, 12): Decimal("4.21"),
    # 2023: Peak restrictive rates
    (2023, 1): Decimal("4.50"), (2023, 2): Decimal("4.65"), (2023, 3): Decimal("4.69"),
    (2023, 4): Decimal("4.93"), (2023, 5): Decimal("5.14"), (2023, 6): Decimal("5.16"),
    (2023, 7): Decimal("5.25"), (2023, 8): Decimal("5.30"), (2023, 9): Decimal("5.32"),
    (2023, 10): Decimal("5.34"), (2023, 11): Decimal("5.27"), (2023, 12): Decimal("5.24"),
    # 2024: Peak rates through mid-year easing
    (2024, 1): Decimal("5.22"), (2024, 2): Decimal("5.23"), (2024, 3): Decimal("5.24"),
    (2024, 4): Decimal("5.24"), (2024, 5): Decimal("5.25"), (2024, 6): Decimal("5.24"),
    (2024, 7): Decimal("5.18"), (2024, 8): Decimal("5.02"), (2024, 9): Decimal("4.74"),
    (2024, 10): Decimal("4.51"), (2024, 11): Decimal("4.45"), (2024, 12): Decimal("4.35"),
    # 2025: Normalized restrictive
    (2025, 1): Decimal("4.25"), (2025, 2): Decimal("4.20"), (2025, 3): Decimal("4.15"),
    (2025, 4): Decimal("4.10"), (2025, 5): Decimal("4.05"), (2025, 6): Decimal("4.00"),
    (2025, 7): Decimal("3.95"), (2025, 8): Decimal("3.90"), (2025, 9): Decimal("3.85"),
    (2025, 10): Decimal("3.80"), (2025, 11): Decimal("3.75"), (2025, 12): Decimal("3.70"),
}


@dataclass(frozen=True)
class DailyCashYieldRecord:
    session_date: date
    days_accrued: int
    unencumbered_cash_usd: Decimal
    fred_discount_rate_pct: Decimal
    bond_equivalent_yield_pct: Decimal
    interest_earned_usd: Decimal
    cumulative_interest_usd: Decimal
    raw_equity_usd: Decimal
    adjusted_equity_usd: Decimal


@dataclass(frozen=True)
class CashYieldResult:
    total_interest_earned_usd: Decimal
    annualized_cash_yield_pct: Decimal
    raw_final_equity_usd: Decimal
    adjusted_final_equity_usd: Decimal
    raw_total_return_pct: Decimal
    adjusted_total_return_pct: Decimal
    raw_cagr_pct: Decimal
    adjusted_cagr_pct: Decimal
    raw_sharpe_ratio: Decimal
    adjusted_sharpe_ratio: Decimal
    daily_records: List[DailyCashYieldRecord]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_interest_earned_usd": str(self.total_interest_earned_usd),
            "annualized_cash_yield_pct": str(self.annualized_cash_yield_pct),
            "raw_final_equity_usd": str(self.raw_final_equity_usd),
            "adjusted_final_equity_usd": str(self.adjusted_final_equity_usd),
            "raw_total_return_pct": str(self.raw_total_return_pct),
            "adjusted_total_return_pct": str(self.adjusted_total_return_pct),
            "raw_cagr_pct": str(self.raw_cagr_pct),
            "adjusted_cagr_pct": str(self.adjusted_cagr_pct),
            "raw_sharpe_ratio": str(self.raw_sharpe_ratio),
            "adjusted_sharpe_ratio": str(self.adjusted_sharpe_ratio),
            "records_count": len(self.daily_records),
        }


class TBillCashYieldCalculator:
    """Calculates risk-free yield accrual on unencumbered cash balances using FRED 3-Month T-Bill rates."""

    def __init__(self, custom_rate_table: Optional[Dict[date, Decimal]] = None):
        self.custom_rates = custom_rate_table or {}

    @staticmethod
    def discount_to_bond_equivalent_yield(discount_rate_pct: Decimal) -> Decimal:
        """Convert a 3-month Treasury bill discount rate (Actual/360) to a Bond-Equivalent Annual Yield (Actual/365).

        Formula:
            y = (365 * d) / (360 - 91 * d)
        where d is the discount rate in decimal (e.g. 0.0525 for 5.25%).
        """
        d = discount_rate_pct / Decimal("100.0")
        denom = Decimal("360.0") - (Decimal("91.0") * d)
        if denom <= Decimal("0.0"):
            return discount_rate_pct
        y = (Decimal("365.0") * d) / denom
        return (y * Decimal("100.0")).quantize(FOUR_DECIMALS, ROUND_HALF_UP)

    def get_observed_rate(self, observation_date: date) -> Decimal:
        """Lookup the FRED DTB3 discount rate active on or before observation_date."""
        if observation_date in self.custom_rates:
            return self.custom_rates[observation_date]

        # Monthly table fallback
        key = (observation_date.year, observation_date.month)
        if key in DEFAULT_FRED_DTB3_REGIME_TABLE:
            return DEFAULT_FRED_DTB3_REGIME_TABLE[key]

        # Clamped boundaries
        if observation_date.year < 2020:
            return Decimal("1.50")
        return Decimal("3.70")

    def calculate_cash_accrual(
        self,
        snapshots: Sequence[PortfolioDailySnapshot],
        initial_capital: Decimal = Decimal("5000.00"),
    ) -> CashYieldResult:
        if not snapshots:
            return CashYieldResult(
                total_interest_earned_usd=Decimal("0.00"),
                annualized_cash_yield_pct=Decimal("0.00"),
                raw_final_equity_usd=initial_capital,
                adjusted_final_equity_usd=initial_capital,
                raw_total_return_pct=Decimal("0.00"),
                adjusted_total_return_pct=Decimal("0.00"),
                raw_cagr_pct=Decimal("0.00"),
                adjusted_cagr_pct=Decimal("0.00"),
                raw_sharpe_ratio=Decimal("0.00"),
                adjusted_sharpe_ratio=Decimal("0.00"),
                daily_records=[],
            )

        daily_records: List[DailyCashYieldRecord] = []
        cum_interest = Decimal("0.00")
        adjusted_daily_returns: List[Decimal] = []
        raw_daily_returns: List[Decimal] = []

        prev_raw_eq = snapshots[0].equity_usd
        prev_adj_eq = snapshots[0].equity_usd

        for i, snap in enumerate(snapshots):
            curr_date = snap.session_date
            raw_eq = snap.equity_usd

            if i == 0:
                # First session: no prior days elapsed for interest accrual
                days_accrued = 0
                interest_today = Decimal("0.00")
                d_rate = self.get_observed_rate(curr_date)
                bey_rate = self.discount_to_bond_equivalent_yield(d_rate)
            else:
                prev_date = snapshots[i - 1].session_date
                prev_cash = snapshots[i - 1].cash_usd

                # Calendar days elapsed (weekday = 1, weekend = 3, holidays = 2-4)
                days_accrued = max(1, (curr_date - prev_date).days)

                # Observed rate from prior session (strictly t-1 information)
                d_rate = self.get_observed_rate(prev_date)
                bey_rate = self.discount_to_bond_equivalent_yield(d_rate)

                # Daily rate compounding: (1 + y)^(days / 365) - 1
                annual_y_dec = bey_rate / Decimal("100.0")
                if prev_cash > Decimal("0.00") and annual_y_dec > Decimal("0.00"):
                    rate_float = float(annual_y_dec)
                    factor_float = ((1.0 + rate_float) ** (float(days_accrued) / 365.0)) - 1.0
                    interest_today = (prev_cash * Decimal(str(factor_float))).quantize(TWO_DECIMALS, ROUND_HALF_UP)
                else:
                    interest_today = Decimal("0.00")

                cum_interest += interest_today

            adj_eq = raw_eq + cum_interest

            if i > 0:
                raw_ret = (raw_eq - prev_raw_eq) / prev_raw_eq if prev_raw_eq > Decimal("0.00") else Decimal("0.00")
                adj_ret = (adj_eq - prev_adj_eq) / prev_adj_eq if prev_adj_eq > Decimal("0.00") else Decimal("0.00")
                raw_daily_returns.append(raw_ret)
                adjusted_daily_returns.append(adj_ret)

            prev_raw_eq = raw_eq
            prev_adj_eq = adj_eq

            daily_records.append(
                DailyCashYieldRecord(
                    session_date=curr_date,
                    days_accrued=days_accrued,
                    unencumbered_cash_usd=snap.cash_usd,
                    fred_discount_rate_pct=d_rate,
                    bond_equivalent_yield_pct=bey_rate,
                    interest_earned_usd=interest_today,
                    cumulative_interest_usd=cum_interest,
                    raw_equity_usd=raw_eq,
                    adjusted_equity_usd=adj_eq,
                )
            )

        raw_final = snapshots[-1].equity_usd
        adj_final = raw_final + cum_interest

        raw_tot_ret = ((raw_final - initial_capital) / initial_capital) * Decimal("100.0")
        adj_tot_ret = ((adj_final - initial_capital) / initial_capital) * Decimal("100.0")

        num_sessions = len(snapshots)
        years = Decimal(str(max(1, num_sessions))) / Decimal("252.0")

        def calc_cagr(final_val: Decimal) -> Decimal:
            if final_val > Decimal("0.00") and years > Decimal("0.00"):
                ratio = float(final_val / initial_capital)
                cagr_f = (ratio ** (1.0 / float(years))) - 1.0 if ratio > 0 else -1.0
                return (Decimal(str(cagr_f)) * Decimal("100.0")).quantize(TWO_DECIMALS, ROUND_HALF_UP)
            return Decimal("0.00")

        raw_cagr = calc_cagr(raw_final)
        adj_cagr = calc_cagr(adj_final)

        def calc_sharpe(rets: List[Decimal]) -> Decimal:
            if not rets or len(rets) < 2:
                return Decimal("0.00")
            mean_r = sum(rets) / Decimal(len(rets))
            var = sum((r - mean_r) ** 2 for r in rets) / Decimal(len(rets) - 1)
            std_r = Decimal(str(math.sqrt(float(var))))
            if std_r <= Decimal("0.00"):
                return Decimal("0.00")
            return ((mean_r / std_r) * Decimal(str(math.sqrt(252)))).quantize(TWO_DECIMALS, ROUND_HALF_UP)

        raw_sharpe = calc_sharpe(raw_daily_returns)
        adj_sharpe = calc_sharpe(adjusted_daily_returns)

        # Average cash yield
        avg_cash = sum(r.unencumbered_cash_usd for r in daily_records) / Decimal(len(daily_records))
        ann_cash_yield = Decimal("0.00")
        if avg_cash > Decimal("0.00") and years > Decimal("0.00"):
            ann_cash_yield = ((cum_interest / (avg_cash * years)) * Decimal("100.0")).quantize(TWO_DECIMALS, ROUND_HALF_UP)

        return CashYieldResult(
            total_interest_earned_usd=cum_interest,
            annualized_cash_yield_pct=ann_cash_yield,
            raw_final_equity_usd=raw_final,
            adjusted_final_equity_usd=adj_final,
            raw_total_return_pct=raw_tot_ret.quantize(TWO_DECIMALS, ROUND_HALF_UP),
            adjusted_total_return_pct=adj_tot_ret.quantize(TWO_DECIMALS, ROUND_HALF_UP),
            raw_cagr_pct=raw_cagr,
            adjusted_cagr_pct=adj_cagr,
            raw_sharpe_ratio=raw_sharpe,
            adjusted_sharpe_ratio=adj_sharpe,
            daily_records=daily_records,
        )


# --- 3. RISK-ADJUSTED ATTRIBUTION CALCULATOR ---

@dataclass(frozen=True)
class RiskAdjustedAttributionResult:
    portfolio_cagr_pct: Decimal
    benchmark_cagr_pct: Decimal
    excess_cagr_pct: Decimal
    portfolio_annualized_vol_pct: Decimal
    benchmark_annualized_vol_pct: Decimal
    beta: Decimal
    jensens_alpha_pct: Decimal
    tracking_error_pct: Decimal
    information_ratio: Optional[Decimal]
    treynor_ratio: Optional[Decimal]
    portfolio_calmar_ratio: Optional[Decimal]
    benchmark_calmar_ratio: Optional[Decimal]
    portfolio_sortino_ratio: Optional[Decimal]
    benchmark_sortino_ratio: Optional[Decimal]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "portfolio_cagr_pct": str(self.portfolio_cagr_pct),
            "benchmark_cagr_pct": str(self.benchmark_cagr_pct),
            "excess_cagr_pct": str(self.excess_cagr_pct),
            "portfolio_annualized_vol_pct": str(self.portfolio_annualized_vol_pct),
            "benchmark_annualized_vol_pct": str(self.benchmark_annualized_vol_pct),
            "beta": str(self.beta),
            "jensens_alpha_pct": str(self.jensens_alpha_pct),
            "tracking_error_pct": str(self.tracking_error_pct),
            "information_ratio": str(self.information_ratio) if self.information_ratio is not None else None,
            "treynor_ratio": str(self.treynor_ratio) if self.treynor_ratio is not None else None,
            "portfolio_calmar_ratio": str(self.portfolio_calmar_ratio) if self.portfolio_calmar_ratio is not None else None,
            "benchmark_calmar_ratio": str(self.benchmark_calmar_ratio) if self.benchmark_calmar_ratio is not None else None,
            "portfolio_sortino_ratio": str(self.portfolio_sortino_ratio) if self.portfolio_sortino_ratio is not None else None,
            "benchmark_sortino_ratio": str(self.benchmark_sortino_ratio) if self.benchmark_sortino_ratio is not None else None,
        }


class RiskAdjustedAttributionCalculator:
    """Calculates capital-asset pricing model (CAPM) and downside risk-adjusted attribution metrics."""

    @staticmethod
    def calculate(
        portfolio_returns: Sequence[Decimal],
        benchmark_returns: Sequence[Decimal],
        portfolio_cagr_pct: Decimal,
        benchmark_cagr_pct: Decimal,
        portfolio_max_dd_pct: Decimal,
        benchmark_max_dd_pct: Decimal,
        risk_free_rate_pct: Decimal = Decimal("2.00"),
    ) -> RiskAdjustedAttributionResult:
        if len(portfolio_returns) != len(benchmark_returns) or len(portfolio_returns) < 5:
            raise ValueError(
                f"Portfolio and benchmark returns must have identical length >= 5 (got {len(portfolio_returns)} vs {len(benchmark_returns)})."
            )

        n = len(portfolio_returns)
        p_mean = sum(portfolio_returns) / Decimal(n)
        b_mean = sum(benchmark_returns) / Decimal(n)

        # Variances and Covariance
        p_var = sum((r - p_mean) ** 2 for r in portfolio_returns) / Decimal(n - 1)
        b_var = sum((r - b_mean) ** 2 for r in benchmark_returns) / Decimal(n - 1)
        cov = sum((portfolio_returns[i] - p_mean) * (benchmark_returns[i] - b_mean) for i in range(n)) / Decimal(n - 1)

        p_vol = (Decimal(str(math.sqrt(float(p_var)))) * Decimal(str(math.sqrt(252))) * Decimal("100.0")).quantize(TWO_DECIMALS, ROUND_HALF_UP)
        b_vol = (Decimal(str(math.sqrt(float(b_var)))) * Decimal(str(math.sqrt(252))) * Decimal("100.0")).quantize(TWO_DECIMALS, ROUND_HALF_UP)

        # Beta
        beta = (cov / b_var).quantize(TWO_DECIMALS, ROUND_HALF_UP) if b_var > Decimal("0.00") else Decimal("0.00")

        # Jensen's Alpha: R_p - [R_f + Beta * (R_b - R_f)]
        rf = risk_free_rate_pct
        alpha = portfolio_cagr_pct - (rf + (beta * (benchmark_cagr_pct - rf)))
        alpha = alpha.quantize(TWO_DECIMALS, ROUND_HALF_UP)

        # Tracking Error & Information Ratio
        diff_returns = [portfolio_returns[i] - benchmark_returns[i] for i in range(n)]
        diff_mean = sum(diff_returns) / Decimal(n)
        diff_var = sum((d - diff_mean) ** 2 for d in diff_returns) / Decimal(n - 1)
        te_daily = Decimal(str(math.sqrt(float(diff_var))))
        te_ann_pct = (te_daily * Decimal(str(math.sqrt(252))) * Decimal("100.0")).quantize(TWO_DECIMALS, ROUND_HALF_UP)

        info_ratio = None
        if te_ann_pct > Decimal("0.00"):
            ir_val = (portfolio_cagr_pct - benchmark_cagr_pct) / te_ann_pct
            info_ratio = ir_val.quantize(TWO_DECIMALS, ROUND_HALF_UP)

        # Treynor Ratio: (R_p - R_f) / Beta
        treynor = None
        if beta > Decimal("0.00"):
            t_val = (portfolio_cagr_pct - rf) / beta
            treynor = t_val.quantize(TWO_DECIMALS, ROUND_HALF_UP)

        # Calmar Ratios: CAGR / MaxDD
        p_calmar = (portfolio_cagr_pct / portfolio_max_dd_pct).quantize(TWO_DECIMALS, ROUND_HALF_UP) if portfolio_max_dd_pct > Decimal("0.00") else None
        b_calmar = (benchmark_cagr_pct / benchmark_max_dd_pct).quantize(TWO_DECIMALS, ROUND_HALF_UP) if benchmark_max_dd_pct > Decimal("0.00") else None

        # Sortino Ratios: (R_p - R_f) / Downside Volatility
        def calc_sortino(returns: Sequence[Decimal], cagr_pct: Decimal) -> Optional[Decimal]:
            rf_daily = (rf / Decimal("100.0")) / Decimal("252.0")
            downside_sq = [min(Decimal("0.0"), r - rf_daily) ** 2 for r in returns]
            downside_var = sum(downside_sq) / Decimal(max(1, len(returns) - 1))
            downside_vol = Decimal(str(math.sqrt(float(downside_var)))) * Decimal(str(math.sqrt(252))) * Decimal("100.0")
            if downside_vol > Decimal("0.00"):
                return ((cagr_pct - rf) / downside_vol).quantize(TWO_DECIMALS, ROUND_HALF_UP)
            return None

        p_sortino = calc_sortino(portfolio_returns, portfolio_cagr_pct)
        b_sortino = calc_sortino(benchmark_returns, benchmark_cagr_pct)

        excess_cagr = (portfolio_cagr_pct - benchmark_cagr_pct).quantize(TWO_DECIMALS, ROUND_HALF_UP)

        return RiskAdjustedAttributionResult(
            portfolio_cagr_pct=portfolio_cagr_pct,
            benchmark_cagr_pct=benchmark_cagr_pct,
            excess_cagr_pct=excess_cagr,
            portfolio_annualized_vol_pct=p_vol,
            benchmark_annualized_vol_pct=b_vol,
            beta=beta,
            jensens_alpha_pct=alpha,
            tracking_error_pct=te_ann_pct,
            information_ratio=info_ratio,
            treynor_ratio=treynor,
            portfolio_calmar_ratio=p_calmar,
            benchmark_calmar_ratio=b_calmar,
            portfolio_sortino_ratio=p_sortino,
            benchmark_sortino_ratio=b_sortino,
        )
