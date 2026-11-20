"""Unit test suite for trader-swing local market data cache.

Verifies:
1. Cache initialization and table creation in SQLite.
2. Storing and reading historical candles.
3. Duplicate timestamp prevention (replaces or updates record, does not create duplicates).
4. Updating a FORMING candle to COMPLETE.
5. Historical range querying with start/end bounds.
6. Latest candle retrieval.
7. Metadata persistence in cache_meta.
8. Cache freshness and staleness checks (is_stale).
9. Partial and full cache clearance (clear).
10. Interrupted write rollback and transactional integrity.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
import sqlite3
import sys
import unittest

SWING_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".agents",
    "skills",
    "trader-swing",
)
if SWING_DIR not in sys.path:
    sys.path.insert(0, SWING_DIR)

from market_data_models import (
    CandleStatus,
    OHLCVCandle,
    to_utc,
    UTC,
)
from market_data_cache import MarketDataCache


class TestSwingMarketDataCache(unittest.TestCase):
    """Test suite for local market data cache persistence, querying, and transactional isolation."""

    def setUp(self):
        # Use isolated in-memory database for unit tests
        self.cache = MarketDataCache(db_path=":memory:")
        self.ref_time = datetime(2026, 10, 14, 14, 0, 0, tzinfo=UTC)

    def tearDown(self):
        self.cache.close()

    def _make_candle(self, timestamp, ticker="AAPL", interval="15m", close="150.00", status=CandleStatus.COMPLETE):
        return OHLCVCandle(
            ticker=ticker,
            interval=interval,
            timestamp=timestamp,
            open=Decimal("149.00"),
            high=Decimal("151.00"),
            low=Decimal("148.50"),
            close=Decimal(close),
            volume=10000,
            status=status,
            source="yahoo",
            received_timestamp=self.ref_time,
            is_delayed=True,
        )

    def test_01_store_and_read_candles(self):
        c1 = self._make_candle(self.ref_time - timedelta(minutes=30), close="150.00")
        c2 = self._make_candle(self.ref_time - timedelta(minutes=15), close="151.00")

        stored_count = self.cache.store_candles([c1, c2])
        self.assertEqual(stored_count, 2)

        bars = self.cache.get_historical_candles(
            "AAPL", "15m", self.ref_time - timedelta(hours=1), self.ref_time
        )
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0].close, Decimal("150.00"))
        self.assertEqual(bars[1].close, Decimal("151.00"))

    def test_02_duplicate_prevention_on_conflict(self):
        c1 = self._make_candle(self.ref_time, close="150.00")
        self.cache.store_candles([c1])

        # Store candle with same timestamp but updated close price
        c1_updated = self._make_candle(self.ref_time, close="152.00")
        self.cache.store_candles([c1_updated])

        bars = self.cache.get_historical_candles(
            "AAPL", "15m", self.ref_time - timedelta(minutes=5), self.ref_time + timedelta(minutes=5)
        )
        # Should be updated in-place without creating a duplicate record
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].close, Decimal("152.00"))

    def test_03_update_forming_to_complete(self):
        # Bar initially stored as FORMING
        forming_bar = self._make_candle(self.ref_time, close="150.50", status=CandleStatus.FORMING)
        self.cache.store_candles([forming_bar])

        latest = self.cache.get_latest_candle("AAPL", "15m")
        self.assertEqual(latest.status, CandleStatus.FORMING)

        # Later, bar completes and is stored as COMPLETE
        completed_bar = self._make_candle(self.ref_time, close="151.00", status=CandleStatus.COMPLETE)
        self.cache.store_candles([completed_bar])

        latest_updated = self.cache.get_latest_candle("AAPL", "15m")
        self.assertEqual(latest_updated.status, CandleStatus.COMPLETE)
        self.assertEqual(latest_updated.close, Decimal("151.00"))

    def test_04_range_filtering(self):
        t0 = self.ref_time - timedelta(minutes=45)
        t1 = self.ref_time - timedelta(minutes=30)
        t2 = self.ref_time - timedelta(minutes=15)
        t3 = self.ref_time

        c0 = self._make_candle(t0, close="148.00")
        c1 = self._make_candle(t1, close="149.00")
        c2 = self._make_candle(t2, close="150.00")
        c3 = self._make_candle(t3, close="151.00")
        self.cache.store_candles([c0, c1, c2, c3])

        # Query only t1 to t2
        sub_range = self.cache.get_historical_candles("AAPL", "15m", t1, t2)
        self.assertEqual(len(sub_range), 2)
        self.assertEqual(sub_range[0].timestamp, t1)
        self.assertEqual(sub_range[1].timestamp, t2)

    def test_05_latest_candle_retrieval(self):
        c1 = self._make_candle(self.ref_time - timedelta(minutes=15), close="149.00")
        c2 = self._make_candle(self.ref_time, close="152.50")
        self.cache.store_candles([c1, c2])

        latest = self.cache.get_latest_candle("AAPL", "15m")
        self.assertIsNotNone(latest)
        self.assertEqual(latest.timestamp, self.ref_time)
        self.assertEqual(latest.close, Decimal("152.50"))

    def test_06_metadata_and_staleness_tracking(self):
        c1 = self._make_candle(self.ref_time)
        self.cache.store_candles([c1])

        # Immediately after writing, age is 0 -> not stale within 60 seconds
        self.assertFalse(self.cache.is_stale("AAPL", "15m", max_age_seconds=60, current_time=self.ref_time))

        # Check 5 minutes later -> stale for 60-second threshold
        future_time = self.ref_time + timedelta(minutes=5)
        self.assertTrue(self.cache.is_stale("AAPL", "15m", max_age_seconds=60, current_time=future_time))

        # Unknown ticker is always stale
        self.assertTrue(self.cache.is_stale("UNKNOWN", "15m", max_age_seconds=60, current_time=self.ref_time))

    def test_07_clear_cache(self):
        c_aapl = self._make_candle(self.ref_time, ticker="AAPL")
        c_msft = self._make_candle(self.ref_time, ticker="MSFT")
        self.cache.store_candles([c_aapl, c_msft])

        # Clear only AAPL
        self.cache.clear(ticker="AAPL")
        self.assertIsNone(self.cache.get_latest_candle("AAPL", "15m"))
        self.assertIsNotNone(self.cache.get_latest_candle("MSFT", "15m"))

        # Clear all
        self.cache.clear()
        self.assertIsNone(self.cache.get_latest_candle("MSFT", "15m"))

    def test_08_transactional_rollback_on_interrupted_write(self):
        c_good = self._make_candle(self.ref_time)
        self.cache.store_candles([c_good])

        # Attempt to insert an invalid batch directly into database transaction
        try:
            with self.cache._conn:
                self.cache._conn.execute(
                    "INSERT INTO candles (ticker, interval, timestamp, open, high, low, close, volume, status, source, received_timestamp, is_delayed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    ("AAPL", "15m", (self.ref_time + timedelta(minutes=15)).isoformat(), "150", "151", "149", "150", 100, "COMPLETE", "yahoo", self.ref_time.isoformat(), 1),
                )
                # Intentionally cause constraint violation or SQL syntax error to trigger rollback
                raise sqlite3.OperationalError("Simulated interruption/power failure")
        except sqlite3.OperationalError:
            pass

        # Verify database remains in healthy, uncorrupted state with only original candle
        bars = self.cache.get_historical_candles("AAPL", "15m", self.ref_time - timedelta(hours=1), self.ref_time + timedelta(hours=1))
        self.assertEqual(len(bars), 1)

    # 9. Fix D-3: COMPLETE to FORMING regression is rejected and OHLCV preserved
    def test_09_complete_to_forming_regression_is_rejected_and_ohlcv_preserved(self):
        # Step 1: Store a finalized COMPLETE candle
        complete_bar = OHLCVCandle(
            ticker="AAPL",
            interval="15m",
            timestamp=self.ref_time,
            open=Decimal("150.00"),
            high=Decimal("155.00"),
            low=Decimal("148.00"),
            close=Decimal("153.00"),
            volume=50000,
            status=CandleStatus.COMPLETE,
            source="yahoo",
            received_timestamp=self.ref_time,
        )
        self.cache.store_candles([complete_bar])

        # Step 2: Attempt to overwrite with a FORMING candle having different values
        corrupting_bar = OHLCVCandle(
            ticker="AAPL",
            interval="15m",
            timestamp=self.ref_time,
            open=Decimal("100.00"),
            high=Decimal("105.00"),
            low=Decimal("95.00"),
            close=Decimal("102.00"),
            volume=100,
            status=CandleStatus.FORMING,
            source="delayed_feed",
            received_timestamp=self.ref_time + timedelta(minutes=10),
        )
        self.cache.store_candles([corrupting_bar])

        # Step 3: Verify the record in candles table retained COMPLETE status and original OHLCV
        latest = self.cache.get_latest_candle("AAPL", "15m")
        self.assertEqual(latest.status, CandleStatus.COMPLETE)
        self.assertEqual(latest.open, Decimal("150.00"))
        self.assertEqual(latest.high, Decimal("155.00"))
        self.assertEqual(latest.low, Decimal("148.00"))
        self.assertEqual(latest.close, Decimal("153.00"))
        self.assertEqual(latest.volume, 50000)
        self.assertEqual(latest.source, "yahoo")

        # Step 4: Verify cache_meta last_status also remains COMPLETE
        row = self.cache._conn.execute(
            "SELECT last_status FROM cache_meta WHERE ticker = ? AND interval = ?",
            ("AAPL", "15m")
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["last_status"], "COMPLETE")

    # 10. Fix D-3: COMPLETE to COMPLETE behaves deterministically
    def test_10_complete_to_complete_updates_deterministically(self):
        c1 = self._make_candle(self.ref_time, close="150.00", status=CandleStatus.COMPLETE)
        self.cache.store_candles([c1])

        # Final reconciliation or split adjustment updates values
        c2 = self._make_candle(self.ref_time, close="150.25", status=CandleStatus.COMPLETE)
        self.cache.store_candles([c2])

        latest = self.cache.get_latest_candle("AAPL", "15m")
        self.assertEqual(latest.status, CandleStatus.COMPLETE)
        self.assertEqual(latest.close, Decimal("150.25"))

    # 11. Fix D-3: UNKNOWN status transitions behave explicitly
    def test_11_unknown_status_transitions_handled_safely(self):
        # Direct SQL insertion of an UNKNOWN record to test recovery
        with self.cache._conn:
            self.cache._conn.execute(
                """
                INSERT INTO candles (
                    ticker, interval, timestamp, open, high, low, close, volume,
                    status, source, received_timestamp, is_delayed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("AAPL", "15m", self.ref_time.isoformat(), "150", "151", "149", "150", 1000, "UNKNOWN", "yahoo", self.ref_time.isoformat(), 1),
            )

        # Incoming FORMING bar promotes UNKNOWN to FORMING
        forming_bar = self._make_candle(self.ref_time, close="150.50", status=CandleStatus.FORMING)
        self.cache.store_candles([forming_bar])
        latest = self.cache.get_latest_candle("AAPL", "15m")
        self.assertEqual(latest.status, CandleStatus.FORMING)

        # Incoming COMPLETE bar promotes FORMING to COMPLETE
        complete_bar = self._make_candle(self.ref_time, close="151.00", status=CandleStatus.COMPLETE)
        self.cache.store_candles([complete_bar])
        latest2 = self.cache.get_latest_candle("AAPL", "15m")
        self.assertEqual(latest2.status, CandleStatus.COMPLETE)

    # 12. Fix D-3: Other ticker/interval/timestamp records remain unaffected
    def test_12_other_records_unaffected_by_regression_attempt(self):
        t1 = self.ref_time
        t2 = self.ref_time + timedelta(minutes=15)

        c_aapl_1 = self._make_candle(t1, ticker="AAPL", interval="15m", status=CandleStatus.COMPLETE)
        c_aapl_2 = self._make_candle(t2, ticker="AAPL", interval="15m", status=CandleStatus.FORMING)
        c_msft_1 = self._make_candle(t1, ticker="MSFT", interval="15m", status=CandleStatus.COMPLETE)
        c_aapl_1h = self._make_candle(t1, ticker="AAPL", interval="1h", status=CandleStatus.COMPLETE)
        self.cache.store_candles([c_aapl_1, c_aapl_2, c_msft_1, c_aapl_1h])

        # Attempt regression on AAPL 15m t1
        attempted_regression = self._make_candle(t1, ticker="AAPL", interval="15m", close="999.00", status=CandleStatus.FORMING)
        self.cache.store_candles([attempted_regression])

        # Verify AAPL 15m t1 is unchanged
        aapl_t1 = self.cache.get_historical_candles("AAPL", "15m", t1, t1)[0]
        self.assertEqual(aapl_t1.status, CandleStatus.COMPLETE)
        self.assertEqual(aapl_t1.close, Decimal("150.00"))

        # Verify AAPL 15m t2 is intact (FORMING)
        aapl_t2 = self.cache.get_historical_candles("AAPL", "15m", t2, t2)[0]
        self.assertEqual(aapl_t2.status, CandleStatus.FORMING)

        # Verify MSFT is intact (COMPLETE)
        msft_t1 = self.cache.get_historical_candles("MSFT", "15m", t1, t1)[0]
        self.assertEqual(msft_t1.status, CandleStatus.COMPLETE)

        # Verify AAPL 1h is intact (COMPLETE)
        aapl_1h = self.cache.get_historical_candles("AAPL", "1h", t1, t1)[0]
        self.assertEqual(aapl_1h.status, CandleStatus.COMPLETE)

    # 13. Fix D: Full pre-market to close cache lifecycle regression
    def test_13_full_pre_market_to_close_cache_lifecycle_regression(self):
        bar_ts = datetime(2026, 10, 19, 4, 0, 0, tzinfo=UTC)

        # 1. 8:00 AM ET - pre-market observation
        c_8am = OHLCVCandle(
            ticker="AAPL",
            interval="1d",
            timestamp=bar_ts,
            open=Decimal("150.00"),
            high=Decimal("152.00"),
            low=Decimal("149.00"),
            close=Decimal("151.00"),
            volume=5000,
            status=CandleStatus.UNKNOWN,
        )
        self.cache.store_candles([c_8am])
        stored_8am = self.cache.get_latest_candle("AAPL", "1d")
        self.assertEqual(stored_8am.status, CandleStatus.UNKNOWN)
        self.assertEqual(stored_8am.close, Decimal("151.00"))
        meta_row = self.cache._conn.execute("SELECT last_status FROM cache_meta WHERE ticker='AAPL' AND interval='1d'").fetchone()
        self.assertEqual(meta_row["last_status"], "UNKNOWN")

        # 2. 10:00 AM ET - regular session active (FORMING update)
        c_10am = OHLCVCandle(
            ticker="AAPL",
            interval="1d",
            timestamp=bar_ts,
            open=Decimal("151.00"),
            high=Decimal("155.00"),
            low=Decimal("150.00"),
            close=Decimal("154.00"),
            volume=500000,
            status=CandleStatus.FORMING,
        )
        self.cache.store_candles([c_10am])
        stored_10am = self.cache.get_latest_candle("AAPL", "1d")
        self.assertEqual(stored_10am.status, CandleStatus.FORMING)
        self.assertEqual(stored_10am.open, Decimal("151.00"))
        self.assertEqual(stored_10am.high, Decimal("155.00"))
        self.assertEqual(stored_10am.low, Decimal("150.00"))
        self.assertEqual(stored_10am.close, Decimal("154.00"))
        self.assertEqual(stored_10am.volume, 500000)
        meta_row = self.cache._conn.execute("SELECT last_status FROM cache_meta WHERE ticker='AAPL' AND interval='1d'").fetchone()
        self.assertEqual(meta_row["last_status"], "FORMING")

        # 3. 4:05 PM ET - regular session closed (COMPLETE finalized update)
        c_close = OHLCVCandle(
            ticker="AAPL",
            interval="1d",
            timestamp=bar_ts,
            open=Decimal("151.00"),
            high=Decimal("158.00"),
            low=Decimal("149.50"),
            close=Decimal("157.25"),
            volume=2500000,
            status=CandleStatus.COMPLETE,
        )
        self.cache.store_candles([c_close])
        stored_close = self.cache.get_latest_candle("AAPL", "1d")
        self.assertEqual(stored_close.status, CandleStatus.COMPLETE)
        self.assertEqual(stored_close.high, Decimal("158.00"))
        self.assertEqual(stored_close.close, Decimal("157.25"))
        self.assertEqual(stored_close.volume, 2500000)
        meta_row = self.cache._conn.execute("SELECT last_status FROM cache_meta WHERE ticker='AAPL' AND interval='1d'").fetchone()
        self.assertEqual(meta_row["last_status"], "COMPLETE")

        # 4. Attempt to write an older FORMING observation afterward
        c_older = OHLCVCandle(
            ticker="AAPL",
            interval="1d",
            timestamp=bar_ts,
            open=Decimal("100.00"),
            high=Decimal("101.00"),
            low=Decimal("99.00"),
            close=Decimal("100.50"),
            volume=100,
            status=CandleStatus.FORMING,
        )
        self.cache.store_candles([c_older])
        stored_after = self.cache.get_latest_candle("AAPL", "1d")
        # Finalized values and COMPLETE status must remain strictly preserved
        self.assertEqual(stored_after.status, CandleStatus.COMPLETE)
        self.assertEqual(stored_after.open, Decimal("151.00"))
        self.assertEqual(stored_after.high, Decimal("158.00"))
        self.assertEqual(stored_after.low, Decimal("149.50"))
        self.assertEqual(stored_after.close, Decimal("157.25"))
        self.assertEqual(stored_after.volume, 2500000)
        meta_row = self.cache._conn.execute("SELECT last_status FROM cache_meta WHERE ticker='AAPL' AND interval='1d'").fetchone()
        self.assertEqual(meta_row["last_status"], "COMPLETE")

    # 14. Fix D: Explicit cache transition matrix verification
    def test_14_cache_transition_matrix_explicit_verification(self):
        t = self.ref_time

        # A. FORMING -> UNKNOWN is blocked
        c_forming = self._make_candle(t, close="150.00", status=CandleStatus.FORMING)
        self.cache.store_candles([c_forming])
        c_unknown = self._make_candle(t, close="999.00", status=CandleStatus.UNKNOWN)
        self.cache.store_candles([c_unknown])
        latest = self.cache.get_latest_candle("AAPL", "15m")
        self.assertEqual(latest.status, CandleStatus.FORMING)
        self.assertEqual(latest.close, Decimal("150.00"))

        # B. FORMING -> FORMING succeeds
        c_forming2 = self._make_candle(t, close="152.00", status=CandleStatus.FORMING)
        self.cache.store_candles([c_forming2])
        latest = self.cache.get_latest_candle("AAPL", "15m")
        self.assertEqual(latest.status, CandleStatus.FORMING)
        self.assertEqual(latest.close, Decimal("152.00"))

        # C. FORMING -> COMPLETE succeeds
        c_complete = self._make_candle(t, close="153.00", status=CandleStatus.COMPLETE)
        self.cache.store_candles([c_complete])
        latest = self.cache.get_latest_candle("AAPL", "15m")
        self.assertEqual(latest.status, CandleStatus.COMPLETE)
        self.assertEqual(latest.close, Decimal("153.00"))

        # D. COMPLETE -> UNKNOWN is blocked
        self.cache.store_candles([c_unknown])
        latest = self.cache.get_latest_candle("AAPL", "15m")
        self.assertEqual(latest.status, CandleStatus.COMPLETE)
        self.assertEqual(latest.close, Decimal("153.00"))


if __name__ == "__main__":
    unittest.main()
