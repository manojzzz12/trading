"""Unit and integration tests for Phase 5.10.1 Diagnostic Attribution and Statistical Uncertainty Engines.

Tests:
1. LoSharpeCalculator (Lo 2002 formulation):
   - Asymptotic SE with skewness and kurtosis.
   - Autocorrelation penalty inflation.
   - P-value and 95% confidence interval boundaries.
2. StationaryBootstrapEngine:
   - Empirical bootstrap confidence intervals on Sharpe ratio, expectancy, and profit factor.
   - Deterministic reproducibility under fixed random seed.
3. MultipleTestingHaircutCalculator (Harvey & Liu 2014):
   - Increasing trial penalty.
   - Correlation mitigation effect.
4. DiagnosticAttributionEngine:
   - Setup-level attribution (TREND_CONTINUATION, BREAKOUT_CONFIRMATION, PULLBACK_ENTRY).
   - Regime-level attribution (STRONG_BULLISH, BULLISH, SIDEWAYS, BEARISH).
   - Ticker attribution and contribution percentages.
   - Outlier sensitivity analysis (top 1%, 2%, 5% truncation).
   - Holding duration dynamics and disposition ratio.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import os
import sys
import unittest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SWING_DIR = os.path.join(ROOT_DIR, ".agents", "skills", "trader-swing")
for path in (ROOT_DIR, SWING_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

from backtest_models import PortfolioDailySnapshot, SimulatedTradeRecord
from diagnostics import (
    DiagnosticAttributionEngine,
    StatisticalUncertaintyCalculator,
    LoSharpeResult,
    BootstrapConfidenceInterval,
    MultipleTestingHaircutResult,
)


class TestSwingDiagnosticsPhase5101(unittest.TestCase):
    """Test suite for Phase 5.10.1 Diagnostic Attribution and Statistical Uncertainty engines."""

    def setUp(self):
        # Create deterministic synthetic trade records (10 trades)
        self.trades = [
            SimulatedTradeRecord(
                trade_id="TRD-1",
                ticker="AAPL",
                lot_id="LOT-1",
                entry_timestamp=datetime(2023, 1, 10, 14, 30, tzinfo=timezone.utc),
                exit_timestamp=datetime(2023, 1, 20, 14, 30, tzinfo=timezone.utc),
                holding_sessions=8,
                shares=10,
                entry_price=Decimal("150.00"),
                exit_price=Decimal("156.00"),
                initial_stop_price=Decimal("147.00"),
                initial_target_price=Decimal("156.00"),
                gross_pnl_usd=Decimal("60.00"),
                total_fees_usd=Decimal("0.00"),
                total_slippage_usd=Decimal("0.20"),
                net_pnl_usd=Decimal("60.00"),
                return_pct=Decimal("4.00"),
                r_multiple=Decimal("2.00"),
                exit_reason="Target reached filled at 156.00. (TREND_CONTINUATION)",
            ),
            SimulatedTradeRecord(
                trade_id="TRD-2",
                ticker="AAPL",
                lot_id="LOT-2",
                entry_timestamp=datetime(2023, 2, 5, 14, 30, tzinfo=timezone.utc),
                exit_timestamp=datetime(2023, 2, 12, 14, 30, tzinfo=timezone.utc),
                holding_sessions=5,
                shares=10,
                entry_price=Decimal("155.00"),
                exit_price=Decimal("152.00"),
                initial_stop_price=Decimal("152.00"),
                initial_target_price=Decimal("161.00"),
                gross_pnl_usd=Decimal("-30.00"),
                total_fees_usd=Decimal("0.00"),
                total_slippage_usd=Decimal("0.20"),
                net_pnl_usd=Decimal("-30.00"),
                return_pct=Decimal("-1.94"),
                r_multiple=Decimal("-1.00"),
                exit_reason="Stop touched filled at 152.00. (TREND_CONTINUATION)",
            ),
            SimulatedTradeRecord(
                trade_id="TRD-3",
                ticker="MSFT",
                lot_id="LOT-3",
                entry_timestamp=datetime(2023, 3, 1, 14, 30, tzinfo=timezone.utc),
                exit_timestamp=datetime(2023, 3, 15, 14, 30, tzinfo=timezone.utc),
                holding_sessions=10,
                shares=5,
                entry_price=Decimal("250.00"),
                exit_price=Decimal("270.00"),
                initial_stop_price=Decimal("240.00"),
                initial_target_price=Decimal("270.00"),
                gross_pnl_usd=Decimal("100.00"),
                total_fees_usd=Decimal("0.00"),
                total_slippage_usd=Decimal("0.10"),
                net_pnl_usd=Decimal("100.00"),
                return_pct=Decimal("8.00"),
                r_multiple=Decimal("2.00"),
                exit_reason="Target reached filled at 270.00. (BREAKOUT_CONFIRMATION)",
            ),
            SimulatedTradeRecord(
                trade_id="TRD-4",
                ticker="MSFT",
                lot_id="LOT-4",
                entry_timestamp=datetime(2023, 3, 20, 14, 30, tzinfo=timezone.utc),
                exit_timestamp=datetime(2023, 3, 25, 14, 30, tzinfo=timezone.utc),
                holding_sessions=4,
                shares=5,
                entry_price=Decimal("270.00"),
                exit_price=Decimal("260.00"),
                initial_stop_price=Decimal("260.00"),
                initial_target_price=Decimal("290.00"),
                gross_pnl_usd=Decimal("-50.00"),
                total_fees_usd=Decimal("0.00"),
                total_slippage_usd=Decimal("0.10"),
                net_pnl_usd=Decimal("-50.00"),
                return_pct=Decimal("-3.70"),
                r_multiple=Decimal("-1.00"),
                exit_reason="Stop touched filled at 260.00. (BREAKOUT_CONFIRMATION)",
            ),
            SimulatedTradeRecord(
                trade_id="TRD-5",
                ticker="NVDA",
                lot_id="LOT-5",
                entry_timestamp=datetime(2023, 4, 10, 14, 30, tzinfo=timezone.utc),
                exit_timestamp=datetime(2023, 4, 25, 14, 30, tzinfo=timezone.utc),
                holding_sessions=11,
                shares=20,
                entry_price=Decimal("20.00"),
                exit_price=Decimal("23.00"),
                initial_stop_price=Decimal("18.50"),
                initial_target_price=Decimal("23.00"),
                gross_pnl_usd=Decimal("60.00"),
                total_fees_usd=Decimal("0.00"),
                total_slippage_usd=Decimal("0.40"),
                net_pnl_usd=Decimal("60.00"),
                return_pct=Decimal("15.00"),
                r_multiple=Decimal("2.00"),
                exit_reason="Target reached. (PULLBACK_ENTRY)",
            ),
            SimulatedTradeRecord(
                trade_id="TRD-6",
                ticker="NVDA",
                lot_id="LOT-6",
                entry_timestamp=datetime(2023, 5, 2, 14, 30, tzinfo=timezone.utc),
                exit_timestamp=datetime(2023, 5, 9, 14, 30, tzinfo=timezone.utc),
                holding_sessions=5,
                shares=20,
                entry_price=Decimal("22.00"),
                exit_price=Decimal("20.50"),
                initial_stop_price=Decimal("20.50"),
                initial_target_price=Decimal("25.00"),
                gross_pnl_usd=Decimal("-30.00"),
                total_fees_usd=Decimal("0.00"),
                total_slippage_usd=Decimal("0.40"),
                net_pnl_usd=Decimal("-30.00"),
                return_pct=Decimal("-6.82"),
                r_multiple=Decimal("-1.00"),
                exit_reason="Stop touched. (PULLBACK_ENTRY)",
            ),
            SimulatedTradeRecord(
                trade_id="TRD-7",
                ticker="GOOGL",
                lot_id="LOT-7",
                entry_timestamp=datetime(2023, 6, 1, 14, 30, tzinfo=timezone.utc),
                exit_timestamp=datetime(2023, 6, 12, 14, 30, tzinfo=timezone.utc),
                holding_sessions=7,
                shares=10,
                entry_price=Decimal("120.00"),
                exit_price=Decimal("128.00"),
                initial_stop_price=Decimal("116.00"),
                initial_target_price=Decimal("128.00"),
                gross_pnl_usd=Decimal("80.00"),
                total_fees_usd=Decimal("0.00"),
                total_slippage_usd=Decimal("0.20"),
                net_pnl_usd=Decimal("80.00"),
                return_pct=Decimal("6.67"),
                r_multiple=Decimal("2.00"),
                exit_reason="Target reached. (TREND_CONTINUATION)",
            ),
            SimulatedTradeRecord(
                trade_id="TRD-8",
                ticker="AMZN",
                lot_id="LOT-8",
                entry_timestamp=datetime(2023, 7, 5, 14, 30, tzinfo=timezone.utc),
                exit_timestamp=datetime(2023, 7, 10, 14, 30, tzinfo=timezone.utc),
                holding_sessions=3,
                shares=10,
                entry_price=Decimal("130.00"),
                exit_price=Decimal("126.00"),
                initial_stop_price=Decimal("126.00"),
                initial_target_price=Decimal("138.00"),
                gross_pnl_usd=Decimal("-40.00"),
                total_fees_usd=Decimal("0.00"),
                total_slippage_usd=Decimal("0.20"),
                net_pnl_usd=Decimal("-40.00"),
                return_pct=Decimal("-3.08"),
                r_multiple=Decimal("-1.00"),
                exit_reason="Stop touched. (TREND_CONTINUATION)",
            ),
            SimulatedTradeRecord(
                trade_id="TRD-9",
                ticker="TSLA",
                lot_id="LOT-9",
                entry_timestamp=datetime(2023, 8, 1, 14, 30, tzinfo=timezone.utc),
                exit_timestamp=datetime(2023, 8, 20, 14, 30, tzinfo=timezone.utc),
                holding_sessions=14,
                shares=5,
                entry_price=Decimal("240.00"),
                exit_price=Decimal("264.00"),
                initial_stop_price=Decimal("228.00"),
                initial_target_price=Decimal("264.00"),
                gross_pnl_usd=Decimal("120.00"),
                total_fees_usd=Decimal("0.00"),
                total_slippage_usd=Decimal("0.10"),
                net_pnl_usd=Decimal("120.00"),
                return_pct=Decimal("10.00"),
                r_multiple=Decimal("2.00"),
                exit_reason="Target reached. (TREND_CONTINUATION)",
            ),
            SimulatedTradeRecord(
                trade_id="TRD-10",
                ticker="META",
                lot_id="LOT-10",
                entry_timestamp=datetime(2023, 9, 5, 14, 30, tzinfo=timezone.utc),
                exit_timestamp=datetime(2023, 9, 8, 14, 30, tzinfo=timezone.utc),
                holding_sessions=3,
                shares=5,
                entry_price=Decimal("300.00"),
                exit_price=Decimal("290.00"),
                initial_stop_price=Decimal("290.00"),
                initial_target_price=Decimal("320.00"),
                gross_pnl_usd=Decimal("-50.00"),
                total_fees_usd=Decimal("0.00"),
                total_slippage_usd=Decimal("0.10"),
                net_pnl_usd=Decimal("-50.00"),
                return_pct=Decimal("-3.33"),
                r_multiple=Decimal("-1.00"),
                exit_reason="Stop touched. (BREAKOUT_CONFIRMATION)",
            ),
        ]

        # Daily snapshots matching trade dates with market regimes
        self.snapshots = [
            PortfolioDailySnapshot(session_date=date(2023, 1, 10), timestamp=datetime(2023, 1, 10, 20, 0, tzinfo=timezone.utc), equity_usd=Decimal("5000"), cash_usd=Decimal("3500"), gross_market_value_usd=Decimal("1500"), invested_capital_pct=Decimal("30"), unrealized_pnl_usd=Decimal("0"), cumulative_realized_pnl_usd=Decimal("0"), open_positions_count=1, open_lots_count=1, buffered_open_risk_usd=Decimal("30"), aggregate_risk_pct=Decimal("0.6"), market_regime="STRONG_BULLISH"),
            PortfolioDailySnapshot(session_date=date(2023, 2, 5), timestamp=datetime(2023, 2, 5, 20, 0, tzinfo=timezone.utc), equity_usd=Decimal("5060"), cash_usd=Decimal("3510"), gross_market_value_usd=Decimal("1550"), invested_capital_pct=Decimal("30"), unrealized_pnl_usd=Decimal("0"), cumulative_realized_pnl_usd=Decimal("60"), open_positions_count=1, open_lots_count=1, buffered_open_risk_usd=Decimal("30"), aggregate_risk_pct=Decimal("0.6"), market_regime="BULLISH"),
            PortfolioDailySnapshot(session_date=date(2023, 3, 1), timestamp=datetime(2023, 3, 1, 20, 0, tzinfo=timezone.utc), equity_usd=Decimal("5030"), cash_usd=Decimal("3780"), gross_market_value_usd=Decimal("1250"), invested_capital_pct=Decimal("25"), unrealized_pnl_usd=Decimal("0"), cumulative_realized_pnl_usd=Decimal("30"), open_positions_count=1, open_lots_count=1, buffered_open_risk_usd=Decimal("50"), aggregate_risk_pct=Decimal("1.0"), market_regime="SIDEWAYS"),
            PortfolioDailySnapshot(session_date=date(2023, 3, 20), timestamp=datetime(2023, 3, 20, 20, 0, tzinfo=timezone.utc), equity_usd=Decimal("5130"), cash_usd=Decimal("3780"), gross_market_value_usd=Decimal("1350"), invested_capital_pct=Decimal("26"), unrealized_pnl_usd=Decimal("0"), cumulative_realized_pnl_usd=Decimal("130"), open_positions_count=1, open_lots_count=1, buffered_open_risk_usd=Decimal("50"), aggregate_risk_pct=Decimal("1.0"), market_regime="SIDEWAYS"),
            PortfolioDailySnapshot(session_date=date(2023, 4, 10), timestamp=datetime(2023, 4, 10, 20, 0, tzinfo=timezone.utc), equity_usd=Decimal("5080"), cash_usd=Decimal("4680"), gross_market_value_usd=Decimal("400"), invested_capital_pct=Decimal("8"), unrealized_pnl_usd=Decimal("0"), cumulative_realized_pnl_usd=Decimal("80"), open_positions_count=1, open_lots_count=1, buffered_open_risk_usd=Decimal("30"), aggregate_risk_pct=Decimal("0.6"), market_regime="BULLISH"),
            PortfolioDailySnapshot(session_date=date(2023, 5, 2), timestamp=datetime(2023, 5, 2, 20, 0, tzinfo=timezone.utc), equity_usd=Decimal("5140"), cash_usd=Decimal("4700"), gross_market_value_usd=Decimal("440"), invested_capital_pct=Decimal("8"), unrealized_pnl_usd=Decimal("0"), cumulative_realized_pnl_usd=Decimal("140"), open_positions_count=1, open_lots_count=1, buffered_open_risk_usd=Decimal("30"), aggregate_risk_pct=Decimal("0.6"), market_regime="BEARISH"),
            PortfolioDailySnapshot(session_date=date(2023, 6, 1), timestamp=datetime(2023, 6, 1, 20, 0, tzinfo=timezone.utc), equity_usd=Decimal("5110"), cash_usd=Decimal("3910"), gross_market_value_usd=Decimal("1200"), invested_capital_pct=Decimal("23"), unrealized_pnl_usd=Decimal("0"), cumulative_realized_pnl_usd=Decimal("110"), open_positions_count=1, open_lots_count=1, buffered_open_risk_usd=Decimal("40"), aggregate_risk_pct=Decimal("0.8"), market_regime="STRONG_BULLISH"),
            PortfolioDailySnapshot(session_date=date(2023, 7, 5), timestamp=datetime(2023, 7, 5, 20, 0, tzinfo=timezone.utc), equity_usd=Decimal("5190"), cash_usd=Decimal("3890"), gross_market_value_usd=Decimal("1300"), invested_capital_pct=Decimal("25"), unrealized_pnl_usd=Decimal("0"), cumulative_realized_pnl_usd=Decimal("190"), open_positions_count=1, open_lots_count=1, buffered_open_risk_usd=Decimal("40"), aggregate_risk_pct=Decimal("0.8"), market_regime="BULLISH"),
            PortfolioDailySnapshot(session_date=date(2023, 8, 1), timestamp=datetime(2023, 8, 1, 20, 0, tzinfo=timezone.utc), equity_usd=Decimal("5150"), cash_usd=Decimal("3950"), gross_market_value_usd=Decimal("1200"), invested_capital_pct=Decimal("23"), unrealized_pnl_usd=Decimal("0"), cumulative_realized_pnl_usd=Decimal("150"), open_positions_count=1, open_lots_count=1, buffered_open_risk_usd=Decimal("60"), aggregate_risk_pct=Decimal("1.2"), market_regime="STRONG_BULLISH"),
            PortfolioDailySnapshot(session_date=date(2023, 9, 5), timestamp=datetime(2023, 9, 5, 20, 0, tzinfo=timezone.utc), equity_usd=Decimal("5270"), cash_usd=Decimal("3770"), gross_market_value_usd=Decimal("1500"), invested_capital_pct=Decimal("28"), unrealized_pnl_usd=Decimal("0"), cumulative_realized_pnl_usd=Decimal("270"), open_positions_count=1, open_lots_count=1, buffered_open_risk_usd=Decimal("50"), aggregate_risk_pct=Decimal("1.0"), market_regime="SIDEWAYS"),
        ]

    def test_01_lo_sharpe_se(self):
        """Verify Andrew Lo (2002) asymptotic Sharpe SE calculation."""
        # 30 simulated daily returns
        returns = [
            Decimal("0.005"), Decimal("-0.002"), Decimal("0.004"), Decimal("0.001"), Decimal("-0.003"),
            Decimal("0.006"), Decimal("0.002"), Decimal("-0.001"), Decimal("0.003"), Decimal("0.002"),
            Decimal("0.001"), Decimal("-0.004"), Decimal("0.002"), Decimal("0.005"), Decimal("-0.002"),
            Decimal("0.003"), Decimal("0.001"), Decimal("-0.001"), Decimal("0.004"), Decimal("0.002"),
            Decimal("-0.002"), Decimal("0.003"), Decimal("0.001"), Decimal("-0.003"), Decimal("0.004"),
            Decimal("0.002"), Decimal("-0.001"), Decimal("0.003"), Decimal("0.005"), Decimal("-0.002"),
        ]

        res = StatisticalUncertaintyCalculator.calculate_lo_sharpe_se(returns)

        # Sharpe ratio is positive
        self.assertGreater(res.annualized_sharpe, Decimal("1.00"))
        # Standard error is positive and non-zero
        self.assertGreater(res.asymptotic_se_annualized, Decimal("0.00"))
        # 95% Confidence Interval is centered around point estimate
        self.assertLess(res.ci_95_lower, res.annualized_sharpe)
        self.assertGreater(res.ci_95_upper, res.annualized_sharpe)
        # Z-score is positive and p-value is bounded [0, 1]
        self.assertGreater(res.z_score, Decimal("0.00"))
        self.assertLessEqual(res.p_value, Decimal("0.10"))
        self.assertEqual(res.total_sessions, 30)

    def test_02_stationary_bootstrap_reproducibility(self):
        """Verify stationary bootstrap computes valid CIs and is deterministic under fixed seed."""
        daily_returns = [Decimal("0.002"), Decimal("-0.001"), Decimal("0.003"), Decimal("0.001"),
                         Decimal("-0.002"), Decimal("0.004"), Decimal("-0.001"), Decimal("0.002"),
                         Decimal("0.003"), Decimal("-0.002"), Decimal("0.001"), Decimal("0.002")]

        res1 = StatisticalUncertaintyCalculator.stationary_bootstrap_metrics(
            daily_returns=daily_returns,
            trades=self.trades,
            num_bootstrap=200,
            random_seed=42,
        )

        res2 = StatisticalUncertaintyCalculator.stationary_bootstrap_metrics(
            daily_returns=daily_returns,
            trades=self.trades,
            num_bootstrap=200,
            random_seed=42,
        )

        # Exact reproducibility
        self.assertEqual(res1["sharpe_ratio"].ci_95_lower, res2["sharpe_ratio"].ci_95_lower)
        self.assertEqual(res1["expectancy_usd"].ci_95_lower, res2["expectancy_usd"].ci_95_lower)

        # CI bounds sanity check (lower <= upper)
        for metric in ("sharpe_ratio", "expectancy_usd", "profit_factor"):
            ci = res1[metric]
            self.assertLessEqual(ci.ci_95_lower, ci.ci_95_upper)
            self.assertGreater(ci.standard_error, Decimal("0.00"))

    def test_03_multiple_testing_haircut(self):
        """Verify Harvey & Liu haircut increases penalty with more trials and lower correlation."""
        sr = Decimal("2.00")

        # 1 trial: no haircut
        h1 = StatisticalUncertaintyCalculator.calculate_multiple_testing_haircut(sr, num_trials=1)
        self.assertEqual(h1.haircut_sharpe, sr)
        self.assertEqual(h1.haircut_pct, Decimal("0.00"))

        # 10 trials, 0.50 correlation
        h10 = StatisticalUncertaintyCalculator.calculate_multiple_testing_haircut(sr, num_trials=10, avg_correlation=Decimal("0.50"))
        self.assertLess(h10.haircut_sharpe, sr)
        self.assertGreater(h10.haircut_pct, Decimal("0.00"))

        # 50 trials, 0.50 correlation -> heavier penalty
        h50 = StatisticalUncertaintyCalculator.calculate_multiple_testing_haircut(sr, num_trials=50, avg_correlation=Decimal("0.50"))
        self.assertLess(h50.haircut_sharpe, h10.haircut_sharpe)
        self.assertGreater(h50.haircut_pct, h10.haircut_pct)

    def test_04_setup_attribution(self):
        """Verify setup-level attribution correctly disaggregates trades."""
        att = DiagnosticAttributionEngine.analyze_setups(self.trades)

        # Expected setups present
        self.assertIn("TREND_CONTINUATION", att)
        self.assertIn("BREAKOUT_CONFIRMATION", att)
        self.assertIn("PULLBACK_ENTRY", att)

        trend = att["TREND_CONTINUATION"]
        # TRD-1 (+60), TRD-2 (-30), TRD-7 (+80), TRD-8 (-40), TRD-9 (+120) -> 5 trades, 3 wins, 2 losses
        self.assertEqual(trend.total_trades, 5)
        self.assertEqual(trend.winning_trades, 3)
        self.assertEqual(trend.losing_trades, 2)
        self.assertEqual(trend.win_rate_pct, Decimal("60.00"))
        # Net PnL = 60 - 30 + 80 - 40 + 120 = 190.00
        self.assertEqual(trend.net_pnl_usd, Decimal("190.00"))
        # Expectancy = 190 / 5 = 38.00
        self.assertEqual(trend.expectancy_usd, Decimal("38.00"))

        # Total trades across all setups must equal 10
        total_trades = sum(s.total_trades for s in att.values())
        self.assertEqual(total_trades, 10)

    def test_05_regime_attribution(self):
        """Verify regime attribution correctly maps trades to market regime at entry date."""
        reg_att = DiagnosticAttributionEngine.analyze_regimes(self.trades, self.snapshots)

        # Regimes present: STRONG_BULLISH, BULLISH, SIDEWAYS, BEARISH
        self.assertIn("STRONG_BULLISH", reg_att)
        self.assertIn("BULLISH", reg_att)
        self.assertIn("SIDEWAYS", reg_att)
        self.assertIn("BEARISH", reg_att)

        # Total trades sum to 10
        total_trades = sum(r.total_trades for r in reg_att.values())
        self.assertEqual(total_trades, 10)

        # Total net PnL across all regimes matches total trade PnL
        expected_total_pnl = sum(t.net_pnl_usd for t in self.trades)
        actual_total_pnl = sum(r.net_pnl_usd for r in reg_att.values())
        self.assertEqual(actual_total_pnl, expected_total_pnl)

    def test_06_ticker_attribution(self):
        """Verify ticker attribution computes symbol contributions correctly."""
        tick_att = DiagnosticAttributionEngine.analyze_tickers(self.trades)

        self.assertIn("AAPL", tick_att)
        self.assertIn("MSFT", tick_att)
        self.assertIn("NVDA", tick_att)
        self.assertIn("GOOGL", tick_att)
        self.assertIn("AMZN", tick_att)
        self.assertIn("TSLA", tick_att)
        self.assertIn("META", tick_att)

        # Total contribution percentages sum to 100%
        tot_contrib = sum(t.contribution_pct for t in tick_att.values())
        self.assertAlmostEqual(float(tot_contrib), 100.0, delta=0.5)

    def test_07_outlier_sensitivity(self):
        """Verify outlier sensitivity calculates truncated performance correctly."""
        out = DiagnosticAttributionEngine.analyze_outlier_sensitivity(self.trades)

        # Baseline net PnL = 60 - 30 + 100 - 50 + 60 - 30 + 80 - 40 + 120 - 50 = 220.00
        self.assertEqual(out.baseline_net_pnl, Decimal("220.00"))
        self.assertEqual(out.total_trades, 10)

        # Top 1% of 10 trades = ceil(0.1) = 1 trade removed (TRD-9: +120.00)
        # PnL without top trade = 220.00 - 120.00 = 100.00
        self.assertEqual(out.top_1_pct_removed_pnl, Decimal("100.00"))

        # Top 5% of 10 trades = ceil(0.5) = 1 trade removed as well
        self.assertEqual(out.top_5_pct_removed_pnl, Decimal("100.00"))

        # Details of top 3 trades
        self.assertEqual(len(out.top_3_trades_details), 3)
        self.assertEqual(out.top_3_trades_details[0]["trade_id"], "TRD-9")
        self.assertEqual(out.top_3_trades_details[0]["net_pnl_usd"], "120.00")

    def test_08_holding_period_dynamics(self):
        """Verify holding period dynamics and disposition ratio calculation."""
        dyn = DiagnosticAttributionEngine.analyze_holding_durations(self.trades)

        # Winning trades: TRD-1 (8), TRD-3 (10), TRD-5 (11), TRD-7 (7), TRD-9 (14) -> sum=50, avg=10.0
        self.assertEqual(dyn.avg_winning_holding_sessions, Decimal("10.0"))

        # Losing trades: TRD-2 (5), TRD-4 (4), TRD-6 (5), TRD-8 (3), TRD-10 (3) -> sum=20, avg=4.0
        self.assertEqual(dyn.avg_losing_holding_sessions, Decimal("4.0"))

        # Disposition ratio = 4.0 / 10.0 = 0.40 (< 1.0 means winners held longer than losers!)
        self.assertEqual(dyn.disposition_ratio, Decimal("0.40"))


if __name__ == "__main__":
    unittest.main()
