"""Unit test suite for trader-swing market data models and validation.

Verifies:
1. Valid OHLCV candle creation and serialization.
2. Missing OHLC values rejection.
3. Non-finite prices (NaN, Inf) rejection.
4. Zero and negative price rejection.
5. Negative volume rejection.
6. High lower than low rejection.
7. Open outside high-low range rejection.
8. Close outside high-low range rejection.
9. Timezone-naive timestamp rejection.
10. Future-dated timestamp rejection beyond skew tolerance.
11. Duplicate timestamps in sequence rejection.
12. Out-of-order sequence rejection.
13. Mixed tickers in sequence rejection.
14. Mixed intervals in sequence rejection.
15. Non-terminal forming candle in sequence rejection.
16. Valid forming candle verification during open session.
17. Forming candle rejection when market session is closed.
18. Forming candle rejection when bar period has fully elapsed.
19. Unknown lifecycle status rejection in validated candles.
20. Empty sequence handling.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
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
    DataFreshness,
    MarketClock,
    MarketSessionState,
    OHLCVCandle,
    USEastern,
    to_utc,
    to_eastern,
    UTC,
    EASTERN,
)
from market_data_validation import (
    CandleValidationError,
    FormingCandleError,
    InvalidPriceError,
    InvalidVolumeError,
    SequenceValidationError,
    TimestampValidationError,
    validate_candle_sequence,
    validate_single_candle,
    verify_forming_candle,
    resolve_candle_lifecycle,
)


class TestSwingMarketDataValidation(unittest.TestCase):
    """Test suite covering single-candle invariants, sequence consistency, and forming-bar verification."""

    def setUp(self):
        # Anchor fixed reference time: Wednesday 2026-10-14 14:00:00 UTC (10:00 AM EDT, market open)
        self.ref_time = datetime(2026, 10, 14, 14, 0, 0, tzinfo=UTC)
        self.valid_candle = OHLCVCandle(
            ticker="AAPL",
            interval="15m",
            timestamp=datetime(2026, 10, 14, 13, 45, 0, tzinfo=UTC),
            open=Decimal("150.00"),
            high=Decimal("152.50"),
            low=Decimal("149.50"),
            close=Decimal("151.20"),
            volume=50000,
            status=CandleStatus.COMPLETE,
            source="yahoo",
            received_timestamp=self.ref_time,
            is_delayed=True,
        )

    def test_01_valid_ohlcv_candle(self):
        validate_single_candle(self.valid_candle, current_time=self.ref_time)
        d = self.valid_candle.to_dict()
        self.assertEqual(d["ticker"], "AAPL")
        self.assertEqual(d["open"], "150.00")
        self.assertEqual(d["status"], "COMPLETE")

        reconstructed = OHLCVCandle.from_dict(d)
        self.assertEqual(reconstructed.ticker, "AAPL")
        self.assertEqual(reconstructed.close, Decimal("151.20"))
        self.assertEqual(reconstructed.volume, 50000)

    def test_02_missing_values_rejected(self):
        c = OHLCVCandle(
            ticker="AAPL",
            interval="15m",
            timestamp=datetime(2026, 10, 14, 13, 45, 0, tzinfo=UTC),
            open=Decimal("150.00"),
            high=Decimal("152.00"),
            low=Decimal("149.00"),
            close=Decimal("151.00"),
            volume=1000,
        )
        c.open = None
        with self.assertRaises(InvalidPriceError):
            validate_single_candle(c, current_time=self.ref_time)

    def test_03_non_finite_price_nan_rejected(self):
        with self.assertRaises(InvalidPriceError):
            c = OHLCVCandle(
                ticker="AAPL",
                interval="15m",
                timestamp=datetime(2026, 10, 14, 13, 45, 0, tzinfo=UTC),
                open=Decimal("NaN"),
                high=Decimal("152.00"),
                low=Decimal("149.00"),
                close=Decimal("151.00"),
                volume=1000,
            )
            validate_single_candle(c, current_time=self.ref_time)

    def test_04_non_finite_price_infinity_rejected(self):
        with self.assertRaises(InvalidPriceError):
            c = OHLCVCandle(
                ticker="AAPL",
                interval="15m",
                timestamp=datetime(2026, 10, 14, 13, 45, 0, tzinfo=UTC),
                open=Decimal("150.00"),
                high=Decimal("Infinity"),
                low=Decimal("149.00"),
                close=Decimal("151.00"),
                volume=1000,
            )
            validate_single_candle(c, current_time=self.ref_time)

    def test_05_zero_and_negative_price_rejected(self):
        c_zero = OHLCVCandle(
            ticker="AAPL",
            interval="15m",
            timestamp=datetime(2026, 10, 14, 13, 45, 0, tzinfo=UTC),
            open=Decimal("0.00"),
            high=Decimal("150.00"),
            low=Decimal("0.00"),
            close=Decimal("150.00"),
            volume=1000,
        )
        with self.assertRaises(InvalidPriceError):
            validate_single_candle(c_zero, current_time=self.ref_time)

        c_neg = OHLCVCandle(
            ticker="AAPL",
            interval="15m",
            timestamp=datetime(2026, 10, 14, 13, 45, 0, tzinfo=UTC),
            open=Decimal("150.00"),
            high=Decimal("152.00"),
            low=Decimal("-1.00"),
            close=Decimal("150.00"),
            volume=1000,
        )
        with self.assertRaises(InvalidPriceError):
            validate_single_candle(c_neg, current_time=self.ref_time)

    def test_06_negative_volume_rejected(self):
        c = OHLCVCandle(
            ticker="AAPL",
            interval="15m",
            timestamp=datetime(2026, 10, 14, 13, 45, 0, tzinfo=UTC),
            open=Decimal("150.00"),
            high=Decimal("152.00"),
            low=Decimal("149.00"),
            close=Decimal("151.00"),
            volume=-10,
        )
        with self.assertRaises(InvalidVolumeError):
            validate_single_candle(c, current_time=self.ref_time)

    def test_07_high_lower_than_low_rejected(self):
        c = OHLCVCandle(
            ticker="AAPL",
            interval="15m",
            timestamp=datetime(2026, 10, 14, 13, 45, 0, tzinfo=UTC),
            open=Decimal("150.00"),
            high=Decimal("148.00"),  # High < Low
            low=Decimal("149.00"),
            close=Decimal("148.50"),
            volume=1000,
        )
        with self.assertRaises(InvalidPriceError):
            validate_single_candle(c, current_time=self.ref_time)

    def test_08_open_outside_high_low_range_rejected(self):
        c = OHLCVCandle(
            ticker="AAPL",
            interval="15m",
            timestamp=datetime(2026, 10, 14, 13, 45, 0, tzinfo=UTC),
            open=Decimal("153.00"),  # Above High 152.00
            high=Decimal("152.00"),
            low=Decimal("149.00"),
            close=Decimal("151.00"),
            volume=1000,
        )
        with self.assertRaises(InvalidPriceError):
            validate_single_candle(c, current_time=self.ref_time)

    def test_09_close_outside_high_low_range_rejected(self):
        c = OHLCVCandle(
            ticker="AAPL",
            interval="15m",
            timestamp=datetime(2026, 10, 14, 13, 45, 0, tzinfo=UTC),
            open=Decimal("150.00"),
            high=Decimal("152.00"),
            low=Decimal("149.00"),
            close=Decimal("147.00"),  # Below Low 149.00
            volume=1000,
        )
        with self.assertRaises(InvalidPriceError):
            validate_single_candle(c, current_time=self.ref_time)

    def test_10_timezone_naive_timestamp_rejected(self):
        c = OHLCVCandle(
            ticker="AAPL",
            interval="15m",
            timestamp=datetime(2026, 10, 14, 13, 45, 0, tzinfo=UTC),
            open=Decimal("150.00"),
            high=Decimal("152.00"),
            low=Decimal("149.00"),
            close=Decimal("151.00"),
            volume=1000,
        )
        c.timestamp = datetime(2026, 10, 14, 13, 45, 0)  # Naive
        with self.assertRaises(TimestampValidationError):
            validate_single_candle(c, current_time=self.ref_time)

    def test_11_future_timestamp_rejected(self):
        c = OHLCVCandle(
            ticker="AAPL",
            interval="15m",
            # Timestamp is 1 hour in the future relative to ref_time (14:00)
            timestamp=datetime(2026, 10, 14, 15, 0, 0, tzinfo=UTC),
            open=Decimal("150.00"),
            high=Decimal("152.00"),
            low=Decimal("149.00"),
            close=Decimal("151.00"),
            volume=1000,
        )
        with self.assertRaises(TimestampValidationError):
            validate_single_candle(c, current_time=self.ref_time)

    def test_12_duplicate_timestamps_in_sequence_rejected(self):
        c1 = self.valid_candle
        c2 = OHLCVCandle(
            ticker="AAPL",
            interval="15m",
            timestamp=c1.timestamp,  # Exact duplicate timestamp
            open=Decimal("151.00"),
            high=Decimal("153.00"),
            low=Decimal("150.00"),
            close=Decimal("152.00"),
            volume=2000,
        )
        with self.assertRaises(SequenceValidationError):
            validate_candle_sequence([c1, c2], current_time=self.ref_time)

    def test_13_out_of_order_sequence_rejected(self):
        c1 = self.valid_candle
        c2 = OHLCVCandle(
            ticker="AAPL",
            interval="15m",
            # Timestamp earlier than c1
            timestamp=c1.timestamp - timedelta(minutes=15),
            open=Decimal("149.00"),
            high=Decimal("150.00"),
            low=Decimal("148.00"),
            close=Decimal("149.50"),
            volume=2000,
        )
        with self.assertRaises(SequenceValidationError):
            validate_candle_sequence([c1, c2], current_time=self.ref_time)

    def test_14_mixed_tickers_in_sequence_rejected(self):
        c1 = self.valid_candle
        c2 = OHLCVCandle(
            ticker="MSFT",  # Mixed ticker
            interval="15m",
            timestamp=c1.timestamp + timedelta(minutes=15),
            open=Decimal("300.00"),
            high=Decimal("302.00"),
            low=Decimal("299.00"),
            close=Decimal("301.00"),
            volume=2000,
        )
        with self.assertRaises(SequenceValidationError):
            validate_candle_sequence([c1, c2], current_time=self.ref_time)

    def test_15_mixed_intervals_in_sequence_rejected(self):
        c1 = self.valid_candle
        c2 = OHLCVCandle(
            ticker="AAPL",
            interval="5m",  # Mixed interval
            timestamp=c1.timestamp + timedelta(minutes=15),
            open=Decimal("151.00"),
            high=Decimal("152.00"),
            low=Decimal("150.00"),
            close=Decimal("151.50"),
            volume=2000,
        )
        with self.assertRaises(SequenceValidationError):
            validate_candle_sequence([c1, c2], current_time=self.ref_time)

    def test_16_forming_candle_at_intermediary_position_rejected(self):
        c1 = OHLCVCandle(
            ticker="AAPL",
            interval="15m",
            timestamp=datetime(2026, 10, 14, 13, 15, 0, tzinfo=UTC),
            open=Decimal("149.00"),
            high=Decimal("150.00"),
            low=Decimal("148.00"),
            close=Decimal("149.50"),
            volume=1000,
            status=CandleStatus.FORMING,  # Intermediary bar cannot be forming
        )
        c2 = OHLCVCandle(
            ticker="AAPL",
            interval="15m",
            timestamp=datetime(2026, 10, 14, 13, 30, 0, tzinfo=UTC),
            open=Decimal("150.00"),
            high=Decimal("151.00"),
            low=Decimal("149.00"),
            close=Decimal("150.50"),
            volume=1000,
            status=CandleStatus.COMPLETE,
        )
        with self.assertRaises(SequenceValidationError):
            validate_candle_sequence([c1, c2], current_time=self.ref_time)

    def test_17_valid_forming_candle_verified(self):
        # Current time is 14:05 UTC. Bar starts at 14:00 and ends at 14:15.
        cur_time = datetime(2026, 10, 14, 14, 5, 0, tzinfo=UTC)
        candidate = OHLCVCandle(
            ticker="AAPL",
            interval="15m",
            timestamp=datetime(2026, 10, 14, 14, 0, 0, tzinfo=UTC),
            open=Decimal("150.00"),
            high=Decimal("151.00"),
            low=Decimal("149.80"),
            close=Decimal("150.50"),
            volume=12000,
            status=CandleStatus.FORMING,
        )
        self.assertTrue(verify_forming_candle(candidate, current_time=cur_time))

    def test_18_forming_candle_fails_when_session_closed(self):
        # Sunday night: market closed
        sunday_time = datetime(2026, 10, 18, 20, 0, 0, tzinfo=UTC)
        candidate = OHLCVCandle(
            ticker="AAPL",
            interval="15m",
            timestamp=datetime(2026, 10, 18, 19, 50, 0, tzinfo=UTC),
            open=Decimal("150.00"),
            high=Decimal("151.00"),
            low=Decimal("149.80"),
            close=Decimal("150.50"),
            volume=12000,
            status=CandleStatus.FORMING,
        )
        self.assertFalse(verify_forming_candle(candidate, current_time=sunday_time))

    def test_19_forming_candle_fails_when_duration_elapsed(self):
        # Bar is from 13:30 to 13:45. Current time is 14:00 (bar is already completed, cannot be forming).
        cur_time = datetime(2026, 10, 14, 14, 0, 0, tzinfo=UTC)
        candidate = OHLCVCandle(
            ticker="AAPL",
            interval="15m",
            timestamp=datetime(2026, 10, 14, 13, 30, 0, tzinfo=UTC),
            open=Decimal("150.00"),
            high=Decimal("151.00"),
            low=Decimal("149.80"),
            close=Decimal("150.50"),
            volume=12000,
            status=CandleStatus.FORMING,
        )
        self.assertFalse(verify_forming_candle(candidate, current_time=cur_time))

    # 20. Fix C: Structurally valid UNKNOWN candle accepted, invalid UNKNOWN candle rejected
    def test_20_unknown_status_validation_semantics(self):
        # Structurally valid candle with status UNKNOWN is accepted by validate_single_candle
        valid_unknown = OHLCVCandle(
            ticker="AAPL",
            interval="15m",
            timestamp=datetime(2026, 10, 14, 13, 45, 0, tzinfo=UTC),
            open=Decimal("150.00"),
            high=Decimal("152.00"),
            low=Decimal("149.00"),
            close=Decimal("151.00"),
            volume=1000,
            status=CandleStatus.UNKNOWN,
        )
        validate_single_candle(valid_unknown, current_time=self.ref_time)
        self.assertEqual(valid_unknown.status, CandleStatus.UNKNOWN)

        # Structurally invalid candle with status UNKNOWN (e.g. high < low) must still be rejected
        invalid_unknown = OHLCVCandle(
            ticker="AAPL",
            interval="15m",
            timestamp=datetime(2026, 10, 14, 13, 45, 0, tzinfo=UTC),
            open=Decimal("150.00"),
            high=Decimal("145.00"),  # High < Low
            low=Decimal("148.00"),
            close=Decimal("146.00"),
            volume=1000,
            status=CandleStatus.UNKNOWN,
        )
        with self.assertRaises(InvalidPriceError):
            validate_single_candle(invalid_unknown, current_time=self.ref_time)

    # 21. Fix D-2: Daily candle is FORMING at 3:30 PM Eastern during regular trading
    def test_21_daily_candle_forming_at_330_pm_regular_session(self):
        # Wednesday 2026-10-14 15:30 EDT (19:30 UTC)
        cur_time = datetime(2026, 10, 14, 19, 30, 0, tzinfo=UTC)
        daily_candle = OHLCVCandle(
            ticker="AAPL",
            interval="1d",
            timestamp=datetime(2026, 10, 14, 13, 30, 0, tzinfo=UTC),  # 9:30 AM EDT
            open=Decimal("150.00"),
            high=Decimal("154.00"),
            low=Decimal("149.50"),
            close=Decimal("153.25"),
            volume=2500000,
            status=CandleStatus.FORMING,
        )
        self.assertTrue(verify_forming_candle(daily_candle, current_time=cur_time))

    # 22. Fix D-2: Daily candle is COMPLETE at 4:00 PM Eastern regular close
    def test_22_daily_candle_complete_at_400_pm_session_close(self):
        # Wednesday 2026-10-14 16:00 EDT (20:00 UTC)
        cur_time = datetime(2026, 10, 14, 20, 0, 0, tzinfo=UTC)
        daily_candle = OHLCVCandle(
            ticker="AAPL",
            interval="1d",
            timestamp=datetime(2026, 10, 14, 13, 30, 0, tzinfo=UTC),
            open=Decimal("150.00"),
            high=Decimal("154.00"),
            low=Decimal("149.50"),
            close=Decimal("153.25"),
            volume=3000000,
            status=CandleStatus.FORMING,
        )
        # At or after 16:00 regular close, verify_forming_candle must be False (candle is complete)
        self.assertFalse(verify_forming_candle(daily_candle, current_time=cur_time))

    # 23. Fix D-2: Daily candle is COMPLETE during post-market (4:30 PM Eastern)
    def test_23_daily_candle_complete_at_430_pm_post_market(self):
        # Wednesday 2026-10-14 16:30 EDT (20:30 UTC, POST_MARKET)
        cur_time = datetime(2026, 10, 14, 20, 30, 0, tzinfo=UTC)
        daily_candle = OHLCVCandle(
            ticker="AAPL",
            interval="1d",
            timestamp=datetime(2026, 10, 14, 13, 30, 0, tzinfo=UTC),
            open=Decimal("150.00"),
            high=Decimal("154.00"),
            low=Decimal("149.50"),
            close=Decimal("153.25"),
            volume=3000000,
            status=CandleStatus.FORMING,
        )
        # During post-market, regular session daily bar is COMPLETE, NOT FORMING
        self.assertFalse(verify_forming_candle(daily_candle, current_time=cur_time))

    # 24. Fix D-2: Early close session (1:00 PM Eastern scheduled close)
    def test_24_daily_candle_early_close_transition(self):
        # Black Friday 2026-11-27: early close at 1:00 PM EST (18:00 UTC)
        # At 12:30 PM EST (17:30 UTC): actively FORMING
        time_1230 = datetime(2026, 11, 27, 17, 30, 0, tzinfo=UTC)
        daily_candle = OHLCVCandle(
            ticker="AAPL",
            interval="1d",
            timestamp=datetime(2026, 11, 27, 14, 30, 0, tzinfo=UTC),  # 9:30 AM EST
            open=Decimal("150.00"),
            high=Decimal("152.00"),
            low=Decimal("149.00"),
            close=Decimal("151.00"),
            volume=1000000,
            status=CandleStatus.FORMING,
        )
        self.assertTrue(verify_forming_candle(daily_candle, current_time=time_1230))

        # At 1:00 PM EST (18:00 UTC): regular session has closed, candle is COMPLETE
        time_1300 = datetime(2026, 11, 27, 18, 0, 0, tzinfo=UTC)
        self.assertFalse(verify_forming_candle(daily_candle, current_time=time_1300))

    # 25. Fix D-2: Post-market after early close (1:30 PM Eastern)
    def test_25_daily_candle_complete_post_market_after_early_close(self):
        time_1330 = datetime(2026, 11, 27, 18, 30, 0, tzinfo=UTC)  # 1:30 PM EST
        daily_candle = OHLCVCandle(
            ticker="AAPL",
            interval="1d",
            timestamp=datetime(2026, 11, 27, 14, 30, 0, tzinfo=UTC),
            open=Decimal("150.00"),
            high=Decimal("152.00"),
            low=Decimal("149.00"),
            close=Decimal("151.00"),
            volume=1000000,
            status=CandleStatus.FORMING,
        )
        self.assertFalse(verify_forming_candle(daily_candle, current_time=time_1330))

    # 26. Fix D-2: Daily candle fails forming verification on weekends
    def test_26_daily_candle_fails_forming_on_weekend(self):
        sat_time = datetime(2026, 10, 17, 16, 0, 0, tzinfo=UTC)
        daily_candle = OHLCVCandle(
            ticker="AAPL",
            interval="1d",
            timestamp=datetime(2026, 10, 17, 13, 30, 0, tzinfo=UTC),
            open=Decimal("150.00"),
            high=Decimal("152.00"),
            low=Decimal("149.00"),
            close=Decimal("151.00"),
            volume=1000,
            status=CandleStatus.FORMING,
        )
        self.assertFalse(verify_forming_candle(daily_candle, current_time=sat_time))

    # 27. Fix D-2: Daily candle fails forming verification on holidays
    def test_27_daily_candle_fails_forming_on_holiday(self):
        thanksgiving = datetime(2026, 11, 26, 16, 0, 0, tzinfo=UTC)
        daily_candle = OHLCVCandle(
            ticker="AAPL",
            interval="1d",
            timestamp=datetime(2026, 11, 26, 14, 30, 0, tzinfo=UTC),
            open=Decimal("150.00"),
            high=Decimal("152.00"),
            low=Decimal("149.00"),
            close=Decimal("151.00"),
            volume=1000,
            status=CandleStatus.FORMING,
        )
        self.assertFalse(verify_forming_candle(daily_candle, current_time=thanksgiving))

    # 28. Fix D-2: Daily candle forming across DST transitions (EDT and EST)
    def test_28_daily_candle_forming_across_dst_transitions(self):
        # EDT session (March 2026): 3:00 PM EDT is 19:00 UTC
        cur_edt = datetime(2026, 3, 9, 19, 0, 0, tzinfo=UTC)
        c_edt = OHLCVCandle(
            ticker="AAPL",
            interval="1d",
            timestamp=datetime(2026, 3, 9, 13, 30, 0, tzinfo=UTC),
            open=Decimal("150.00"),
            high=Decimal("152.00"),
            low=Decimal("149.00"),
            close=Decimal("151.00"),
            volume=1000,
            status=CandleStatus.FORMING,
        )
        self.assertTrue(verify_forming_candle(c_edt, current_time=cur_edt))

        # EST session (November 2026): 3:00 PM EST is 20:00 UTC
        cur_est = datetime(2026, 11, 2, 20, 0, 0, tzinfo=UTC)
        c_est = OHLCVCandle(
            ticker="AAPL",
            interval="1d",
            timestamp=datetime(2026, 11, 2, 14, 30, 0, tzinfo=UTC),
            open=Decimal("150.00"),
            high=Decimal("152.00"),
            low=Decimal("149.00"),
            close=Decimal("151.00"),
            volume=1000,
            status=CandleStatus.FORMING,
        )
        self.assertTrue(verify_forming_candle(c_est, current_time=cur_est))

    # 29. Fix D-2: Verification that weekly and intraday candles are not finalized prematurely
    def test_29_intraday_and_weekly_candles_preserve_lifecycle(self):
        # 15m candle at 14:05 UTC (active window 14:00 - 14:15) is FORMING
        time_1405 = datetime(2026, 10, 14, 14, 5, 0, tzinfo=UTC)
        c_15m = OHLCVCandle(
            ticker="AAPL",
            interval="15m",
            timestamp=datetime(2026, 10, 14, 14, 0, 0, tzinfo=UTC),
            open=Decimal("150.00"),
            high=Decimal("151.00"),
            low=Decimal("149.00"),
            close=Decimal("150.50"),
            volume=5000,
            status=CandleStatus.FORMING,
        )
        self.assertTrue(verify_forming_candle(c_15m, current_time=time_1405))

        # Weekly candle on Wednesday at 15:30 EDT is still FORMING (week ends Friday close)
        time_wed = datetime(2026, 10, 14, 19, 30, 0, tzinfo=UTC)
        c_1wk = OHLCVCandle(
            ticker="AAPL",
            interval="1wk",
            timestamp=datetime(2026, 10, 12, 13, 30, 0, tzinfo=UTC),  # Monday open
            open=Decimal("150.00"),
            high=Decimal("155.00"),
            low=Decimal("148.00"),
            close=Decimal("153.00"),
            volume=20000000,
            status=CandleStatus.FORMING,
        )
        self.assertTrue(verify_forming_candle(c_1wk, current_time=time_wed))

    # 30. Fix A: Pre-market daily candles resolve to UNKNOWN, never COMPLETE or FORMING
    def test_30_resolve_candle_lifecycle_pre_market_daily_unknown(self):
        # Monday 2026-10-19: session open is 9:30 AM EDT (13:30 UTC)
        bar_ts = datetime(2026, 10, 19, 4, 0, 0, tzinfo=UTC)  # Midnight EDT
        candle = OHLCVCandle(
            ticker="AAPL",
            interval="1d",
            timestamp=bar_ts,
            open=Decimal("150.00"),
            high=Decimal("152.00"),
            low=Decimal("149.00"),
            close=Decimal("151.00"),
            volume=1000,
            status=CandleStatus.UNKNOWN,
        )

        # 8:00 AM EDT (12:00 UTC) - pre-market
        cur_8am = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)
        status_8am = resolve_candle_lifecycle(candle, current_time=cur_8am)
        self.assertEqual(status_8am, CandleStatus.UNKNOWN)
        self.assertFalse(verify_forming_candle(candle, current_time=cur_8am))

        # 9:00 AM EDT (13:00 UTC) - pre-market
        cur_9am = datetime(2026, 10, 19, 13, 0, 0, tzinfo=UTC)
        status_9am = resolve_candle_lifecycle(candle, current_time=cur_9am)
        self.assertEqual(status_9am, CandleStatus.UNKNOWN)
        self.assertFalse(verify_forming_candle(candle, current_time=cur_9am))

    # 31. Fix A: Regular session progression (9:30 AM, 10:00 AM, 12:00 PM, 3:30 PM) resolves to FORMING
    def test_31_resolve_candle_lifecycle_regular_session_progression(self):
        bar_ts = datetime(2026, 10, 19, 4, 0, 0, tzinfo=UTC)
        candle = OHLCVCandle(
            ticker="AAPL",
            interval="1d",
            timestamp=bar_ts,
            open=Decimal("150.00"),
            high=Decimal("155.00"),
            low=Decimal("149.00"),
            close=Decimal("154.00"),
            volume=500000,
            status=CandleStatus.FORMING,
        )

        # 9:30 AM EDT (13:30 UTC)
        cur_930 = datetime(2026, 10, 19, 13, 30, 0, tzinfo=UTC)
        self.assertEqual(resolve_candle_lifecycle(candle, current_time=cur_930), CandleStatus.FORMING)
        self.assertTrue(verify_forming_candle(candle, current_time=cur_930))

        # 10:00 AM EDT (14:00 UTC)
        cur_1000 = datetime(2026, 10, 19, 14, 0, 0, tzinfo=UTC)
        self.assertEqual(resolve_candle_lifecycle(candle, current_time=cur_1000), CandleStatus.FORMING)

        # 12:00 PM EDT (16:00 UTC)
        cur_1200 = datetime(2026, 10, 19, 16, 0, 0, tzinfo=UTC)
        self.assertEqual(resolve_candle_lifecycle(candle, current_time=cur_1200), CandleStatus.FORMING)

        # 3:30 PM EDT (19:30 UTC)
        cur_1530 = datetime(2026, 10, 19, 19, 30, 0, tzinfo=UTC)
        self.assertEqual(resolve_candle_lifecycle(candle, current_time=cur_1530), CandleStatus.FORMING)

    # 32. Fix A: Session close and post-market (4:00 PM, 4:30 PM) resolves to COMPLETE
    def test_32_resolve_candle_lifecycle_session_close_and_post_market(self):
        bar_ts = datetime(2026, 10, 19, 4, 0, 0, tzinfo=UTC)
        candle = OHLCVCandle(
            ticker="AAPL",
            interval="1d",
            timestamp=bar_ts,
            open=Decimal("150.00"),
            high=Decimal("156.00"),
            low=Decimal("149.00"),
            close=Decimal("155.50"),
            volume=2000000,
            status=CandleStatus.COMPLETE,
        )

        # 4:00 PM EDT (20:00 UTC) - scheduled regular close
        cur_1600 = datetime(2026, 10, 19, 20, 0, 0, tzinfo=UTC)
        self.assertEqual(resolve_candle_lifecycle(candle, current_time=cur_1600), CandleStatus.COMPLETE)
        self.assertFalse(verify_forming_candle(candle, current_time=cur_1600))

        # 4:30 PM EDT (20:30 UTC) - post-market
        cur_1630 = datetime(2026, 10, 19, 20, 30, 0, tzinfo=UTC)
        self.assertEqual(resolve_candle_lifecycle(candle, current_time=cur_1630), CandleStatus.COMPLETE)
        self.assertFalse(verify_forming_candle(candle, current_time=cur_1630))

    # 33. Fix A: Weekend and holiday daily bars - past confirmed session COMPLETE vs non-trading date UNKNOWN
    def test_33_resolve_candle_lifecycle_weekend_and_holiday_daily_bars(self):
        # Prior Friday candle (2026-10-16) inspected on Saturday (2026-10-17 12:00 EDT)
        fri_bar = OHLCVCandle(
            ticker="AAPL",
            interval="1d",
            timestamp=datetime(2026, 10, 16, 4, 0, 0, tzinfo=UTC),
            open=Decimal("150.00"),
            high=Decimal("152.00"),
            low=Decimal("149.00"),
            close=Decimal("151.00"),
            volume=1000000,
            status=CandleStatus.COMPLETE,
        )
        sat_time = datetime(2026, 10, 17, 16, 0, 0, tzinfo=UTC)
        self.assertEqual(resolve_candle_lifecycle(fri_bar, current_time=sat_time), CandleStatus.COMPLETE)

        # Candle stamped for Saturday (2026-10-17): non-trading date must resolve to UNKNOWN
        sat_bar = OHLCVCandle(
            ticker="AAPL",
            interval="1d",
            timestamp=datetime(2026, 10, 17, 4, 0, 0, tzinfo=UTC),
            open=Decimal("150.00"),
            high=Decimal("152.00"),
            low=Decimal("149.00"),
            close=Decimal("151.00"),
            volume=100,
            status=CandleStatus.UNKNOWN,
        )
        self.assertEqual(resolve_candle_lifecycle(sat_bar, current_time=sat_time), CandleStatus.UNKNOWN)

        # Candle stamped for Thanksgiving holiday (2026-11-26): non-trading date must resolve to UNKNOWN
        tg_time = datetime(2026, 11, 26, 17, 0, 0, tzinfo=UTC)
        tg_bar = OHLCVCandle(
            ticker="AAPL",
            interval="1d",
            timestamp=datetime(2026, 11, 26, 5, 0, 0, tzinfo=UTC),
            open=Decimal("150.00"),
            high=Decimal("152.00"),
            low=Decimal("149.00"),
            close=Decimal("151.00"),
            volume=100,
            status=CandleStatus.UNKNOWN,
        )
        self.assertEqual(resolve_candle_lifecycle(tg_bar, current_time=tg_time), CandleStatus.UNKNOWN)

    # 34. Fix A: Unsupported calendar year daily bar resolves to UNKNOWN
    def test_34_resolve_candle_lifecycle_unsupported_year_daily_bar(self):
        dt_2014 = datetime(2014, 10, 15, 14, 0, 0, tzinfo=UTC)
        c_2014 = OHLCVCandle(
            ticker="AAPL",
            interval="1d",
            timestamp=datetime(2014, 10, 15, 4, 0, 0, tzinfo=UTC),
            open=Decimal("150.00"),
            high=Decimal("152.00"),
            low=Decimal("149.00"),
            close=Decimal("151.00"),
            volume=1000,
            status=CandleStatus.UNKNOWN,
        )
        self.assertEqual(resolve_candle_lifecycle(c_2014, current_time=dt_2014), CandleStatus.UNKNOWN)

    # 35. Fix A: Early-close day lifecycle progression
    def test_35_resolve_candle_lifecycle_early_close(self):
        # 2026-11-27: early close at 1:00 PM EST (18:00 UTC)
        bar_ts = datetime(2026, 11, 27, 5, 0, 0, tzinfo=UTC)
        c_bf = OHLCVCandle(
            ticker="AAPL",
            interval="1d",
            timestamp=bar_ts,
            open=Decimal("150.00"),
            high=Decimal("153.00"),
            low=Decimal("149.00"),
            close=Decimal("152.00"),
            volume=1000000,
            status=CandleStatus.FORMING,
        )
        # 12:30 PM EST (17:30 UTC): FORMING
        t_1230 = datetime(2026, 11, 27, 17, 30, 0, tzinfo=UTC)
        self.assertEqual(resolve_candle_lifecycle(c_bf, current_time=t_1230), CandleStatus.FORMING)

        # 1:00 PM EST (18:00 UTC): scheduled early close -> COMPLETE
        t_1300 = datetime(2026, 11, 27, 18, 0, 0, tzinfo=UTC)
        self.assertEqual(resolve_candle_lifecycle(c_bf, current_time=t_1300), CandleStatus.COMPLETE)

        # 1:30 PM EST (18:30 UTC): post-market -> COMPLETE
        t_1330 = datetime(2026, 11, 27, 18, 30, 0, tzinfo=UTC)
        self.assertEqual(resolve_candle_lifecycle(c_bf, current_time=t_1330), CandleStatus.COMPLETE)


if __name__ == "__main__":
    unittest.main()
