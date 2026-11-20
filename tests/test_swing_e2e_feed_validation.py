# ==============================================================================
# tests/test_swing_e2e_feed_validation.py
# Phase 6.2B — Step 5: Read-Only End-to-End Validation Test Suite
#
# Validates the full path:
# Historical CSV -> OHLCV conversion -> YFinance Provider -> Feed Adapter ->
# Readiness Evaluator -> SwingTradingStrategyService.run_analysis_with_feed()
# ==============================================================================

import csv
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

# Ensure skill directory is in sys.path
SKILL_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".agents", "skills", "trader-swing"))
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)

from accounting import AccountingLedger
from feed_adapter import (
    FeedReadinessResult,
    MarketDataFeedAdapter,
    MarketDataReadinessEvaluator,
    ReadinessStatus,
    StockExclusion,
)
from market_data_models import (
    CandleStatus,
    MarketClock,
    NoDataError,
    OHLCVCandle,
    ProviderUnavailableError,
    RateLimitError,
)
from pipeline import IntegratedStrategyAnalysisReport, SwingTradingStrategyService
from providers.yfinance_provider import YFinanceMarketDataProvider
from regime_models import MarketRegime


def _get_input_dir() -> str:
    temp_dir = os.environ.get("TEMP", "")
    return os.path.join(temp_dir, "trader_swing_inputs")


def csv_downloader(ticker: str, start=None, end=None, **kwargs):
    sym = ticker.upper().strip()
    input_dir = _get_input_dir()
    fpath = os.path.join(input_dir, f"{sym}_daily.csv")
    if not os.path.exists(fpath):
        raise NoDataError(f"Historical CSV file not found for ticker {sym} at {fpath}.")

    bars = []
    with open(fpath, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            bars.append({
                "date": r["Date"],
                "open": r["Open"],
                "high": r["High"],
                "low": r["Low"],
                "close": r["Close"],
                "volume": r["Volume"],
                "splits": 0.0,
                "dividends": 0.0,
            })
    return bars


class TestSwingE2EFeedValidation(unittest.TestCase):
    """End-to-End Read-Only Validation of Feed Adapter & Pipeline Integration."""

    @classmethod
    def setUpClass(cls):
        cls.input_dir = _get_input_dir()
        cls.required_tickers = ["AAPL", "SPY", "QQQ", "IWM"]
        # Evaluation timestamp: Friday 2026-11-06 post-close (22:00:00 UTC = 17:00:00 EST)
        cls.eval_time = datetime(2026, 11, 6, 22, 0, 0, tzinfo=timezone.utc)

    def test_01_historical_csv_files_exist_and_geometry_valid(self):
        """Input validation: Check required OHLCV columns, geometry, ordering, and date ranges."""
        self.assertTrue(
            os.path.exists(self.input_dir),
            f"Expected input directory does not exist: {self.input_dir}"
        )

        expected_cols = {"Date", "Open", "High", "Low", "Close", "Volume"}

        for sym in self.required_tickers:
            fname = f"{sym}_daily.csv"
            fpath = os.path.join(self.input_dir, fname)
            self.assertTrue(os.path.exists(fpath), f"Missing input file: {fpath}")

            with open(fpath, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                fields = set(reader.fieldnames or [])
                self.assertTrue(
                    expected_cols.issubset(fields),
                    f"{sym} missing required columns. Found: {fields}"
                )
                rows = list(reader)

            # Row count: 252 trading sessions spanning 1 full year
            self.assertEqual(len(rows), 252, f"{sym} should have exactly 252 bars, found {len(rows)}")

            # Date range: 2025-11-06 to 2026-11-06
            self.assertEqual(rows[0]["Date"], "2025-11-06", f"{sym} first bar date mismatch")
            self.assertEqual(rows[-1]["Date"], "2026-11-06", f"{sym} latest bar date mismatch")

            # Check geometry, chronological monotonicity, and non-negative volume
            prev_d = None
            dates_seen = set()
            for idx, r in enumerate(rows):
                d_str = r["Date"]
                d = date.fromisoformat(d_str)

                # Duplicate date check
                self.assertNotIn(d, dates_seen, f"Duplicate date {d} at row {idx} in {sym}")
                dates_seen.add(d)

                # Chronological ordering check
                if prev_d is not None:
                    self.assertGreater(d, prev_d, f"Out of order date {d} at row {idx} in {sym}")
                prev_d = d

                # Geometry check
                o = Decimal(str(r["Open"]))
                h = Decimal(str(r["High"]))
                l = Decimal(str(r["Low"]))
                c = Decimal(str(r["Close"]))
                v = Decimal(str(r["Volume"]))

                self.assertGreaterEqual(h, l, f"High < Low at row {idx} in {sym}")
                self.assertGreaterEqual(o, l, f"Open < Low at row {idx} in {sym}")
                self.assertLessEqual(o, h, f"Open > High at row {idx} in {sym}")
                self.assertGreaterEqual(c, l, f"Close < Low at row {idx} in {sym}")
                self.assertLessEqual(c, h, f"Close > High at row {idx} in {sym}")
                self.assertGreaterEqual(v, 0, f"Volume < 0 at row {idx} in {sym}")

    def test_02_feed_adapter_readiness_with_historical_csvs(self):
        """Feed adapter validation: SPY >= 200 bars, proxies, AAPL >= 50 bars, session alignment."""
        provider = YFinanceMarketDataProvider(
            yf_download_fn=csv_downloader,
            cache_db_path=":memory:",
        )
        adapter = MarketDataFeedAdapter(provider=provider)

        result = adapter.fetch_and_evaluate(
            stock_universe=["AAPL"],
            current_time=self.eval_time,
        )

        self.assertTrue(result.is_ready, f"Feed readiness failed: {result.failure_reasons}")
        self.assertEqual(result.status, ReadinessStatus.READY)
        self.assertEqual(result.latest_completed_session, date(2026, 11, 6))

        # Check proxy summaries
        self.assertIn("SPY", result.ticker_summaries)
        self.assertEqual(result.ticker_summaries["SPY"].bar_count, 252)
        self.assertGreaterEqual(result.ticker_summaries["SPY"].bar_count, 200)
        self.assertEqual(result.ticker_summaries["SPY"].status, "VALIDATED")

        self.assertIn("QQQ", result.ticker_summaries)
        self.assertEqual(result.ticker_summaries["QQQ"].bar_count, 252)
        self.assertEqual(result.ticker_summaries["QQQ"].status, "VALIDATED")

        self.assertIn("IWM", result.ticker_summaries)
        self.assertEqual(result.ticker_summaries["IWM"].bar_count, 252)
        self.assertEqual(result.ticker_summaries["IWM"].status, "VALIDATED")

        # Check candidate summary
        self.assertIn("AAPL", result.ticker_summaries)
        self.assertEqual(result.ticker_summaries["AAPL"].bar_count, 252)
        self.assertGreaterEqual(result.ticker_summaries["AAPL"].bar_count, 50)
        self.assertEqual(result.ticker_summaries["AAPL"].status, "VALIDATED")

        # Included / Excluded tickers
        self.assertEqual(result.included_tickers, ["AAPL"])
        self.assertEqual(len(result.excluded_tickers), 0)
        self.assertEqual(len(result.failure_reasons), 0)

    def test_03_end_to_end_pipeline_integration_with_historical_csvs(self):
        """Pipeline integration: Full execution of run_analysis_with_feed() on historical CSV data."""
        provider = YFinanceMarketDataProvider(
            yf_download_fn=csv_downloader,
            cache_db_path=":memory:",
        )
        adapter = MarketDataFeedAdapter(provider=provider)
        service = SwingTradingStrategyService(feed_adapter=adapter)
        ledger = AccountingLedger(initial_capital="5000.00")

        report = service.run_analysis_with_feed(
            stock_universe=["AAPL"],
            ledger=ledger,
            current_time=self.eval_time,
        )

        # 1. Feed Readiness Verification
        self.assertIsInstance(report, IntegratedStrategyAnalysisReport)
        self.assertTrue(report.readiness.is_ready)
        self.assertEqual(report.readiness.status, ReadinessStatus.READY)
        self.assertEqual(report.readiness.latest_completed_session, date(2026, 11, 6))
        self.assertEqual(report.readiness.included_tickers, ["AAPL"])

        # 2. Strategy Report Verification
        strat = report.strategy_report
        self.assertIsNotNone(strat, "Strategy report should not be None when feed is ready.")

        # 3. Market Regime Verification
        self.assertEqual(strat.market_regime.regime, MarketRegime.STRONG_BEARISH)
        self.assertEqual(strat.market_regime.confidence, Decimal("1.0000"))
        self.assertEqual(strat.market_regime.aggregate_score, Decimal("-0.7000"))
        self.assertFalse(strat.market_regime.policy.allow_new_longs)
        self.assertIn("SPY", strat.market_regime.proxy_analyses)
        self.assertIn("QQQ", strat.market_regime.proxy_analyses)
        self.assertIn("IWM", strat.market_regime.proxy_analyses)

        # 4. Scanner Output Verification
        self.assertEqual(strat.scan_result.total_scanned, 1)
        self.assertEqual(strat.scan_result.total_passed, 0)
        self.assertEqual(len(strat.scan_result.candidates), 0)
        self.assertIn("AAPL", strat.scan_result.rejected_candidates)
        rejections = strat.scan_result.rejected_candidates["AAPL"]
        self.assertTrue(any("RVOL" in r for r in rejections))
        self.assertTrue(any("STRONG_BEARISH" in r for r in rejections))

        # 5. Signal Engine Verification
        self.assertEqual(strat.signal_summary.total_evaluated, 0)
        self.assertEqual(strat.signal_summary.total_approved, 0)
        self.assertEqual(len(strat.signal_summary.risk_approved_plans), 0)
        self.assertEqual(len(strat.signal_summary.risk_rejected_plans), 0)

        # 6. Policy Warning Verification
        self.assertTrue(any("STRONG_BEARISH" in w for w in report.warnings))

        # 7. Ledger Immutability Verification
        self.assertEqual(ledger.cash_usd, Decimal("5000.00"))
        self.assertEqual(ledger.total_equity_usd, Decimal("5000.00"))
        self.assertEqual(len(ledger.positions), 0)
        self.assertEqual(len(ledger.closed_trades), 0)

    def test_04_failure_path_missing_spy_prevents_analysis(self):
        """Failure-path: Missing primary proxy SPY halts downstream analysis immediately."""
        def missing_spy_downloader(ticker: str, *args, **kwargs):
            if ticker.upper() == "SPY":
                raise NoDataError("No data for SPY")
            return csv_downloader(ticker, *args, **kwargs)

        provider = YFinanceMarketDataProvider(
            yf_download_fn=missing_spy_downloader,
            cache_db_path=":memory:",
        )
        adapter = MarketDataFeedAdapter(provider=provider)
        service = SwingTradingStrategyService(feed_adapter=adapter)

        report = service.run_analysis_with_feed(
            stock_universe=["AAPL"],
            current_time=self.eval_time,
        )

        self.assertFalse(report.readiness.is_ready)
        self.assertEqual(report.readiness.status, ReadinessStatus.HARD_FAIL)
        self.assertIsNone(report.strategy_report, "Strategy report must be None on missing SPY")
        self.assertTrue(any("MISSING_PRIMARY_PROXY_SPY" in r for r in report.failure_reasons))

    def test_05_failure_path_insufficient_spy_history_prevents_analysis(self):
        """Failure-path: SPY history < 200 bars halts downstream analysis immediately."""
        def short_spy_downloader(ticker: str, *args, **kwargs):
            bars = csv_downloader(ticker, *args, **kwargs)
            if ticker.upper() == "SPY":
                return bars[-100:]  # Only 100 bars (< 200 required)
            return bars

        provider = YFinanceMarketDataProvider(
            yf_download_fn=short_spy_downloader,
            cache_db_path=":memory:",
        )
        adapter = MarketDataFeedAdapter(provider=provider)
        service = SwingTradingStrategyService(feed_adapter=adapter)

        report = service.run_analysis_with_feed(
            stock_universe=["AAPL"],
            current_time=self.eval_time,
        )

        self.assertFalse(report.readiness.is_ready)
        self.assertEqual(report.readiness.status, ReadinessStatus.HARD_FAIL)
        self.assertIsNone(report.strategy_report, "Strategy report must be None on insufficient SPY")
        self.assertTrue(any("INSUFFICIENT_SPY_BARS" in r for r in report.failure_reasons))

    def test_06_failure_path_stale_spy_prevents_analysis(self):
        """Failure-path: SPY ending before latest completed session is flagged as STALE."""
        def stale_spy_downloader(ticker: str, *args, **kwargs):
            bars = csv_downloader(ticker, *args, **kwargs)
            if ticker.upper() == "SPY":
                # Truncate SPY to 2026-11-04 (stale vs expected completed session 2026-11-06)
                return [b for b in bars if b["date"] <= "2026-11-04"]
            return bars

        provider = YFinanceMarketDataProvider(
            yf_download_fn=stale_spy_downloader,
            cache_db_path=":memory:",
        )
        adapter = MarketDataFeedAdapter(provider=provider)
        service = SwingTradingStrategyService(feed_adapter=adapter)

        report = service.run_analysis_with_feed(
            stock_universe=["AAPL"],
            current_time=self.eval_time,
        )

        self.assertFalse(report.readiness.is_ready)
        self.assertEqual(report.readiness.status, ReadinessStatus.HARD_FAIL)
        self.assertIsNone(report.strategy_report, "Strategy report must be None on stale SPY")
        self.assertTrue(any("STALE_SPY_DATA" in r for r in report.failure_reasons))

    def test_07_failure_path_candidate_stock_exclusion(self):
        """Failure-path: Candidate stock with < 50 bars is excluded without halting proxy analysis."""
        def short_stock_downloader(ticker: str, *args, **kwargs):
            bars = csv_downloader(ticker, *args, **kwargs)
            if ticker.upper() == "AAPL":
                return bars[-30:]  # Only 30 bars (< 50 required)
            return bars

        provider = YFinanceMarketDataProvider(
            yf_download_fn=short_stock_downloader,
            cache_db_path=":memory:",
        )
        adapter = MarketDataFeedAdapter(provider=provider)
        service = SwingTradingStrategyService(feed_adapter=adapter)

        report = service.run_analysis_with_feed(
            stock_universe=["AAPL"],
            current_time=self.eval_time,
        )

        # Feed readiness succeeds because proxies (SPY, QQQ, IWM) are complete
        self.assertTrue(report.readiness.is_ready)
        self.assertEqual(report.readiness.status, ReadinessStatus.READY)
        self.assertEqual(report.readiness.included_tickers, [])
        self.assertEqual(len(report.readiness.excluded_tickers), 1)

        exclusion = report.readiness.excluded_tickers[0]
        self.assertEqual(exclusion.ticker, "AAPL")
        self.assertIn("INSUFFICIENT_BARS: 30 < 50", exclusion.reason)
        self.assertIsNotNone(report.strategy_report)

    def test_08_failure_path_secondary_proxy_failure_emits_warning(self):
        """Failure-path: Failure in optional secondary proxy (QQQ) emits warning without halting."""
        def qqq_fail_downloader(ticker: str, *args, **kwargs):
            if ticker.upper() == "QQQ":
                raise ProviderUnavailableError("QQQ endpoint network timeout")
            return csv_downloader(ticker, *args, **kwargs)

        provider = YFinanceMarketDataProvider(
            yf_download_fn=qqq_fail_downloader,
            cache_db_path=":memory:",
        )
        adapter = MarketDataFeedAdapter(provider=provider)
        service = SwingTradingStrategyService(feed_adapter=adapter)

        report = service.run_analysis_with_feed(
            stock_universe=["AAPL"],
            current_time=self.eval_time,
        )

        self.assertTrue(report.readiness.is_ready)
        self.assertEqual(report.readiness.status, ReadinessStatus.READY)
        self.assertTrue(any("QQQ endpoint network timeout" in w for w in report.warnings))
        self.assertIsNotNone(report.strategy_report)
        # Regime evaluated using available proxies: SPY and IWM
        self.assertEqual(set(report.strategy_report.market_regime.proxy_analyses.keys()), {"SPY", "IWM"})

    def test_09_failure_path_fatal_provider_error_produces_structured_failure(self):
        """Failure-path: Fatal provider rate limit (HTTP 429) produces structured failure."""
        def fatal_downloader(ticker: str, *args, **kwargs):
            raise RateLimitError("HTTP 429 Too Many Requests: Rate limit exceeded")

        provider = YFinanceMarketDataProvider(
            yf_download_fn=fatal_downloader,
            max_retries=0,
            backoff_intervals=(),
            cache_db_path=":memory:",
        )
        adapter = MarketDataFeedAdapter(provider=provider)
        service = SwingTradingStrategyService(feed_adapter=adapter)

        report = service.run_analysis_with_feed(
            stock_universe=["AAPL"],
            current_time=self.eval_time,
        )

        self.assertFalse(report.readiness.is_ready)
        self.assertEqual(report.readiness.status, ReadinessStatus.HARD_FAIL)
        self.assertIsNone(report.strategy_report)
        self.assertTrue(any("MISSING_PRIMARY_PROXY_SPY" in r for r in report.failure_reasons))

    def test_10_preservation_and_immutability_guarantees(self):
        """Preservation: No disk database written, no paper trading mutation, capital intact."""
        ledger = AccountingLedger(initial_capital="5000.00")
        provider = YFinanceMarketDataProvider(
            yf_download_fn=csv_downloader,
            cache_db_path=":memory:",
        )
        adapter = MarketDataFeedAdapter(provider=provider)
        service = SwingTradingStrategyService(feed_adapter=adapter)

        report = service.run_analysis_with_feed(
            stock_universe=["AAPL"],
            ledger=ledger,
            current_time=self.eval_time,
        )

        # Confirm ledger strictly intact
        self.assertEqual(ledger.cash_usd, Decimal("5000.00"))
        self.assertEqual(ledger.total_equity_usd, Decimal("5000.00"))
        self.assertEqual(len(ledger.positions), 0)
        self.assertEqual(len(ledger.closed_trades), 0)

        # Confirm storage/journal rejects forbidden root filenames in isolated temp directory
        with tempfile.TemporaryDirectory(prefix="test_e2e_journal_") as tmp_dir:
            from paper_storage import PaperStorageManager, PaperStorageSafetyError
            forbidden_path = os.path.join(tmp_dir, "paper_journal.json")
            with self.assertRaises(PaperStorageSafetyError):
                PaperStorageManager(state_dir=forbidden_path)


if __name__ == "__main__":
    unittest.main()
