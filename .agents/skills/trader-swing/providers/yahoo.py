"""Yahoo Finance market data provider implementation for trader-swing.

Features:
1. Free, read-only Yahoo Finance v8 chart API integration.
2. Standard library urllib.request with query2 and query1 fallback hosts.
3. Bounded exponential backoff retries for transient errors (5xx, 429, timeouts).
4. Non-retryable permanent error isolation (400, 401, 403, 404).
5. Explicit candle status resolution (COMPLETE vs FORMING).
6. Timezone-aware normalization with provider metadata preservation.
7. Pluggable HTTP client function for deterministic network-free testing.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
import urllib.error
import urllib.parse
import urllib.request

# Ensure local imports work regardless of execution context
PARENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PARENT_DIR not in sys.path:
    sys.path.insert(0, PARENT_DIR)

from market_data_models import (
    CandleStatus,
    MarketClock,
    OHLCVCandle,
    interval_to_timedelta,
    to_utc,
    to_eastern,
    UTC,
    EASTERN,
)
from market_data_validation import (
    InvalidPriceError,
    InvalidResponseError,
    NoDataError,
    ProviderError,
    ProviderUnavailableError,
    RateLimitError,
    TimeoutError,
    validate_single_candle,
    verify_forming_candle,
    resolve_candle_lifecycle,
)
from .base import MarketDataProvider

USER_AGENT = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
DEFAULT_HOSTS = ("query2.finance.yahoo.com", "query1.finance.yahoo.com")


class YahooMarketDataProvider(MarketDataProvider):
    """Yahoo Finance read-only provider for historical and recent OHLCV bars."""

    name: str = "yahoo"
    is_delayed: bool = True  # Default assumption for Yahoo Finance free tier
    assumed_delay_minutes: int = 15
    delay_status: str = "ASSUMED_UNVERIFIED"
    native_adjustment_type: str = "SPLIT_ADJUSTED"

    def __init__(
        self,
        hosts: Optional[Sequence[str]] = None,
        max_retries: int = 3,
        backoff_seconds: float = 1.0,
        timeout_seconds: int = 15,
        http_get_fn: Optional[Callable[[str, int], str]] = None,
        clock: Optional[MarketClock] = None,
    ):
        self.hosts = tuple(hosts) if hosts else DEFAULT_HOSTS
        self.max_retries = max(1, max_retries)
        self.backoff_seconds = max(0.0, backoff_seconds)
        self.timeout_seconds = max(1, timeout_seconds)
        self.http_get_fn = http_get_fn or self._default_http_get
        self.clock = clock or MarketClock()

    def _default_http_get(self, url: str, timeout: int) -> str:
        """Standard library urllib GET implementation with browser User-Agent."""
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw_bytes = resp.read()
            return raw_bytes.decode("utf-8", errors="replace")

    def _fetch_url_with_retries(self, path: str) -> str:
        """Execute request across fallback hosts with bounded retries on transient errors."""
        last_error: Optional[Exception] = None

        for attempt in range(self.max_retries):
            for host in self.hosts:
                url = f"https://{host}{path}"
                try:
                    return self.http_get_fn(url, self.timeout_seconds)
                except urllib.error.HTTPError as e:
                    code = e.code
                    last_error = e
                    # Non-retryable client errors (except 429)
                    if code in (400, 401, 403, 404):
                        if code == 404:
                            raise NoDataError(f"Yahoo reported 404 Not Found for {path}.") from e
                        raise ProviderError(f"Yahoo HTTP client error {code} for {path}.") from e
                    elif code == 429:
                        last_error = RateLimitError(f"Yahoo rate limit exceeded (HTTP 429) on {host}.")
                        continue  # Try next host or next attempt
                    elif code >= 500:
                        last_error = ProviderUnavailableError(f"Yahoo server error (HTTP {code}) on {host}.")
                        continue
                    else:
                        raise ProviderError(f"Yahoo unexpected HTTP error {code}.") from e
                except (urllib.error.URLError, TimeoutError, OSError) as e:
                    last_error = TimeoutError(f"Yahoo network timeout/failure on {host}: {e}")
                    continue

            # Exponential backoff between full attempts
            if attempt < self.max_retries - 1:
                sleep_time = self.backoff_seconds * (2 ** attempt)
                if sleep_time > 0:
                    time.sleep(sleep_time)

        # Retries exhausted
        if isinstance(last_error, ProviderError):
            raise last_error
        raise ProviderUnavailableError(f"Yahoo provider unavailable after {self.max_retries} attempts: {last_error}")

    def fetch_historical_bars(
        self,
        ticker: str,
        interval: str,
        start: datetime,
        end: datetime,
        current_time: Optional[datetime] = None,
    ) -> List[OHLCVCandle]:
        """Fetch historical bars within the specified time range."""
        sym = ticker.upper().strip()
        inv = interval.lower().strip()
        p1 = int(to_utc(start).timestamp())
        p2 = int(to_utc(end).timestamp())

        include_prepost = "false" if inv == "1d" else "true"
        path = f"/v8/finance/chart/{sym}?period1={p1}&period2={p2}&interval={inv}&includePrePost={include_prepost}&events=div%7Csplit"
        raw_json = self._fetch_url_with_retries(path)
        return self._parse_chart_json(raw_json, sym, inv, current_time=current_time)

    def fetch_latest_bar(
        self,
        ticker: str,
        interval: str,
        current_time: Optional[datetime] = None,
    ) -> Optional[OHLCVCandle]:
        """Fetch the most recent bar for the given ticker and interval."""
        sym = ticker.upper().strip()
        inv = interval.lower().strip()
        # Interval-specific latest-bar query window (Fix O-6)
        if inv == "1wk":
            query_range = "1mo"
        elif inv == "1d":
            query_range = "1mo"
        else:
            query_range = "5d"

        include_prepost = "false" if inv == "1d" else "true"
        path = f"/v8/finance/chart/{sym}?range={query_range}&interval={inv}&includePrePost={include_prepost}"
        raw_json = self._fetch_url_with_retries(path)
        bars = self._parse_chart_json(raw_json, sym, inv, current_time=current_time)
        return bars[-1] if bars else None

    def _parse_chart_json(
        self,
        raw_json: str,
        ticker: str,
        interval: str,
        current_time: Optional[datetime] = None,
    ) -> List[OHLCVCandle]:
        """Parse raw Yahoo v8 chart JSON into normalized OHLCVCandle instances."""
        try:
            payload = json.loads(raw_json)
        except Exception as e:
            raise InvalidResponseError(f"Failed to parse Yahoo JSON response: {e}") from e

        chart = payload.get("chart", {})
        if chart.get("error"):
            err = chart["error"]
            code = err.get("code")
            desc = err.get("description", "Unknown error")
            if code == "Not Found" or "No data found" in desc:
                raise NoDataError(f"Yahoo returned no data for {ticker}: {desc}")
            raise ProviderError(f"Yahoo chart error ({code}): {desc}")

        result_list = chart.get("result")
        if not result_list or not isinstance(result_list, list):
            raise NoDataError(f"Empty result list in Yahoo response for {ticker}.")

        res = result_list[0]
        meta = res.get("meta", {})
        timestamps = res.get("timestamp", [])
        indicators = res.get("indicators", {})
        quotes = indicators.get("quote", [])

        if not quotes or not quotes[0] or not timestamps:
            return []

        q = quotes[0]
        opens = q.get("open", [])
        highs = q.get("high", [])
        lows = q.get("low", [])
        closes = q.get("close", [])
        volumes = q.get("volume", [])

        now_utc = to_utc(current_time) if current_time is not None else datetime.now(UTC)
        reg_market_time = meta.get("regularMarketTime")
        provider_event_ts = datetime.fromtimestamp(reg_market_time, tz=UTC) if reg_market_time else None

        parsed_candles: List[OHLCVCandle] = []
        n = len(timestamps)

        for i in range(n):
            ts_val = timestamps[i]
            if ts_val is None:
                continue

            o_val = opens[i] if i < len(opens) else None
            h_val = highs[i] if i < len(highs) else None
            l_val = lows[i] if i < len(lows) else None
            c_val = closes[i] if i < len(closes) else None
            v_val = volumes[i] if i < len(volumes) else None

            # Skip null candles occasionally returned during market halts or pre-market gaps
            if any(x is None for x in (o_val, h_val, l_val, c_val)):
                continue

            bar_ts = datetime.fromtimestamp(ts_val, tz=UTC)
            dec_open = Decimal(str(o_val))
            dec_high = Decimal(str(h_val))
            dec_low = Decimal(str(l_val))
            dec_close = Decimal(str(c_val))
            int_vol = int(v_val or 0)

            # Determine lifecycle status using structured resolver (Fix A & Fix B)
            candidate = OHLCVCandle(
                ticker=ticker,
                interval=interval,
                timestamp=bar_ts,
                open=dec_open,
                high=dec_high,
                low=dec_low,
                close=dec_close,
                volume=int_vol,
                status=CandleStatus.UNKNOWN,
                source=self.name,
                provider_event_timestamp=provider_event_ts,
                received_timestamp=now_utc,
                is_delayed=self.is_delayed,
            )

            resolved_status = resolve_candle_lifecycle(candidate, current_time=now_utc)

            # Fix B: For regular-session daily bars (interval == '1d'):
            # If the candidate observation resolves to UNKNOWN (e.g. before regular market open on session date,
            # or an unverified observation on a weekend/holiday), drop it.
            # This ensures pre-market indicative quotes cannot become regular-session daily OHLCV records,
            # and the previous completed regular-session daily bar is retained as latest.
            if interval == "1d" and resolved_status == CandleStatus.UNKNOWN:
                continue

            candle = OHLCVCandle(
                ticker=ticker,
                interval=interval,
                timestamp=bar_ts,
                open=dec_open,
                high=dec_high,
                low=dec_low,
                close=dec_close,
                volume=int_vol,
                status=resolved_status,
                source=self.name,
                provider_event_timestamp=provider_event_ts,
                received_timestamp=now_utc,
                is_delayed=self.is_delayed,
            )

            # Validate candle invariants before inclusion
            validate_single_candle(candle, current_time=now_utc)
            parsed_candles.append(candle)

        return parsed_candles

    def get_provider_metadata(self) -> Dict[str, Any]:
        """Return provider configuration, latency disclosures, and endpoint properties."""
        return {
            "provider_name": self.name,
            "is_delayed": self.is_delayed,
            "assumed_delay_minutes": self.assumed_delay_minutes,
            "delay_status": self.delay_status,
            "delay_minutes": self.assumed_delay_minutes,
            "hosts": list(self.hosts),
            "max_retries": self.max_retries,
            "timeout_seconds": self.timeout_seconds,
            "disclosure": (
                "Yahoo Finance free tier provides delayed quotes (unverified; "
                "assumed ~15 min delay; actual exchange latency varies by venue and is not guaranteed). "
                "Not executable quotes."
            ),
        }
