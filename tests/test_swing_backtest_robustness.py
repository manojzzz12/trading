"""Unit tests for Phase 5.8 Robustness, Out-of-Sample Partitioning, and Reporting in trader-swing.

Enforces:
1. In-sample and out-of-sample date partitioning without look-ahead or overlap.
2. Sensitivity analysis across execution cost scenarios.
3. Isolated report persistence to dedicated reports directory.
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SWING_DIR = os.path.join(ROOT_DIR, ".agents", "skills", "trader-swing")
for path in (ROOT_DIR, SWING_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

from backtest_data import HistoricalDataManager
from backtest_models import BacktestConfig
from backtest_report import BacktestReportGenerator
from backtest_validation import BacktestValidationSuite
from market_data_models import CandleStatus, MarketClock, OHLCVCandle, UTC

_ROBUSTNESS_TEST_TRADING_DAYS = []
_curr_d = date(2026, 1, 5)
while len(_ROBUSTNESS_TEST_TRADING_DAYS) < 100:
    if MarketClock.get_session(_curr_d):
        _ROBUSTNESS_TEST_TRADING_DAYS.append(_curr_d)
    _curr_d += timedelta(days=1)


def make_candle(ticker: str, dt: datetime, close_p: str) -> OHLCVCandle:
    c_dec = Decimal(close_p)
    return OHLCVCandle(
        ticker=ticker,
        interval="1d",
        timestamp=dt,
        open=c_dec,
        high=c_dec + Decimal("2.00"),
        low=c_dec - Decimal("2.00"),
        close=c_dec,
        volume=20000000,
        status=CandleStatus.COMPLETE,
        source="yahoo",
        received_timestamp=dt,
        is_delayed=True,
    )


class TestSwingBacktestRobustness(unittest.TestCase):
    """Test suite covering data partitioning, sensitivity testing, and reporting."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.base_date = datetime(2026, 1, 5, 20, 0, tzinfo=UTC)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_01_in_sample_out_of_sample_partitioning(self):
        """Verify chronological date partitioning prevents look-ahead and overlap."""
        sessions = [date(2026, 1, 5) + timedelta(days=i) for i in range(30)]
        partition = BacktestValidationSuite.partition_date_range(sessions, in_sample_ratio=0.70)

        self.assertEqual(partition.in_sample_start, sessions[0])
        self.assertEqual(partition.in_sample_end, sessions[20])
        self.assertEqual(partition.out_of_sample_start, sessions[21])
        self.assertEqual(partition.out_of_sample_end, sessions[-1])

        # Strictly non-overlapping
        self.assertTrue(partition.out_of_sample_start > partition.in_sample_end)

    def test_02_sensitivity_scenario_execution(self):
        """Verify sensitivity analysis runs across low-cost, base, and conservative models."""
        mgr = HistoricalDataManager()
        for sym in ["SPY", "QQQ", "IWM", "AAPL"]:
            candles = [
                make_candle(
                    sym,
                    datetime.combine(_ROBUSTNESS_TEST_TRADING_DAYS[i], time(20, 0), tzinfo=UTC),
                    "150.00",
                )
                for i in range(25)
            ]
            mgr.load_candles(sym, candles)

        sessions = mgr.get_all_session_dates()
        config = BacktestConfig(
            start_date=sessions[0],
            end_date=sessions[-1],
            universe=["AAPL"],
            benchmark_ticker="SPY",
        )

        scenarios = BacktestValidationSuite.run_sensitivity_scenarios(config, mgr)
        self.assertIn("LOW_COST", scenarios)
        self.assertIn("BASE_CASE", scenarios)
        self.assertIn("CONSERVATIVE", scenarios)

    def test_03_report_generation_and_persistence(self):
        """Verify reports write to dedicated directory and serialize correctly."""
        gen = BacktestReportGenerator(output_dir=self.temp_dir)
        sample_results = {
            "portfolio_metrics": {"total_return_pct": "12.50", "final_equity_usd": "5625.00"},
            "trade_metrics": {"total_trades": 5, "win_rate_pct": "80.00"},
        }
        json_path = gen.save_json_report(sample_results, filename="test_report.json")
        self.assertTrue(os.path.exists(json_path))

        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["portfolio_metrics"]["total_return_pct"], "12.50")

        # Test CSV export
        sample_trades = [
            {
                "trade_id": "TRD-1",
                "ticker": "AAPL",
                "lot_id": "LOT-1",
                "entry_timestamp": "2026-01-05T20:00:00",
                "exit_timestamp": "2026-01-08T20:00:00",
                "holding_sessions": 3,
                "shares": 10,
                "entry_price": "150.00",
                "exit_price": "165.00",
                "initial_stop_price": "142.50",
                "initial_target_price": "165.00",
                "gross_pnl_usd": "150.00",
                "total_fees_usd": "2.00",
                "total_slippage_usd": "1.50",
                "net_pnl_usd": "146.50",
                "return_pct": "10.00",
                "r_multiple": "2.00",
                "exit_reason": "Target",
            }
        ]
        csv_path = gen.save_trades_csv(sample_trades, filename="test_trades.csv")
        self.assertTrue(os.path.exists(csv_path))


if __name__ == "__main__":
    unittest.main()
