"""Deterministic offline unit test suite for Phase 7.1.3 Mocked Alpaca Market Data Provider.

Verifies:
1. Successful historical bar fetching and normalization from mocked IEX responses.
2. Multi-ticker batch fetching (up to 100 symbols per call).
3. Latest bar retrieval.
4. Strict Anti-Substitution Guard: asserts is_consolidated_volume is False and
   get_consolidated_volume() raises InvalidVolumeError.
5. Empty response handling (NoDataError).
6. Missing symbol handling (NoDataError).
7. Malformed missing price fields (InvalidResponseError).
8. Inverted candle geometry rejection (InvalidPriceError).
9. Negative volume rejection (InvalidVolumeError).
10. Duplicate timestamps in sequence rejection (SequenceValidationError).
11. HTTP 429 rate limit error mapping (RateLimitError).
12. HTTP 401 / 403 authentication and forbidden error mapping (ProviderUnavailableError).
13. Transient HTTP 500 error retry exhaustion (ProviderUnavailableError).
14. Transient recovery after initial retry.
15. Provider metadata disclosures and feed classification.
16. Offline safety guard: unconfigured / unmocked calls fail safely without network requests.
"""

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import json
import os
import sys
import unittest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SWING_DIR = os.path.join(ROOT_DIR, ".agents", "skills", "trader-swing")
for path in (ROOT_DIR, SWING_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

from market_data_models import (
    CandleStatus,
    EASTERN,
    MarketClock,
    OHLCVCandle,
    to_eastern,
    to_utc,
    UTC,
)
from market_data_validation import (
    CandleValidationError,
    InvalidPriceError,
    InvalidResponseError,
    InvalidVolumeError,
    NoDataError,
    ProviderError,
    ProviderUnavailableError,
    RateLimitError,
    SequenceValidationError,
    TimeoutError,
)
from providers.alpaca_provider import AlpacaCandle, AlpacaMarketDataProvider

FIXTURE_PATH = os.path.join(
    os.path.dirname(__file__),
    "fixtures",
    "trader-swing",
    "alpaca",
    "provider_responses.json",
)


class TestSwingAlpacaProvider(unittest.TestCase):
    """Test suite for AlpacaMarketDataProvider using deterministic mock fixtures."""

    @classmethod
    def setUpClass(cls):
        with open(FIXTURE_PATH, "r", encoding="utf-8") as f:
            cls.fixtures = json.load(f)

    def setUp(self):
        self.clock = MarketClock()
        # Pinned mock current time after market close on 2026-11-16
        self.mock_now = datetime(2026, 11, 16, 21, 5, 0, tzinfo=UTC)

    # -------------------------------------------------------------------------
    # 1. Historical Bars Fetching & Normalization
    # -------------------------------------------------------------------------
    def test_01_historical_bars_fetching_and_normalization(self):
        """Historical bars must be normalized with Decimal prices, session timestamps, and IEX provenance."""
        def mock_request(endpoint, params):
            return self.fixtures["valid_sample_spy"]

        provider = AlpacaMarketDataProvider(alpaca_request_fn=mock_request, clock=self.clock)
        bars = provider.fetch_historical_bars(
            ticker="SPY",
            interval="1d",
            start=datetime(2026, 11, 12, tzinfo=UTC),
            end=datetime(2026, 11, 16, tzinfo=UTC),
            current_time=self.mock_now,
        )

        self.assertEqual(len(bars), 3)
        b0 = bars[0]
        self.assertIsInstance(b0, AlpacaCandle)
        self.assertEqual(b0.ticker, "SPY")
        self.assertEqual(b0.interval, "1d")
        self.assertEqual(b0.open, Decimal("752.10"))
        self.assertEqual(b0.high, Decimal("755.40"))
        self.assertEqual(b0.low, Decimal("750.80"))
        self.assertEqual(b0.close, Decimal("754.20"))
        self.assertEqual(b0.volume, 1420500)
        self.assertEqual(b0.vwap, Decimal("753.15"))
        self.assertEqual(b0.trade_count, 16500)
        self.assertEqual(b0.feed_classification, "IEX")
        self.assertEqual(b0.source, "alpaca_iex")
        self.assertFalse(b0.is_consolidated_volume)

        # Check timestamp normalization to NYSE session open 09:30:00 ET
        dt_est = to_eastern(b0.timestamp)
        self.assertEqual(dt_est.date(), date(2026, 11, 12))
        self.assertEqual(dt_est.time(), time(9, 30, 0))

        # Check status: 2026-11-16 session has elapsed relative to mock_now (21:05 UTC)
        b2 = bars[2]
        self.assertEqual(b2.status, CandleStatus.COMPLETE)

    # -------------------------------------------------------------------------
    # 2. Multi-Ticker Batch Fetching
    # -------------------------------------------------------------------------
    def test_02_multi_ticker_batch_fetching(self):
        """Batch query must return mapped candle series for each ticker."""
        def mock_request(endpoint, params):
            return self.fixtures["valid_multi_ticker"]

        provider = AlpacaMarketDataProvider(alpaca_request_fn=mock_request, clock=self.clock)
        multi_bars = provider.fetch_multi_bars(
            tickers=["SPY", "AAPL", "QQQ"],
            interval="1d",
            start=datetime(2026, 11, 16, tzinfo=UTC),
            end=datetime(2026, 11, 16, tzinfo=UTC),
            current_time=self.mock_now,
        )

        self.assertIn("SPY", multi_bars)
        self.assertIn("AAPL", multi_bars)
        self.assertIn("QQQ", multi_bars)

        aapl_bar = multi_bars["AAPL"][0]
        self.assertEqual(aapl_bar.ticker, "AAPL")
        self.assertEqual(aapl_bar.close, Decimal("352.78"))
        self.assertEqual(aapl_bar.volume, 1245000)
        self.assertEqual(aapl_bar.trade_count, 15420)

    # -------------------------------------------------------------------------
    # 3. Latest Bar Retrieval
    # -------------------------------------------------------------------------
    def test_03_latest_bar_retrieval(self):
        """Fetch latest bar must return the single most recent candle."""
        def mock_request(endpoint, params):
            return self.fixtures["valid_sample_spy"]

        provider = AlpacaMarketDataProvider(alpaca_request_fn=mock_request, clock=self.clock)
        latest = provider.fetch_latest_bar("SPY", "1d", current_time=self.mock_now)

        self.assertIsNotNone(latest)
        self.assertEqual(latest.ticker, "SPY")
        self.assertEqual(latest.close, Decimal("739.96"))
        self.assertEqual(to_eastern(latest.timestamp).date(), date(2026, 11, 16))

    # -------------------------------------------------------------------------
    # 4. Anti-Substitution Guard
    # -------------------------------------------------------------------------
    def test_04_anti_volume_substitution_guard(self):
        """AlpacaCandle must forbid treating IEX volume as consolidated tape volume."""
        def mock_request(endpoint, params):
            return self.fixtures["valid_sample_aapl"]

        provider = AlpacaMarketDataProvider(alpaca_request_fn=mock_request, clock=self.clock)
        bars = provider.fetch_historical_bars("AAPL", "1d", datetime(2026, 11, 16, tzinfo=UTC), datetime(2026, 11, 16, tzinfo=UTC))
        candle = bars[0]

        self.assertFalse(candle.is_consolidated_volume)
        with self.assertRaises(InvalidVolumeError) as ctx:
            candle.get_consolidated_volume()
        self.assertIn("unconsolidated IEX volume", str(ctx.exception))

    # -------------------------------------------------------------------------
    # 5 & 6. Empty and Missing Data Responses
    # -------------------------------------------------------------------------
    def test_05_empty_bar_series_raises_no_data_error(self):
        """Empty bars array in response must raise NoDataError."""
        def mock_request(endpoint, params):
            return self.fixtures["empty_bars"]

        provider = AlpacaMarketDataProvider(alpaca_request_fn=mock_request, clock=self.clock)
        with self.assertRaises(NoDataError):
            provider.fetch_historical_bars("EMPTY_SYM", "1d", datetime(2026, 11, 16, tzinfo=UTC), datetime(2026, 11, 16, tzinfo=UTC))

    def test_06_missing_symbol_raises_no_data_error(self):
        """Response lacking ticker key must raise NoDataError."""
        def mock_request(endpoint, params):
            return self.fixtures["no_data_response"]

        provider = AlpacaMarketDataProvider(alpaca_request_fn=mock_request, clock=self.clock)
        with self.assertRaises(NoDataError):
            provider.fetch_historical_bars("MISSING_SYM", "1d", datetime(2026, 11, 16, tzinfo=UTC), datetime(2026, 11, 16, tzinfo=UTC))

    # -------------------------------------------------------------------------
    # 7. Malformed Missing Price Field
    # -------------------------------------------------------------------------
    def test_07_malformed_missing_close_raises_invalid_response(self):
        """Null or missing price field must raise InvalidResponseError."""
        def mock_request(endpoint, params):
            return self.fixtures["malformed_missing_close"]

        provider = AlpacaMarketDataProvider(alpaca_request_fn=mock_request, clock=self.clock)
        with self.assertRaises(InvalidResponseError):
            provider.fetch_historical_bars("BAD_SYM", "1d", datetime(2026, 11, 16, tzinfo=UTC), datetime(2026, 11, 16, tzinfo=UTC))

    # -------------------------------------------------------------------------
    # 8. Inverted Geometry Rejection
    # -------------------------------------------------------------------------
    def test_08_inverted_geometry_raises_invalid_price(self):
        """Inverted high/low prices must fail intrinsic candle validation."""
        def mock_request(endpoint, params):
            return self.fixtures["malformed_inverted_high_low"]

        provider = AlpacaMarketDataProvider(alpaca_request_fn=mock_request, clock=self.clock)
        with self.assertRaises(InvalidPriceError):
            provider.fetch_historical_bars("INV_SYM", "1d", datetime(2026, 11, 16, tzinfo=UTC), datetime(2026, 11, 16, tzinfo=UTC))

    # -------------------------------------------------------------------------
    # 9. Negative Volume Rejection
    # -------------------------------------------------------------------------
    def test_09_negative_volume_raises_invalid_volume(self):
        """Negative volume must fail intrinsic candle validation."""
        def mock_request(endpoint, params):
            return self.fixtures["malformed_negative_volume"]

        provider = AlpacaMarketDataProvider(alpaca_request_fn=mock_request, clock=self.clock)
        with self.assertRaises(InvalidVolumeError):
            provider.fetch_historical_bars("NEG_VOL", "1d", datetime(2026, 11, 16, tzinfo=UTC), datetime(2026, 11, 16, tzinfo=UTC))

    # -------------------------------------------------------------------------
    # 10. Duplicate Timestamp Rejection
    # -------------------------------------------------------------------------
    def test_10_duplicate_timestamps_raises_sequence_validation_error(self):
        """Duplicate timestamps in sequence must be rejected by sequence validation."""
        def mock_request(endpoint, params):
            return self.fixtures["duplicate_timestamps"]

        provider = AlpacaMarketDataProvider(alpaca_request_fn=mock_request, clock=self.clock)
        with self.assertRaises(SequenceValidationError):
            provider.fetch_historical_bars("DUP_SYM", "1d", datetime(2026, 11, 16, tzinfo=UTC), datetime(2026, 11, 16, tzinfo=UTC))

    # -------------------------------------------------------------------------
    # 11. Rate Limit HTTP 429 Mapping
    # -------------------------------------------------------------------------
    def test_11_rate_limit_maps_to_rate_limit_error(self):
        """HTTP 429 response must map directly to RateLimitError."""
        def mock_request(endpoint, params):
            return self.fixtures["rate_limit_error"]

        provider = AlpacaMarketDataProvider(alpaca_request_fn=mock_request, clock=self.clock)
        with self.assertRaises(RateLimitError):
            provider.fetch_historical_bars("SPY", "1d", datetime(2026, 11, 16, tzinfo=UTC), datetime(2026, 11, 16, tzinfo=UTC))

    # -------------------------------------------------------------------------
    # 12. Authentication & Authorization Errors
    # -------------------------------------------------------------------------
    def test_12_authentication_errors_map_to_provider_unavailable(self):
        """HTTP 401 and 403 must map to ProviderUnavailableError."""
        def mock_request_auth(endpoint, params):
            return self.fixtures["auth_error"]

        provider_auth = AlpacaMarketDataProvider(alpaca_request_fn=mock_request_auth, clock=self.clock)
        with self.assertRaises(ProviderUnavailableError):
            provider_auth.fetch_historical_bars("SPY", "1d", datetime(2026, 11, 16, tzinfo=UTC), datetime(2026, 11, 16, tzinfo=UTC))

        def mock_request_forbidden(endpoint, params):
            return self.fixtures["forbidden_error"]

        provider_forbidden = AlpacaMarketDataProvider(alpaca_request_fn=mock_request_forbidden, clock=self.clock)
        with self.assertRaises(ProviderUnavailableError):
            provider_forbidden.fetch_historical_bars("SPY", "1d", datetime(2026, 11, 16, tzinfo=UTC), datetime(2026, 11, 16, tzinfo=UTC))

    # -------------------------------------------------------------------------
    # 13. Transient HTTP 500 Retry Exhaustion
    # -------------------------------------------------------------------------
    def test_13_transient_http_500_retries_and_exhausts(self):
        """Transient HTTP 500 errors must retry up to max_retries and raise ProviderUnavailableError."""
        call_count = 0

        def mock_failing_request(endpoint, params):
            nonlocal call_count
            call_count += 1
            return self.fixtures["server_error"]

        provider = AlpacaMarketDataProvider(
            alpaca_request_fn=mock_failing_request,
            max_retries=2,
            backoff_intervals=(0.01, 0.02),
            clock=self.clock,
        )

        with self.assertRaises(ProviderUnavailableError):
            provider.fetch_historical_bars("SPY", "1d", datetime(2026, 11, 16, tzinfo=UTC), datetime(2026, 11, 16, tzinfo=UTC))

        # Initial attempt + 2 retries = 3 total attempts
        self.assertEqual(call_count, 3)

    # -------------------------------------------------------------------------
    # 14. Transient Recovery After Retry
    # -------------------------------------------------------------------------
    def test_14_transient_recovery_after_retry(self):
        """Initial transient failure recovering on retry must return valid data."""
        call_count = 0

        def mock_recovering_request(endpoint, params):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return self.fixtures["server_error"]
            return self.fixtures["valid_sample_aapl"]

        provider = AlpacaMarketDataProvider(
            alpaca_request_fn=mock_recovering_request,
            max_retries=2,
            backoff_intervals=(0.01, 0.02),
            clock=self.clock,
        )

        bars = provider.fetch_historical_bars("AAPL", "1d", datetime(2026, 11, 16, tzinfo=UTC), datetime(2026, 11, 16, tzinfo=UTC))
        self.assertEqual(call_count, 2)
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].ticker, "AAPL")

    # -------------------------------------------------------------------------
    # 15. Provider Metadata Disclosures
    # -------------------------------------------------------------------------
    def test_15_provider_metadata_disclosures(self):
        """Metadata must clearly identify provider, IEX feed, and unconsolidated volume."""
        provider = AlpacaMarketDataProvider(clock=self.clock)
        meta = provider.get_provider_metadata()

        self.assertEqual(meta["name"], "alpaca")
        self.assertEqual(meta["feed"], "iex")
        self.assertEqual(meta["feed_classification"], "IEX_UNCONSOLIDATED")
        self.assertFalse(meta["is_consolidated_volume"])
        self.assertEqual(meta["native_adjustment_type"], "SPLIT_ADJUSTED")
        self.assertIn("cannot be substituted for consolidated tape volume", meta["disclosure"])

    # -------------------------------------------------------------------------
    # 16. Offline Safety Guard Blocks Unmocked Network
    # -------------------------------------------------------------------------
    def test_16_offline_safety_guard_blocks_unmocked_network(self):
        """Provider must refuse to make live network requests when no mock callable is injected."""
        provider = AlpacaMarketDataProvider(clock=self.clock)  # No callable injected
        with self.assertRaises(ProviderUnavailableError) as ctx:
            provider.fetch_historical_bars("SPY", "1d", datetime(2026, 11, 16, tzinfo=UTC), datetime(2026, 11, 16, tzinfo=UTC))
        self.assertIn("disabled in offline mode", str(ctx.exception))

    # -------------------------------------------------------------------------
    # 17. Volume Isolation Bypass Audit Test
    # -------------------------------------------------------------------------
    def test_17_volume_isolation_bypass_audit(self):
        """Audits the volume isolation boundary.

        Proves:
        1. AlpacaCandle.get_consolidated_volume() raises InvalidVolumeError.
        2. AlpacaCandle.is_consolidated_volume is strictly False.
        3. Legacy calculate_average_volume accesses candle.volume directly, proving
           that raw AlpacaCandle must NEVER be passed directly to the strategy engine
           without the Phase 7.1.4 Dual-Source Aggregator.
        """
        def mock_request(endpoint, params):
            return self.fixtures["valid_sample_aapl"]

        provider = AlpacaMarketDataProvider(alpaca_request_fn=mock_request, clock=self.clock)
        bars = provider.fetch_historical_bars("AAPL", "1d", datetime(2026, 11, 16, tzinfo=UTC), datetime(2026, 11, 16, tzinfo=UTC))
        candle = bars[0]

        # Guard 1: Consolidated volume access raises error
        with self.assertRaises(InvalidVolumeError):
            candle.get_consolidated_volume()

        # Guard 2: Provenance flag is explicit
        self.assertFalse(candle.is_consolidated_volume)

        # Audit Finding: Raw attribute access exposes unconsolidated IEX volume
        self.assertEqual(candle.volume, 1245000)

        # Proves that legacy engine would ingest IEX volume if fed directly
        from indicators import calculate_average_volume
        avg_vol = calculate_average_volume([candle], period=1, validate=False)
        self.assertEqual(avg_vol.value, Decimal("1245000.00"))

    # -------------------------------------------------------------------------
    # 18. Non-Positive Price Rejection
    # -------------------------------------------------------------------------
    def test_18_non_positive_price_rejection(self):
        """Zero or negative prices must raise InvalidPriceError."""
        provider = AlpacaMarketDataProvider(clock=self.clock)
        bar_zero_open = {
            "t": "2026-11-16T05:00:00Z",
            "o": "0.00",
            "h": "105.00",
            "l": "0.00",
            "c": "100.00",
            "v": 1000,
        }
        with self.assertRaises(InvalidPriceError):
            provider.parse_alpaca_bar(bar_zero_open, ticker="ZERO_P", interval="1d", current_time=self.mock_now)

    # -------------------------------------------------------------------------
    # 19. Future Timestamp Rejection
    # -------------------------------------------------------------------------
    def test_19_future_timestamp_rejection(self):
        """Bar with future timestamp relative to current_time must raise TimestampValidationError."""
        from market_data_models import TimestampValidationError
        provider = AlpacaMarketDataProvider(clock=self.clock)
        future_bar = {
            "t": "2026-11-20T05:00:00Z",
            "o": "100.00",
            "h": "105.00",
            "l": "95.00",
            "c": "100.00",
            "v": 1000,
        }
        with self.assertRaises(TimestampValidationError):
            provider.parse_alpaca_bar(future_bar, ticker="FUT_P", interval="1d", current_time=self.mock_now)


if __name__ == "__main__":
    unittest.main()
