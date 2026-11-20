"""Unit and integration tests for Phase 6.4.3 Isolated Current Daily Data Acquisition.

Verifies:
1. Storage isolation: output writes strictly to isolated directory; forbidden paths rejected.
2. Pacing enforcement: minimum 2.5s pacing interval, non-negative jitter floor, bounded retry backoff.
3. Missing/stale SPY hard-fail: missing, stale, or < 200 bars raises AcquisitionHardFailure.
4. Stale stock exclusion: stocks missing latest session or < 50 bars excluded with explicit reason.
5. Incomplete candle rejection: forming/intraday bars beyond latest completed session excluded.
6. Session date alignment: all accepted tickers share identical latest completed session date.
7. Historical partition immutability: SHA-256 hashes of historical partitions unchanged before/after.
8. Paper state untouched: external TradingPaperState remains 100% unchanged before/after.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from typing import Any, Dict, List, Optional

WORKSPACE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADER_SWING_DIR = os.path.join(WORKSPACE_ROOT, ".agents", "skills", "trader-swing")
if WORKSPACE_ROOT not in sys.path:
    sys.path.insert(0, WORKSPACE_ROOT)
if TRADER_SWING_DIR not in sys.path:
    sys.path.insert(0, TRADER_SWING_DIR)

from market_data_models import CandleStatus, MarketClock, OHLCVCandle, to_eastern, to_utc, EASTERN, UTC
from scripts.acquire_current_daily import (
    CurrentDailyAcquirer,
    AcquisitionHardFailure,
    assert_safe_current_path,
    calculate_exclusive_end_date,
    MIN_PACING_DELAY,
    MIN_SPY_BARS,
    MIN_STOCK_BARS,
    DEFAULT_CURATED_WATCHLIST,
)
from paper_storage import DEFAULT_PAPER_STATE_DIR
from observation_workflow import run_observation_scan


def compute_dir_hashes(directory: str) -> Dict[str, str]:
    """Compute SHA-256 hashes for all files in a directory tree."""
    file_hashes = {}
    if not os.path.exists(directory):
        return file_hashes
    for root, _, files in sorted(os.walk(directory)):
        for f in sorted(files):
            full_path = os.path.join(root, f)
            rel_path = os.path.relpath(full_path, directory)
            with open(full_path, "rb") as fp:
                file_hashes[rel_path] = hashlib.sha256(fp.read()).hexdigest()
    return file_hashes


def create_synthetic_candle(
    ticker: str,
    d: date,
    base_price: float = 100.0,
    status: CandleStatus = CandleStatus.COMPLETE,
) -> OHLCVCandle:
    """Helper to create a valid OHLCVCandle on a given date."""
    ts = datetime.combine(d, MarketClock.REGULAR_OPEN, tzinfo=EASTERN)
    bp = Decimal(str(base_price))
    return OHLCVCandle(
        ticker=ticker,
        interval="1d",
        timestamp=ts,
        open=bp,
        high=bp + Decimal("2.00"),
        low=bp - Decimal("1.50"),
        close=bp + Decimal("1.00"),
        volume=10000000 if ticker in ("SPY", "QQQ", "IWM") else 1500000,
        status=status,
    )


def generate_series(
    ticker: str,
    target_end_date: date,
    num_bars: int,
    base_price: float = 100.0,
    include_forming_future_bar: bool = False,
) -> List[OHLCVCandle]:
    """Generate a series of historical daily trading bars up to target_end_date."""
    candles: List[OHLCVCandle] = []
    curr = target_end_date
    collected = 0

    while collected < num_bars:
        is_trade, _ = MarketClock.is_historical_trading_day(curr)
        if curr.weekday() < 5 and is_trade:
            candles.append(create_synthetic_candle(ticker, curr, base_price=base_price + collected * 0.1))
            collected += 1
        curr -= timedelta(days=1)

    candles.reverse()

    if include_forming_future_bar:
        next_day = target_end_date + timedelta(days=1)
        candles.append(
            create_synthetic_candle(
                ticker, next_day, base_price=base_price + 5.0, status=CandleStatus.FORMING
            )
        )

    return candles


class TestSwingCurrentAcquisition(unittest.TestCase):
    """Test suite validating Phase 6.4.3 current daily data acquisition."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_curr_acq_")
        self.output_dir = os.path.join(self.test_dir, "current_daily")
        self.staging_dir = os.path.join(self.test_dir, "staging")
        self.sleep_calls: List[float] = []

        # Synthetic reference date: Friday 2026-11-13 18:00 ET (market closed)
        # Latest completed session is 2026-11-13
        self.ref_time_et = datetime(2026, 11, 13, 18, 0, 0, tzinfo=EASTERN)
        self.ref_time_utc = to_utc(self.ref_time_et)
        self.target_session = date(2026, 11, 13)

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def record_sleep(self, seconds: float):
        self.sleep_calls.append(seconds)

    # --------------------------------------------------------------------------
    # TEST 1: Storage Isolation & Path Safety
    # --------------------------------------------------------------------------
    def test_01_storage_isolation_and_path_safety(self):
        """Verifies forbidden paths raise PermissionError and output stays in isolated target."""
        from paper_storage import get_default_paper_state_dir
        forbidden_candidates = [
            os.path.join(WORKSPACE_ROOT, ".planning", "data", "historical_partitions", "2021_2023_primary_corrected"),
            os.path.join(WORKSPACE_ROOT, "paper_journal.json"),
            os.path.join(WORKSPACE_ROOT, "2026_lockbox"),
            get_default_paper_state_dir(),
        ]
        for f_path in forbidden_candidates:
            with self.assertRaises(PermissionError):
                assert_safe_current_path(f_path)

        # Valid isolated output path succeeds
        safe_path = assert_safe_current_path(self.output_dir)
        self.assertTrue(os.path.isabs(safe_path))

    # --------------------------------------------------------------------------
    # TEST 2: Network Pacing & Jitter Floor Enforcement
    # --------------------------------------------------------------------------
    def test_02_pacing_and_jitter_enforcement(self):
        """Verifies pacing delay >= 2.5s and jitter is non-negative on each request."""
        # Attempting to initialize with pacing < 2.5s raises ValueError
        with self.assertRaises(ValueError):
            CurrentDailyAcquirer(output_dir=self.output_dir, pacing_delay=1.0)

        # Mock download function
        def mock_downloader(ticker, start, end, current_time):
            return generate_series(ticker, self.target_session, num_bars=220)

        acquirer = CurrentDailyAcquirer(
            output_dir=self.output_dir,
            staging_dir=self.staging_dir,
            candidate_tickers=["AAPL", "MSFT"],
            pacing_delay=2.5,
            download_fn=mock_downloader,
            sleep_fn=self.record_sleep,
            random_fn=lambda: 0.25,
        )

        manifest = acquirer.acquire_current_dataset(reference_time=self.ref_time_utc)
        self.assertEqual(manifest["as_of_session_date"], self.target_session.isoformat())

        # Total tickers: SPY, QQQ, IWM + AAPL, MSFT = 5 tickers -> 4 pacing sleeps
        self.assertEqual(len(self.sleep_calls), 4)
        for sleep_dur in self.sleep_calls:
            self.assertGreaterEqual(sleep_dur, MIN_PACING_DELAY)
            self.assertAlmostEqual(sleep_dur, 2.75, places=2)

    # --------------------------------------------------------------------------
    # TEST 3: Missing or Stale SPY Hard-Fail
    # --------------------------------------------------------------------------
    def test_03_missing_or_stale_spy_hard_fail(self):
        """Verifies missing, stale, or insufficient SPY raises AcquisitionHardFailure."""
        # Case A: SPY returns empty list
        def mock_empty_spy(ticker, start, end, current_time):
            if ticker == "SPY":
                return []
            return generate_series(ticker, self.target_session, 220)

        acq_empty = CurrentDailyAcquirer(
            output_dir=self.output_dir,
            staging_dir=self.staging_dir,
            download_fn=mock_empty_spy,
            sleep_fn=self.record_sleep,
        )
        with self.assertRaises(AcquisitionHardFailure):
            acq_empty.acquire_current_dataset(reference_time=self.ref_time_utc)

        # Case B: SPY is stale (latest date older than latest completed session)
        stale_date = self.target_session - timedelta(days=3)

        def mock_stale_spy(ticker, start, end, current_time):
            if ticker == "SPY":
                return generate_series(ticker, stale_date, 220)
            return generate_series(ticker, self.target_session, 220)

        acq_stale = CurrentDailyAcquirer(
            output_dir=self.output_dir,
            staging_dir=self.staging_dir,
            download_fn=mock_stale_spy,
            sleep_fn=self.record_sleep,
        )
        with self.assertRaises(AcquisitionHardFailure) as ctx:
            acq_stale.acquire_current_dataset(reference_time=self.ref_time_utc)
        self.assertIn("STALE_PRIMARY_PROXY", str(ctx.exception))

        # Case C: SPY has insufficient bars (< 200 bars)
        def mock_short_spy(ticker, start, end, current_time):
            if ticker == "SPY":
                return generate_series(ticker, self.target_session, num_bars=150)
            return generate_series(ticker, self.target_session, 220)

        acq_short = CurrentDailyAcquirer(
            output_dir=self.output_dir,
            staging_dir=self.staging_dir,
            download_fn=mock_short_spy,
            sleep_fn=self.record_sleep,
        )
        with self.assertRaises(AcquisitionHardFailure) as ctx:
            acq_short.acquire_current_dataset(reference_time=self.ref_time_utc)
        self.assertIn("INSUFFICIENT_SPY_BARS", str(ctx.exception))

    # --------------------------------------------------------------------------
    # TEST 4: Stale Stock Exclusion with Explicit Reasons
    # --------------------------------------------------------------------------
    def test_04_stale_stock_exclusion(self):
        """Verifies stale individual stocks are excluded and recorded in manifest."""
        def mock_mixed_stocks(ticker, start, end, current_time):
            if ticker in ("SPY", "QQQ", "IWM", "AAPL"):
                return generate_series(ticker, self.target_session, 220)
            elif ticker == "AMZN":
                # Stale by 2 sessions
                stale_d = self.target_session - timedelta(days=3)
                return generate_series(ticker, stale_d, 220)
            elif ticker == "GOOGL":
                # Too few bars (< 50)
                return generate_series(ticker, self.target_session, 30)
            return generate_series(ticker, self.target_session, 220)

        acquirer = CurrentDailyAcquirer(
            output_dir=self.output_dir,
            staging_dir=self.staging_dir,
            candidate_tickers=["AAPL", "AMZN", "GOOGL"],
            download_fn=mock_mixed_stocks,
            sleep_fn=self.record_sleep,
        )
        manifest = acquirer.acquire_current_dataset(reference_time=self.ref_time_utc)

        self.assertIn("AAPL", manifest["included_equities"])
        self.assertNotIn("AMZN", manifest["included_equities"])
        self.assertNotIn("GOOGL", manifest["included_equities"])

        # Check explicit exclusion reasons in manifest
        excluded = {x["ticker"]: x["reason"] for x in manifest["excluded_equities"]}
        self.assertIn("AMZN", excluded)
        self.assertIn("STALE_STOCK_DATA", excluded["AMZN"])
        self.assertIn("GOOGL", excluded)
        self.assertIn("INSUFFICIENT_BARS", excluded["GOOGL"])

        # Verify excluded tickers did not produce output CSVs
        self.assertTrue(os.path.exists(os.path.join(self.output_dir, "AAPL_daily.csv")))
        self.assertFalse(os.path.exists(os.path.join(self.output_dir, "AMZN_daily.csv")))
        self.assertFalse(os.path.exists(os.path.join(self.output_dir, "GOOGL_daily.csv")))

    # --------------------------------------------------------------------------
    # TEST 5: Incomplete / Forming Candle Rejection
    # --------------------------------------------------------------------------
    def test_05_incomplete_candle_rejection(self):
        """Verifies candles beyond latest completed session are rejected and excluded."""
        def mock_downloader_with_future(ticker, start, end, current_time):
            # Include forming bar on 2026-11-14
            return generate_series(
                ticker, self.target_session, num_bars=220, include_forming_future_bar=True
            )

        acquirer = CurrentDailyAcquirer(
            output_dir=self.output_dir,
            staging_dir=self.staging_dir,
            candidate_tickers=["AAPL"],
            download_fn=mock_downloader_with_future,
            sleep_fn=self.record_sleep,
        )
        manifest = acquirer.acquire_current_dataset(reference_time=self.ref_time_utc)

        # Check SPY and AAPL CSVs
        for sym in ("SPY", "AAPL"):
            csv_path = os.path.join(self.output_dir, f"{sym}_daily.csv")
            with open(csv_path, "r", encoding="utf-8") as f:
                lines = [line.strip() for line in f if line.strip()]
            last_line = lines[-1]
            last_date_str = last_line.split(",")[0]
            self.assertEqual(last_date_str, self.target_session.isoformat())
            # Ensure forming future bar (2026-11-14) is NOT present
            self.assertNotIn("2026-11-14", "\n".join(lines))

    # --------------------------------------------------------------------------
    # TEST 6: Session Date Alignment Across All Tickers
    # --------------------------------------------------------------------------
    def test_06_session_date_alignment(self):
        """Verifies all acquired CSV files share the exact same final session date."""
        def mock_downloader(ticker, start, end, current_time):
            return generate_series(ticker, self.target_session, num_bars=220)

        acquirer = CurrentDailyAcquirer(
            output_dir=self.output_dir,
            staging_dir=self.staging_dir,
            candidate_tickers=["AAPL", "MSFT", "NVDA"],
            download_fn=mock_downloader,
            sleep_fn=self.record_sleep,
        )
        manifest = acquirer.acquire_current_dataset(reference_time=self.ref_time_utc)
        expected_date_str = self.target_session.isoformat()

        # Verify manifest recorded validation_results
        self.assertIn("validation_results", manifest)
        self.assertEqual(manifest["validation_results"]["validation_status"], "PASSED")
        self.assertEqual(manifest["validation_results"]["proxies_validated"], 3)
        self.assertEqual(manifest["validation_results"]["equities_validated"], 3)
        self.assertEqual(manifest["validation_results"]["equities_excluded"], 0)

        all_tickers = manifest["proxies"] + manifest["included_equities"]
        for sym in all_tickers:
            csv_path = os.path.join(self.output_dir, f"{sym}_daily.csv")
            self.assertTrue(os.path.exists(csv_path), f"CSV for {sym} must exist")
            with open(csv_path, "r", encoding="utf-8") as f:
                lines = [line.strip() for line in f if line.strip()]
            last_date_str = lines[-1].split(",")[0]
            self.assertEqual(
                last_date_str,
                expected_date_str,
                f"Ticker {sym} last date {last_date_str} != expected {expected_date_str}",
            )

    # --------------------------------------------------------------------------
    # TEST 7: Historical Partition Immutability
    # --------------------------------------------------------------------------
    def test_07_historical_partition_immutability(self):
        """Verifies historical partition files are completely untouched before and after acquisition."""
        hist_partition_dir = os.path.join(
            WORKSPACE_ROOT, ".planning", "data", "historical_partitions", "2021_2023_primary_corrected"
        )
        if not os.path.exists(hist_partition_dir):
            self.skipTest(f"Historical partition not present at {hist_partition_dir}")

        hashes_before = compute_dir_hashes(hist_partition_dir)
        self.assertGreater(len(hashes_before), 0, "Historical partition must have files to hash")

        # Run acquisition with mock data into self.output_dir
        def mock_downloader(ticker, start, end, current_time):
            return generate_series(ticker, self.target_session, num_bars=220)

        acquirer = CurrentDailyAcquirer(
            output_dir=self.output_dir,
            staging_dir=self.staging_dir,
            candidate_tickers=["AAPL"],
            download_fn=mock_downloader,
            sleep_fn=self.record_sleep,
        )
        acquirer.acquire_current_dataset(reference_time=self.ref_time_utc)

        hashes_after = compute_dir_hashes(hist_partition_dir)
        self.assertEqual(
            hashes_before,
            hashes_after,
            "Historical partition files were modified or corrupted during current acquisition!",
        )

    # --------------------------------------------------------------------------
    # TEST 8: Paper Trading State Immutability
    # --------------------------------------------------------------------------
    def test_08_paper_state_immutability(self):
        """Verifies paper trading state files remain 100% untouched before and after acquisition."""
        paper_state_dir = DEFAULT_PAPER_STATE_DIR
        if not os.path.exists(paper_state_dir):
            self.skipTest(f"TradingPaperState not found at {paper_state_dir}")

        hashes_before = compute_dir_hashes(paper_state_dir)
        self.assertGreater(len(hashes_before), 0, "Paper state directory must contain files")

        # Run acquisition
        def mock_downloader(ticker, start, end, current_time):
            return generate_series(ticker, self.target_session, num_bars=220)

        acquirer = CurrentDailyAcquirer(
            output_dir=self.output_dir,
            staging_dir=self.staging_dir,
            candidate_tickers=["AAPL", "MSFT"],
            download_fn=mock_downloader,
            sleep_fn=self.record_sleep,
        )
        acquirer.acquire_current_dataset(reference_time=self.ref_time_utc)

        hashes_after = compute_dir_hashes(paper_state_dir)
        self.assertEqual(
            hashes_before,
            hashes_after,
            "Paper trading state files were modified during current daily data acquisition!",
        )

    # --------------------------------------------------------------------------
    # TEST 9: Observe --current Cannot Accidentally Bypass Completed Session
    # --------------------------------------------------------------------------
    def test_09_observe_current_cannot_bypass_completed_session(self):
        """Verifies that observe --current strictly rejects uncompleted/forming session dates."""
        # Acquire mock current dataset up to self.target_session (2026-11-13)
        def mock_downloader(ticker, start, end, current_time):
            return generate_series(ticker, self.target_session, num_bars=220)

        acquirer = CurrentDailyAcquirer(
            output_dir=self.output_dir,
            staging_dir=self.staging_dir,
            candidate_tickers=["AAPL"],
            download_fn=mock_downloader,
            sleep_fn=self.record_sleep,
        )
        acquirer.acquire_current_dataset(reference_time=self.ref_time_utc)

        # Attempt to observe with an as_of_date in the future (e.g. 2026-11-20)
        future_date = date(2026, 11, 20)
        report = run_observation_scan(
            data_dir=self.output_dir,
            as_of_date=future_date,
            write_records=False,
        )

        self.assertEqual(report.feed_status, "HARD_FAIL")
        self.assertEqual(report.data_mode, "CURRENT_DAILY_OBSERVATION")
        self.assertTrue(any("INCOMPLETE_SESSION" in fail for fail in report.failure_reasons))
        self.assertEqual(len(report.eligible_candidates), 0)

    # --------------------------------------------------------------------------
    # TEST 10: Exclusive End Date Calculation
    # --------------------------------------------------------------------------
    def test_10_exclusive_end_date_calculation(self):
        """Verifies calculate_exclusive_end_date produces day AFTER target session."""
        self.assertEqual(calculate_exclusive_end_date(date(2026, 11, 13)), date(2026, 11, 14))
        self.assertEqual(calculate_exclusive_end_date(date(2026, 10, 31)), date(2026, 11, 1))
        self.assertEqual(calculate_exclusive_end_date(date(2026, 12, 31)), date(2027, 1, 1))
        self.assertEqual(calculate_exclusive_end_date(date(2024, 2, 28)), date(2024, 2, 29))

        with self.assertRaises(TypeError):
            calculate_exclusive_end_date("2026-11-13")  # type: ignore

    # --------------------------------------------------------------------------
    # TEST 11: November 13 Included When Latest Completed Session
    # --------------------------------------------------------------------------
    def test_11_november_13_included_when_latest_completed_session(self):
        """Verifies provider requests day after (2026-11-14) and 2026-11-13 is included in CSV & manifest."""
        requested_bounds: Dict[str, Tuple[str, str]] = {}

        def mock_downloader(ticker, start, end, current_time):
            requested_bounds[ticker] = (start, end)
            return generate_series(ticker, self.target_session, num_bars=220)

        acquirer = CurrentDailyAcquirer(
            output_dir=self.output_dir,
            staging_dir=self.staging_dir,
            candidate_tickers=["AAPL"],
            download_fn=mock_downloader,
            sleep_fn=self.record_sleep,
        )
        manifest = acquirer.acquire_current_dataset(reference_time=self.ref_time_utc)

        # 1. Verify provider requested exclusive end boundary = 2026-11-14 (day after target_session)
        for sym in ("SPY", "QQQ", "IWM", "AAPL"):
            self.assertIn(sym, requested_bounds)
            _, end_req = requested_bounds[sym]
            self.assertEqual(end_req, "2026-11-14", f"Downloader for {sym} must request exclusive boundary 2026-11-14")

        # 2. Verify SPY latest session date is 2026-11-13 (NOT stale)
        self.assertEqual(manifest["as_of_session_date"], "2026-11-13")
        self.assertIn("SPY", manifest["proxies"])

        # 3. Verify CSV file ends on 2026-11-13
        spy_csv = os.path.join(self.output_dir, "SPY_daily.csv")
        self.assertTrue(os.path.exists(spy_csv))
        with open(spy_csv, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
        last_row = lines[-1].split(",")
        self.assertEqual(last_row[0], "2026-11-13")

    # --------------------------------------------------------------------------
    # TEST 12: November 14 Not Accepted as Trading Candle
    # --------------------------------------------------------------------------
    def test_12_november_14_not_accepted_as_trading_candle(self):
        """Verifies November 14 (Saturday) is rejected as non-trading and excluded from output."""
        # 1. Calendar check: November 14 is Saturday weekend
        is_trade, reason = MarketClock.is_trading_day(date(2026, 11, 14))
        self.assertFalse(is_trade)
        self.assertEqual(reason, "Weekend")

        # 2. If downloader returns a candle with date 2026-11-14, acquirer filters it out
        def mock_downloader_with_nov14(ticker, start, end, current_time):
            series = generate_series(ticker, self.target_session, num_bars=220)
            # Add an invalid weekend bar on 2026-11-14
            series.append(
                create_synthetic_candle(ticker, date(2026, 11, 14), base_price=105.0)
            )
            return series

        acquirer = CurrentDailyAcquirer(
            output_dir=self.output_dir,
            staging_dir=self.staging_dir,
            candidate_tickers=["AAPL"],
            download_fn=mock_downloader_with_nov14,
            sleep_fn=self.record_sleep,
        )
        manifest = acquirer.acquire_current_dataset(reference_time=self.ref_time_utc)

        # Output must strictly cap at 2026-11-13 and NOT contain 2026-11-14
        self.assertEqual(manifest["as_of_session_date"], "2026-11-13")
        spy_csv = os.path.join(self.output_dir, "SPY_daily.csv")
        with open(spy_csv, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn("2026-11-14", content)
        self.assertIn("2026-11-13", content)

    # --------------------------------------------------------------------------
    # TEST 13: Forming Sessions Remain Excluded
    # --------------------------------------------------------------------------
    def test_13_forming_sessions_remain_excluded(self):
        """Verifies that intraday/forming sessions (e.g. today during market hours) are filtered out."""
        # Simulate execution at 12:00 ET on Monday 2026-11-16 (market is open)
        simulated_now_et = datetime(2026, 11, 16, 12, 0, 0, tzinfo=EASTERN)
        simulated_now_utc = to_utc(simulated_now_et)

        def mock_downloader_with_forming_today(ticker, start, end, current_time):
            series = generate_series(ticker, self.target_session, num_bars=220)
            # Add forming candle for Monday 2026-11-16
            series.append(
                create_synthetic_candle(
                    ticker, date(2026, 11, 16), base_price=110.0, status=CandleStatus.FORMING
                )
            )
            return series

        acquirer = CurrentDailyAcquirer(
            output_dir=self.output_dir,
            staging_dir=self.staging_dir,
            candidate_tickers=["AAPL"],
            download_fn=mock_downloader_with_forming_today,
            sleep_fn=self.record_sleep,
        )
        manifest = acquirer.acquire_current_dataset(reference_time=simulated_now_utc)

        # Latest completed session is Friday 2026-11-13; Monday 2026-11-16 must be excluded
        self.assertEqual(manifest["as_of_session_date"], "2026-11-13")
        spy_csv = os.path.join(self.output_dir, "SPY_daily.csv")
        with open(spy_csv, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn("2026-11-16", content)
        self.assertIn("2026-11-13", content)

    # --------------------------------------------------------------------------
    # TEST 14: Stale SPY Hard-Fail Remains Intact
    # --------------------------------------------------------------------------
    def test_14_stale_spy_hard_fail_remains_intact(self):
        """Verifies that SPY ending prior to latest completed session strictly triggers AcquisitionHardFailure."""
        # SPY ends on 2026-11-12 when latest completed session is 2026-11-13
        stale_date = date(2026, 11, 12)

        def mock_stale_spy_downloader(ticker, start, end, current_time):
            if ticker == "SPY":
                return generate_series(ticker, stale_date, num_bars=220)
            return generate_series(ticker, self.target_session, num_bars=220)

        acquirer = CurrentDailyAcquirer(
            output_dir=self.output_dir,
            staging_dir=self.staging_dir,
            candidate_tickers=["AAPL"],
            download_fn=mock_stale_spy_downloader,
            sleep_fn=self.record_sleep,
        )
        with self.assertRaises(AcquisitionHardFailure) as ctx:
            acquirer.acquire_current_dataset(reference_time=self.ref_time_utc)

        self.assertIn("STALE_PRIMARY_PROXY", str(ctx.exception))
        self.assertIn("2026-11-12", str(ctx.exception))
        self.assertIn("2026-11-13", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
