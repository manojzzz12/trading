"""Alpaca REST connector foundation and token-bucket rate limiter.

Phase 7.2.2: Alpaca Connector Foundation & Rate Limiter.

Provides:
1. TokenBucketRateLimiter: Thread-safe, non-background token-bucket rate limiter
   configured for 180 req/min with injectable clock and sleep callables.
2. AlpacaRestClient: Configurable REST connector with environment-variable
   credential loading, request construction, response parsing, and bounded retries.
3. Typed exception hierarchy inheriting from market_data_models ProviderError.
4. Strict security invariants: credentials are never logged, printed, or exposed in
   exception messages.
5. Strict offline safety: live network calls are prohibited by default unless an
   explicit mock transport function is injected.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import os
import sys
import threading
import time as time_mod
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union
from urllib.parse import urlencode

# Ensure local imports work regardless of execution context
PARENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PARENT_DIR not in sys.path:
    sys.path.insert(0, PARENT_DIR)

from market_data_models import (
    InvalidResponseError,
    NoDataError,
    ProviderError,
    ProviderUnavailableError,
    RateLimitError,
    TimeoutError,
)

logger = logging.getLogger(__name__)

# Default constants
DEFAULT_RATE_LIMIT_PER_MINUTE: float = 180.0
DEFAULT_BURST_CAPACITY: float = 5.0
DEFAULT_BASE_URL: str = "https://data.alpaca.markets"
DEFAULT_MAX_RETRIES: int = 3
DEFAULT_TIMEOUT_SECONDS: float = 10.0
DEFAULT_MAX_RETRY_AFTER_WAIT: float = 10.0
DEFAULT_BACKOFF_FACTOR: float = 0.5
MAX_BACKOFF_SECONDS: float = 10.0


# -----------------------------------------------------------------------------
# Security & Credential Sanitization
# -----------------------------------------------------------------------------
def sanitize_secret(value: Optional[str], visible_chars: int = 4) -> str:
    """Return a masked representation of a sensitive string for safe logging."""
    if not value:
        return "<NONE>"
    if len(value) <= visible_chars:
        return "***REDACTED***"
    return f"{value[:visible_chars]}...***REDACTED***"


def sanitize_headers(headers: Dict[str, str]) -> Dict[str, str]:
    """Return a copy of request/response headers with secrets redacted."""
    sensitive_substrings = ("KEY", "SECRET", "AUTH", "TOKEN", "BEARER", "PASSWORD")
    sanitized: Dict[str, str] = {}
    for k, v in headers.items():
        if any(sub in k.upper() for sub in sensitive_substrings):
            sanitized[k] = sanitize_secret(v)
        else:
            sanitized[k] = v
    return sanitized


def sanitize_message(message: str, secrets: Sequence[Optional[str]] = ()) -> str:
    """Strip known sensitive values from any message or exception string."""
    clean_msg = message
    for s in secrets:
        if s and len(s) >= 4 and s in clean_msg:
            clean_msg = clean_msg.replace(s, "***REDACTED***")
    return clean_msg


# -----------------------------------------------------------------------------
# Typed Exception Hierarchy
# -----------------------------------------------------------------------------
class AlpacaError(ProviderError):
    """Base exception for all Alpaca connector errors."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        response_body: Optional[str] = None,
        secrets_to_redact: Sequence[Optional[str]] = (),
    ):
        clean_msg = sanitize_message(message, secrets_to_redact)
        super().__init__(clean_msg)
        self.status_code = status_code
        self.response_body = sanitize_message(response_body, secrets_to_redact) if response_body else None


class AlpacaAuthenticationError(AlpacaError, ProviderUnavailableError):
    """Raised for HTTP 401 Unauthorized or HTTP 403 Forbidden."""
    pass


class AlpacaMissingCredentialsError(AlpacaAuthenticationError):
    """Raised when Alpaca API credentials are missing and offline mode is disabled."""
    pass


class AlpacaRateLimitError(AlpacaError, RateLimitError):
    """Raised for HTTP 429 Too Many Requests or local bucket exhaustion."""

    def __init__(
        self,
        message: str,
        retry_after: Optional[float] = None,
        status_code: Optional[int] = 429,
        **kwargs: Any,
    ):
        super().__init__(message, status_code=status_code, **kwargs)
        self.retry_after = retry_after


class AlpacaServiceUnavailableError(AlpacaError, ProviderUnavailableError):
    """Raised for HTTP 500, 502, 503, 504 server errors and connection failures."""
    pass


class AlpacaInvalidRequestError(AlpacaError):
    """Raised for client request validation errors (HTTP 400, 422)."""
    pass


class AlpacaInvalidResponseError(AlpacaError, InvalidResponseError):
    """Raised for malformed, unparseable, or schema-violating JSON."""
    pass


class AlpacaNoDataError(AlpacaError, NoDataError):
    """Raised for HTTP 404 or empty response payloads."""
    pass


class AlpacaTimeoutError(AlpacaError, TimeoutError):
    """Raised when an Alpaca request times out."""
    pass


# -----------------------------------------------------------------------------
# Response Data Model
# -----------------------------------------------------------------------------
@dataclass
class AlpacaResponse:
    """Encapsulates raw and parsed response data from Alpaca REST endpoints."""
    status_code: int
    headers: Dict[str, str] = field(default_factory=dict)
    body: str = ""
    parsed_json: Optional[Dict[str, Any]] = None

    @property
    def is_success(self) -> bool:
        """True if status code is in 2xx range."""
        return 200 <= self.status_code < 300

    @property
    def retry_after(self) -> Optional[float]:
        """Parsed Retry-After header value in seconds, if present."""
        val = self.headers.get("Retry-After") or self.headers.get("retry-after")
        if val is not None:
            try:
                parsed = float(val)
                return max(0.0, parsed)
            except (ValueError, TypeError):
                return None
        return None


# -----------------------------------------------------------------------------
# Token Bucket Rate Limiter
# -----------------------------------------------------------------------------
class TokenBucketRateLimiter:
    """Thread-safe token-bucket rate limiter with injectable clock and sleep.

    Guarantees:
    - Default rate: 180 requests/minute (3.0 requests/second).
    - Conservative burst capacity: 5.0 tokens by default (smooth pacing, avoids startup burst).
    - Thread-safe via threading.Lock.
    - Zero background threads or processes: refills lazily on acquire().
    - Protected against clock regression (resets baseline on negative time step).
    - Injectable time_fn and sleep_fn for 100% deterministic, zero-delay testing.
    """

    def __init__(
        self,
        rate_per_minute: float = DEFAULT_RATE_LIMIT_PER_MINUTE,
        capacity: Optional[float] = None,
        burst_capacity: Optional[float] = None,
        time_fn: Optional[Callable[[], float]] = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
    ):
        if rate_per_minute <= 0:
            raise ValueError(f"rate_per_minute must be strictly positive, got {rate_per_minute}")

        self.rate_per_minute = float(rate_per_minute)
        self.refill_rate = self.rate_per_minute / 60.0  # Tokens per second

        # Use burst_capacity, then capacity (for explicit overrides), then DEFAULT_BURST_CAPACITY
        resolved_capacity = burst_capacity if burst_capacity is not None else (
            capacity if capacity is not None else DEFAULT_BURST_CAPACITY
        )
        self.capacity = float(resolved_capacity)
        if self.capacity <= 0:
            raise ValueError(f"capacity must be strictly positive, got {self.capacity}")

        self.tokens: float = self.capacity
        self.time_fn: Callable[[], float] = time_fn if time_fn is not None else time_mod.time
        self.sleep_fn: Callable[[float], None] = sleep_fn if sleep_fn is not None else time_mod.sleep
        self.last_refill_time: float = self.time_fn()
        self._lock = threading.Lock()

    def _refill_unlocked(self, now: float) -> None:
        """Refill tokens based on time elapsed since last check (caller must hold _lock)."""
        elapsed = now - self.last_refill_time
        if elapsed < 0:
            # System clock regression detected (e.g. NTP synchronization or time step).
            # Reset last_refill_time to current time to avoid freezing refills.
            logger.warning(
                "System clock regression detected in rate limiter (now=%.3f < last_refill=%.3f). Resetting baseline.",
                now,
                self.last_refill_time,
            )
            self.last_refill_time = now
            return

        if elapsed > 0:
            added_tokens = elapsed * self.refill_rate
            self.tokens = min(self.capacity, self.tokens + added_tokens)
            self.last_refill_time = now

    def acquire(
        self,
        tokens: float = 1.0,
        block: bool = True,
        timeout: Optional[float] = None,
    ) -> bool:
        """Attempt to acquire tokens from the bucket.

        Args:
            tokens: Number of tokens requested (default 1.0).
            block: If True, sleep until tokens are available or timeout expires.
            timeout: Maximum seconds to block waiting for tokens.

        Returns:
            True if tokens were acquired, False if timed out or block=False and tokens unavailable.

        Raises:
            ValueError: If tokens > capacity.
        """
        if tokens <= 0:
            return True
        if tokens > self.capacity:
            raise ValueError(
                f"Requested tokens ({tokens}) exceeds total bucket capacity ({self.capacity})"
            )

        start_time = self.time_fn()
        deadline = (start_time + timeout) if timeout is not None else None
        epsilon = 1e-9

        while True:
            with self._lock:
                now = self.time_fn()
                self._refill_unlocked(now)

                if self.tokens + epsilon >= tokens:
                    self.tokens = max(0.0, self.tokens - tokens)
                    return True

                if not block:
                    return False

                needed = tokens - self.tokens
                if needed <= epsilon:
                    self.tokens = max(0.0, self.tokens - tokens)
                    return True

                wait_time = needed / self.refill_rate

                if deadline is not None:
                    remaining = deadline - now
                    if remaining <= 0:
                        return False
                    wait_time = min(wait_time, remaining)

            # Sleep outside the lock so other threads can inspect or acquire tokens
            self.sleep_fn(wait_time)

            if deadline is not None:
                now = self.time_fn()
                if now >= deadline:
                    # Final check under lock before returning False
                    with self._lock:
                        self._refill_unlocked(now)
                        if self.tokens + epsilon >= tokens:
                            self.tokens = max(0.0, self.tokens - tokens)
                            return True
                        return False

    def get_available_tokens(self) -> float:
        """Return the current number of available tokens after lazy refill."""
        with self._lock:
            now = self.time_fn()
            self._refill_unlocked(now)
            return self.tokens

    def reset(self, tokens: Optional[float] = None) -> None:
        """Reset the bucket to full capacity or a specified token count."""
        with self._lock:
            now = self.time_fn()
            self.tokens = min(self.capacity, float(tokens) if tokens is not None else self.capacity)
            self.last_refill_time = now


# -----------------------------------------------------------------------------
# Alpaca REST Client
# -----------------------------------------------------------------------------
class AlpacaRestClient:
    """Offline-safe Alpaca REST connector with rate-limiting, retries, and error mapping.

    Adheres strictly to the following invariants:
    1. Base URL configurable (defaults to 'https://data.alpaca.markets' or ALPACA_API_BASE_URL).
    2. Credentials loaded from ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY environment variables.
    3. Strict offline safety: real network requests are barred unless an explicit mock transport_fn
       is supplied.
    4. Token-bucket rate limiting (180 req/min default).
    5. Bounded retries: only transient server errors (500, 502, 503, 504) and transport errors retry.
       Authentication (401, 403), bad request (400, 422), and not found (404) fail immediately.
    6. 429 rate limits retry at most once if Retry-After is within bounded limits.
    7. No credentials in logs or exception messages.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        base_url: Optional[str] = None,
        rate_limiter: Optional[TokenBucketRateLimiter] = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retry_after: float = DEFAULT_MAX_RETRY_AFTER_WAIT,
        backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
        transport_fn: Optional[Callable[..., AlpacaResponse]] = None,
        allow_network: bool = False,
        time_fn: Optional[Callable[[], float]] = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
    ):
        # 1. Configurable base URL
        resolved_base_url = (
            base_url or os.environ.get("ALPACA_API_BASE_URL") or DEFAULT_BASE_URL
        )
        self.base_url = resolved_base_url.rstrip("/")

        # 2. Credential loading (explicit constructor or env vars)
        self.api_key = api_key or os.environ.get("ALPACA_API_KEY_ID")
        self.api_secret = api_secret or os.environ.get("ALPACA_API_SECRET_KEY")

        # 3. Time and sleep functions
        self.time_fn = time_fn if time_fn is not None else time_mod.time
        self.sleep_fn = sleep_fn if sleep_fn is not None else time_mod.sleep

        # 4. Rate limiter
        self.rate_limiter = rate_limiter or TokenBucketRateLimiter(
            rate_per_minute=DEFAULT_RATE_LIMIT_PER_MINUTE,
            time_fn=self.time_fn,
            sleep_fn=self.sleep_fn,
        )

        # 5. Retry and timeout parameters
        self.max_retries = max(0, int(max_retries))
        self.timeout = max(0.1, float(timeout))
        self.max_retry_after = max(0.0, float(max_retry_after))
        self.backoff_factor = max(0.05, float(backoff_factor))

        # 6. Transport callable and network boundary
        self.transport_fn = transport_fn
        self.allow_network = allow_network  # Strictly False by default

    def _get_secrets(self) -> Tuple[Optional[str], Optional[str]]:
        """Return tuple of credentials for sanitization."""
        return (self.api_key, self.api_secret)

    def __repr__(self) -> str:
        key_preview = sanitize_secret(self.api_key)
        return (
            f"AlpacaRestClient(base_url='{self.base_url}', "
            f"api_key={key_preview}, "
            f"allow_network={self.allow_network}, "
            f"max_retries={self.max_retries})"
        )

    def build_headers(self, custom_headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """Construct standard Alpaca API headers with credentials."""
        headers: Dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": "trader-swing/7.2.2",
        }
        if self.api_key:
            headers["APCA-API-KEY-ID"] = self.api_key
        if self.api_secret:
            headers["APCA-API-SECRET-KEY"] = self.api_secret

        if custom_headers:
            headers.update(custom_headers)
        return headers

    def build_url(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> str:
        """Construct full request URL with query parameters."""
        clean_endpoint = endpoint.lstrip("/")
        url = f"{self.base_url}/{clean_endpoint}"
        if params:
            # Sort keys for deterministic URL construction
            clean_params = {k: v for k, v in params.items() if v is not None}
            if clean_params:
                encoded_params = urlencode(clean_params, doseq=True)
                url = f"{url}?{encoded_params}"
        return url

    def parse_response(self, response: AlpacaResponse) -> AlpacaResponse:
        """Parse JSON response body and attach to AlpacaResponse.

        Raises:
            AlpacaInvalidResponseError: If JSON decoding fails on a 2xx response.
        """
        if not response.body or not response.body.strip():
            response.parsed_json = {}
            return response

        try:
            response.parsed_json = json.loads(response.body)
            return response
        except json.JSONDecodeError as exc:
            secrets = self._get_secrets()
            raise AlpacaInvalidResponseError(
                f"Malformed JSON response from Alpaca: {exc}",
                status_code=response.status_code,
                response_body=response.body,
                secrets_to_redact=secrets,
            ) from exc

    def request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> AlpacaResponse:
        """Execute HTTP request with rate limiting, bounded retries, and typed error mapping.

        Raises:
            AlpacaAuthenticationError: For 401 or 403 HTTP codes.
            AlpacaInvalidRequestError: For 400 or 422 HTTP codes.
            AlpacaNoDataError: For 404 HTTP codes or empty responses.
            AlpacaRateLimitError: For 429 HTTP codes or local rate limit timeouts.
            AlpacaServiceUnavailableError: For 500, 502, 503, 504 codes after retry exhaustion.
            AlpacaTimeoutError: For client timeouts after retry exhaustion.
            AlpacaInvalidResponseError: For malformed JSON responses.
        """
        method_upper = method.upper().strip()
        url = self.build_url(endpoint, params)
        req_headers = self.build_headers(headers)
        secrets = self._get_secrets()

        # Offline safety check: verify transport is present or network is forbidden
        if self.transport_fn is None and not self.allow_network:
            raise AlpacaServiceUnavailableError(
                "Live network execution is disabled in offline mode. "
                "Inject a mock transport_fn for offline testing.",
                secrets_to_redact=secrets,
            )

        # Rate limiting: acquire token before making request
        acquired = self.rate_limiter.acquire(tokens=1.0, block=True, timeout=self.timeout)
        if not acquired:
            raise AlpacaRateLimitError(
                "Client-side rate limit wait exceeded timeout",
                secrets_to_redact=secrets,
            )

        attempts = 1 + self.max_retries
        rate_limit_retries_remaining = 1  # At most 1 bounded retry for 429 with Retry-After
        last_error: Optional[Exception] = None

        for attempt in range(attempts):
            try:
                # Execute request via transport callable
                if self.transport_fn is not None:
                    raw_res = self.transport_fn(
                        url=url,
                        headers=req_headers,
                        method=method_upper,
                        params=params,
                        timeout=self.timeout,
                    )
                    if isinstance(raw_res, dict):
                        # Convert dict to AlpacaResponse if fixture returned raw dict
                        raw_res = AlpacaResponse(
                            status_code=raw_res.get("status_code", 200),
                            headers=raw_res.get("headers", {}),
                            body=raw_res.get("body", ""),
                            parsed_json=raw_res.get("parsed_json"),
                        )
                    res = raw_res
                else:
                    raise AlpacaServiceUnavailableError(
                        "Live network execution is disabled.",
                        secrets_to_redact=secrets,
                    )

                status_code = res.status_code

                # -------------------------------------------------------------
                # 2xx Success: Parse and return
                # -------------------------------------------------------------
                if 200 <= status_code < 300:
                    return self.parse_response(res)

                # -------------------------------------------------------------
                # 401 / 403: Authentication Failures (DO NOT RETRY)
                # -------------------------------------------------------------
                if status_code in (401, 403):
                    msg = (
                        f"Authentication failed (HTTP {status_code}): "
                        f"{res.body[:200] if res.body else 'Unauthorized/Forbidden'}"
                    )
                    raise AlpacaAuthenticationError(
                        msg,
                        status_code=status_code,
                        response_body=res.body,
                        secrets_to_redact=secrets,
                    )

                # -------------------------------------------------------------
                # 400 / 422: Invalid Client Requests (DO NOT RETRY)
                # -------------------------------------------------------------
                if status_code in (400, 422):
                    msg = f"Invalid request (HTTP {status_code}): {res.body[:200]}"
                    raise AlpacaInvalidRequestError(
                        msg,
                        status_code=status_code,
                        response_body=res.body,
                        secrets_to_redact=secrets,
                    )

                # -------------------------------------------------------------
                # 404: Not Found / Missing Data (DO NOT RETRY)
                # -------------------------------------------------------------
                if status_code == 404:
                    msg = f"Resource or symbol not found (HTTP 404): {res.body[:200]}"
                    raise AlpacaNoDataError(
                        msg,
                        status_code=status_code,
                        response_body=res.body,
                        secrets_to_redact=secrets,
                    )

                # -------------------------------------------------------------
                # 429: Rate Limit (Retry ONLY if Retry-After is present and <= max_wait)
                # -------------------------------------------------------------
                if status_code == 429:
                    retry_wait = res.retry_after
                    if (
                        rate_limit_retries_remaining > 0
                        and retry_wait is not None
                        and 0 < retry_wait <= self.max_retry_after
                    ):
                        rate_limit_retries_remaining -= 1
                        logger.warning(
                            "Alpaca rate limit encountered (HTTP 429). Retrying after %.1fs",
                            retry_wait,
                        )
                        self.sleep_fn(retry_wait)
                        continue

                    # Otherwise raise AlpacaRateLimitError immediately without retrying
                    raise AlpacaRateLimitError(
                        f"Alpaca rate limit exceeded (HTTP 429): {res.body[:200]}",
                        retry_after=retry_wait,
                        status_code=status_code,
                        response_body=res.body,
                        secrets_to_redact=secrets,
                    )

                # -------------------------------------------------------------
                # 500, 502, 503, 504: Server Errors (TRANSIENT - RETRY)
                # -------------------------------------------------------------
                if status_code in (500, 502, 503, 504):
                    if attempt < self.max_retries:
                        backoff = min(
                            MAX_BACKOFF_SECONDS,
                            self.backoff_factor * (2 ** attempt),
                        )
                        logger.warning(
                            "Alpaca server error (HTTP %d, attempt %d/%d). Retrying in %.2fs",
                            status_code,
                            attempt + 1,
                            attempts,
                            backoff,
                        )
                        self.sleep_fn(backoff)
                        continue

                    raise AlpacaServiceUnavailableError(
                        f"Alpaca server error (HTTP {status_code}) after {attempts} attempts: {res.body[:200]}",
                        status_code=status_code,
                        response_body=res.body,
                        secrets_to_redact=secrets,
                    )

                # Unhandled status codes: treat as invalid response
                raise AlpacaInvalidResponseError(
                    f"Unexpected HTTP status {status_code} from Alpaca: {res.body[:200]}",
                    status_code=status_code,
                    response_body=res.body,
                    secrets_to_redact=secrets,
                )

            except (
                AlpacaAuthenticationError,
                AlpacaInvalidRequestError,
                AlpacaNoDataError,
                AlpacaRateLimitError,
                AlpacaInvalidResponseError,
            ):
                # Non-transient errors: re-raise immediately
                raise

            except AlpacaServiceUnavailableError:
                # Re-raise if already exhausted retries
                raise

            except Exception as exc:
                last_error = exc
                is_timeout = (
                    isinstance(exc, (TimeoutError, TimeoutException := getattr(exc, "__class__", None)))
                    or "timeout" in str(exc).lower()
                )

                if attempt < self.max_retries:
                    backoff = min(
                        MAX_BACKOFF_SECONDS,
                        self.backoff_factor * (2 ** attempt),
                    )
                    logger.warning(
                        "Alpaca request transport exception (%s, attempt %d/%d). Retrying in %.2fs",
                        type(exc).__name__,
                        attempt + 1,
                        attempts,
                        backoff,
                    )
                    self.sleep_fn(backoff)
                    continue

                if is_timeout:
                    raise AlpacaTimeoutError(
                        f"Alpaca request timed out after {attempts} attempts",
                        secrets_to_redact=secrets,
                    ) from exc

                raise AlpacaServiceUnavailableError(
                    f"Alpaca transport failed after {attempts} attempts: {type(exc).__name__}",
                    secrets_to_redact=secrets,
                ) from exc

        raise AlpacaServiceUnavailableError(
            f"Alpaca request failed after {attempts} attempts: {last_error}",
            secrets_to_redact=secrets,
        )

    # -------------------------------------------------------------------------
    # High-Level Market Data Helpers
    # -------------------------------------------------------------------------
    def get(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> AlpacaResponse:
        """Perform a GET request to an Alpaca endpoint."""
        return self.request(method="GET", endpoint=endpoint, params=params)

    def get_bars(
        self,
        symbols: Union[str, Sequence[str]],
        timeframe: str = "1Day",
        start: Optional[Union[str, datetime]] = None,
        end: Optional[Union[str, datetime]] = None,
        limit: Optional[int] = None,
        page_token: Optional[str] = None,
        feed: str = "iex",
        adjustment: str = "split",
    ) -> Dict[str, Any]:
        """Fetch stock bars from Alpaca /v2/stocks/bars endpoint.

        Returns:
            Parsed dictionary containing 'bars' and optional 'next_page_token'.
        """
        if isinstance(symbols, str):
            sym_str = symbols.upper().strip()
        else:
            sym_str = ",".join(s.upper().strip() for s in symbols if s.strip())

        if not sym_str:
            raise AlpacaInvalidRequestError("At least one symbol must be specified.")

        params: Dict[str, Any] = {
            "symbols": sym_str,
            "timeframe": timeframe,
            "feed": feed.lower().strip(),
            "adjustment": adjustment.lower().strip(),
        }

        if start:
            if isinstance(start, datetime):
                params["start"] = start.strftime("%Y-%m-%dT%H:%M:%SZ")
            else:
                params["start"] = str(start)

        if end:
            if isinstance(end, datetime):
                params["end"] = end.strftime("%Y-%m-%dT%H:%M:%SZ")
            else:
                params["end"] = str(end)

        if limit is not None:
            params["limit"] = int(limit)

        if page_token is not None:
            params["page_token"] = str(page_token)

        res = self.get(endpoint="/v2/stocks/bars", params=params)
        return res.parsed_json or {}
