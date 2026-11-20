"""Unit and integration test suite for trader-swing public market data service and session clock.

Verifies:
1. Regular NYSE trading session resolution (9:30 AM - 4:00 PM Eastern).
2. Pre-market and post-market session state resolution.
3. Weekend market closure resolution.
4. NYSE holiday resolution (Good Friday, Thanksgiving).
5. Early close session resolution (Black Friday 1:00 PM close).
6. US Eastern Daylight Saving Time transitions (EDT vs EST).
7. Machine timezone neutrality (UTC and Eastern normalization).
8. End-to-end integration: Provider to normalized model to cache.
9. Cache fallback during simulated provider network outage.
10. Provider failure raised when cache is empty during outage.
11. Market data status reporting FRESH when market is closed and last bar is from last session.
12. Market data status reporting DELAYED during open session with delayed provider data.
13. Market data status reporting STALE when open session data exceeds latency tolerance.
14. Public API validate_candles returns structured error strings.
15. Verification of read-only architecture (no order placing or broker mutation).
"""

from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal
import json
import os
import sys
import unittest
import urllib.error

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
    MarketDataStatus,
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
    ProviderUnavailableError,
    RateLimitError,
)
from market_data_cache import MarketDataCache
from market_data import MarketDataService
from providers.yahoo import YahooMarketDataProvider


def make_sample_yahoo_json(ticker="AAPL", timestamps=None, opens=None, highs=None, lows=None, closes=None, volumes=None, reg_time=1791985500):
    if timestamps is None:
        timestamps = [1791984600, 1791985500]  # two 15-minute bars on 2026-10-14 (13:30, 13:45 UTC)
    if opens is None:
        opens = [150.0, 151.0]
    if highs is None:
        highs = [152.0, 153.0]
    if lows is None:
        lows = [149.0, 150.0]
    if closes is None:
        closes = [151.0, 152.5]
    if volumes is None:
        volumes = [10000, 15000]

    return json.dumps({
        "chart": {
            "result": [
                {
                    "meta": {
                        "currency": "USD",
                        "symbol": ticker,
                        "regularMarketTime": reg_time,
                        "gmtoffset": -14400,
                        "timezone": "EDT",
                        "exchangeTimezoneName": "America/New_York",
                    },
                    "timestamp": timestamps,
                    "indicators": {
                        "quote": [
                            {
                                "open": opens,
                                "high": highs,
                                "low": lows,
                                "close": closes,
                                "volume": volumes,
                            }
                        ]
                    },
                }
            ],
            "error": None,
        }
    })


class TestSwingMarketDataService(unittest.TestCase):
    """Test suite for NYSE trading clock, service coordination, caching fallback, and freshness detection."""

    def setUp(self):
        # Anchor fixed reference time: Wednesday 2026-10-14 14:00:00 UTC (10:00 AM EDT, regular trading session)
        self.ref_time = datetime(2026, 10, 14, 14, 0, 0, tzinfo=UTC)
        self.cache = MarketDataCache(db_path=":memory:")

    def tearDown(self):
        self.cache.close()

    # 1. Regular session resolution
    def test_01_regular_trading_session(self):
        # 10:00 AM Eastern on a Wednesday
        dt = datetime(2026, 10, 14, 10, 0, 0, tzinfo=EASTERN)
        self.assertEqual(MarketClock.get_session_state(dt), MarketSessionState.REGULAR)
        self.assertTrue(MarketClock.is_market_open(dt))

    # 2. Pre-market and post-market resolution
    def test_02_pre_and_post_market(self):
        # 8:00 AM Eastern -> PRE_MARKET
        dt_pre = datetime(2026, 10, 14, 8, 0, 0, tzinfo=EASTERN)
        self.assertEqual(MarketClock.get_session_state(dt_pre), MarketSessionState.PRE_MARKET)
        self.assertFalse(MarketClock.is_market_open(dt_pre))

        # 5:00 PM Eastern (17:00) -> POST_MARKET
        dt_post = datetime(2026, 10, 14, 17, 0, 0, tzinfo=EASTERN)
        self.assertEqual(MarketClock.get_session_state(dt_post), MarketSessionState.POST_MARKET)
        self.assertFalse(MarketClock.is_market_open(dt_post))

    # 3. Weekend market closure
    def test_03_weekend_closed(self):
        # Saturday at 12:00 PM Eastern
        dt_sat = datetime(2026, 10, 17, 12, 0, 0, tzinfo=EASTERN)
        self.assertEqual(MarketClock.get_session_state(dt_sat), MarketSessionState.CLOSED)
        is_open, reason = MarketClock.is_trading_day(dt_sat)
        self.assertFalse(is_open)
        self.assertEqual(reason, "Weekend")

    # 4. NYSE holiday resolution
    def test_04_nyse_holidays(self):
        # Good Friday 2026-04-03
        dt_gf = datetime(2026, 4, 3, 10, 0, 0, tzinfo=EASTERN)
        is_open, reason = MarketClock.is_trading_day(dt_gf)
        self.assertFalse(is_open)
        self.assertIn("Good Friday", reason)
        self.assertEqual(MarketClock.get_session_state(dt_gf), MarketSessionState.CLOSED)

        # Thanksgiving 2026-11-26
        dt_tg = datetime(2026, 11, 26, 10, 0, 0, tzinfo=EASTERN)
        is_open2, reason2 = MarketClock.is_trading_day(dt_tg)
        self.assertFalse(is_open2)
        self.assertIn("Thanksgiving", reason2)

    # 5. Early close resolution (Black Friday)
    def test_05_early_close_session(self):
        # Black Friday 2026-11-27: early close at 1:00 PM (13:00 Eastern)
        sess = MarketClock.get_session(date(2026, 11, 27))
        self.assertIsNotNone(sess)
        open_t, close_t = sess
        self.assertEqual(open_t, time(9, 30))
        self.assertEqual(close_t, time(13, 0))

        # At 12:30 PM Eastern -> REGULAR
        dt_open = datetime(2026, 11, 27, 12, 30, 0, tzinfo=EASTERN)
        self.assertEqual(MarketClock.get_session_state(dt_open), MarketSessionState.REGULAR)

        # At 1:15 PM Eastern -> POST_MARKET
        dt_closed = datetime(2026, 11, 27, 13, 15, 0, tzinfo=EASTERN)
        self.assertEqual(MarketClock.get_session_state(dt_closed), MarketSessionState.POST_MARKET)

    # 6. Daylight saving transitions
    def test_06_daylight_saving_transitions(self):
        # Summer date (July) -> EDT (UTC-4)
        dt_summer_utc = datetime(2026, 7, 15, 14, 0, 0, tzinfo=UTC)
        dt_summer_et = to_eastern(dt_summer_utc)
        self.assertEqual(dt_summer_et.hour, 10)  # 14 - 4 = 10 AM
        self.assertEqual(dt_summer_et.tzname(), "EDT")

        # Winter date (January) -> EST (UTC-5)
        dt_winter_utc = datetime(2026, 1, 15, 15, 0, 0, tzinfo=UTC)
        dt_winter_et = to_eastern(dt_winter_utc)
        self.assertEqual(dt_winter_et.hour, 10)  # 15 - 5 = 10 AM
        self.assertEqual(dt_winter_et.tzname(), "EST")

    # 7. Machine timezone neutrality
    def test_07_machine_timezone_neutrality(self):
        # Timezone-naive datetime defaults to Eastern representation
        naive_dt = datetime(2026, 10, 14, 10, 0, 0)
        utc_dt = to_utc(naive_dt)
        self.assertEqual(utc_dt.tzinfo, UTC)
        self.assertEqual(utc_dt.hour, 14)  # 10 AM EDT -> 14 UTC

    # 8. End-to-end integration: Provider to validation to cache
    def test_08_provider_to_validation_to_cache(self):
        sample_json = make_sample_yahoo_json()

        def mock_get(url, timeout):
            return sample_json

        provider = YahooMarketDataProvider(http_get_fn=mock_get)
        service = MarketDataService(provider=provider, cache=self.cache)

        bars = service.get_historical_bars(
            ticker="AAPL",
            interval="15m",
            start=datetime(2026, 10, 14, 13, 0, 0, tzinfo=UTC),
            end=datetime(2026, 10, 14, 15, 0, 0, tzinfo=UTC),
            current_time=self.ref_time,
        )
        self.assertEqual(len(bars), 2)

        # Verify bars are saved in cache
        cached_bars = self.cache.get_historical_candles(
            "AAPL", "15m", datetime(2026, 10, 14, 13, 0, 0, tzinfo=UTC), datetime(2026, 10, 14, 15, 0, 0, tzinfo=UTC)
        )
        self.assertEqual(len(cached_bars), 2)
        self.assertEqual(cached_bars[0].open, Decimal("150.0"))

    # 9. Cache fallback during provider outage
    def test_09_cache_fallback_during_provider_outage(self):
        sample_json = make_sample_yahoo_json()
        outage = [False]

        def mock_get(url, timeout):
            if outage[0]:
                fp = None
                raise urllib.error.HTTPError(url, 503, "Service Unavailable", {}, fp)
            return sample_json

        provider = YahooMarketDataProvider(http_get_fn=mock_get, max_retries=2, backoff_seconds=0.001)
        service = MarketDataService(provider=provider, cache=self.cache)

        # Step 1: Successful initial population of cache
        bars = service.get_historical_bars("AAPL", "15m", self.ref_time - timedelta(hours=1), self.ref_time, current_time=self.ref_time)
        self.assertEqual(len(bars), 2)

        # Step 2: Trigger outage
        outage[0] = True
        fallback_bars = service.get_historical_bars("AAPL", "15m", self.ref_time - timedelta(hours=1), self.ref_time, current_time=self.ref_time)
        self.assertEqual(len(fallback_bars), 2)
        self.assertEqual(fallback_bars[0].source, "yahoo_cached_fallback")

    # 10. Provider failure raised when cache is empty during outage
    def test_10_provider_failure_when_cache_empty(self):
        def mock_get(url, timeout):
            fp = None
            raise urllib.error.HTTPError(url, 500, "Server Error", {}, fp)

        provider = YahooMarketDataProvider(http_get_fn=mock_get, max_retries=2, backoff_seconds=0.001)
        service = MarketDataService(provider=provider, cache=self.cache)

        with self.assertRaises(ProviderUnavailableError):
            service.get_historical_bars("AAPL", "15m", self.ref_time - timedelta(hours=1), self.ref_time)

    # 11. Market closed status reporting FRESH
    def test_11_market_closed_status_is_fresh(self):
        # Populate cache with a bar from Friday close
        fri_close_ts = datetime(2026, 10, 16, 19, 45, 0, tzinfo=UTC)  # Friday 3:45 PM EDT
        c = OHLCVCandle(
            ticker="AAPL",
            interval="15m",
            timestamp=fri_close_ts,
            open=Decimal("150.00"),
            high=Decimal("152.00"),
            low=Decimal("149.00"),
            close=Decimal("151.00"),
            volume=50000,
            status=CandleStatus.COMPLETE,
            received_timestamp=fri_close_ts,
        )
        self.cache.store_candles([c], now=fri_close_ts)

        service = MarketDataService(cache=self.cache)
        # Saturday afternoon (market closed)
        sat_time = datetime(2026, 10, 17, 18, 0, 0, tzinfo=UTC)
        status = service.get_market_data_status("AAPL", "15m", current_time=sat_time)

        # Must report FRESH because market is closed and data is from the most recent session
        self.assertEqual(status.freshness, DataFreshness.FRESH)
        self.assertIn("Market is CLOSED", status.message)

    # 12. Market open with delayed provider data reports DELAYED
    def test_12_market_open_delayed_data_reports_delayed(self):
        # Wednesday 14:20 UTC (10:20 AM EDT, market open).
        # Last available bar was at 14:00 UTC (20 minutes ago, standard 15-min delayed publication).
        bar_ts = datetime(2026, 10, 14, 14, 0, 0, tzinfo=UTC)
        c = OHLCVCandle(
            ticker="AAPL",
            interval="15m",
            timestamp=bar_ts,
            open=Decimal("150.00"),
            high=Decimal("152.00"),
            low=Decimal("149.00"),
            close=Decimal("151.00"),
            volume=50000,
            status=CandleStatus.COMPLETE,
            received_timestamp=bar_ts,
            is_delayed=True,
        )
        self.cache.store_candles([c], now=bar_ts)

        service = MarketDataService(cache=self.cache, stale_threshold_seconds=1200)
        cur_open_time = datetime(2026, 10, 14, 14, 20, 0, tzinfo=UTC)
        status = service.get_market_data_status("AAPL", "15m", current_time=cur_open_time)

        self.assertEqual(status.freshness, DataFreshness.DELAYED)
        self.assertIn("delayed by ~15 min", status.message)

    # 13. Market open with data older than tolerance reports STALE
    def test_13_market_open_stale_data_reports_stale(self):
        # Wednesday 15:30 UTC. Last bar was 13:45 UTC (1h 45m ago during open session).
        bar_ts = datetime(2026, 10, 14, 13, 45, 0, tzinfo=UTC)
        c = OHLCVCandle(
            ticker="AAPL",
            interval="15m",
            timestamp=bar_ts,
            open=Decimal("150.00"),
            high=Decimal("152.00"),
            low=Decimal("149.00"),
            close=Decimal("151.00"),
            volume=50000,
            status=CandleStatus.COMPLETE,
            received_timestamp=bar_ts,
            is_delayed=True,
        )
        self.cache.store_candles([c], now=bar_ts)

        service = MarketDataService(cache=self.cache, stale_threshold_seconds=1200)
        cur_open_time = datetime(2026, 10, 14, 15, 30, 0, tzinfo=UTC)
        status = service.get_market_data_status("AAPL", "15m", current_time=cur_open_time)

        self.assertEqual(status.freshness, DataFreshness.STALE)
        self.assertIn("Data is stale", status.message)

    # 14. Public API validate_candles returns structured errors
    def test_14_validate_candles_returns_diagnostics(self):
        service = MarketDataService()
        bad_candle = OHLCVCandle(
            ticker="AAPL",
            interval="15m",
            timestamp=self.ref_time,
            open=Decimal("150.00"),
            high=Decimal("140.00"),  # High < Low
            low=Decimal("145.00"),
            close=Decimal("148.00"),
            volume=1000,
        )
        errors = service.validate_candles([bad_candle])
        self.assertEqual(len(errors), 1)
        self.assertIn("lower than low", errors[0])

    # 15. Read-only architecture verification
    def test_15_read_only_isolation(self):
        service = MarketDataService(cache=self.cache)
        # Ensure service does not expose order or portfolio modification methods
        for forbidden in ("place_order", "execute_trade", "modify_risk", "submit_ticket", "connect_broker"):
            self.assertFalse(hasattr(service, forbidden), f"Service unexpectedly exposes '{forbidden}'")

    # 16. Fix D-1: Friday close inspected on Saturday reports FRESH
    def test_16_closed_market_friday_close_on_saturday_fresh(self):
        # Friday 2026-10-16 15:45 EDT (19:45 UTC)
        fri_bar_ts = datetime(2026, 10, 16, 19, 45, 0, tzinfo=UTC)
        c = OHLCVCandle(
            ticker="MSFT",
            interval="15m",
            timestamp=fri_bar_ts,
            open=Decimal("400.00"),
            high=402,
            low=399,
            close=401,
            volume=20000,
            status=CandleStatus.COMPLETE,
            received_timestamp=fri_bar_ts,
        )
        self.cache.store_candles([c], now=fri_bar_ts)

        service = MarketDataService(cache=self.cache)
        sat_time = datetime(2026, 10, 17, 16, 0, 0, tzinfo=UTC)  # Saturday 12:00 EDT
        status = service.get_market_data_status("MSFT", "15m", current_time=sat_time)
        self.assertEqual(status.freshness, DataFreshness.FRESH)
        self.assertIn("CLOSED", status.message)

    # 17. Fix D-1: Friday close inspected on Monday pre-market reports FRESH
    def test_17_closed_market_friday_close_on_monday_premarket_fresh(self):
        fri_bar_ts = datetime(2026, 10, 16, 19, 45, 0, tzinfo=UTC)
        c = OHLCVCandle(
            ticker="MSFT",
            interval="15m",
            timestamp=fri_bar_ts,
            open=Decimal("400.00"),
            high=402,
            low=399,
            close=401,
            volume=20000,
            status=CandleStatus.COMPLETE,
            received_timestamp=fri_bar_ts,
        )
        self.cache.store_candles([c], now=fri_bar_ts)

        service = MarketDataService(cache=self.cache)
        mon_premarket = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)  # Monday 8:00 AM EDT
        status = service.get_market_data_status("MSFT", "15m", current_time=mon_premarket)
        self.assertEqual(status.freshness, DataFreshness.FRESH)
        self.assertIn("PRE_MARKET", status.message)

    # 18. Fix D-1: A two-week-old cached candle on a weekend reports STALE
    def test_18_closed_market_two_week_old_cached_candle_stale(self):
        old_bar_ts = datetime(2026, 10, 2, 19, 45, 0, tzinfo=UTC)  # Two weeks prior
        c = OHLCVCandle(
            ticker="MSFT",
            interval="15m",
            timestamp=old_bar_ts,
            open=Decimal("400.00"),
            high=402,
            low=399,
            close=401,
            volume=20000,
            status=CandleStatus.COMPLETE,
            received_timestamp=datetime(2026, 10, 17, 12, 0, 0, tzinfo=UTC),  # Recent retrieval timestamp
        )
        self.cache.store_candles([c])

        service = MarketDataService(cache=self.cache)
        sat_time = datetime(2026, 10, 17, 16, 0, 0, tzinfo=UTC)
        status = service.get_market_data_status("MSFT", "15m", current_time=sat_time)
        self.assertEqual(status.freshness, DataFreshness.STALE)
        self.assertIn("stale", status.message.lower())

    # 19. Fix D-1: Holiday checks (Thanksgiving) - prior session FRESH vs older STALE
    def test_19_closed_market_holiday_prior_session_fresh_and_older_stale(self):
        # Wednesday 2026-11-25 15:45 EST (20:45 UTC)
        wed_ts = datetime(2026, 11, 25, 20, 45, 0, tzinfo=UTC)
        c_wed = OHLCVCandle(
            ticker="AAPL",
            interval="15m",
            timestamp=wed_ts,
            open=Decimal("150.00"),
            high=152,
            low=149,
            close=151,
            volume=10000,
            status=CandleStatus.COMPLETE,
            received_timestamp=wed_ts,
        )
        self.cache.store_candles([c_wed], now=wed_ts)

        service = MarketDataService(cache=self.cache)
        thanksgiving_noon = datetime(2026, 11, 26, 17, 0, 0, tzinfo=UTC)  # Thanksgiving 12:00 EST
        status = service.get_market_data_status("AAPL", "15m", current_time=thanksgiving_noon)
        self.assertEqual(status.freshness, DataFreshness.FRESH)

        # Older candle from Tuesday on Thanksgiving -> STALE
        self.cache.clear()
        tue_ts = datetime(2026, 11, 24, 20, 45, 0, tzinfo=UTC)
        c_tue = OHLCVCandle(
            ticker="AAPL",
            interval="15m",
            timestamp=tue_ts,
            open=Decimal("150.00"),
            high=152,
            low=149,
            close=151,
            volume=10000,
            status=CandleStatus.COMPLETE,
        )
        self.cache.store_candles([c_tue], now=tue_ts)
        status_old = service.get_market_data_status("AAPL", "15m", current_time=thanksgiving_noon)
        self.assertEqual(status_old.freshness, DataFreshness.STALE)

    # 20. Fix D-1: Early close session (Black Friday) - close bar FRESH vs older STALE
    def test_20_closed_market_early_close_session_fresh_after_close(self):
        # Black Friday early close at 1:00 PM EST (18:00 UTC). Final 15m bar starts at 12:45 EST (17:45 UTC).
        bf_bar_ts = datetime(2026, 11, 27, 17, 45, 0, tzinfo=UTC)
        c_bf = OHLCVCandle(
            ticker="AAPL",
            interval="15m",
            timestamp=bf_bar_ts,
            open=Decimal("150.00"),
            high=152,
            low=149,
            close=151,
            volume=10000,
            status=CandleStatus.COMPLETE,
            received_timestamp=bf_bar_ts,
        )
        self.cache.store_candles([c_bf], now=bf_bar_ts)

        service = MarketDataService(cache=self.cache)
        # Inspected at 2:00 PM EST (19:00 UTC, POST_MARKET after early close)
        post_early_close = datetime(2026, 11, 27, 19, 0, 0, tzinfo=UTC)
        status = service.get_market_data_status("AAPL", "15m", current_time=post_early_close)
        self.assertEqual(status.freshness, DataFreshness.FRESH)

        # Older candle from Wednesday before Thanksgiving -> STALE on Black Friday afternoon
        self.cache.clear()
        wed_ts = datetime(2026, 11, 25, 20, 45, 0, tzinfo=UTC)
        c_wed = OHLCVCandle(
            ticker="AAPL",
            interval="15m",
            timestamp=wed_ts,
            open=Decimal("150.00"),
            high=152,
            low=149,
            close=151,
            volume=10000,
            status=CandleStatus.COMPLETE,
        )
        self.cache.store_candles([c_wed], now=wed_ts)
        status_stale = service.get_market_data_status("AAPL", "15m", current_time=post_early_close)
        self.assertEqual(status_stale.freshness, DataFreshness.STALE)

    # 21. Fix D-1: Inactive or suspended ticker with no bar at session close reports STALE
    def test_21_closed_market_inactive_ticker_without_close_bar_stale(self):
        # Ticker traded on Friday morning but ceased at 10:30 EDT (14:30 UTC) due to halt or lack of liquidity
        halt_bar_ts = datetime(2026, 10, 16, 14, 30, 0, tzinfo=UTC)
        c_halt = OHLCVCandle(
            ticker="HALT",
            interval="15m",
            timestamp=halt_bar_ts,
            open=Decimal("20.00"),
            high=Decimal("20.50"),
            low=Decimal("19.50"),
            close=Decimal("20.00"),
            volume=100,
            status=CandleStatus.COMPLETE,
            received_timestamp=halt_bar_ts,
        )
        self.cache.store_candles([c_halt], now=halt_bar_ts)

        service = MarketDataService(cache=self.cache)
        sat_time = datetime(2026, 10, 17, 16, 0, 0, tzinfo=UTC)
        status = service.get_market_data_status("HALT", "15m", current_time=sat_time)
        # Must report STALE because the candle did not reach the scheduled session close
        self.assertEqual(status.freshness, DataFreshness.STALE)
        self.assertIn("before expected session close", status.message)

    # 22. Fix O-4: Unsupported calendar year reports UNKNOWN
    def test_22_unsupported_calendar_year_reports_unknown(self):
        # Date in 2025 (outside supported 2026-2027 calendar)
        dt_2025 = datetime(2025, 10, 15, 14, 0, 0, tzinfo=UTC)
        c_2025 = OHLCVCandle(
            ticker="AAPL",
            interval="15m",
            timestamp=dt_2025,
            open=Decimal("150.00"),
            high=152,
            low=149,
            close=151,
            volume=10000,
            status=CandleStatus.COMPLETE,
        )
        self.cache.store_candles([c_2025])

        service = MarketDataService(cache=self.cache)
        status = service.get_market_data_status("AAPL", "15m", current_time=dt_2025)
        self.assertEqual(status.freshness, DataFreshness.UNKNOWN)
        self.assertIn("Calendar coverage unverified", status.message)

    # 23. Fix D-1: Daily bar matching latest completed session is FRESH on weekend
    def test_23_daily_bar_closed_market_prior_session_fresh(self):
        fri_daily_ts = datetime(2026, 10, 16, 13, 30, 0, tzinfo=UTC)  # Friday daily open (9:30 AM EDT)
        c_daily = OHLCVCandle(
            ticker="AAPL",
            interval="1d",
            timestamp=fri_daily_ts,
            open=Decimal("150.00"),
            high=153,
            low=149,
            close=152,
            volume=1000000,
            status=CandleStatus.COMPLETE,
            received_timestamp=datetime(2026, 10, 16, 20, 5, 0, tzinfo=UTC),
        )
        self.cache.store_candles([c_daily])

        service = MarketDataService(cache=self.cache)
        sat_time = datetime(2026, 10, 17, 16, 0, 0, tzinfo=UTC)
        status = service.get_market_data_status("AAPL", "1d", current_time=sat_time)
        self.assertEqual(status.freshness, DataFreshness.FRESH)

    # 24. Fix D-1: Daily bar from an older session is STALE on weekend
    def test_24_daily_bar_closed_market_older_session_stale(self):
        thu_daily_ts = datetime(2026, 10, 15, 13, 30, 0, tzinfo=UTC)  # Thursday daily
        c_daily = OHLCVCandle(
            ticker="AAPL",
            interval="1d",
            timestamp=thu_daily_ts,
            open=Decimal("150.00"),
            high=153,
            low=149,
            close=152,
            volume=1000000,
            status=CandleStatus.COMPLETE,
        )
        self.cache.store_candles([c_daily])

        service = MarketDataService(cache=self.cache)
        sat_time = datetime(2026, 10, 17, 16, 0, 0, tzinfo=UTC)
        status = service.get_market_data_status("AAPL", "1d", current_time=sat_time)
        self.assertEqual(status.freshness, DataFreshness.STALE)
        self.assertIn("last completed session was 2026-10-16", status.message)


if __name__ == "__main__":
    unittest.main()
