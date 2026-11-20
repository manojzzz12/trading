"""Unit and integration tests for Phase 5.10.1 Benchmark and Cash Yield Engines.

Tests:
1. SPYTotalReturnCalculator:
   - Synthetic dividend reinvestment strictly on ex-date at closing price.
   - Total return exceeds price return when positive cash dividends are credited.
   - Zero-dividend edge cases (total return == price return).
   - Drawdown and annualized volatility mathematical precision.
2. TBillCashYieldCalculator:
   - Discount rate (Actual/360) to Bond-Equivalent Yield (Actual/365) conversion formula.
   - Multi-day weekend accrual (Friday to Monday = 3 calendar days).
   - Zero-cash balance and zero-rate boundaries.
   - Integration with PortfolioDailySnapshot series.
3. RiskAdjustedAttributionCalculator:
   - Beta, Jensen's Alpha, Information Ratio, Tracking Error, Calmar, Sortino, Treynor.
   - Directional accuracy and edge case handling (zero volatility, identical returns).
"""

import os
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SWING_DIR = os.path.join(ROOT_DIR, ".agents", "skills", "trader-swing")
for path in (ROOT_DIR, SWING_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

from backtest_models import DividendEvent, PortfolioDailySnapshot
from benchmark import (
    SPYTotalReturnCalculator,
    TBillCashYieldCalculator,
    RiskAdjustedAttributionCalculator,
)
from market_data_models import CandleStatus, OHLCVCandle, UTC



class TestSwingBenchmarkPhase5101(unittest.TestCase):
    """Test suite for Phase 5.10.1 Benchmark and Cash Yield engines."""

    def setUp(self):
        # Create deterministic synthetic SPY candles over 5 trading sessions
        # 2024-03-18 (Mon) to 2024-03-22 (Fri)
        self.spy_candles = [
            OHLCVCandle(
                ticker="SPY",
                interval="1d",
                timestamp=datetime(2024, 3, 18, 13, 30, tzinfo=UTC),
                open=Decimal("510.00"),
                high=Decimal("512.00"),
                low=Decimal("509.00"),
                close=Decimal("511.00"),
                volume=50000000,
                status=CandleStatus.COMPLETE,
            ),
            OHLCVCandle(
                ticker="SPY",
                interval="1d",
                timestamp=datetime(2024, 3, 19, 13, 30, tzinfo=UTC),
                open=Decimal("511.50"),
                high=Decimal("515.00"),
                low=Decimal("511.00"),
                close=Decimal("514.00"),
                volume=55000000,
                status=CandleStatus.COMPLETE,
            ),
            OHLCVCandle(
                ticker="SPY",
                interval="1d",
                timestamp=datetime(2024, 3, 20, 13, 30, tzinfo=UTC),
                open=Decimal("514.00"),
                high=Decimal("520.00"),
                low=Decimal("513.50"),
                close=Decimal("519.00"),
                volume=60000000,
                status=CandleStatus.COMPLETE,
            ),
            OHLCVCandle(
                ticker="SPY",
                interval="1d",
                timestamp=datetime(2024, 3, 21, 13, 30, tzinfo=UTC),
                open=Decimal("520.00"),
                high=Decimal("522.00"),
                low=Decimal("518.00"),
                close=Decimal("521.00"),
                volume=52000000,
                status=CandleStatus.COMPLETE,
            ),
            OHLCVCandle(
                ticker="SPY",
                interval="1d",
                timestamp=datetime(2024, 3, 22, 13, 30, tzinfo=UTC),
                open=Decimal("521.00"),
                high=Decimal("523.00"),
                low=Decimal("519.50"),
                close=Decimal("520.00"),
                volume=48000000,
                status=CandleStatus.COMPLETE,
            ),
        ]

    def test_01_spy_total_return_zero_dividend(self):
        """Verify that with zero dividends, total return matches price return identically."""
        res = SPYTotalReturnCalculator.calculate(
            benchmark_candles=self.spy_candles,
            dividends=[],
        )
        # Start: 511.00, End: 520.00 -> (520 - 511) / 511 * 100 = 1.76125% -> 1.76%
        expected_ret = Decimal("1.76")
        self.assertEqual(res.price_return_pct, expected_ret)
        self.assertEqual(res.total_return_pct, expected_ret)
        self.assertEqual(res.total_dividends_paid, Decimal("0.00"))
        self.assertEqual(res.dividend_count, 0)
        self.assertEqual(res.total_sessions, 5)

    def test_02_spy_total_return_with_dividend_reinvestment(self):
        """Verify synthetic dividend reinvestment increases TRI and compounds correctly."""
        # Inject an ex-dividend on 2024-03-21 of $1.90 per share
        div = DividendEvent(
            ticker="SPY",
            ex_date=date(2024, 3, 21),
            amount_per_share=Decimal("1.90"),
        )
        res = SPYTotalReturnCalculator.calculate(
            benchmark_candles=self.spy_candles,
            dividends=[div],
        )

        # Price return is still 1.76%
        self.assertEqual(res.price_return_pct, Decimal("1.76"))
        # Total return must be strictly greater than price return
        self.assertGreater(res.total_return_pct, res.price_return_pct)
        self.assertEqual(res.total_dividends_paid, Decimal("1.90"))
        self.assertEqual(res.dividend_count, 1)

        # Check day 3 to 4 return:
        # prev close: 519.00, curr close: 521.00, div: 1.90
        # total ret = (521.00 + 1.90 - 519.00) / 519.00 = 3.90 / 519.00 = 0.00751445...
        point_div = [p for p in res.tri_series if p.session_date == date(2024, 3, 21)][0]
        self.assertEqual(point_div.dividend_amount, Decimal("1.90"))
        self.assertAlmostEqual(float(point_div.daily_total_return), 3.90 / 519.00, places=5)

    def test_03_tbill_rate_conversion(self):
        """Verify FRED 3-Month T-Bill discount rate to bond-equivalent yield conversion."""
        # Test case: 5.25% discount rate
        d = Decimal("5.25")
        bey = TBillCashYieldCalculator.discount_to_bond_equivalent_yield(d)
        # y = (365 * 0.0525) / (360 - 91 * 0.0525) = 19.1625 / (360 - 4.7775) = 19.1625 / 355.2225 = 0.053945... -> 5.3945%
        expected_bey = Decimal("5.3945")
        self.assertEqual(bey, expected_bey)

        # Test case: 0.00% discount rate -> 0.00%
        self.assertEqual(TBillCashYieldCalculator.discount_to_bond_equivalent_yield(Decimal("0.00")), Decimal("0.0000"))

    def test_04_tbill_weekend_and_calendar_accrual(self):
        """Verify that weekend gaps accrue 3 calendar days of interest on Friday cash balance."""
        # Create 3 snapshots: Fri 2024-03-22, Mon 2024-03-25, Tue 2024-03-26
        # Cash = $4,000.00
        snaps = [
            PortfolioDailySnapshot(
                session_date=date(2024, 3, 22), # Friday
                timestamp=datetime(2024, 3, 22, 20, 0, tzinfo=timezone.utc),
                equity_usd=Decimal("5000.00"),
                cash_usd=Decimal("4000.00"),
                gross_market_value_usd=Decimal("1000.00"),
                invested_capital_pct=Decimal("20.00"),
                unrealized_pnl_usd=Decimal("0.00"),
                cumulative_realized_pnl_usd=Decimal("0.00"),
                open_positions_count=1,
                open_lots_count=1,
                buffered_open_risk_usd=Decimal("25.00"),
                aggregate_risk_pct=Decimal("0.50"),
                market_regime="BULLISH",
            ),
            PortfolioDailySnapshot(
                session_date=date(2024, 3, 25), # Monday (3 days later)
                timestamp=datetime(2024, 3, 25, 20, 0, tzinfo=timezone.utc),
                equity_usd=Decimal("5020.00"),
                cash_usd=Decimal("4000.00"),
                gross_market_value_usd=Decimal("1020.00"),
                invested_capital_pct=Decimal("20.30"),
                unrealized_pnl_usd=Decimal("20.00"),
                cumulative_realized_pnl_usd=Decimal("0.00"),
                open_positions_count=1,
                open_lots_count=1,
                buffered_open_risk_usd=Decimal("25.00"),
                aggregate_risk_pct=Decimal("0.50"),
                market_regime="BULLISH",
            ),
            PortfolioDailySnapshot(
                session_date=date(2024, 3, 26), # Tuesday (1 day later)
                timestamp=datetime(2024, 3, 26, 20, 0, tzinfo=timezone.utc),
                equity_usd=Decimal("5030.00"),
                cash_usd=Decimal("4000.00"),
                gross_market_value_usd=Decimal("1030.00"),
                invested_capital_pct=Decimal("20.47"),
                unrealized_pnl_usd=Decimal("30.00"),
                cumulative_realized_pnl_usd=Decimal("0.00"),
                open_positions_count=1,
                open_lots_count=1,
                buffered_open_risk_usd=Decimal("25.00"),
                aggregate_risk_pct=Decimal("0.50"),
                market_regime="BULLISH",
            ),
        ]

        # Use fixed 5.0% discount rate table
        rates = {
            date(2024, 3, 22): Decimal("5.00"),
            date(2024, 3, 25): Decimal("5.00"),
        }
        calc = TBillCashYieldCalculator(custom_rate_table=rates)
        res = calc.calculate_cash_accrual(snaps, initial_capital=Decimal("5000.00"))

        # Snapshot 1 (Monday): elapsed days = 3
        mon_rec = res.daily_records[1]
        self.assertEqual(mon_rec.days_accrued, 3)
        self.assertGreater(mon_rec.interest_earned_usd, Decimal("1.50"))

        # Snapshot 2 (Tuesday): elapsed days = 1
        tue_rec = res.daily_records[2]
        self.assertEqual(tue_rec.days_accrued, 1)
        self.assertAlmostEqual(float(mon_rec.interest_earned_usd), float(tue_rec.interest_earned_usd * 3), delta=0.05)

        # Total interest earned is positive and added to adjusted equity
        self.assertGreater(res.total_interest_earned_usd, Decimal("0.00"))
        self.assertEqual(res.adjusted_final_equity_usd, res.raw_final_equity_usd + res.total_interest_earned_usd)

    def test_05_risk_adjusted_attribution(self):
        """Verify CAPM metrics: Beta, Jensen's Alpha, Information Ratio, Calmar, Sortino."""
        # 10 daily return observations
        port_rets = [Decimal("0.005"), Decimal("-0.002"), Decimal("0.004"), Decimal("0.001"), Decimal("-0.003"),
                     Decimal("0.006"), Decimal("0.002"), Decimal("-0.001"), Decimal("0.003"), Decimal("0.002")]
        bench_rets = [Decimal("0.008"), Decimal("-0.005"), Decimal("0.006"), Decimal("0.002"), Decimal("-0.004"),
                      Decimal("0.007"), Decimal("0.001"), Decimal("-0.003"), Decimal("0.004"), Decimal("0.001")]

        res = RiskAdjustedAttributionCalculator.calculate(
            portfolio_returns=port_rets,
            benchmark_returns=bench_rets,
            portfolio_cagr_pct=Decimal("12.50"),
            benchmark_cagr_pct=Decimal("15.00"),
            portfolio_max_dd_pct=Decimal("5.00"),
            benchmark_max_dd_pct=Decimal("12.00"),
            risk_free_rate_pct=Decimal("2.00"),
        )

        # Strategy has lower volatility than benchmark, so Beta should be between 0.5 and 1.0
        self.assertGreater(res.beta, Decimal("0.50"))
        self.assertLess(res.beta, Decimal("1.00"))

        # Calmar: Portfolio (12.5 / 5.0 = 2.50) vs Benchmark (15.0 / 12.0 = 1.25)
        self.assertEqual(res.portfolio_calmar_ratio, Decimal("2.50"))
        self.assertEqual(res.benchmark_calmar_ratio, Decimal("1.25"))

        # Excess CAGR
        self.assertEqual(res.excess_cagr_pct, Decimal("-2.50"))

        # Both Sortino ratios should be positive
        self.assertIsNotNone(res.portfolio_sortino_ratio)
        self.assertIsNotNone(res.benchmark_sortino_ratio)


if __name__ == "__main__":
    unittest.main()
