"""Unit and Integration Tests for Historical Partition Acquisition Utility.

Phase 6.2C — Step 3.4.3: Acquisition Tests with Targeted Safety Remediation

Tests:
1. Pre-request date ceiling rejection: Requested end_date > 2023-12-31 raises PermissionError.
2. Post-response date ceiling rejection: Returned candle > 2023-12-31 raises PermissionError.
3. Lockbox path rejection: Staging or output dir pointing to 2026 lockbox raises PermissionError.
4. Pacing rejection: Sub-2.5s, zero, and negative pacing delays raise ValueError.
5. Consistent per-request pacing: Pacing delay enforced between every request, zero delay before first.
6. Jitter bounds: Non-negative jitter guarantees delay is never below 2.5s at min and max jitter.
7. Overwrite rejection: Existing destination raises FileExistsError; existing files preserved byte-for-byte.
8. Safe atomic promotion on same filesystem via os.replace.
9. Cross-filesystem promotion rejection: Fails safely without modifying destination.
10. Atomic promotion rename failure: Preserves staging data for diagnostics.
11. Promotion gate rejection on missing core proxy (SPY); staging preserved.
12. Promotion gate rejection on candidate equities deficit (< 45 of 50); staging preserved.
13. Exact retry sequence: Retries transient errors with [3.0, 6.0] delays and succeeds on attempt 3.
14. Retry max attempts exhausted: 1 initial + 3 retries ([3.0, 6.0, 12.0] delays) before failing.
15. Permanent errors fail immediately without retry.
16. PermissionError fails immediately without retry.
17. Canonical path containment and symlink safety.
18. DEFECT-01: include_proxies parameter removed from HistoricalPartitionAcquirer constructor.
19. DEFECT-01: --skip-proxies argument removed from CLI parser.
20. DEFECT-01: Missing QQQ proxy blocks promotion.
21. DEFECT-01: Missing IWM proxy blocks promotion.
22. DEFECT-01: Corrupted proxy data fails PartitionVerifier and blocks promotion.
23. DEFECT-02: RETRY_BACKOFF_SCHEDULE values are all >= 2.5s pacing floor and strictly increasing.
24. DEFECT-02: Jitter pacing floor: Negative or zero jitter strictly enforced at >= 2.5s floor.
25. DEFECT-02: Underlying provider instantiated with max_retries=0 to prevent retry compounding.
26. DEFECT-03: validate_safe_staging_dir rejects parent directory.
27. DEFECT-03: validate_safe_staging_dir rejects primary/final partition directory.
28. DEFECT-03: validate_safe_staging_dir rejects unrelated directories outside .planning/data/historical_partitions.
29. DEFECT-03: validate_safe_staging_dir rejects path traversal escaping partitions directory.
30. DEFECT-03: validate_safe_staging_dir rejects symlinks and junctions.
31. DEFECT-03: validate_safe_staging_dir accepts valid staging path and canonicalizes safely.
"""

from datetime import date, timedelta
from decimal import Decimal
import argparse
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from scripts.acquire_historical_partitions import (
    assert_safe_date,
    assert_safe_path,
    is_transient_error,
    validate_safe_staging_dir,
    verify_same_filesystem,
    HistoricalPartitionAcquirer,
    MAX_ALLOWED_DATE,
    MIN_PACING_DELAY,
    MIN_EQUITIES_ACCEPTED,
    RETRY_BACKOFF_SCHEDULE,
    CORE_PROXIES,
)
from market_data_models import MarketClock
from universe import DEFAULT_CURATED_WATCHLIST


def get_mock_nyse_days(start_date: date, count: int):
    days = []
    curr = start_date
    while len(days) < count:
        is_trading, _ = MarketClock.is_historical_trading_day(curr)
        if is_trading:
            days.append(curr)
        curr += timedelta(days=1)
    return days


def generate_mock_bars(days, base_price=Decimal("150.00"), base_vol=1_000_000):
    bars = []
    price = base_price
    for d in days:
        o = price
        h = o + Decimal("2.00")
        l = o - Decimal("1.50")
        c = o + Decimal("0.50")
        bars.append({
            "Date": d.isoformat(),
            "Open": f"{o:.4f}",
            "High": f"{h:.4f}",
            "Low": f"{l:.4f}",
            "Close": f"{c:.4f}",
            "Volume": base_vol,
        })
        price = c
    return bars


class TestHistoricalAcquisition(unittest.TestCase):
    """Safety and functional test suite for acquire_historical_partitions.py."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_partition_acq_")
        self.partitions_dir = os.path.join(self.temp_dir, ".planning", "data", "historical_partitions")
        os.makedirs(self.partitions_dir, exist_ok=True)
        self.staging_dir = os.path.join(self.partitions_dir, ".staging_2021_2023")
        self.output_dir = os.path.join(self.partitions_dir, "2021_2023_primary")
        self.mock_days = get_mock_nyse_days(date(2021, 1, 4), 30)
        self.sleep_calls = []

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def mock_sleep(self, seconds: float):
        self.sleep_calls.append(seconds)

    def test_01_pre_request_date_ceiling_rejection(self):
        """Setting start_date or end_date > 2023-12-31 raises PermissionError before any request."""
        with self.assertRaises(PermissionError) as ctx:
            assert_safe_date(date(2024, 1, 1), "end_date")
        self.assertIn("exceeds MAX_ALLOWED_DATE", str(ctx.exception))

        with self.assertRaises(PermissionError):
            HistoricalPartitionAcquirer(
                staging_dir=self.staging_dir,
                output_dir=self.output_dir,
                end_date=date(2024, 6, 1),
            )

        with self.assertRaises(PermissionError):
            HistoricalPartitionAcquirer(
                staging_dir=self.staging_dir,
                output_dir=self.output_dir,
                start_date=date(2026, 1, 1),
                end_date=date(2026, 11, 6),
            )

    def test_02_post_response_date_ceiling_rejection(self):
        """If downloaded data contains a candle > 2023-12-31, immediately raises PermissionError."""
        def mock_downloader_with_leak(ticker, start=None, end=None):
            bars = generate_mock_bars(self.mock_days[:5])
            bars.append({
                "Date": "2024-01-02",
                "Open": "100.00",
                "High": "105.00",
                "Low": "99.00",
                "Close": "104.00",
                "Volume": 1000,
            })
            return bars

        acquirer = HistoricalPartitionAcquirer(
            staging_dir=self.staging_dir,
            output_dir=self.output_dir,
            start_date=date(2021, 1, 4),
            end_date=date(2021, 2, 1),
            candidate_tickers=["AAPL"],
            download_fn=mock_downloader_with_leak,
            sleep_fn=self.mock_sleep,
        )

        with self.assertRaises(PermissionError) as ctx:
            acquirer.fetch_ticker_data_with_retries("AAPL")
        self.assertIn("exceeds MAX_ALLOWED_DATE", str(ctx.exception))

    def test_02b_exclusive_end_date_range_and_defaults(self):
        """Verify DEFAULT_END_DATE is 2023-12-31 and exclusive-end parameter behavior."""
        from scripts.acquire_historical_partitions import DEFAULT_START_DATE, DEFAULT_END_DATE, MAX_ALLOWED_DATE
        from market_data_models import MarketClock

        # 1. Verify default constants
        self.assertEqual(DEFAULT_START_DATE, date(2021, 1, 1))
        self.assertEqual(DEFAULT_END_DATE, date(2023, 12, 31))
        self.assertEqual(MAX_ALLOWED_DATE, date(2023, 12, 31))

        # 2. Verify NYSE session bounds for 2021-01-01 through 2023-12-31
        nyse_sessions = []
        curr = DEFAULT_START_DATE
        while curr <= DEFAULT_END_DATE:
            is_trading, _ = MarketClock.is_historical_trading_day(curr)
            if is_trading:
                nyse_sessions.append(curr)
            curr += timedelta(days=1)

        self.assertEqual(len(nyse_sessions), 753)
        self.assertEqual(nyse_sessions[0], date(2021, 1, 4))
        self.assertEqual(nyse_sessions[-1], date(2023, 12, 29))

        # 3. Verify acquirer defaults to these bounds safely without exceeding ceiling
        acquirer = HistoricalPartitionAcquirer(
            staging_dir=self.staging_dir,
            output_dir=self.output_dir,
            candidate_tickers=["AAPL"],
            sleep_fn=self.mock_sleep,
        )
        self.assertEqual(acquirer.start_date, date(2021, 1, 1))
        self.assertEqual(acquirer.end_date, date(2023, 12, 31))

    def test_03_lockbox_path_rejection(self):
        """Any path containing 'lockbox' or '2026' is rejected with PermissionError."""
        with self.assertRaises(PermissionError) as ctx:
            assert_safe_path("C:/Users/test_user/trading/data/2026_lockbox_sealed")
        self.assertIn("forbidden token", str(ctx.exception))

        with self.assertRaises(PermissionError):
            HistoricalPartitionAcquirer(
                staging_dir=self.staging_dir,
                output_dir="C:/Users/test_user/trading/data/lockbox",
            )

    def test_04_pacing_rejects_sub_minimum_and_invalid_delays(self):
        """Pacing delays below 2.5s, zero, or negative raise ValueError (Section 2)."""
        # Sub-2.5s pacing
        with self.assertRaises(ValueError) as ctx:
            HistoricalPartitionAcquirer(
                staging_dir=self.staging_dir,
                output_dir=self.output_dir,
                pacing_delay=2.4,
            )
        self.assertIn("Minimum allowed pacing interval is 2.5", str(ctx.exception))

        # 1.0s pacing
        with self.assertRaises(ValueError):
            HistoricalPartitionAcquirer(
                staging_dir=self.staging_dir,
                output_dir=self.output_dir,
                pacing_delay=1.0,
            )

        # 0.0s pacing
        with self.assertRaises(ValueError):
            HistoricalPartitionAcquirer(
                staging_dir=self.staging_dir,
                output_dir=self.output_dir,
                pacing_delay=0.0,
            )

        # Negative pacing
        with self.assertRaises(ValueError):
            HistoricalPartitionAcquirer(
                staging_dir=self.staging_dir,
                output_dir=self.output_dir,
                pacing_delay=-2.5,
            )

    def test_05_consistent_per_request_pacing(self):
        """Pacing delay is enforced consistently between every request with zero delay before the first."""
        def mock_downloader(ticker, start=None, end=None):
            return generate_mock_bars(self.mock_days)

        acquirer = HistoricalPartitionAcquirer(
            staging_dir=self.staging_dir,
            output_dir=self.output_dir,
            candidate_tickers=["AAPL", "MSFT", "NVDA"],
            pacing_delay=2.5,
            download_fn=mock_downloader,
            sleep_fn=self.mock_sleep,
            random_fn=lambda: 0.2,  # deterministic jitter
        )

        res = acquirer.run_acquisition()
        self.assertEqual(res["status"], "SUCCESS")
        # 3 core proxies + 3 candidates = 6 tickers => exactly 5 inter-request pacing sleeps (zero before request 1)
        self.assertEqual(len(self.sleep_calls), 5)
        for s in self.sleep_calls:
            self.assertGreaterEqual(s, 2.5)
            self.assertAlmostEqual(s, 2.7, places=2)

    def test_06_jitter_bounds_min_and_max(self):
        """Jitter guarantees delay is never below 2.5s at min (0.0) and max (0.5) jitter."""
        # Test min jitter (0.0)
        acquirer_min = HistoricalPartitionAcquirer(
            staging_dir=self.staging_dir,
            output_dir=self.output_dir,
            candidate_tickers=["AAPL", "MSFT"],
            pacing_delay=2.5,
            download_fn=lambda t, **kw: generate_mock_bars(self.mock_days),
            sleep_fn=self.mock_sleep,
            random_fn=lambda: 0.0,
        )
        acquirer_min.run_acquisition()
        # 3 proxies + 2 candidates = 5 tickers => 4 inter-request sleeps
        self.assertEqual(len(self.sleep_calls), 4)
        for s in self.sleep_calls:
            self.assertEqual(s, 2.5)

        # Clean staging and destination for next test
        if os.path.exists(self.output_dir):
            shutil.rmtree(self.output_dir)
        self.sleep_calls.clear()

        # Test max jitter (0.5)
        acquirer_max = HistoricalPartitionAcquirer(
            staging_dir=self.staging_dir,
            output_dir=self.output_dir,
            candidate_tickers=["AAPL", "MSFT"],
            pacing_delay=2.5,
            download_fn=lambda t, **kw: generate_mock_bars(self.mock_days),
            sleep_fn=self.mock_sleep,
            random_fn=lambda: 0.5,
        )
        acquirer_max.run_acquisition()
        self.assertEqual(len(self.sleep_calls), 4)
        for s in self.sleep_calls:
            self.assertEqual(s, 3.0)

    def test_07_destructive_overwrite_rejection(self):
        """Existing destination raises FileExistsError; existing files preserved byte-for-byte (Section 3)."""
        os.makedirs(self.output_dir, exist_ok=True)
        test_file = os.path.join(self.output_dir, "existing_data.csv")
        with open(test_file, "w") as f:
            f.write("ORIGINAL_BYTE_FOR_BYTE_DATA\n")

        # Must fail at init
        with self.assertRaises(FileExistsError) as ctx:
            HistoricalPartitionAcquirer(
                staging_dir=self.staging_dir,
                output_dir=self.output_dir,
            )
        self.assertIn("already exists", str(ctx.exception))

        # Destination file must be intact byte-for-byte
        self.assertTrue(os.path.exists(test_file))
        with open(test_file, "r") as f:
            self.assertEqual(f.read(), "ORIGINAL_BYTE_FOR_BYTE_DATA\n")

    def test_08_safe_atomic_promotion_same_filesystem(self):
        """Successful same-filesystem promotion uses os.replace and moves staging to destination (Section 4)."""
        def mock_downloader(ticker, start=None, end=None):
            return generate_mock_bars(self.mock_days)

        acquirer = HistoricalPartitionAcquirer(
            staging_dir=self.staging_dir,
            output_dir=self.output_dir,
            candidate_tickers=["AAPL"],
            download_fn=mock_downloader,
            sleep_fn=self.mock_sleep,
        )

        res = acquirer.run_acquisition()
        self.assertEqual(res["status"], "SUCCESS")
        self.assertTrue(os.path.exists(self.output_dir))
        self.assertFalse(os.path.exists(self.staging_dir))
        self.assertTrue(os.path.exists(os.path.join(self.output_dir, "DATASET_MANIFEST.json")))

    def test_09_safe_atomic_promotion_cross_filesystem_rejection(self):
        """Cross-filesystem promotion is rejected; staging preserved and destination untouched (Section 4)."""
        def mock_downloader(ticker, start=None, end=None):
            return generate_mock_bars(self.mock_days)

        acquirer = HistoricalPartitionAcquirer(
            staging_dir=self.staging_dir,
            output_dir=self.output_dir,
            candidate_tickers=["AAPL"],
            download_fn=mock_downloader,
            sleep_fn=self.mock_sleep,
        )

        with patch("scripts.acquire_historical_partitions.verify_same_filesystem") as mock_verify:
            mock_verify.side_effect = OSError("Cross-filesystem promotion detected: drives do not match.")
            with self.assertRaises(OSError) as ctx:
                acquirer.run_acquisition()
            self.assertIn("Cross-filesystem promotion detected", str(ctx.exception))

        # Destination untouched; staging preserved with evidence
        self.assertFalse(os.path.exists(self.output_dir))
        self.assertTrue(os.path.exists(self.staging_dir))
        self.assertTrue(os.path.exists(os.path.join(self.staging_dir, "AAPL_daily.csv")))

    def test_10_atomic_promotion_rename_failure_preserves_staging(self):
        """Failure during atomic rename raises OSError and preserves staging directory (Section 4)."""
        def mock_downloader(ticker, start=None, end=None):
            return generate_mock_bars(self.mock_days)

        acquirer = HistoricalPartitionAcquirer(
            staging_dir=self.staging_dir,
            output_dir=self.output_dir,
            candidate_tickers=["AAPL"],
            download_fn=mock_downloader,
            sleep_fn=self.mock_sleep,
        )

        with patch("os.replace") as mock_replace:
            mock_replace.side_effect = OSError("Simulated filesystem rename failure")
            with self.assertRaises(OSError) as ctx:
                acquirer.run_acquisition()
            self.assertIn("Simulated filesystem rename failure", str(ctx.exception))

        # Staging preserved
        self.assertTrue(os.path.exists(self.staging_dir))
        self.assertFalse(os.path.exists(self.output_dir))

    def test_11_promotion_blocked_on_missing_proxy(self):
        """Promotion rejected if core proxy (SPY) fails; staging preserved and destination untouched."""
        def mock_downloader(ticker, start=None, end=None):
            if ticker == "SPY":
                raise ConnectionError("Simulated SPY provider outage")
            return generate_mock_bars(self.mock_days)

        acquirer = HistoricalPartitionAcquirer(
            staging_dir=self.staging_dir,
            output_dir=self.output_dir,
            candidate_tickers=["AAPL", "MSFT"],
            download_fn=mock_downloader,
            sleep_fn=self.mock_sleep,
        )

        res = acquirer.run_acquisition()
        self.assertEqual(res["status"], "PROMOTION_REJECTED")
        self.assertIn("SPY", res["missing_proxies"])
        self.assertFalse(os.path.exists(self.output_dir))
        # Staging preserved for diagnostics
        self.assertTrue(os.path.exists(self.staging_dir))

    def test_12_promotion_blocked_on_equities_deficit(self):
        """Promotion rejected if candidate equities < 45 of 50; staging preserved."""
        watchlist_50 = list(DEFAULT_CURATED_WATCHLIST)[:50]
        failed_tickers = set(watchlist_50[44:])  # 6 fail, 44 succeed

        def mock_downloader(ticker, start=None, end=None):
            if ticker in failed_tickers:
                raise ValueError(f"Simulated failure for {ticker}")
            return generate_mock_bars(self.mock_days)

        acquirer = HistoricalPartitionAcquirer(
            staging_dir=self.staging_dir,
            output_dir=self.output_dir,
            candidate_tickers=watchlist_50,
            download_fn=mock_downloader,
            sleep_fn=self.mock_sleep,
        )

        res = acquirer.run_acquisition()
        self.assertEqual(res["status"], "PROMOTION_REJECTED")
        self.assertEqual(res["candidate_success_count"], 44)
        self.assertFalse(os.path.exists(self.output_dir))
        self.assertTrue(os.path.exists(self.staging_dir))

    def test_13_exact_retry_sequence_delays_and_recovery(self):
        """Transient error retried with exact schedule [3.0, 6.0] and recovers on attempt 3 (DEFECT-02)."""
        call_count = {"AAPL": 0}

        def mock_downloader_transient(ticker, start=None, end=None):
            call_count[ticker] += 1
            if call_count[ticker] < 3:
                raise RuntimeError("HTTP 429 Too Many Requests (Rate limit exceeded)")
            return generate_mock_bars(self.mock_days)

        acquirer = HistoricalPartitionAcquirer(
            staging_dir=self.staging_dir,
            output_dir=self.output_dir,
            candidate_tickers=["AAPL"],
            max_retries=3,
            download_fn=mock_downloader_transient,
            sleep_fn=self.mock_sleep,
            random_fn=lambda: 0.0,  # Zero jitter for exact determinism
        )

        bars = acquirer.fetch_ticker_data_with_retries("AAPL")
        self.assertEqual(len(bars), len(self.mock_days))
        self.assertEqual(call_count["AAPL"], 3)
        # Exact backoff schedule: 3.0s, 6.0s (DEFECT-02)
        self.assertEqual(self.sleep_calls, [3.0, 6.0])

    def test_14_retry_max_attempts_exhausted(self):
        """Transient error fails 4 times (1 initial + 3 retries with [3.0, 6.0, 12.0] delays) then fails (DEFECT-02)."""
        call_count = {"AAPL": 0}

        def mock_downloader_always_fails(ticker, start=None, end=None):
            call_count[ticker] += 1
            raise RuntimeError("HTTP 503 Service Unavailable")

        acquirer = HistoricalPartitionAcquirer(
            staging_dir=self.staging_dir,
            output_dir=self.output_dir,
            candidate_tickers=["AAPL"],
            max_retries=3,
            download_fn=mock_downloader_always_fails,
            sleep_fn=self.mock_sleep,
            random_fn=lambda: 0.0,  # Zero jitter for exact determinism
        )

        with self.assertRaises(RuntimeError) as ctx:
            acquirer.fetch_ticker_data_with_retries("AAPL")
        self.assertIn("HTTP 503", str(ctx.exception))
        self.assertEqual(call_count["AAPL"], 4)
        # Exact 3 retries: 3.0s, 6.0s, 12.0s
        self.assertEqual(self.sleep_calls, [3.0, 6.0, 12.0])

    def test_15_permanent_errors_fail_immediately_without_retry(self):
        """Permanent error (e.g. 404 Not Found) fails immediately on attempt 1 with zero retry delays (Section 5)."""
        call_count = {"FAIL": 0}

        def mock_downloader_permanent(ticker, start=None, end=None):
            call_count[ticker] += 1
            raise ValueError("HTTP 404 Not Found for ticker FAIL")

        acquirer = HistoricalPartitionAcquirer(
            staging_dir=self.staging_dir,
            output_dir=self.output_dir,
            candidate_tickers=["FAIL"],
            max_retries=3,
            download_fn=mock_downloader_permanent,
            sleep_fn=self.mock_sleep,
        )

        with self.assertRaises(ValueError) as ctx:
            acquirer.fetch_ticker_data_with_retries("FAIL")
        self.assertEqual(call_count["FAIL"], 1)
        self.assertEqual(len(self.sleep_calls), 0)

    def test_16_permission_error_fails_immediately_without_retry(self):
        """PermissionError (date ceiling or lockbox) fails immediately without any retries (Section 5)."""
        call_count = {"AAPL": 0}

        def mock_downloader_leak(ticker, start=None, end=None):
            call_count[ticker] += 1
            bars = generate_mock_bars(self.mock_days[:2])
            bars.append({"Date": "2024-01-02", "Open": "100", "High": "101", "Low": "99", "Close": "100", "Volume": 100})
            return bars

        acquirer = HistoricalPartitionAcquirer(
            staging_dir=self.staging_dir,
            output_dir=self.output_dir,
            candidate_tickers=["AAPL"],
            max_retries=3,
            download_fn=mock_downloader_leak,
            sleep_fn=self.mock_sleep,
        )

        with self.assertRaises(PermissionError):
            acquirer.fetch_ticker_data_with_retries("AAPL")
        self.assertEqual(call_count["AAPL"], 1)
        self.assertEqual(len(self.sleep_calls), 0)

    def test_17_canonical_path_containment(self):
        """Canonical path resolution and forbidden token checking (Section 8)."""
        # Normal safe path passes
        safe = assert_safe_path(os.path.join(self.temp_dir, "safe_dir"))
        self.assertTrue(os.path.isabs(safe))

        # Forbidden tokens rejected
        with self.assertRaises(PermissionError):
            assert_safe_path("data/2026_lockbox")
        with self.assertRaises(PermissionError):
            assert_safe_path("data/TradingPaperState/logs")
        with self.assertRaises(PermissionError):
            assert_safe_path("sealed_test/primary")

    # =========================================================================
    # DEFECT-01 TESTS: Mandatory Core Proxies & Complete Bypass Removal
    # =========================================================================

    def test_18_defect01_proxy_bypass_removed_from_constructor(self):
        """Passing include_proxies to HistoricalPartitionAcquirer raises TypeError (DEFECT-01)."""
        with self.assertRaises(TypeError):
            HistoricalPartitionAcquirer(
                staging_dir=self.staging_dir,
                output_dir=self.output_dir,
                include_proxies=False,  # Parameter must not exist
            )

    def test_19_defect01_skip_proxies_removed_from_cli(self):
        """Passing --skip-proxies to CLI raises SystemExit/error (DEFECT-01)."""
        from scripts.acquire_historical_partitions import main
        with patch("sys.argv", ["acquire_historical_partitions.py", "--skip-proxies"]):
            with self.assertRaises(SystemExit) as ctx:
                main()
            # argparse exits with code 2 on unrecognized argument
            self.assertEqual(ctx.exception.code, 2)

    def test_20_defect01_missing_qqq_blocks_promotion(self):
        """Promotion is rejected if QQQ fails acquisition (DEFECT-01)."""
        def mock_downloader(ticker, start=None, end=None):
            if ticker == "QQQ":
                raise ConnectionError("Simulated QQQ provider outage")
            return generate_mock_bars(self.mock_days)

        acquirer = HistoricalPartitionAcquirer(
            staging_dir=self.staging_dir,
            output_dir=self.output_dir,
            candidate_tickers=["AAPL"],
            download_fn=mock_downloader,
            sleep_fn=self.mock_sleep,
        )

        res = acquirer.run_acquisition()
        self.assertEqual(res["status"], "PROMOTION_REJECTED")
        self.assertIn("QQQ", res["missing_proxies"])
        self.assertFalse(res["proxies_intact"])
        self.assertFalse(os.path.exists(self.output_dir))

    def test_21_defect01_missing_iwm_blocks_promotion(self):
        """Promotion is rejected if IWM fails acquisition (DEFECT-01)."""
        def mock_downloader(ticker, start=None, end=None):
            if ticker == "IWM":
                raise ConnectionError("Simulated IWM provider outage")
            return generate_mock_bars(self.mock_days)

        acquirer = HistoricalPartitionAcquirer(
            staging_dir=self.staging_dir,
            output_dir=self.output_dir,
            candidate_tickers=["AAPL"],
            download_fn=mock_downloader,
            sleep_fn=self.mock_sleep,
        )

        res = acquirer.run_acquisition()
        self.assertEqual(res["status"], "PROMOTION_REJECTED")
        self.assertIn("IWM", res["missing_proxies"])
        self.assertFalse(res["proxies_intact"])
        self.assertFalse(os.path.exists(self.output_dir))

    def test_22_defect01_corrupted_proxy_fails_verifier_and_blocks_promotion(self):
        """If a core proxy is downloaded but fails PartitionVerifier checks, promotion is aborted (DEFECT-01)."""
        def mock_downloader(ticker, start=None, end=None):
            bars = generate_mock_bars(self.mock_days)
            if ticker == "SPY":
                # Inject volume corruption: negative volume
                bars[5]["Volume"] = -1000
            return bars

        acquirer = HistoricalPartitionAcquirer(
            staging_dir=self.staging_dir,
            output_dir=self.output_dir,
            candidate_tickers=["AAPL"],
            download_fn=mock_downloader,
            sleep_fn=self.mock_sleep,
        )

        res = acquirer.run_acquisition()
        self.assertEqual(res["status"], "VERIFICATION_FAILED")
        self.assertFalse(os.path.exists(self.output_dir))
        self.assertTrue(os.path.exists(self.staging_dir))

    # =========================================================================
    # DEFECT-02 TESTS: Retry Pacing & >= 2.5s Floor Enforcement
    # =========================================================================

    def test_23_defect02_retry_backoff_schedule_strictly_ge_2_5s(self):
        """RETRY_BACKOFF_SCHEDULE values are all >= 2.5s and strictly increasing (DEFECT-02)."""
        self.assertEqual(RETRY_BACKOFF_SCHEDULE, [3.0, 6.0, 12.0])
        for delay in RETRY_BACKOFF_SCHEDULE:
            self.assertGreaterEqual(delay, MIN_PACING_DELAY)
        for i in range(len(RETRY_BACKOFF_SCHEDULE) - 1):
            self.assertLess(RETRY_BACKOFF_SCHEDULE[i], RETRY_BACKOFF_SCHEDULE[i + 1])

    def test_24_defect02_retry_jitter_pacing_floor_enforcement(self):
        """Negative or zero jitter cannot reduce retry delay below 2.5s pacing floor (DEFECT-02)."""
        call_count = {"AAPL": 0}

        def mock_downloader_transient(ticker, start=None, end=None):
            call_count[ticker] += 1
            if call_count[ticker] < 4:
                raise RuntimeError("HTTP 429 Too Many Requests")
            return generate_mock_bars(self.mock_days)

        # Inject negative jitter generator: should be floored safely
        acquirer = HistoricalPartitionAcquirer(
            staging_dir=self.staging_dir,
            output_dir=self.output_dir,
            candidate_tickers=["AAPL"],
            max_retries=3,
            download_fn=mock_downloader_transient,
            sleep_fn=self.mock_sleep,
            random_fn=lambda: -5.0,  # Hostile/negative jitter
        )

        bars = acquirer.fetch_ticker_data_with_retries("AAPL")
        self.assertEqual(len(bars), len(self.mock_days))
        self.assertEqual(call_count["AAPL"], 4)
        self.assertEqual(len(self.sleep_calls), 3)
        for s in self.sleep_calls:
            self.assertGreaterEqual(s, MIN_PACING_DELAY)
        # Even with -5.0 jitter, max(0.0, -5.0) = 0.0, so delays are [3.0, 6.0, 12.0]
        self.assertEqual(self.sleep_calls, [3.0, 6.0, 12.0])

    def test_25_defect02_underlying_provider_max_retries_zero(self):
        """_default_download_candles instantiates YFinanceMarketDataProvider with max_retries=0 (DEFECT-02)."""
        acquirer = HistoricalPartitionAcquirer(
            staging_dir=self.staging_dir,
            output_dir=self.output_dir,
            candidate_tickers=["AAPL"],
        )

        with patch("providers.yfinance_provider.YFinanceMarketDataProvider") as mock_provider_cls:
            mock_inst = MagicMock()
            mock_inst.fetch_historical_bars.return_value = []
            mock_provider_cls.return_value = mock_inst

            acquirer._default_download_candles("AAPL", date(2021, 1, 4), date(2021, 1, 10))

            mock_provider_cls.assert_called_once()
            _, kwargs = mock_provider_cls.call_args
            self.assertEqual(kwargs.get("max_retries"), 0)
            self.assertEqual(kwargs.get("cache_db_path"), ":memory:")

    # =========================================================================
    # DEFECT-03 TESTS: Constrained Staging Directory Cleanup
    # =========================================================================

    def test_26_defect03_staging_cleanup_rejects_parent_dir(self):
        """validate_safe_staging_dir rejects the parent partitions directory itself (DEFECT-03)."""
        with self.assertRaises(PermissionError) as ctx:
            validate_safe_staging_dir(self.partitions_dir)
        self.assertIn("must begin with '.staging_'", str(ctx.exception))

    def test_27_defect03_staging_cleanup_rejects_primary_dir(self):
        """validate_safe_staging_dir rejects the primary output partition directory (DEFECT-03)."""
        with self.assertRaises(PermissionError) as ctx:
            validate_safe_staging_dir(self.output_dir)
        self.assertIn("must begin with '.staging_'", str(ctx.exception))

    def test_28_defect03_staging_cleanup_rejects_unrelated_dir(self):
        """validate_safe_staging_dir rejects unrelated directories outside .planning/data/historical_partitions (DEFECT-03)."""
        # Directory named .staging_foo but in unrelated root or temp
        unrelated = os.path.join(self.temp_dir, ".staging_unrelated")
        with self.assertRaises(PermissionError) as ctx:
            validate_safe_staging_dir(unrelated)
        self.assertIn("is not inside an authorized", str(ctx.exception))

    def test_29_defect03_staging_cleanup_rejects_path_traversal(self):
        """validate_safe_staging_dir rejects path traversal attempting to escape partitions directory (DEFECT-03)."""
        traversal = os.path.join(self.partitions_dir, "..", ".staging_escaped")
        with self.assertRaises(PermissionError) as ctx:
            validate_safe_staging_dir(traversal)
        self.assertIn("is not inside an authorized", str(ctx.exception))

    def test_30_defect03_staging_cleanup_rejects_symlink(self):
        """validate_safe_staging_dir rejects symlinks targeting staging (DEFECT-03)."""
        target_dir = os.path.join(self.partitions_dir, ".staging_real")
        os.makedirs(target_dir, exist_ok=True)
        link_dir = os.path.join(self.partitions_dir, ".staging_symlink")

        try:
            os.symlink(target_dir, link_dir, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks not supported in current environment/permissions.")

        try:
            with self.assertRaises(PermissionError) as ctx:
                validate_safe_staging_dir(link_dir)
            self.assertIn("is a symlink", str(ctx.exception))
        finally:
            if os.path.islink(link_dir):
                os.unlink(link_dir)

    def test_31_defect03_staging_cleanup_valid_staging_dir_passes(self):
        """validate_safe_staging_dir accepts valid staging path and canonicalizes safely (DEFECT-03)."""
        valid_staging = os.path.join(self.partitions_dir, ".staging_2021_2023")
        resolved = validate_safe_staging_dir(valid_staging)
        self.assertTrue(os.path.isabs(resolved))
        self.assertEqual(resolved, os.path.realpath(valid_staging))
        self.assertTrue(os.path.basename(resolved).startswith(".staging_"))


if __name__ == "__main__":
    unittest.main()
