"""Unit test suite for trader-swing market data provider abstraction and Yahoo implementation.

Verifies:
1. Successful historical bar fetching and normalization.
2. Latest bar retrieval.
3. Provider metadata preservation (15-min delay disclosure).
4. Empty provider response handling (NoDataError).
5. Chart error in JSON response handling (NoDataError, ProviderError).
6. Malformed JSON handling (InvalidResponseError).
7. Transient HTTP 500 error leading to retry exhaustion.
8. Transient HTTP 429 rate limit error handling.
9. Network timeout handling (TimeoutError).
10. Successful recovery after transient retry (503 then 200).
11. Non-retryable client error (HTTP 404 raises NoDataError).
12. Terminal forming candle detection when bar period is active.
13. Terminal bar marked COMPLETE when period has elapsed.
14. Fallback to COMPLETE when forming preconditions fail.
"""

from datetime import datetime, timedelta, timezone
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
    OHLCVCandle,
    to_utc,
    UTC,
)
from market_data_validation import (
    InvalidResponseError,
    NoDataError,
    ProviderError,
    ProviderUnavailableError,
    RateLimitError,
    TimeoutError,
)
from providers.yahoo import YahooMarketDataProvider


def make_sample_yahoo_json(ticker="AAPL", timestamps=None, opens=None, highs=None, lows=None, closes=None, volumes=None, reg_time=1791921600):
    if timestamps is None:
        timestamps = [1791898200, 1791899100]  # two 15-minute bars
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


class TestSwingMarketDataProvider(unittest.TestCase):
    """Test suite for provider communication, parsing, retry semantics, and status resolution."""

    def setUp(self):
        self.ref_time = datetime(2026, 10, 14, 14, 0, 0, tzinfo=UTC)

    def test_01_successful_historical_fetch(self):
        sample_json = make_sample_yahoo_json()

        def mock_get(url, timeout):
            return sample_json

        provider = YahooMarketDataProvider(http_get_fn=mock_get)
        bars = provider.fetch_historical_bars(
            ticker="AAPL",
            interval="15m",
            start=datetime(2026, 10, 14, 13, 0, 0, tzinfo=UTC),
            end=datetime(2026, 10, 14, 15, 0, 0, tzinfo=UTC),
        )
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0].ticker, "AAPL")
        self.assertEqual(bars[0].interval, "15m")
        self.assertEqual(bars[0].open, Decimal("150.0"))
        self.assertEqual(bars[1].close, Decimal("152.5"))
        self.assertEqual(bars[0].status, CandleStatus.COMPLETE)

    def test_02_fetch_latest_bar(self):
        sample_json = make_sample_yahoo_json()

        def mock_get(url, timeout):
            return sample_json

        provider = YahooMarketDataProvider(http_get_fn=mock_get)
        latest = provider.fetch_latest_bar(ticker="AAPL", interval="15m")
        self.assertIsNotNone(latest)
        self.assertEqual(latest.ticker, "AAPL")
        self.assertEqual(latest.close, Decimal("152.5"))

    def test_03_provider_metadata_preservation(self):
        provider = YahooMarketDataProvider()
        meta = provider.get_provider_metadata()
        self.assertEqual(meta["provider_name"], "yahoo")
        self.assertTrue(meta["is_delayed"])
        self.assertEqual(meta["delay_minutes"], 15)
        self.assertIn("delayed quotes", meta["disclosure"])

    def test_04_empty_provider_response(self):
        empty_json = json.dumps({"chart": {"result": [], "error": None}})

        def mock_get(url, timeout):
            return empty_json

        provider = YahooMarketDataProvider(http_get_fn=mock_get)
        with self.assertRaises(NoDataError):
            provider.fetch_historical_bars("AAPL", "15m", self.ref_time, self.ref_time)

    def test_05_chart_error_in_json(self):
        error_json = json.dumps({
            "chart": {
                "result": None,
                "error": {"code": "Not Found", "description": "No data found for symbol ZZZZ"},
            }
        })

        def mock_get(url, timeout):
            return error_json

        provider = YahooMarketDataProvider(http_get_fn=mock_get)
        with self.assertRaises(NoDataError):
            provider.fetch_historical_bars("ZZZZ", "15m", self.ref_time, self.ref_time)

    def test_06_malformed_json_response(self):
        def mock_get(url, timeout):
            return "<html><body>Server Error 500</body></html>"

        provider = YahooMarketDataProvider(http_get_fn=mock_get)
        with self.assertRaises(InvalidResponseError):
            provider.fetch_historical_bars("AAPL", "15m", self.ref_time, self.ref_time)

    def test_07_http_500_retries_and_exhausts(self):
        calls = []

        def mock_get(url, timeout):
            calls.append(url)
            fp = None
            raise urllib.error.HTTPError(url, 500, "Internal Server Error", {}, fp)

        provider = YahooMarketDataProvider(
            hosts=["host1", "host2"],
            max_retries=2,
            backoff_seconds=0.001,
            http_get_fn=mock_get,
        )
        with self.assertRaises(ProviderUnavailableError):
            provider.fetch_historical_bars("AAPL", "15m", self.ref_time, self.ref_time)
        # 2 attempts * 2 hosts = 4 calls
        self.assertEqual(len(calls), 4)

    def test_08_http_429_rate_limit(self):
        def mock_get(url, timeout):
            fp = None
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, fp)

        provider = YahooMarketDataProvider(
            hosts=["host1"],
            max_retries=1,
            backoff_seconds=0.001,
            http_get_fn=mock_get,
        )
        with self.assertRaises(RateLimitError):
            provider.fetch_historical_bars("AAPL", "15m", self.ref_time, self.ref_time)

    def test_09_network_timeout(self):
        def mock_get(url, timeout):
            raise TimeoutError("Connection timed out")

        provider = YahooMarketDataProvider(
            hosts=["host1"],
            max_retries=1,
            backoff_seconds=0.001,
            http_get_fn=mock_get,
        )
        with self.assertRaises(TimeoutError):
            provider.fetch_historical_bars("AAPL", "15m", self.ref_time, self.ref_time)

    def test_10_recovery_after_transient_retry(self):
        attempts = [0]
        sample_json = make_sample_yahoo_json()

        def mock_get(url, timeout):
            attempts[0] += 1
            if attempts[0] == 1:
                fp = None
                raise urllib.error.HTTPError(url, 503, "Service Unavailable", {}, fp)
            return sample_json

        provider = YahooMarketDataProvider(
            hosts=["host1"],
            max_retries=3,
            backoff_seconds=0.001,
            http_get_fn=mock_get,
        )
        bars = provider.fetch_historical_bars("AAPL", "15m", self.ref_time, self.ref_time)
        self.assertEqual(len(bars), 2)
        self.assertEqual(attempts[0], 2)

    def test_11_http_404_not_found_fails_fast_without_retries(self):
        calls = []

        def mock_get(url, timeout):
            calls.append(url)
            fp = None
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, fp)

        provider = YahooMarketDataProvider(
            hosts=["host1", "host2"],
            max_retries=3,
            http_get_fn=mock_get,
        )
        with self.assertRaises(NoDataError):
            provider.fetch_historical_bars("INVALID", "15m", self.ref_time, self.ref_time)
        # Should fail fast on the first call without retrying
        self.assertEqual(len(calls), 1)

    def test_12_terminal_forming_candle_detected(self):
        # Current time: 14:05 UTC (10:05 AM EDT).
        # Bar 1: 13:45 to 14:00 (COMPLETE).
        # Bar 2: 14:00 to 14:15 (FORMING, because 14:05 is within this 15m window).
        ts1 = int(datetime(2026, 10, 14, 13, 45, 0, tzinfo=UTC).timestamp())
        ts2 = int(datetime(2026, 10, 14, 14, 0, 0, tzinfo=UTC).timestamp())

        sample_json = make_sample_yahoo_json(
            timestamps=[ts1, ts2],
            opens=[150.0, 151.0],
            highs=[151.5, 152.0],
            lows=[149.5, 150.5],
            closes=[151.0, 151.8],
            volumes=[10000, 4000],
        )

        def mock_get(url, timeout):
            return sample_json

        provider = YahooMarketDataProvider(http_get_fn=mock_get)
        cur_time = datetime(2026, 10, 14, 14, 5, 0, tzinfo=UTC)
        bars = provider._parse_chart_json(sample_json, "AAPL", "15m", current_time=cur_time)

        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0].status, CandleStatus.COMPLETE)
        self.assertEqual(bars[1].status, CandleStatus.FORMING)

    def test_13_terminal_bar_complete_when_period_elapsed(self):
        # Current time: 14:20 UTC.
        # Bar 2 started at 14:00 and ended at 14:15.
        # Since current time (14:20) > bar end (14:15), Bar 2 is COMPLETE.
        ts1 = int(datetime(2026, 10, 14, 13, 45, 0, tzinfo=UTC).timestamp())
        ts2 = int(datetime(2026, 10, 14, 14, 0, 0, tzinfo=UTC).timestamp())

        sample_json = make_sample_yahoo_json(timestamps=[ts1, ts2])
        provider = YahooMarketDataProvider()
        cur_time = datetime(2026, 10, 14, 14, 20, 0, tzinfo=UTC)
        bars = provider._parse_chart_json(sample_json, "AAPL", "15m", current_time=cur_time)

        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0].status, CandleStatus.COMPLETE)
        self.assertEqual(bars[1].status, CandleStatus.COMPLETE)

    def test_14_forming_fallback_to_complete_when_closed_session(self):
        # Sunday night (market closed). A bar cannot be FORMING when market is closed.
        ts = int(datetime(2026, 10, 18, 19, 50, 0, tzinfo=UTC).timestamp())
        sample_json = make_sample_yahoo_json(timestamps=[ts], opens=[150.0], highs=[151.0], lows=[149.0], closes=[150.5], volumes=[1000])

        provider = YahooMarketDataProvider()
        sunday_time = datetime(2026, 10, 18, 20, 0, 0, tzinfo=UTC)
        bars = provider._parse_chart_json(sample_json, "AAPL", "15m", current_time=sunday_time)

        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].status, CandleStatus.COMPLETE)

    # 15. Fix O-5: Corrected Yahoo delay metadata semantics
    def test_15_provider_metadata_delay_semantics(self):
        provider = YahooMarketDataProvider()
        meta = provider.get_provider_metadata()
        self.assertEqual(meta["provider_name"], "yahoo")
        self.assertTrue(meta["is_delayed"])
        self.assertEqual(meta["assumed_delay_minutes"], 15)
        self.assertEqual(meta["delay_status"], "ASSUMED_UNVERIFIED")
        self.assertEqual(meta["delay_minutes"], 15)
        self.assertIn("unverified", meta["disclosure"].lower())
        self.assertIn("not guaranteed", meta["disclosure"].lower())

    # 16. Fix O-6: Interval-specific latest-bar query ranges
    def test_16_interval_specific_latest_bar_ranges(self):
        requested_urls = []
        sample_json = make_sample_yahoo_json()

        def mock_get(url, timeout):
            requested_urls.append(url)
            return sample_json

        provider = YahooMarketDataProvider(http_get_fn=mock_get)

        # 1. Weekly interval: must use range=1mo
        provider.fetch_latest_bar("AAPL", "1wk")
        self.assertIn("range=1mo", requested_urls[-1])
        self.assertIn("interval=1wk", requested_urls[-1])

        # 2. Daily interval: must use range=1mo
        provider.fetch_latest_bar("AAPL", "1d")
        self.assertIn("range=1mo", requested_urls[-1])
        self.assertIn("interval=1d", requested_urls[-1])

        # 3. 1h interval: must use range=5d
        provider.fetch_latest_bar("AAPL", "1h")
        self.assertIn("range=5d", requested_urls[-1])
        self.assertIn("interval=1h", requested_urls[-1])

        # 4. 15m interval: must use range=5d
        provider.fetch_latest_bar("AAPL", "15m")
        self.assertIn("range=5d", requested_urls[-1])
        self.assertIn("interval=15m", requested_urls[-1])

        # 5. 5m interval: must use range=5d
        provider.fetch_latest_bar("AAPL", "5m")
        self.assertIn("range=5d", requested_urls[-1])
        self.assertIn("interval=5m", requested_urls[-1])

        # 6. 1m interval: must use range=5d
        provider.fetch_latest_bar("AAPL", "1m")
        self.assertIn("range=5d", requested_urls[-1])
        self.assertIn("interval=1m", requested_urls[-1])

    # 17. Fix B: Daily chart requests exclude extended hours (includePrePost=false)
    def test_17_yahoo_daily_chart_excludes_prepost(self):
        requested_urls = []
        sample_json = make_sample_yahoo_json()

        def mock_get(url, timeout):
            requested_urls.append(url)
            return sample_json

        provider = YahooMarketDataProvider(http_get_fn=mock_get)
        provider.fetch_latest_bar("AAPL", "1d")
        self.assertIn("includePrePost=false", requested_urls[-1])

        provider.fetch_historical_bars("AAPL", "1d", self.ref_time - timedelta(days=5), self.ref_time)
        self.assertIn("includePrePost=false", requested_urls[-1])

        # For intraday (15m), includePrePost=true remains enabled
        provider.fetch_latest_bar("AAPL", "15m")
        self.assertIn("includePrePost=true", requested_urls[-1])

    # 18. Fix B: Pre-market daily bar dropped, Friday completed bar preserved as latest
    def test_18_yahoo_pre_market_daily_bar_dropped_and_friday_preserved(self):
        # Timestamps: Friday 2026-10-16 00:00 EDT (04:00 UTC) and Monday 2026-10-19 00:00 EDT (04:00 UTC)
        ts_fri = int(datetime(2026, 10, 16, 4, 0, 0, tzinfo=UTC).timestamp())
        ts_mon = int(datetime(2026, 10, 19, 4, 0, 0, tzinfo=UTC).timestamp())

        # Response at 8:00 AM EDT (12:00 UTC) on Monday
        now_mon_8am = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)

        sample_json = make_sample_yahoo_json(
            timestamps=[ts_fri, ts_mon],
            opens=[150.0, 151.0],
            highs=[153.0, 152.0],
            lows=[149.0, 150.5],
            closes=[152.5, 151.5],
            volumes=[20000000, 50000],  # Monday has early indicative volume
            reg_time=int(now_mon_8am.timestamp()),
        )

        def mock_get(url, timeout):
            return sample_json

        provider = YahooMarketDataProvider(http_get_fn=mock_get)
        latest = provider.fetch_latest_bar("AAPL", "1d", current_time=now_mon_8am)
        self.assertIsNotNone(latest)

        # Must return Friday's completed daily bar, NOT Monday's pre-market bar
        self.assertEqual(latest.timestamp, datetime(2026, 10, 16, 4, 0, 0, tzinfo=UTC))
        self.assertEqual(latest.status, CandleStatus.COMPLETE)
        self.assertEqual(latest.close, Decimal("152.5"))

        # Explicitly verify parsing drops the pre-market bar
        bars = provider._parse_chart_json(sample_json, "AAPL", "1d", current_time=now_mon_8am)
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].timestamp, datetime(2026, 10, 16, 4, 0, 0, tzinfo=UTC))
        self.assertEqual(bars[0].status, CandleStatus.COMPLETE)


if __name__ == "__main__":
    unittest.main()
