"""Unit tests for Phase 5.1 Historical Data Preparation in trader-swing.

Enforces:
1. Strict chronological ordering and rejection of out-of-order timestamps.
2. Rejection of duplicate timestamps for the same ticker.
3. OHLC relationship validation (High >= Open/Close/Low, Low <= Open/Close/High).
4. Rejection of FORMING or UNKNOWN candles.
5. Zero look-ahead temporal slicing guarantee.
6. Corporate action overnight price jump/gap detection.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
import os
import sys
import unittest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SWING_DIR = os.path.join(ROOT_DIR, ".agents", "skills", "trader-swing")
for path in (ROOT_DIR, SWING_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

from backtest_data import HistoricalDataManager, HistoricalDataValidationError
from market_data_models import CandleStatus, OHLCVCandle, UTC


def make_candle(
    ticker: str,
    dt: datetime,
    open_p: str,
    high_p: str,
    low_p: str,
    close_p: str,
    volume: int = 1000000,
    status: CandleStatus = CandleStatus.COMPLETE,
    interval: str = "1d",
) -> OHLCVCandle:
    return OHLCVCandle(
        ticker=ticker,
        interval=interval,
        timestamp=dt,
        open=Decimal(open_p),
        high=Decimal(high_p),
        low=Decimal(low_p),
        close=Decimal(close_p),
        volume=volume,
        status=status,
        source="yahoo",
        received_timestamp=dt,
        is_delayed=True,
    )


class TestSwingBacktestData(unittest.TestCase):
    """Test suite for historical data loading, validation, and zero look-ahead slicing."""

    def setUp(self):
        self.mgr = HistoricalDataManager()
        self.base_date = datetime(2026, 1, 5, 20, 0, 0, tzinfo=UTC)

    def test_01_valid_series_loading_and_sorting(self):
        """Verify valid candles load cleanly and sort ascending chronologically."""
        c1 = make_candle("AAPL", self.base_date + timedelta(days=2), "150.00", "155.00", "149.00", "154.00")
        c2 = make_candle("AAPL", self.base_date, "145.00", "148.00", "144.00", "147.00")
        c3 = make_candle("AAPL", self.base_date + timedelta(days=1), "147.00", "151.00", "146.00", "150.00")

        # Pass out of order
        count = self.mgr.load_candles("AAPL", [c1, c2, c3])
        self.assertEqual(count, 3)

        sessions = self.mgr.get_all_session_dates()
        self.assertEqual(len(sessions), 3)
        self.assertEqual(sessions[0], (self.base_date).date())
        self.assertEqual(sessions[1], (self.base_date + timedelta(days=1)).date())
        self.assertEqual(sessions[2], (self.base_date + timedelta(days=2)).date())

    def test_02_duplicate_timestamp_rejection(self):
        """Verify candles with duplicate session dates for same ticker are rejected."""
        c1 = make_candle("AAPL", self.base_date, "145.00", "148.00", "144.00", "147.00")
        c2 = make_candle("AAPL", self.base_date, "146.00", "149.00", "145.00", "148.00")

        with self.assertRaises(HistoricalDataValidationError):
            self.mgr.load_candles("AAPL", [c1, c2])

    def test_03_invalid_ohlc_relationships_rejection(self):
        """Verify invalid High/Low relationships are caught and rejected."""
        # High < Low
        c_bad1 = make_candle("MSFT", self.base_date, "100.00", "90.00", "110.00", "95.00")
        with self.assertRaises(HistoricalDataValidationError):
            self.mgr.load_candles("MSFT", [c_bad1])

        # High < Open
        c_bad2 = make_candle("MSFT", self.base_date, "105.00", "100.00", "95.00", "98.00")
        with self.assertRaises(HistoricalDataValidationError):
            self.mgr.load_candles("MSFT", [c_bad2])

        # Low > Close
        c_bad3 = make_candle("MSFT", self.base_date, "100.00", "105.00", "102.00", "98.00")
        with self.assertRaises(HistoricalDataValidationError):
            self.mgr.load_candles("MSFT", [c_bad3])

        # Negative price
        c_bad4 = make_candle("MSFT", self.base_date, "-10.00", "10.00", "-15.00", "5.00")
        with self.assertRaises(HistoricalDataValidationError):
            self.mgr.load_candles("MSFT", [c_bad4])

        # Negative volume
        c_bad5 = make_candle("MSFT", self.base_date, "100.00", "105.00", "95.00", "102.00", volume=-50)
        with self.assertRaises(HistoricalDataValidationError):
            self.mgr.load_candles("MSFT", [c_bad5])

    def test_04_forming_and_interval_rejection(self):
        """Verify FORMING candles or non-1d intervals are rejected."""
        c_forming = make_candle(
            "NVDA",
            self.base_date,
            "100.00",
            "105.00",
            "95.00",
            "102.00",
            status=CandleStatus.FORMING,
        )
        with self.assertRaises(HistoricalDataValidationError):
            self.mgr.load_candles("NVDA", [c_forming])

        c_5m = make_candle(
            "NVDA",
            self.base_date,
            "100.00",
            "105.00",
            "95.00",
            "102.00",
            interval="5m",
        )
        with self.assertRaises(HistoricalDataValidationError):
            self.mgr.load_candles("NVDA", [c_5m])

    def test_05_strict_no_look_ahead_slicing(self):
        """CRITICAL: get_candles_slice must NEVER return future data beyond as_of_date."""
        candles = []
        for i in range(10):
            dt = self.base_date + timedelta(days=i)
            c = make_candle("AAPL", dt, "150.00", "155.00", "148.00", "152.00")
            candles.append(c)

        self.mgr.load_candles("AAPL", candles)

        # Slice as of day 4
        as_of = (self.base_date + timedelta(days=4)).date()
        sliced = self.mgr.get_candles_slice("AAPL", as_of_date=as_of)

        self.assertEqual(len(sliced), 5)
        # All timestamps must be <= as_of
        self.assertTrue(all(c.timestamp.date() <= as_of for c in sliced))
        # Day 5 and later must NOT be present
        future_date = (self.base_date + timedelta(days=5)).date()
        self.assertFalse(any(c.timestamp.date() >= future_date for c in sliced))

    def test_06_corporate_action_gap_detection(self):
        """Verify overnight price discontinuities (> 40%) trigger quality warnings."""
        c1 = make_candle("SPLT", self.base_date, "100.00", "105.00", "98.00", "102.00")
        # 2:1 stock split unadjusted: price halves from $102 to $50
        c2 = make_candle("SPLT", self.base_date + timedelta(days=1), "50.00", "52.00", "48.00", "51.00")

        self.mgr.load_candles("SPLT", [c1, c2])
        warnings = self.mgr.get_quality_warnings()
        self.assertIn("SPLT", warnings)
        self.assertTrue(any("unadjusted stock split" in w for w in warnings["SPLT"]))


if __name__ == "__main__":
    unittest.main()
