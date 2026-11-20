"""Deterministic offline unit test suite for Phase 7.2.2 Alpaca REST connector and rate limiter.

Verifies:
1. Connector Foundation & Initialization (base URL, env credentials, headers, repr).
2. TokenBucketRateLimiter (thread-safety, lazy refill, 180 req/min, blocking wait, timeout).
3. Error Mapping & Non-Retriable Statuses (401, 403, 400, 404, 422 fail immediately).
4. Bounded Retries on Transient Server Errors (500, 502, 503, 504 retry up to max_retries).
5. Transient Error Recovery (retries succeed before exhaustion).
6. 429 Rate Limit Handling (honors Retry-After bounded wait, bounded to 1 retry).
7. Credential Sanitization (secrets never leaked in repr, headers, or exception messages).
8. Offline Mock Harness & Fixtures (successful, empty, malformed, HTML, truncated JSON).
9. Integration with AlpacaMarketDataProvider (connector delegation, volume provenance guard).
10. Strict Offline Safety (prohibits unconfigured live network requests).
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import os
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SWING_DIR = os.path.join(ROOT_DIR, ".agents", "skills", "trader-swing")
for path in (ROOT_DIR, SWING_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

from market_data_models import (
    CandleStatus,
    MarketClock,
    OHLCVCandle,
    ProviderUnavailableError,
    RateLimitError,
    InvalidResponseError,
    NoDataError,
    TimeoutError,
    UTC,
)
from market_data_validation import InvalidVolumeError
from providers.alpaca_connector import (
    AlpacaAuthenticationError,
    AlpacaError,
    AlpacaInvalidRequestError,
    AlpacaInvalidResponseError,
    AlpacaMissingCredentialsError,
    AlpacaNoDataError,
    AlpacaRateLimitError,
    AlpacaResponse,
    AlpacaRestClient,
    AlpacaServiceUnavailableError,
    AlpacaTimeoutError,
    DEFAULT_BURST_CAPACITY,
    DEFAULT_RATE_LIMIT_PER_MINUTE,
    TokenBucketRateLimiter,
    sanitize_headers,
    sanitize_message,
    sanitize_secret,
)
from providers.alpaca_provider import AlpacaCandle, AlpacaMarketDataProvider

FIXTURE_PATH = os.path.join(
    os.path.dirname(__file__),
    "fixtures",
    "trader-swing",
    "alpaca",
    "connector_fixtures.json",
)


class MockClock:
    """Deterministic simulated clock and sleep function for instant, zero-delay testing."""

    def __init__(self, initial_time: float = 1_000_000.0):
        self.current_time = initial_time
        self.sleep_calls = []

    def time(self) -> float:
        return self.current_time

    def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self.current_time += seconds


class TestAlpacaRateLimiter(unittest.TestCase):
    """Test suite for TokenBucketRateLimiter burst safety and deterministic mechanics."""

    def setUp(self):
        self.clock = MockClock(1000.0)

    def test_default_rate_and_conservative_burst_capacity(self):
        """Rate limiter defaults to 180 req/min (3.0 tokens/sec) and conservative burst capacity 5.0."""
        limiter = TokenBucketRateLimiter(
            rate_per_minute=180.0,
            time_fn=self.clock.time,
            sleep_fn=self.clock.sleep,
        )
        self.assertEqual(limiter.rate_per_minute, 180.0)
        self.assertEqual(limiter.refill_rate, 3.0)
        self.assertEqual(limiter.capacity, DEFAULT_BURST_CAPACITY)
        self.assertEqual(limiter.capacity, 5.0)
        self.assertEqual(limiter.get_available_tokens(), 5.0)

    def test_invalid_parameters_raise(self):
        """Non-positive rates or capacities raise ValueError."""
        with self.assertRaises(ValueError):
            TokenBucketRateLimiter(rate_per_minute=0)
        with self.assertRaises(ValueError):
            TokenBucketRateLimiter(rate_per_minute=-10)
        with self.assertRaises(ValueError):
            TokenBucketRateLimiter(capacity=0)
        with self.assertRaises(ValueError):
            TokenBucketRateLimiter(burst_capacity=-5)

    def test_initial_startup_burst_strictly_capped(self):
        """Initial burst at startup allows exactly burst_capacity (5) requests, then blocks."""
        limiter = TokenBucketRateLimiter(
            rate_per_minute=180.0,  # 3.0 tokens/sec
            time_fn=self.clock.time,
            sleep_fn=self.clock.sleep,
        )
        # First 5 calls acquire immediately with zero sleep
        for _ in range(5):
            self.assertTrue(limiter.acquire(1.0, block=False))
        self.assertEqual(self.clock.sleep_calls, [])
        self.assertEqual(limiter.get_available_tokens(), 0.0)

        # 6th call cannot burst immediately
        self.assertFalse(limiter.acquire(1.0, block=False))

        # 6th call with block=True sleeps for 1/3.0s
        self.assertTrue(limiter.acquire(1.0, block=True))
        self.assertEqual(len(self.clock.sleep_calls), 1)
        self.assertAlmostEqual(self.clock.sleep_calls[0], 1.0 / 3.0, places=3)

    def test_empty_bucket_behavior(self):
        """Empty bucket rejects non-blocking requests and times out when timeout is exceeded."""
        limiter = TokenBucketRateLimiter(
            rate_per_minute=180.0,
            capacity=5.0,
            time_fn=self.clock.time,
            sleep_fn=self.clock.sleep,
        )
        # Drain bucket
        self.assertTrue(limiter.acquire(5.0))
        self.assertEqual(limiter.get_available_tokens(), 0.0)

        # Non-blocking acquire returns False
        self.assertFalse(limiter.acquire(1.0, block=False))

        # Blocking acquire with timeout shorter than refill time (1/3s = 0.333s) returns False
        self.assertFalse(limiter.acquire(1.0, block=True, timeout=0.1))

    def test_sustained_request_rate(self):
        """Under sustained demand from empty bucket, requests are paced at exactly 3.0 req/sec."""
        limiter = TokenBucketRateLimiter(
            rate_per_minute=180.0,
            capacity=5.0,
            time_fn=self.clock.time,
            sleep_fn=self.clock.sleep,
        )
        limiter.acquire(5.0)  # Drain initial burst

        # Issue 15 consecutive requests
        for _ in range(15):
            self.assertTrue(limiter.acquire(1.0, block=True))

        # 15 requests at 3 req/sec require exactly 5.0 seconds total sleep
        total_sleep = sum(self.clock.sleep_calls)
        self.assertAlmostEqual(total_sleep, 5.0, places=3)
        self.assertEqual(len(self.clock.sleep_calls), 15)

    def test_rolling_60_second_request_count(self):
        """In any 60-second window, total requests cannot exceed burst_capacity + (60 * refill_rate)."""
        limiter = TokenBucketRateLimiter(
            rate_per_minute=180.0,  # 3.0 tokens/sec
            capacity=5.0,
            time_fn=self.clock.time,
            sleep_fn=self.clock.sleep,
        )
        granted_requests = 0

        # Step through 60 seconds second by second
        # At t=0, take whatever is available
        while limiter.acquire(1.0, block=False):
            granted_requests += 1

        # Now simulate 60 elapsed seconds (1s steps)
        for _ in range(60):
            self.clock.current_time += 1.0  # Advance 1 second
            while limiter.acquire(1.0, block=False):
                granted_requests += 1

        # Max requests in 60s window starting from full bucket is 5 + 60*3 = 185 requests
        self.assertEqual(granted_requests, 185)
        self.assertLessEqual(granted_requests, 185)

    def test_clock_advancement(self):
        """Advancing clock refills tokens up to capacity and does not exceed capacity."""
        limiter = TokenBucketRateLimiter(
            rate_per_minute=180.0,
            capacity=5.0,
            time_fn=self.clock.time,
            sleep_fn=self.clock.sleep,
        )
        limiter.acquire(5.0)  # 0 tokens
        self.assertEqual(limiter.get_available_tokens(), 0.0)

        # Advance 1.0s -> refills 3.0 tokens
        self.clock.current_time += 1.0
        self.assertAlmostEqual(limiter.get_available_tokens(), 3.0, places=2)

        # Advance 100s -> tokens cap at 5.0
        self.clock.current_time += 100.0
        self.assertEqual(limiter.get_available_tokens(), 5.0)

    def test_clock_moving_backward_protection(self):
        """Clock moving backward does not freeze rate limiter or corrupt token balance."""
        limiter = TokenBucketRateLimiter(
            rate_per_minute=180.0,
            capacity=5.0,
            time_fn=self.clock.time,
            sleep_fn=self.clock.sleep,
        )
        limiter.acquire(5.0)  # Empty at t = 1000.0

        # Clock jumps backward by 100 seconds to t = 900.0 (e.g. NTP sync)
        self.clock.current_time = 900.0
        self.assertEqual(limiter.get_available_tokens(), 0.0)

        # Rate limiter baseline reset to 900.0; advancing to 901.0 refills 3.0 tokens
        self.clock.current_time = 901.0
        self.assertAlmostEqual(limiter.get_available_tokens(), 3.0, places=2)
        self.assertTrue(limiter.acquire(1.0, block=False))

    def test_sleep_outside_the_lock(self):
        """Verifies that the rate limiter lock is released before sleep_fn is invoked."""
        lock_held_during_sleep = []

        def checking_sleep(seconds):
            # Check if rate limiter lock is held by caller
            lock_held = limiter._lock.locked()
            lock_held_during_sleep.append(lock_held)
            self.clock.sleep(seconds)

        limiter = TokenBucketRateLimiter(
            rate_per_minute=180.0,
            capacity=5.0,
            time_fn=self.clock.time,
            sleep_fn=checking_sleep,
        )
        limiter.acquire(5.0)  # Empty

        # Block waiting for 1 token
        limiter.acquire(1.0, block=True)
        self.assertEqual(lock_held_during_sleep, [False])

    def test_no_background_threads(self):
        """Verifies that no background threads or processes are created by the rate limiter."""
        threads_before = threading.active_count()
        limiter = TokenBucketRateLimiter(
            rate_per_minute=180.0,
            capacity=5.0,
            time_fn=self.clock.time,
            sleep_fn=self.clock.sleep,
        )
        limiter.acquire(1.0)
        self.clock.current_time += 10.0
        limiter.acquire(1.0)
        threads_after = threading.active_count()
        self.assertEqual(threads_before, threads_after)

    def test_thread_safety_and_concurrent_callers(self):
        """Multiple concurrent callers cannot exceed the rate limit or corrupt token balance."""
        real_limiter = TokenBucketRateLimiter(
            rate_per_minute=60000.0,  # High rate to avoid sleep delays in thread test
            capacity=100.0,
        )
        errors = []

        def worker():
            try:
                for _ in range(10):
                    success = real_limiter.acquire(1.0, block=True, timeout=1.0)
                    if not success:
                        errors.append("Acquisition timed out")
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertAlmostEqual(real_limiter.get_available_tokens(), 0.0, delta=5.0)


class TestAlpacaSecurityAndSanitization(unittest.TestCase):
    """Test suite for credential protection and security invariants."""

    def test_sanitize_secret_masking(self):
        """Secrets are properly masked."""
        self.assertEqual(sanitize_secret(None), "<NONE>")
        self.assertEqual(sanitize_secret(""), "<NONE>")
        self.assertEqual(sanitize_secret("abc"), "***REDACTED***")
        self.assertEqual(sanitize_secret("PKTEST12345678"), "PKTE...***REDACTED***")

    def test_sanitize_headers(self):
        """Headers containing sensitive names are redacted."""
        headers = {
            "Content-Type": "application/json",
            "APCA-API-KEY-ID": "PKTEST12345678",
            "APCA-API-SECRET-KEY": "SECRET_KEY_ABCDEF_XYZ",
            "Authorization": "Bearer secret_token",
        }
        sanitized = sanitize_headers(headers)
        self.assertEqual(sanitized["Content-Type"], "application/json")
        self.assertNotIn("PKTEST12345678", sanitized["APCA-API-KEY-ID"])
        self.assertNotIn("SECRET_KEY_ABCDEF_XYZ", sanitized["APCA-API-SECRET-KEY"])
        self.assertIn("***REDACTED***", sanitized["APCA-API-SECRET-KEY"])
        self.assertIn("***REDACTED***", sanitized["Authorization"])

    def test_sanitize_message_strips_known_secrets(self):
        """Exception messages never leak known secrets."""
        key = "MY_SUPER_SECRET_KEY_123"
        secret = "ANOTHER_SECRET_VALUE_456"
        msg = f"Failed to connect using key {key} and secret {secret} at endpoint"
        clean = sanitize_message(msg, [key, secret])
        self.assertNotIn(key, clean)
        self.assertNotIn(secret, clean)
        self.assertIn("***REDACTED***", clean)

    def test_client_repr_does_not_leak_secret(self):
        """AlpacaRestClient repr shows masked key and zero secret."""
        client = AlpacaRestClient(
            api_key="PKTEST12345678",
            api_secret="SECRET_VAL_DO_NOT_LEAK",
            base_url="https://data.alpaca.markets",
        )
        repr_str = repr(client)
        self.assertNotIn("SECRET_VAL_DO_NOT_LEAK", repr_str)
        self.assertIn("PKTE...***REDACTED***", repr_str)


class TestAlpacaRestClient(unittest.TestCase):
    """Test suite for AlpacaRestClient request lifecycle, retries, and error mapping."""

    @classmethod
    def setUpClass(cls):
        with open(FIXTURE_PATH, "r", encoding="utf-8") as f:
            cls.fixtures = json.load(f)

    def setUp(self):
        self.clock = MockClock(1000.0)
        self.rate_limiter = TokenBucketRateLimiter(
            rate_per_minute=180.0,
            time_fn=self.clock.time,
            sleep_fn=self.clock.sleep,
        )

    def _create_client(self, transport_fn, max_retries=3):
        return AlpacaRestClient(
            api_key="TEST_KEY_ID",
            api_secret="TEST_SECRET_KEY",
            base_url="https://data.alpaca.markets",
            rate_limiter=self.rate_limiter,
            max_retries=max_retries,
            transport_fn=transport_fn,
            allow_network=False,
            time_fn=self.clock.time,
            sleep_fn=self.clock.sleep,
        )

    def test_environment_variable_loading(self):
        """Loads credentials and base URL from environment variables if not passed explicitly."""
        with patch.dict(
            os.environ,
            {
                "ALPACA_API_KEY_ID": "ENV_KEY_12345",
                "ALPACA_API_SECRET_KEY": "ENV_SECRET_67890",
                "ALPACA_API_BASE_URL": "https://custom.data.alpaca.markets/",
            },
        ):
            client = AlpacaRestClient()
            self.assertEqual(client.api_key, "ENV_KEY_12345")
            self.assertEqual(client.api_secret, "ENV_SECRET_67890")
            self.assertEqual(client.base_url, "https://custom.data.alpaca.markets")

    def test_offline_safety_guard_blocks_unconfigured_calls(self):
        """Attempting a request without transport_fn in offline mode raises AlpacaServiceUnavailableError."""
        client = AlpacaRestClient(allow_network=False, transport_fn=None)
        with self.assertRaises(AlpacaServiceUnavailableError) as ctx:
            client.get("/v2/stocks/bars", params={"symbols": "SPY"})
        self.assertIn("Live network execution is disabled in offline mode", str(ctx.exception))

    def test_successful_bars_response(self):
        """200 OK returns parsed JSON payload correctly."""
        fixture = self.fixtures["successful_bars_spy"]

        def mock_transport(**kwargs):
            return AlpacaResponse(
                status_code=fixture["status_code"],
                headers=fixture["headers"],
                body=fixture["body"],
            )

        client = self._create_client(mock_transport)
        data = client.get_bars("SPY", timeframe="1Day")
        self.assertIn("bars", data)
        self.assertIn("SPY", data["bars"])
        self.assertEqual(len(data["bars"]["SPY"]), 3)
        self.assertEqual(data["bars"]["SPY"][0]["c"], 754.20)

    def test_successful_multi_bars_response(self):
        """Batch multi-symbol request correctly formats symbols and parses response."""
        fixture = self.fixtures["successful_multi_bars"]
        captured_url = []

        def mock_transport(url, **kwargs):
            captured_url.append(url)
            return AlpacaResponse(
                status_code=fixture["status_code"],
                headers=fixture["headers"],
                body=fixture["body"],
            )

        client = self._create_client(mock_transport)
        data = client.get_bars(["SPY", "AAPL", "QQQ"], timeframe="1Day")
        self.assertIn("bars", data)
        self.assertEqual(set(data["bars"].keys()), {"SPY", "AAPL", "QQQ"})
        self.assertIn("symbols=SPY%2CAAPL%2CQQQ", captured_url[0])

    def test_empty_response_handling(self):
        """Empty bar dictionary returns empty dict without throwing errors."""
        fixture = self.fixtures["empty_bars_dict"]

        def mock_transport(**kwargs):
            return AlpacaResponse(
                status_code=fixture["status_code"],
                headers=fixture["headers"],
                body=fixture["body"],
            )

        client = self._create_client(mock_transport)
        data = client.get_bars("EMPTY_SYM")
        self.assertEqual(data.get("bars"), {})

    def test_empty_string_body_handling(self):
        """Empty string response body produces empty parsed_json dict."""
        fixture = self.fixtures["empty_string_body"]

        def mock_transport(**kwargs):
            return AlpacaResponse(
                status_code=200,
                headers=fixture["headers"],
                body=fixture["body"],
            )

        client = self._create_client(mock_transport)
        res = client.get("/v2/stocks/bars")
        self.assertEqual(res.parsed_json, {})

    def test_http_401_unauthorized_fails_immediately_no_retry(self):
        """HTTP 401 raises AlpacaAuthenticationError immediately without retrying."""
        fixture = self.fixtures["http_401_unauthorized"]
        call_count = [0]

        def mock_transport(**kwargs):
            call_count[0] += 1
            return AlpacaResponse(status_code=401, headers=fixture["headers"], body=fixture["body"])

        client = self._create_client(mock_transport, max_retries=3)
        with self.assertRaises(AlpacaAuthenticationError) as ctx:
            client.get_bars("SPY")

        self.assertEqual(call_count[0], 1, "401 must not trigger retry attempts")
        self.assertEqual(ctx.exception.status_code, 401)
        # Verify it inherits from ProviderUnavailableError for compatibility
        self.assertIsInstance(ctx.exception, ProviderUnavailableError)

    def test_http_403_forbidden_fails_immediately_no_retry(self):
        """HTTP 403 raises AlpacaAuthenticationError immediately without retrying."""
        fixture = self.fixtures["http_403_forbidden"]
        call_count = [0]

        def mock_transport(**kwargs):
            call_count[0] += 1
            return AlpacaResponse(status_code=403, headers=fixture["headers"], body=fixture["body"])

        client = self._create_client(mock_transport, max_retries=3)
        with self.assertRaises(AlpacaAuthenticationError) as ctx:
            client.get_bars("SPY")

        self.assertEqual(call_count[0], 1, "403 must not trigger retry attempts")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_http_400_and_422_fail_immediately_no_retry(self):
        """HTTP 400 and 422 raise AlpacaInvalidRequestError immediately without retrying."""
        fixture_400 = self.fixtures["http_400_bad_request"]
        call_count_400 = [0]

        def mock_transport_400(**kwargs):
            call_count_400[0] += 1
            return AlpacaResponse(status_code=400, headers=fixture_400["headers"], body=fixture_400["body"])

        client = self._create_client(mock_transport_400, max_retries=3)
        with self.assertRaises(AlpacaInvalidRequestError):
            client.get_bars("SPY")
        self.assertEqual(call_count_400[0], 1)

        fixture_422 = self.fixtures["http_422_unprocessable"]
        call_count_422 = [0]

        def mock_transport_422(**kwargs):
            call_count_422[0] += 1
            return AlpacaResponse(status_code=422, headers=fixture_422["headers"], body=fixture_422["body"])

        client2 = self._create_client(mock_transport_422, max_retries=3)
        with self.assertRaises(AlpacaInvalidRequestError):
            client2.get_bars("SPY")
        self.assertEqual(call_count_422[0], 1)

    def test_http_404_not_found_fails_immediately_no_retry(self):
        """HTTP 404 raises AlpacaNoDataError immediately without retrying."""
        fixture = self.fixtures["http_404_not_found"]
        call_count = [0]

        def mock_transport(**kwargs):
            call_count[0] += 1
            return AlpacaResponse(status_code=404, headers=fixture["headers"], body=fixture["body"])

        client = self._create_client(mock_transport, max_retries=3)
        with self.assertRaises(AlpacaNoDataError) as ctx:
            client.get_bars("UNKNOWN_TICKER")

        self.assertEqual(call_count[0], 1)
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertIsInstance(ctx.exception, NoDataError)

    def test_bounded_retries_on_500_server_error_exhaustion(self):
        """HTTP 500 retries exactly max_retries times, then raises AlpacaServiceUnavailableError."""
        fixture = self.fixtures["http_500_internal_error"]
        call_count = [0]

        def mock_transport(**kwargs):
            call_count[0] += 1
            return AlpacaResponse(status_code=500, headers=fixture["headers"], body=fixture["body"])

        client = self._create_client(mock_transport, max_retries=3)
        with self.assertRaises(AlpacaServiceUnavailableError) as ctx:
            client.get_bars("SPY")

        # 1 initial + 3 retries = 4 attempts total
        self.assertEqual(call_count[0], 4)
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertEqual(len(self.clock.sleep_calls), 3)
        # Sleep calls follow exponential backoff factor: 0.5 * (2**0)=0.5, 0.5 * (2**1)=1.0, 0.5 * (2**2)=2.0
        self.assertEqual(self.clock.sleep_calls, [0.5, 1.0, 2.0])

    def test_bounded_retries_on_502_and_503_errors(self):
        """HTTP 502 and 503 trigger bounded retries."""
        for code, fixture_key in [(502, "http_502_bad_gateway"), (503, "http_503_service_unavailable")]:
            fixture = self.fixtures[fixture_key]
            call_count = [0]

            def mock_transport(**kwargs):
                call_count[0] += 1
                return AlpacaResponse(status_code=code, headers=fixture["headers"], body=fixture["body"])

            client = self._create_client(mock_transport, max_retries=2)
            with self.assertRaises(AlpacaServiceUnavailableError):
                client.get_bars("SPY")
            self.assertEqual(call_count[0], 3)  # 1 initial + 2 retries

    def test_transient_error_recovery_succeeds(self):
        """Transient 500 errors recover on retry without failing overall request."""
        fixture_500 = self.fixtures["http_500_internal_error"]
        fixture_ok = self.fixtures["successful_bars_spy"]
        call_count = [0]

        def mock_transport(**kwargs):
            call_count[0] += 1
            if call_count[0] <= 2:
                # First 2 calls fail
                return AlpacaResponse(status_code=500, headers=fixture_500["headers"], body=fixture_500["body"])
            # 3rd call succeeds
            return AlpacaResponse(status_code=200, headers=fixture_ok["headers"], body=fixture_ok["body"])

        client = self._create_client(mock_transport, max_retries=3)
        data = client.get_bars("SPY")
        self.assertEqual(call_count[0], 3)
        self.assertIn("bars", data)
        self.assertEqual(len(self.clock.sleep_calls), 2)

    def test_http_429_with_retry_after_retries_once(self):
        """HTTP 429 with valid Retry-After sleeps for Retry-After duration and retries once."""
        fixture_429 = self.fixtures["http_429_with_retry_after"]
        fixture_ok = self.fixtures["successful_bars_spy"]
        call_count = [0]

        def mock_transport(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return AlpacaResponse(status_code=429, headers=fixture_429["headers"], body=fixture_429["body"])
            return AlpacaResponse(status_code=200, headers=fixture_ok["headers"], body=fixture_ok["body"])

        client = self._create_client(mock_transport, max_retries=3)
        data = client.get_bars("SPY")
        self.assertEqual(call_count[0], 2)
        # Sleep call should match Retry-After: 2.0s
        self.assertIn(2.0, self.clock.sleep_calls)
        self.assertIn("bars", data)

    def test_http_429_without_retry_after_raises_immediately(self):
        """HTTP 429 without Retry-After header raises AlpacaRateLimitError immediately without retrying."""
        fixture = self.fixtures["http_429_no_retry_after"]
        call_count = [0]

        def mock_transport(**kwargs):
            call_count[0] += 1
            return AlpacaResponse(status_code=429, headers=fixture["headers"], body=fixture["body"])

        client = self._create_client(mock_transport, max_retries=3)
        with self.assertRaises(AlpacaRateLimitError) as ctx:
            client.get_bars("SPY")

        self.assertEqual(call_count[0], 1)
        self.assertIsInstance(ctx.exception, RateLimitError)

    def test_malformed_json_raises_invalid_response_error(self):
        """Malformed JSON (HTML or truncated) on 200 OK raises AlpacaInvalidResponseError."""
        fixture = self.fixtures["invalid_json_truncated"]

        def mock_transport(**kwargs):
            return AlpacaResponse(status_code=200, headers=fixture["headers"], body=fixture["body"])

        client = self._create_client(mock_transport)
        with self.assertRaises(AlpacaInvalidResponseError) as ctx:
            client.get_bars("SPY")
        self.assertIsInstance(ctx.exception, InvalidResponseError)

    def test_transport_timeout_retries_and_exhausts_to_alpaca_timeout_error(self):
        """Socket / client timeouts retry up to max_retries and raise AlpacaTimeoutError."""
        call_count = [0]

        def mock_transport(**kwargs):
            call_count[0] += 1
            raise TimeoutError("Socket receive timed out")

        client = self._create_client(mock_transport, max_retries=2)
        with self.assertRaises(AlpacaTimeoutError):
            client.get_bars("SPY")

        self.assertEqual(call_count[0], 3)  # 1 initial + 2 retries


class TestAlpacaProviderConnectorIntegration(unittest.TestCase):
    """Test suite verifying integration of AlpacaRestClient with AlpacaMarketDataProvider."""

    @classmethod
    def setUpClass(cls):
        with open(FIXTURE_PATH, "r", encoding="utf-8") as f:
            cls.fixtures = json.load(f)

    def setUp(self):
        self.clock = MarketClock()
        self.sim_clock = MockClock(1000.0)
        self.mock_now = datetime(2026, 11, 16, 21, 5, 0, tzinfo=UTC)

    def test_provider_with_connector_fetches_and_normalizes_candles(self):
        """AlpacaMarketDataProvider using AlpacaRestClient successfully normalizes candles."""
        fixture = self.fixtures["successful_bars_spy"]

        def mock_transport(**kwargs):
            return AlpacaResponse(status_code=200, headers=fixture["headers"], body=fixture["body"])

        connector = AlpacaRestClient(
            api_key="TEST_KEY",
            api_secret="TEST_SECRET",
            transport_fn=mock_transport,
            time_fn=self.sim_clock.time,
            sleep_fn=self.sim_clock.sleep,
        )

        provider = AlpacaMarketDataProvider(
            connector=connector,
            clock=self.clock,
        )

        candles = provider.fetch_historical_bars(
            ticker="SPY",
            interval="1d",
            start=datetime(2026, 11, 10, tzinfo=UTC),
            end=datetime(2026, 11, 17, tzinfo=UTC),
            current_time=self.mock_now,
        )

        self.assertEqual(len(candles), 3)
        self.assertIsInstance(candles[0], AlpacaCandle)
        self.assertEqual(candles[0].ticker, "SPY")
        self.assertEqual(candles[0].open, Decimal("752.10"))
        self.assertEqual(candles[0].feed_classification, "IEX")
        self.assertFalse(candles[0].is_consolidated_volume)

        # Anti-substitution check
        with self.assertRaises(InvalidVolumeError):
            candles[0].get_consolidated_volume()

    def test_provider_with_connector_handles_missing_fields(self):
        """Missing candle fields raise InvalidResponseError through connector."""
        fixture = self.fixtures["missing_close_field"]

        def mock_transport(**kwargs):
            return AlpacaResponse(status_code=200, headers=fixture["headers"], body=fixture["body"])

        connector = AlpacaRestClient(
            api_key="TEST_KEY",
            api_secret="TEST_SECRET",
            transport_fn=mock_transport,
            time_fn=self.sim_clock.time,
            sleep_fn=self.sim_clock.sleep,
        )

        provider = AlpacaMarketDataProvider(
            connector=connector,
            clock=self.clock,
        )

        with self.assertRaises(InvalidResponseError) as ctx:
            provider.fetch_historical_bars(
                ticker="BAD_SYM",
                interval="1d",
                start=datetime(2026, 11, 10, tzinfo=UTC),
                end=datetime(2026, 11, 17, tzinfo=UTC),
                current_time=self.mock_now,
            )
        self.assertIn("missing mandatory field 'c'", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
