"""Unit and Integration Tests for Historical Partition Verification Engine.

Phase 6.2C — Step 3.4: Verification Tests

Tests:
1. Valid dataset verification & SHA-256 manifest compilation.
2. Monotonicity & Calendar gate: Non-trading days, weekends, duplicate dates quarantined.
3. Geometric OHLC invariants: High < Low, Close > High quarantined to quarantine/.
4. Non-positive prices and negative volume quarantined.
5. Mathematical split verification: Unadjusted split cliff (|P_T/P_{T-1} - 1/K| < 0.15/K) rejected.
6. Adjusted split passed without false rejection.
7. Dollar-volume ADV continuity invariant across split boundaries.
8. Unadjusted volume corruption detection.
9. Class 2 legitimate market gap preserved without modification.
10. Class 4 unexplained single-day spike quarantined without silent repair.
11. Core proxies failure (missing SPY) triggers VALIDATION_FAILED.
12. Partial universe threshold (<45 equities) triggers VALIDATION_FAILED.
13. Exactly 45 equities passes threshold.
14. Tamper detection: Verifying SHA-256 checksum mismatch.
"""

from datetime import date, timedelta
from decimal import Decimal
import csv
import json
import os
import shutil
import tempfile
import unittest

from scripts.verify_historical_partitions import (
    compute_sha256,
    PartitionVerifier,
    CORE_PROXIES,
    KNOWN_SPLITS,
)
from market_data_models import MarketClock
from universe import DEFAULT_CURATED_WATCHLIST


def get_nyse_trading_days(start_date: date, count: int):
    """Generate actual NYSE trading days using MarketClock."""
    days = []
    curr = start_date
    while len(days) < count:
        is_trading, _ = MarketClock.is_historical_trading_day(curr)
        if is_trading:
            days.append(curr)
        curr += timedelta(days=1)
    return days


def write_test_csv(filepath: str, bars):
    """Write bars to CSV with standard schema."""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["Date", "Open", "High", "Low", "Close", "Volume"])
        writer.writeheader()
        for b in bars:
            writer.writerow({
                "Date": b["Date"],
                "Open": f"{b['Open']:.4f}" if isinstance(b["Open"], (Decimal, float)) else str(b["Open"]),
                "High": f"{b['High']:.4f}" if isinstance(b["High"], (Decimal, float)) else str(b["High"]),
                "Low": f"{b['Low']:.4f}" if isinstance(b["Low"], (Decimal, float)) else str(b["Low"]),
                "Close": f"{b['Close']:.4f}" if isinstance(b["Close"], (Decimal, float)) else str(b["Close"]),
                "Volume": str(b["Volume"]),
            })


def make_valid_bars(days, base_price=Decimal("150.00"), base_vol=1_000_000):
    """Generate geometrically sound synthetic bars."""
    bars = []
    price = base_price
    for d in days:
        o = price
        h = o + Decimal("2.00")
        l = o - Decimal("1.50")
        c = o + Decimal("0.50")
        bars.append({
            "Date": d.isoformat(),
            "Open": o,
            "High": h,
            "Low": l,
            "Close": c,
            "Volume": base_vol,
        })
        price = c
    return bars


class TestHistoricalVerification(unittest.TestCase):
    """Comprehensive test suite for verify_historical_partitions.py."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_partition_verify_")
        self.partition_dir = os.path.join(self.temp_dir, "test_partition")
        os.makedirs(self.partition_dir, exist_ok=True)
        self.days_50 = get_nyse_trading_days(date(2021, 1, 4), 50)

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def test_01_valid_dataset_passes_and_generates_manifest(self):
        """Valid dataset for proxies + 2 equities passes validation and generates SHA-256 manifest."""
        # Create proxies + 2 equities
        for sym in ["SPY", "QQQ", "IWM", "AAPL", "MSFT"]:
            bars = make_valid_bars(self.days_50, base_price=Decimal("200.00"))
            write_test_csv(os.path.join(self.partition_dir, f"{sym}_daily.csv"), bars)

        verifier = PartitionVerifier(
            partition_dir=self.partition_dir,
            expected_sessions=50,
            min_equities=2,
            target_equities=["AAPL", "MSFT"],
        )
        res = verifier.verify_partition()
        self.assertTrue(res["overall_passed"])
        self.assertEqual(res["manifest_status"], "READY_FOR_BACKTEST")
        self.assertEqual(res["total_validated_files"], 5)
        self.assertEqual(res["quarantined_count"], 0)

        # Manifest exists and is valid JSON
        self.assertTrue(os.path.exists(res["manifest_path"]))
        with open(res["manifest_path"], "r", encoding="utf-8") as f:
            manifest = json.load(f)
        self.assertEqual(manifest["partition_status"], "READY_FOR_BACKTEST")
        self.assertIn("SPY_daily.csv", manifest["files"])
        self.assertEqual(manifest["files"]["SPY_daily.csv"]["price_adjustment"], "SPLIT_ADJUSTED")

        # Audit markdown exists
        self.assertTrue(os.path.exists(res["audit_path"]))

    def test_02_calendar_violation_quarantined(self):
        """Weekend observation is quarantined to quarantine/ without silent repair."""
        bars = make_valid_bars(self.days_50)
        # Inject Saturday date in place of first Saturday: 2021-01-09
        bars[5]["Date"] = "2021-01-09"  # Saturday
        write_test_csv(os.path.join(self.partition_dir, "AAPL_daily.csv"), bars)

        verifier = PartitionVerifier(partition_dir=self.partition_dir, min_equities=1)
        res = verifier.verify_single_csv(os.path.join(self.partition_dir, "AAPL_daily.csv"), "AAPL")

        self.assertEqual(res.status, "EXCLUDED_QUARANTINED")
        self.assertIn("Weekend", res.exclusion_reason)
        # File moved to quarantine
        self.assertFalse(os.path.exists(os.path.join(self.partition_dir, "AAPL_daily.csv")))
        self.assertTrue(os.path.exists(res.quarantine_path))

    def test_03_monotonicity_violation_quarantined(self):
        """Non-monotonic dates are quarantined."""
        bars = make_valid_bars(self.days_50)
        # Reverse order of two dates
        bars[15]["Date"], bars[14]["Date"] = bars[14]["Date"], bars[15]["Date"]
        write_test_csv(os.path.join(self.partition_dir, "AAPL_daily.csv"), bars)

        verifier = PartitionVerifier(partition_dir=self.partition_dir)
        res = verifier.verify_single_csv(os.path.join(self.partition_dir, "AAPL_daily.csv"), "AAPL")
        self.assertEqual(res.status, "EXCLUDED_QUARANTINED")
        self.assertIn("monotonic", res.exclusion_reason.lower())

    def test_04_geometric_ohlc_invariants_quarantined(self):
        """High < Low or Close > High is quarantined."""
        bars = make_valid_bars(self.days_50)
        # Violate High < Low
        bars[5]["High"] = Decimal("100.00")
        bars[5]["Low"] = Decimal("150.00")
        write_test_csv(os.path.join(self.partition_dir, "MSFT_daily.csv"), bars)

        verifier = PartitionVerifier(partition_dir=self.partition_dir)
        res = verifier.verify_single_csv(os.path.join(self.partition_dir, "MSFT_daily.csv"), "MSFT")
        self.assertEqual(res.status, "EXCLUDED_QUARANTINED")
        self.assertIn("Geometric", res.exclusion_reason)

    def test_05_non_positive_price_and_negative_volume_quarantined(self):
        """Zero price or negative volume is quarantined."""
        bars = make_valid_bars(self.days_50)
        bars[3]["Close"] = Decimal("0.00")
        write_test_csv(os.path.join(self.partition_dir, "NVDA_daily.csv"), bars)

        verifier = PartitionVerifier(partition_dir=self.partition_dir)
        res = verifier.verify_single_csv(os.path.join(self.partition_dir, "NVDA_daily.csv"), "NVDA")
        self.assertEqual(res.status, "EXCLUDED_QUARANTINED")
        self.assertIn("Non-positive", res.exclusion_reason)

    def test_06_unadjusted_split_cliff_rejected_and_quarantined(self):
        """Unadjusted forward split (NVDA 4:1 on 2021-07-20) triggers UNADJUSTED_SPLIT_ERROR."""
        split_date = date(2021, 7, 20)
        days = get_nyse_trading_days(date(2021, 6, 1), 60)
        bars = make_valid_bars(days, base_price=Decimal("750.00"), base_vol=1_000_000)

        # Find split date index
        split_idx = next(i for i, b in enumerate(bars) if b["Date"] == split_date.isoformat())

        # Simulate unadjusted raw prices: on split date, price drops by ~4x (K=4.0, ratio ~0.25)
        prev_close = bars[split_idx - 1]["Close"]
        # Set post-split prices to ~0.25 * pre-split price
        for j in range(split_idx, len(bars)):
            bars[j]["Open"] = bars[j]["Open"] / Decimal("4.0")
            bars[j]["High"] = bars[j]["High"] / Decimal("4.0")
            bars[j]["Low"] = bars[j]["Low"] / Decimal("4.0")
            bars[j]["Close"] = bars[j]["Close"] / Decimal("4.0")

        write_test_csv(os.path.join(self.partition_dir, "NVDA_daily.csv"), bars)

        verifier = PartitionVerifier(partition_dir=self.partition_dir)
        res = verifier.verify_single_csv(os.path.join(self.partition_dir, "NVDA_daily.csv"), "NVDA")
        self.assertEqual(res.status, "EXCLUDED_QUARANTINED")
        self.assertIn("UNADJUSTED_SPLIT_ERROR", res.exclusion_reason)
        self.assertTrue(os.path.exists(res.quarantine_path))

    def test_07_adjusted_split_passes_cleanly(self):
        """Properly split-adjusted NVDA series passes split verification."""
        split_date = date(2021, 7, 20)
        days = get_nyse_trading_days(date(2021, 6, 1), 60)
        # Whole series is split-adjusted, so price across split date moves ordinary market return (~1%)
        bars = make_valid_bars(days, base_price=Decimal("180.00"), base_vol=10_000_000)
        write_test_csv(os.path.join(self.partition_dir, "NVDA_daily.csv"), bars)

        verifier = PartitionVerifier(partition_dir=self.partition_dir)
        res = verifier.verify_single_csv(os.path.join(self.partition_dir, "NVDA_daily.csv"), "NVDA")
        self.assertEqual(res.status, "VALIDATED")

    def test_08_dollar_volume_adv_continuity_violation_quarantined(self):
        """Unadjusted volume across split triggers UNADJUSTED_VOLUME_CORRUPTION."""
        split_date = date(2022, 6, 6)  # AMZN 20:1 split
        days = get_nyse_trading_days(date(2022, 4, 15), 60)
        bars = make_valid_bars(days, base_price=Decimal("120.00"), base_vol=5_000_000)

        split_idx = next(i for i, b in enumerate(bars) if b["Date"] == split_date.isoformat())

        # If price was adjusted but volume was NOT adjusted, post-split volume jumps 20x
        for j in range(split_idx, len(bars)):
            bars[j]["Volume"] = bars[j]["Volume"] * 20

        write_test_csv(os.path.join(self.partition_dir, "AMZN_daily.csv"), bars)

        verifier = PartitionVerifier(partition_dir=self.partition_dir)
        res = verifier.verify_single_csv(os.path.join(self.partition_dir, "AMZN_daily.csv"), "AMZN")
        self.assertEqual(res.status, "EXCLUDED_QUARANTINED")
        self.assertIn("UNADJUSTED_VOLUME_CORRUPTION", res.exclusion_reason)

    def test_09_legitimate_market_gap_preserved(self):
        """Class 2 legitimate market gap (e.g. 18% earnings move with high volume) is preserved."""
        bars = make_valid_bars(self.days_50, base_price=Decimal("100.00"), base_vol=1_000_000)
        # Day 25: 18% gap on 3x volume
        bars[25]["Open"] = Decimal("118.00")
        bars[25]["High"] = Decimal("122.00")
        bars[25]["Low"] = Decimal("117.00")
        bars[25]["Close"] = Decimal("120.00")
        bars[25]["Volume"] = 3_000_000

        write_test_csv(os.path.join(self.partition_dir, "AAPL_daily.csv"), bars)

        verifier = PartitionVerifier(partition_dir=self.partition_dir)
        res = verifier.verify_single_csv(os.path.join(self.partition_dir, "AAPL_daily.csv"), "AAPL")
        self.assertEqual(res.status, "VALIDATED")

    def test_10_unexplained_spike_with_thin_volume_quarantined(self):
        """Class 4 corruption (40% single-day spike on thin volume) is quarantined."""
        bars = make_valid_bars(self.days_50, base_price=Decimal("100.00"), base_vol=1_000_000)
        # Day 25: 50% spike on 0.05x volume
        bars[25]["Open"] = Decimal("170.00")
        bars[25]["High"] = Decimal("175.00")
        bars[25]["Low"] = Decimal("168.00")
        bars[25]["Close"] = Decimal("172.00")
        bars[25]["Volume"] = 50_000  # Thin volume

        write_test_csv(os.path.join(self.partition_dir, "TSLA_daily.csv"), bars)

        verifier = PartitionVerifier(partition_dir=self.partition_dir)
        res = verifier.verify_single_csv(os.path.join(self.partition_dir, "TSLA_daily.csv"), "TSLA")
        self.assertEqual(res.status, "EXCLUDED_QUARANTINED")
        self.assertIn("Corruption", res.discontinuity_class)

    def test_11_core_proxies_missing_fails_partition(self):
        """Missing SPY causes partition verification to fail."""
        # Only QQQ, IWM, AAPL present
        for sym in ["QQQ", "IWM", "AAPL"]:
            bars = make_valid_bars(self.days_50)
            write_test_csv(os.path.join(self.partition_dir, f"{sym}_daily.csv"), bars)

        verifier = PartitionVerifier(partition_dir=self.partition_dir, min_equities=1, target_equities=["AAPL"])
        res = verifier.verify_partition()
        self.assertFalse(res["overall_passed"])
        self.assertEqual(res["manifest_status"], "VALIDATION_FAILED")
        self.assertIn("SPY", res["missing_proxies"])

    def test_12_equities_threshold_gate(self):
        """Less than minimum required candidate equities causes validation to fail."""
        # 3 proxies + 2 equities, but min_equities = 3
        for sym in ["SPY", "QQQ", "IWM", "AAPL", "MSFT"]:
            bars = make_valid_bars(self.days_50)
            write_test_csv(os.path.join(self.partition_dir, f"{sym}_daily.csv"), bars)

        verifier = PartitionVerifier(
            partition_dir=self.partition_dir,
            min_equities=3,
            target_equities=["AAPL", "MSFT", "NVDA"],
        )
        res = verifier.verify_partition()
        self.assertFalse(res["overall_passed"])
        self.assertEqual(res["manifest_status"], "VALIDATION_FAILED")
        self.assertEqual(res["equities_passed_count"], 2)

    def test_13_exactly_45_equities_passes_threshold(self):
        """Exactly 45 candidate equities + 3 proxies passes validation."""
        watchlist_50 = list(DEFAULT_CURATED_WATCHLIST)[:50]
        # Create 3 proxies + first 45 equities
        for sym in ["SPY", "QQQ", "IWM"] + watchlist_50[:45]:
            bars = make_valid_bars(self.days_50)
            write_test_csv(os.path.join(self.partition_dir, f"{sym}_daily.csv"), bars)

        verifier = PartitionVerifier(
            partition_dir=self.partition_dir,
            min_equities=45,
            target_equities=watchlist_50,
        )
        res = verifier.verify_partition()
        self.assertTrue(res["overall_passed"])
        self.assertEqual(res["manifest_status"], "READY_FOR_BACKTEST")
        self.assertEqual(res["equities_passed_count"], 45)
        self.assertTrue(res["equities_threshold_met"])

    def test_14_manifest_sha256_checksum_verification_and_tampering(self):
        """SHA-256 in manifest matches file; modifying file triggers mismatch."""
        for sym in ["SPY", "QQQ", "IWM", "AAPL"]:
            bars = make_valid_bars(self.days_50)
            write_test_csv(os.path.join(self.partition_dir, f"{sym}_daily.csv"), bars)

        verifier = PartitionVerifier(
            partition_dir=self.partition_dir,
            min_equities=1,
            target_equities=["AAPL"],
        )
        res = verifier.verify_partition()
        self.assertTrue(res["overall_passed"])

        # Read manifest
        with open(res["manifest_path"], "r", encoding="utf-8") as f:
            manifest = json.load(f)

        aapl_sha_orig = manifest["files"]["AAPL_daily.csv"]["sha256"]
        aapl_file = os.path.join(self.partition_dir, "AAPL_daily.csv")
        self.assertEqual(compute_sha256(aapl_file), aapl_sha_orig)

        # Tamper with file
        with open(aapl_file, "a", encoding="utf-8") as f:
            f.write("# tampered comment line\n")

        # Now hash must not match
        tampered_sha = compute_sha256(aapl_file)
        self.assertNotEqual(tampered_sha, aapl_sha_orig)

    def test_15_nan_and_infinity_in_ohlc_quarantined(self):
        """NaN and Infinity in OHLC values are quarantined (Section 6)."""
        bars = make_valid_bars(self.days_50)
        bars[5]["Open"] = "NaN"
        write_test_csv(os.path.join(self.partition_dir, "AAPL_daily.csv"), bars)

        verifier = PartitionVerifier(partition_dir=self.partition_dir)
        res = verifier.verify_single_csv(os.path.join(self.partition_dir, "AAPL_daily.csv"), "AAPL")
        self.assertEqual(res.status, "EXCLUDED_QUARANTINED")
        self.assertIn("NaN/Inf", res.exclusion_reason)

        # Infinity in Close
        bars2 = make_valid_bars(self.days_50)
        bars2[5]["Close"] = "Infinity"
        write_test_csv(os.path.join(self.partition_dir, "MSFT_daily.csv"), bars2)
        res2 = verifier.verify_single_csv(os.path.join(self.partition_dir, "MSFT_daily.csv"), "MSFT")
        self.assertEqual(res2.status, "EXCLUDED_QUARANTINED")
        self.assertIn("NaN/Inf", res2.exclusion_reason)

    def test_16_nan_and_infinity_in_volume_quarantined(self):
        """NaN and Infinity in volume values are quarantined (Section 6)."""
        bars = make_valid_bars(self.days_50)
        bars[7]["Volume"] = "nan"
        write_test_csv(os.path.join(self.partition_dir, "NVDA_daily.csv"), bars)

        verifier = PartitionVerifier(partition_dir=self.partition_dir)
        res = verifier.verify_single_csv(os.path.join(self.partition_dir, "NVDA_daily.csv"), "NVDA")
        self.assertEqual(res.status, "EXCLUDED_QUARANTINED")
        self.assertIn("NaN/Inf", res.exclusion_reason)

    def test_17_fractional_volume_quarantined_not_truncated(self):
        """Fractional volume values are rejected and quarantined, never silently truncated (Section 6)."""
        bars = make_valid_bars(self.days_50)
        bars[10]["Volume"] = "1000500.5"  # Fractional volume
        write_test_csv(os.path.join(self.partition_dir, "AAPL_daily.csv"), bars)

        verifier = PartitionVerifier(partition_dir=self.partition_dir)
        res = verifier.verify_single_csv(os.path.join(self.partition_dir, "AAPL_daily.csv"), "AAPL")
        self.assertEqual(res.status, "EXCLUDED_QUARANTINED")
        self.assertIn("Fractional volume", res.exclusion_reason)

    def test_18_negative_volume_and_negative_price_quarantined(self):
        """Negative volume and negative prices are quarantined (Section 6)."""
        bars = make_valid_bars(self.days_50)
        bars[12]["Volume"] = -50000
        write_test_csv(os.path.join(self.partition_dir, "GOOGL_daily.csv"), bars)

        verifier = PartitionVerifier(partition_dir=self.partition_dir)
        res = verifier.verify_single_csv(os.path.join(self.partition_dir, "GOOGL_daily.csv"), "GOOGL")
        self.assertEqual(res.status, "EXCLUDED_QUARANTINED")
        self.assertIn("Negative volume", res.exclusion_reason)

        bars2 = make_valid_bars(self.days_50)
        bars2[12]["Low"] = Decimal("-10.00")
        write_test_csv(os.path.join(self.partition_dir, "AMZN_daily.csv"), bars2)
        res2 = verifier.verify_single_csv(os.path.join(self.partition_dir, "AMZN_daily.csv"), "AMZN")
        self.assertEqual(res2.status, "EXCLUDED_QUARANTINED")

    def test_19_quarantine_collision_preserves_evidence_without_overwriting(self):
        """Quarantine preserves evidence without deleting existing files on collision (Section 7)."""
        verifier = PartitionVerifier(partition_dir=self.partition_dir)
        quarantine_dir = verifier.quarantine_dir

        # File 1 with content A
        bars_a = make_valid_bars(self.days_50)
        bars_a[5]["Close"] = Decimal("0.00")  # corrupted
        fpath_1 = os.path.join(self.partition_dir, "CORRUPT_daily.csv")
        write_test_csv(fpath_1, bars_a)

        res_1 = verifier.verify_single_csv(fpath_1, "CORRUPT")
        self.assertEqual(res_1.status, "EXCLUDED_QUARANTINED")
        first_qpath = res_1.quarantine_path
        self.assertTrue(os.path.exists(first_qpath))
        with open(first_qpath, "r") as f:
            content_1 = f.read()

        # File 2 with same filename but different data (content B)
        bars_b = make_valid_bars(self.days_50)
        bars_b[10]["Close"] = Decimal("-50.00")  # corrupted differently
        fpath_2 = os.path.join(self.partition_dir, "CORRUPT_daily.csv")
        write_test_csv(fpath_2, bars_b)

        res_2 = verifier.verify_single_csv(fpath_2, "CORRUPT")
        self.assertEqual(res_2.status, "EXCLUDED_QUARANTINED")
        second_qpath = res_2.quarantine_path

        # Verify BOTH files exist and neither was overwritten
        self.assertNotEqual(first_qpath, second_qpath)
        self.assertTrue(os.path.exists(first_qpath))
        self.assertTrue(os.path.exists(second_qpath))

        with open(first_qpath, "r") as f:
            self.assertEqual(f.read(), content_1)  # Preserved byte-for-byte!
        with open(second_qpath, "r") as f:
            self.assertIn("-50.00", f.read())

    def test_20_path_containment_rejects_lockbox_and_paper_state(self):
        """PartitionVerifier rejects forbidden paths resolving to lockbox or paper state (Section 8)."""
        with self.assertRaises(PermissionError):
            PartitionVerifier(partition_dir="C:/trading/data/2026_lockbox_sealed")

        from paper_storage import get_default_paper_state_dir
        with self.assertRaises(PermissionError):
            PartitionVerifier(
                partition_dir=self.partition_dir,
                quarantine_dir=get_default_paper_state_dir(),
            )


if __name__ == "__main__":
    unittest.main()
