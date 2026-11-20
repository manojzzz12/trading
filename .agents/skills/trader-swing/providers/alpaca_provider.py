"""Alpaca Market Data Provider implementation for trader-swing.

Phase 7.1.3: Mocked Alpaca Market Data Provider Foundation.

Enforces:
1. Native SPLIT_ADJUSTED price basis via Alpaca API (adjustment='split').
2. Explicit IEX feed classification: single-exchange unconsolidated volume.
3. Anti-Substitution Guard: explicitly tags is_consolidated_volume=False,
   preventing IEX volume from ever being substituted for consolidated tape volume.
4. Timestamp and calendar normalization:
   Daily bar timestamp = session date at 09:30:00 America/New_York converted to UTC.
   Session lifecycle (COMPLETE vs FORMING) evaluated via MarketClock.
5. Deterministic error classification:
   HTTP 429 -> RateLimitError
   HTTP 401 / 403 -> ProviderUnavailableError
   HTTP 404 / empty response -> NoDataError
   Timeout / network failure -> TimeoutError / ProviderUnavailableError
6. Intrinsic candle geometry and sequence validation (low <= open <= high, low <= close <= high).
7. 100% offline injectable request callable (alpaca_request_fn) for zero-network testing.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import json
import logging
import os
import sys
import time as time_mod
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

# Ensure local imports work regardless of execution context
PARENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PARENT_DIR not in sys.path:
    sys.path.insert(0, PARENT_DIR)

from accounting import to_decimal
from market_data_models import (
    CandleStatus,
    DataFreshness,
    EASTERN,
    MarketClock,
    OHLCVCandle,
    parse_iso_timestamp,
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
    resolve_candle_lifecycle,
    validate_candle_sequence,
    validate_single_candle,
)
from .base import MarketDataProvider

logger = logging.getLogger(__name__)

DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_INTERVALS = (1.0, 2.0, 4.0)
DEFAULT_TIMEOUT_SECONDS = 10


# -----------------------------------------------------------------------------
# Alpaca Explicit Candle Data Model
# -----------------------------------------------------------------------------
@dataclass
class AlpacaCandle(OHLCVCandle):
    """Normalized Alpaca IEX candle carrying exchange provenance and metadata."""
    vwap: Optional[Decimal] = None
    trade_count: Optional[int] = None
    feed_classification: str = "IEX"
    session_date: Optional[date] = None
    adjustment_type: str = "SPLIT_ADJUSTED"
    is_consolidated_volume: bool = False  # Strictly False: IEX volume is unconsolidated!
    source: str = "alpaca_iex"
    is_delayed: bool = False

    def get_consolidated_volume(self) -> int:
        """Explicit guard preventing silent substitution of IEX volume for consolidated volume.

        Raises InvalidVolumeError if invoked.
        """
        raise InvalidVolumeError(
            f"AlpacaCandle for {self.ticker} holds unconsolidated IEX volume ({self.volume:,} shares). "
            "IEX volume cannot be used as consolidated volume for ADV/RVOL liquidity screening.",
            ticker=self.ticker,
            timestamp=self.timestamp,
        )


# -----------------------------------------------------------------------------
# Alpaca Market Data Provider
# -----------------------------------------------------------------------------
class AlpacaMarketDataProvider(MarketDataProvider):
    """Alpaca Market Data Provider supporting IEX feed queries with mocked testing."""

    name: str = "alpaca"
    feed_name: str = "iex"
    is_delayed: bool = False
    delay_status: str = "REALTIME_UNCONSOLIDATED"
    delay_minutes: int = 0
    native_adjustment_type: str = "SPLIT_ADJUSTED"
    is_consolidated_volume: bool = False

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        base_url: str = "https://data.alpaca.markets",
        feed: str = "iex",
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_intervals: Sequence[float] = DEFAULT_BACKOFF_INTERVALS,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        alpaca_request_fn: Optional[Callable[..., Any]] = None,
        clock: Optional[MarketClock] = None,
        cache_db_path: Optional[str] = None,
        connector: Optional[Any] = None,
    ):
        self.api_key = api_key or os.environ.get("ALPACA_API_KEY_ID")
        self.api_secret = api_secret or os.environ.get("ALPACA_API_SECRET_KEY")
        self.base_url = base_url.rstrip("/")
        self.feed = feed.lower().strip()
        self.max_retries = max(0, max_retries)
        self.backoff_intervals = tuple(backoff_intervals) if backoff_intervals is not None else DEFAULT_BACKOFF_INTERVALS
        self.timeout_seconds = max(1, timeout_seconds)
        self.alpaca_request_fn = alpaca_request_fn
        self.clock = clock or MarketClock()
        self.cache_db_path = cache_db_path
        self.connector = connector

    # -------------------------------------------------------------------------
    # Request Execution & Error Classification
    # -------------------------------------------------------------------------
    def _execute_request(self, endpoint: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Execute request via connector, injectable callable, or raise in offline mode."""
        if self.connector is not None:
            res = self.connector.get(endpoint=endpoint, params=params)
            return res.parsed_json or {}

        if self.alpaca_request_fn is not None:
            fn = self.alpaca_request_fn
        else:
            # Strictly prohibit unconfigured live network requests
            if not self.api_key or not self.api_secret:
                raise ProviderUnavailableError(
                    "Alpaca credentials missing and no mock callable injected. "
                    "Live network calls are disabled in offline mode."
                )
            raise ProviderUnavailableError(
                "Live network execution is disabled in Phase 7.1.3. "
                "Inject alpaca_request_fn for offline testing."
            )

        attempts = 1 + self.max_retries
        last_error: Optional[Exception] = None

        for attempt in range(attempts):
            try:
                response = fn(endpoint=endpoint, params=params)
                if isinstance(response, str):
                    try:
                        return json.loads(response)
                    except json.JSONDecodeError as jde:
                        raise InvalidResponseError(f"Malformed JSON response from Alpaca: {jde}") from jde
                elif isinstance(response, dict):
                    # Check for explicit error dictionaries from mocked error fixtures
                    if "status_code" in response:
                        code = response["status_code"]
                        msg = response.get("error_message", f"HTTP Error {code}")
                        if code in (401, 403):
                            raise ProviderUnavailableError(f"Authentication failed ({code}): {msg}")
                        elif code == 404:
                            raise NoDataError(f"Symbol or endpoint not found: {msg}")
                        elif code == 429:
                            raise RateLimitError(msg)
                        elif code >= 500:
                            # Transient server error: raise to trigger retry
                            raise IOError(f"Alpaca server error ({code}): {msg}")
                        else:
                            raise InvalidResponseError(f"Alpaca error ({code}): {msg}")
                    return response
                else:
                    raise InvalidResponseError(
                        f"Unexpected response type from Alpaca provider: {type(response).__name__}"
                    )
            except (RateLimitError, NoDataError, InvalidResponseError):
                # Non-transient client/data errors raise immediately without retries
                raise
            except ProviderUnavailableError as pue:
                # Authentication failures raise immediately
                if "Authentication failed" in str(pue):
                    raise
                last_error = pue
                if attempt < self.max_retries:
                    backoff = self.backoff_intervals[min(attempt, len(self.backoff_intervals) - 1)]
                    time_mod.sleep(backoff)
                    continue
                raise
            except Exception as exc:
                last_error = exc
                err_msg = str(exc)
                if "429" in err_msg or "Rate limit" in err_msg:
                    raise RateLimitError(f"Alpaca rate limit exceeded: {err_msg}") from exc
                if "401" in err_msg or "403" in err_msg or "Unauthorized" in err_msg or "Forbidden" in err_msg:
                    raise ProviderUnavailableError(f"Authentication failed: {err_msg}") from exc
                if "404" in err_msg or "Not Found" in err_msg:
                    raise NoDataError(f"Alpaca resource not found: {err_msg}") from exc
                if "timed out" in err_msg.lower():
                    if attempt >= self.max_retries:
                        raise TimeoutError(f"Alpaca request timed out after {attempts} attempts: {err_msg}") from exc
                
                # Transient server errors (500, 502, 503) retry with backoff
                if attempt < self.max_retries:
                    backoff = self.backoff_intervals[min(attempt, len(self.backoff_intervals) - 1)]
                    logger.warning("Alpaca request transient error (attempt %d/%d): %s. Backing off %.1fs", attempt + 1, attempts, exc, backoff)
                    time_mod.sleep(backoff)
                else:
                    raise ProviderUnavailableError(
                        f"Alpaca request failed after {attempts} attempts: {last_error}"
                    ) from last_error

        raise ProviderUnavailableError(f"Alpaca retry exhaustion: {last_error}")

    def _classify_http_status_code(self, status_code: int, error_message: str) -> None:
        """Classify HTTP status codes into deterministic domain exceptions."""
        if status_code == 429:
            raise RateLimitError(error_message)
        elif status_code in (401, 403):
            raise ProviderUnavailableError(f"Authentication failed ({status_code}): {error_message}")
        elif status_code == 404:
            raise NoDataError(f"Symbol or endpoint not found: {error_message}")
        elif status_code >= 500:
            raise ProviderUnavailableError(f"Alpaca server error ({status_code}): {error_message}")
        else:
            raise InvalidResponseError(f"Alpaca error ({status_code}): {error_message}")

    # -------------------------------------------------------------------------
    # Bar Deserialization & Normalization
    # -------------------------------------------------------------------------
    def parse_alpaca_bar(
        self,
        bar: Dict[str, Any],
        ticker: str,
        interval: str,
        current_time: Optional[datetime] = None,
    ) -> AlpacaCandle:
        """Parse, validate, and normalize a single Alpaca bar JSON dictionary."""
        sym = ticker.upper().strip()
        inv = interval.lower().strip()

        # 1. Structural check: verify mandatory bar keys
        for key in ("t", "o", "h", "l", "c", "v"):
            if key not in bar or bar[key] is None:
                raise InvalidResponseError(
                    f"Alpaca bar missing mandatory field '{key}' for ticker {sym}: {bar}"
                )

        # 2. Timestamp extraction and normalization
        raw_ts = bar["t"]
        if isinstance(raw_ts, str):
            dt = parse_iso_timestamp(raw_ts)
        elif isinstance(raw_ts, datetime):
            dt = to_utc(raw_ts)
        else:
            raise InvalidResponseError(f"Invalid timestamp type in Alpaca bar: {type(raw_ts)}")

        # Normalize daily bar timestamp to session start at 09:30:00 ET
        if inv == "1d":
            dt_eastern = to_eastern(dt)
            bar_date = dt_eastern.date()
            norm_ts = datetime(bar_date.year, bar_date.month, bar_date.day, 9, 30, 0, tzinfo=EASTERN)
            cand_timestamp = to_utc(norm_ts)
            session_date = bar_date
        else:
            cand_timestamp = dt
            session_date = to_eastern(dt).date()

        # 3. Numeric conversion and validation
        try:
            o_px = Decimal(str(bar["o"]))
            h_px = Decimal(str(bar["h"]))
            l_px = Decimal(str(bar["l"]))
            c_px = Decimal(str(bar["c"]))
        except Exception as exc:
            raise InvalidPriceError(f"Non-numeric price in Alpaca bar for {sym}: {exc}", ticker=sym, timestamp=cand_timestamp) from exc

        try:
            vol = int(bar["v"])
        except Exception as exc:
            raise InvalidVolumeError(f"Non-integer volume in Alpaca bar for {sym}: {exc}", ticker=sym, timestamp=cand_timestamp) from exc

        if vol < 0:
            raise InvalidVolumeError(f"Negative volume ({vol}) in Alpaca bar for {sym}", ticker=sym, timestamp=cand_timestamp)

        vwap_px = Decimal(str(bar["vw"])) if "vw" in bar and bar["vw"] is not None else None
        trade_count = int(bar["n"]) if "n" in bar and bar["n"] is not None else None

        now_dt = to_utc(current_time) if current_time is not None else datetime.now(UTC)

        candle = AlpacaCandle(
            ticker=sym,
            interval=inv,
            timestamp=cand_timestamp,
            open=o_px,
            high=h_px,
            low=l_px,
            close=c_px,
            volume=vol,
            status=CandleStatus.UNKNOWN,
            source="alpaca_iex",
            is_delayed=False,
            vwap=vwap_px,
            trade_count=trade_count,
            feed_classification="IEX",
            session_date=session_date,
            adjustment_type="SPLIT_ADJUSTED",
            is_consolidated_volume=False,
        )

        # 4. Intrinsic single-candle validation (OHLC geometry and price bounds)
        validate_single_candle(candle, current_time=now_dt)

        # 5. Resolve lifecycle status (COMPLETE vs FORMING)
        candle.status = resolve_candle_lifecycle(candle, current_time=now_dt)

        return candle
        trade_count = int(bar["n"]) if "n" in bar and bar["n"] is not None else None

        # 4. Lifecycle resolution (COMPLETE vs FORMING)
        now_dt = to_utc(current_time) if current_time is not None else datetime.now(UTC)
        lifecycle_status = resolve_candle_lifecycle(
            interval=inv,
            candle_timestamp=cand_timestamp,
            current_time=now_dt,
            clock=self.clock,
        )

        candle = AlpacaCandle(
            ticker=sym,
            interval=inv,
            timestamp=cand_timestamp,
            open=o_px,
            high=h_px,
            low=l_px,
            close=c_px,
            volume=vol,
            status=lifecycle_status,
            source="alpaca_iex",
            is_delayed=False,
            vwap=vwap_px,
            trade_count=trade_count,
            feed_classification="IEX",
            session_date=session_date,
            adjustment_type="SPLIT_ADJUSTED",
            is_consolidated_volume=False,
        )

        # 5. Intrinsic single-candle validation (OHLC geometry and price bounds)
        validate_single_candle(candle, current_time=now_dt)

        return candle

    # -------------------------------------------------------------------------
    # Public Provider API
    # -------------------------------------------------------------------------
    def fetch_historical_bars(
        self,
        ticker: str,
        interval: str,
        start: datetime,
        end: datetime,
        current_time: Optional[datetime] = None,
    ) -> List[OHLCVCandle]:
        """Fetch historical completed and forming bars for a single ticker."""
        sym = ticker.upper().strip()
        inv = interval.lower().strip()
        timeframe_map = {"1d": "1Day", "1h": "1Hour", "15m": "15Min", "5m": "5Min", "1m": "1Min"}
        tf = timeframe_map.get(inv, "1Day")

        start_iso = to_utc(start).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_iso = to_utc(end).strftime("%Y-%m-%dT%H:%M:%SZ")

        params = {
            "symbols": sym,
            "timeframe": tf,
            "start": start_iso,
            "end": end_iso,
            "adjustment": "split",
            "feed": self.feed,
        }

        payload = self._execute_request(endpoint="/v2/stocks/bars", params=params)

        bars_data = payload.get("bars", {})
        if not bars_data or sym not in bars_data:
            raise NoDataError(f"No market data returned by Alpaca for {sym} between {start_iso} and {end_iso}")

        raw_bars = bars_data[sym]
        if not raw_bars:
            raise NoDataError(f"Empty bar series returned by Alpaca for {sym}")

        candles: List[OHLCVCandle] = []
        for b in raw_bars:
            c = self.parse_alpaca_bar(b, ticker=sym, interval=inv, current_time=current_time)
            candles.append(c)

        # Validate sequence monotonicity and check for duplicate timestamps
        validate_candle_sequence(candles)

        return candles

    def fetch_multi_bars(
        self,
        tickers: Sequence[str],
        interval: str,
        start: datetime,
        end: datetime,
        current_time: Optional[datetime] = None,
    ) -> Dict[str, List[AlpacaCandle]]:
        """Batch fetch historical bars for multiple tickers (up to 100 symbols per request)."""
        clean_tickers = [t.upper().strip() for t in tickers if t.strip()]
        if not clean_tickers:
            return {}

        inv = interval.lower().strip()
        timeframe_map = {"1d": "1Day", "1h": "1Hour", "15m": "15Min", "5m": "5Min", "1m": "1Min"}
        tf = timeframe_map.get(inv, "1Day")

        start_iso = to_utc(start).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_iso = to_utc(end).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Process in batches of 100
        batch_size = 100
        results: Dict[str, List[AlpacaCandle]] = {}

        for i in range(0, len(clean_tickers), batch_size):
            batch = clean_tickers[i:i + batch_size]
            symbols_param = ",".join(batch)
            params = {
                "symbols": symbols_param,
                "timeframe": tf,
                "start": start_iso,
                "end": end_iso,
                "adjustment": "split",
                "feed": self.feed,
            }

            payload = self._execute_request(endpoint="/v2/stocks/bars", params=params)
            bars_dict = payload.get("bars", {})

            for sym, raw_bars in bars_dict.items():
                if not raw_bars:
                    continue
                sym_candles: List[AlpacaCandle] = []
                for b in raw_bars:
                    c = self.parse_alpaca_bar(b, ticker=sym, interval=inv, current_time=current_time)
                    sym_candles.append(c)
                validate_candle_sequence(sym_candles)
                results[sym] = sym_candles

        return results

    def fetch_latest_bar(
        self,
        ticker: str,
        interval: str,
        current_time: Optional[datetime] = None,
    ) -> Optional[OHLCVCandle]:
        """Fetch the single most recent candle for the specified ticker."""
        now_dt = to_utc(current_time) if current_time is not None else datetime.now(UTC)
        lookback = timedelta(days=5 if interval == "1d" else 1)
        bars = self.fetch_historical_bars(
            ticker=ticker,
            interval=interval,
            start=now_dt - lookback,
            end=now_dt,
            current_time=now_dt,
        )
        return bars[-1] if bars else None

    def get_provider_metadata(self) -> Dict[str, Any]:
        """Return provider properties, feed classifications, and delay disclosures."""
        return {
            "name": self.name,
            "feed": self.feed,
            "feed_classification": "IEX_UNCONSOLIDATED",
            "is_delayed": self.is_delayed,
            "delay_status": self.delay_status,
            "delay_minutes": self.delay_minutes,
            "native_adjustment_type": self.native_adjustment_type,
            "is_consolidated_volume": self.is_consolidated_volume,
            "disclosure": (
                "Alpaca Market Data Free tier reflects single-exchange volume from Investors Exchange (IEX). "
                "IEX volume is unconsolidated (~2% of market) and cannot be substituted for consolidated tape volume."
            ),
        }
