"""Yahoo Finance / yfinance market data provider implementation for trader-swing.

Phase 6.2B — Step 2: Isolated yfinance Provider

Enforces:
1. Native SPLIT_ADJUSTED price basis via yfinance (auto_adjust=False, actions=True).
   Nominal Close is retained; cash dividends do not cause dividend backward deflation.
2. Volume preservation: yfinance volume is natively split-adjusted matching OHLC.
3. Timestamp and calendar normalization:
   Daily bar timestamp = session date at 09:30:00 America/New_York converted to UTC.
   Session close (16:00 ET regular or 13:00 ET early close) handled via MarketClock.
   DST-neutral: zero date-shifting across spring/fall transitions.
4. Bounded retries: initial attempt + up to 3 retries (max_retries=3) with backoff (1s, 2s, 4s).
   Total wait time bounded <= 7 seconds.
5. Deterministic error classification:
   HTTP 429 -> RateLimitError
   HTTP 404 / unavailable ticker -> NoDataError
   Malformed data -> InvalidPriceError / InvalidVolumeError / InvalidResponseError
   Timeout / network failure -> TimeoutError / ProviderUnavailableError
6. Split continuity verification:
   Rejects series with >= 35% unadjusted split cliff drop via SequenceValidationError.
7. Isolated cache boundary:
   Configured for market_data_yfinance_split_adj.db, never writing to market_data.db.
8. 100% offline injectable download callable (yf_download_fn) for zero-network testing.
"""

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import json
import logging
import math
import os
import sys
import time as time_mod
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

# Ensure local imports work regardless of execution context
PARENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PARENT_DIR not in sys.path:
    sys.path.insert(0, PARENT_DIR)

from accounting import to_decimal
from market_data_cache import DEFAULT_CACHE_DIR, MarketDataCache
from market_data_models import (
    CandleStatus,
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

DEFAULT_YFINANCE_CACHE_DB = os.path.join(DEFAULT_CACHE_DIR, "market_data_yfinance_split_adj.db")
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_INTERVALS = (1.0, 2.0, 4.0)
DEFAULT_TIMEOUT_SECONDS = 10
SPLIT_DISCONTINUITY_THRESHOLD = Decimal("0.35")


class YFinanceMarketDataProvider(MarketDataProvider):
    """Yahoo Finance provider using yfinance with split-adjusted price basis."""

    name: str = "yfinance"
    is_delayed: bool = True
    delay_status: str = "UNVERIFIED_DELAY"
    delay_minutes: Optional[int] = None
    native_adjustment_type: str = "SPLIT_ADJUSTED"

    def __init__(
        self,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_intervals: Sequence[float] = DEFAULT_BACKOFF_INTERVALS,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        yf_download_fn: Optional[Callable[..., Any]] = None,
        clock: Optional[MarketClock] = None,
        cache_db_path: Optional[str] = None,
    ):
        self.max_retries = max(0, max_retries)
        self.backoff_intervals = tuple(backoff_intervals) if backoff_intervals is not None else DEFAULT_BACKOFF_INTERVALS
        self.timeout_seconds = max(1, timeout_seconds)
        self.yf_download_fn = yf_download_fn
        self.clock = clock or MarketClock()
        self.cache_db_path = cache_db_path or DEFAULT_YFINANCE_CACHE_DB

    def _default_yf_download(
        self,
        ticker: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
        interval: str = "1d",
        auto_adjust: bool = False,
        actions: bool = True,
        timeout: int = 10,
    ) -> Any:
        """Default network-calling download implementation using yfinance."""
        import yfinance as yf
        t = yf.Ticker(ticker)
        return t.history(
            start=start,
            end=end,
            interval=interval,
            auto_adjust=auto_adjust,
            actions=actions,
            timeout=timeout,
        )

    def _execute_download_with_retries(
        self,
        ticker: str,
        start_str: str,
        end_str: str,
        interval: str,
    ) -> Any:
        """Execute data download with bounded retries on transient errors."""
        fn = self.yf_download_fn or self._default_yf_download
        attempts = 1 + self.max_retries
        last_error: Optional[Exception] = None

        for attempt in range(attempts):
            try:
                # Flexible parameter dispatch for various mock / live callables
                try:
                    result = fn(
                        ticker=ticker,
                        start=start_str,
                        end=end_str,
                        interval=interval,
                        auto_adjust=False,
                        actions=True,
                        timeout=self.timeout_seconds,
                    )
                except TypeError:
                    try:
                        result = fn(ticker, start_str, end_str)
                    except TypeError:
                        try:
                            result = fn(ticker)
                        except TypeError:
                            result = fn()

                # Check if returned result is an error dict (e.g. from static mock fixture)
                if isinstance(result, dict):
                    status_code = result.get("status_code")
                    error_msg = str(result.get("error_message", ""))
                    error_type = str(result.get("error_type", ""))

                    if status_code == 404 or "404" in error_msg:
                        raise NoDataError(f"HTTP 404 Not Found for {ticker}: {error_msg}")

                    if status_code == 429 or "429" in error_msg or "Rate limit" in error_msg:
                        last_error = RateLimitError(f"Rate limit exceeded (HTTP 429) for {ticker}: {error_msg}")
                        if attempt < self.max_retries:
                            sleep_s = self.backoff_intervals[attempt] if attempt < len(self.backoff_intervals) else self.backoff_intervals[-1]
                            if sleep_s > 0:
                                time_mod.sleep(sleep_s)
                            continue
                        raise last_error

                    if error_type == "TimeoutError" or "timed out" in error_msg.lower():
                        last_error = TimeoutError(f"Network timeout for {ticker}: {error_msg}")
                        if attempt < self.max_retries:
                            sleep_s = self.backoff_intervals[attempt] if attempt < len(self.backoff_intervals) else self.backoff_intervals[-1]
                            if sleep_s > 0:
                                time_mod.sleep(sleep_s)
                            continue
                        raise last_error

                return result

            except NoDataError:
                # Permanent non-retryable
                raise
            except CandleValidationError:
                # Permanent data validation error: non-retryable
                raise
            except RateLimitError as e:
                last_error = e
                if attempt < self.max_retries:
                    sleep_s = self.backoff_intervals[attempt] if attempt < len(self.backoff_intervals) else self.backoff_intervals[-1]
                    if sleep_s > 0:
                        time_mod.sleep(sleep_s)
                    continue
                raise
            except (TimeoutError, ProviderUnavailableError) as e:
                last_error = e
                if attempt < self.max_retries:
                    sleep_s = self.backoff_intervals[attempt] if attempt < len(self.backoff_intervals) else self.backoff_intervals[-1]
                    if sleep_s > 0:
                        time_mod.sleep(sleep_s)
                    continue
                raise
            except Exception as e:
                err_str = str(e)
                status_code = getattr(e, "status_code", getattr(e, "code", None))
                if status_code == 404 or "404" in err_str or "Not Found" in err_str:
                    raise NoDataError(f"HTTP 404 Not Found for {ticker}: {e}") from e
                elif status_code == 429 or "429" in err_str or "Rate limit" in err_str or "Too Many Requests" in err_str:
                    last_error = RateLimitError(f"Rate limit exceeded (HTTP 429) for {ticker}: {e}")
                    if attempt < self.max_retries:
                        sleep_s = self.backoff_intervals[attempt] if attempt < len(self.backoff_intervals) else self.backoff_intervals[-1]
                        if sleep_s > 0:
                            time_mod.sleep(sleep_s)
                        continue
                    raise last_error from e
                elif "timed out" in err_str.lower() or "timeout" in err_str.lower():
                    last_error = TimeoutError(f"Network timeout for {ticker}: {e}")
                    if attempt < self.max_retries:
                        sleep_s = self.backoff_intervals[attempt] if attempt < len(self.backoff_intervals) else self.backoff_intervals[-1]
                        if sleep_s > 0:
                            time_mod.sleep(sleep_s)
                        continue
                    raise last_error from e
                elif "connection refused" in err_str.lower() or "connection reset" in err_str.lower() or "503" in err_str:
                    last_error = ProviderUnavailableError(f"Provider unavailable for {ticker}: {e}")
                    if attempt < self.max_retries:
                        sleep_s = self.backoff_intervals[attempt] if attempt < len(self.backoff_intervals) else self.backoff_intervals[-1]
                        if sleep_s > 0:
                            time_mod.sleep(sleep_s)
                        continue
                    raise last_error from e
                else:
                    raise ProviderError(f"Unexpected provider error for {ticker}: {e}") from e

        if last_error is not None:
            raise last_error
        raise ProviderUnavailableError(f"Provider unavailable for {ticker} after {attempts} attempts.")

    @classmethod
    def verify_split_continuity(
        cls,
        candles: Sequence[OHLCVCandle],
        split_events: Sequence[Tuple[date, Decimal]],
        ticker: str,
        threshold: Decimal = SPLIT_DISCONTINUITY_THRESHOLD,
    ) -> None:
        """Verify price continuity across split events.

        Rejects the series with SequenceValidationError if an unadjusted price cliff
        at or exceeding the threshold (default 35%) is detected.
        """
        if not candles or not split_events:
            return

        split_dict = {s_date: s_ratio for s_date, s_ratio in split_events}

        for i in range(1, len(candles)):
            c_date = to_eastern(candles[i].timestamp).date()
            if c_date in split_dict:
                prev_close = candles[i - 1].close
                curr_close = candles[i].close
                if prev_close <= Decimal("0.00"):
                    continue

                pct_change = abs(curr_close - prev_close) / prev_close
                if pct_change >= threshold:
                    ratio = split_dict[c_date]
                    raise SequenceValidationError(
                        f"Unadjusted split discontinuity detected for {ticker} on {c_date}: "
                        f"price change {pct_change:.2%} meets or exceeds {threshold:.0%} threshold "
                        f"(prior close: {prev_close}, split close: {curr_close}, split ratio: {ratio}). "
                        f"Series must be split-adjusted before ingestion."
                    )

    def _verify_split_continuity(
        self,
        candles: Sequence[OHLCVCandle],
        split_events: Sequence[Tuple[date, Decimal]],
        ticker: str,
    ) -> None:
        self.verify_split_continuity(candles, split_events, ticker, SPLIT_DISCONTINUITY_THRESHOLD)

    def _parse_and_validate_data(
        self,
        raw_data: Any,
        ticker: str,
        interval: str,
        current_time: Optional[datetime] = None,
    ) -> List[OHLCVCandle]:
        """Parse, normalize, and validate raw downloaded data into OHLCVCandles."""
        if raw_data is None:
            raise NoDataError(f"No data returned for ticker {ticker}.")

        top_split_ratio = None
        top_split_date = None

        if hasattr(raw_data, "iterrows"):
            # Pandas DataFrame returned by yfinance
            if raw_data.empty:
                raise NoDataError(f"Empty DataFrame returned for ticker {ticker}.")

            raw_bars = []
            for idx, row in raw_data.iterrows():
                b_dict = {
                    "date": idx,
                    "open": row.get("Open", row.get("open")),
                    "high": row.get("High", row.get("high")),
                    "low": row.get("Low", row.get("low")),
                    "close": row.get("Close", row.get("close")),
                    "volume": row.get("Volume", row.get("volume")),
                    "splits": row.get("Stock Splits", row.get("Stock_Splits", row.get("Splits", row.get("splits", 0.0)))),
                    "dividends": row.get("Dividends", row.get("dividends", 0.0)),
                }
                raw_bars.append(b_dict)

        elif isinstance(raw_data, dict):
            if "bars" in raw_data:
                raw_bars = raw_data["bars"]
                top_split_ratio = raw_data.get("split_ratio")
                top_split_date = raw_data.get("split_date")
            else:
                raw_bars = []

            if not raw_bars:
                raise NoDataError(f"No bars returned for ticker {ticker}.")

        elif isinstance(raw_data, list):
            raw_bars = raw_data
            if not raw_bars:
                raise NoDataError(f"No bars returned for ticker {ticker}.")
        else:
            raise InvalidResponseError(f"Unexpected data type returned for {ticker}: {type(raw_data).__name__}")

        now_utc = to_utc(current_time) if current_time is not None else datetime.now(UTC)
        parsed_candles: List[OHLCVCandle] = []
        split_events: List[Tuple[date, Decimal]] = []

        if top_split_date and top_split_ratio is not None:
            try:
                s_d = date.fromisoformat(str(top_split_date)[:10])
                s_r = Decimal(str(top_split_ratio))
                split_events.append((s_d, s_r))
            except Exception:
                pass

        for i, b in enumerate(raw_bars):
            # 1. Date extraction
            date_val = b.get("date") or b.get("Date") or b.get("timestamp") or b.get("Datetime")
            if date_val is None:
                raise InvalidResponseError(f"Missing date in bar record {i} for {ticker}.")

            if type(date_val).__name__ == "Timestamp" or (hasattr(date_val, "date") and hasattr(date_val, "nanosecond")):
                # For pandas Timestamp values representing Yahoo Finance daily session dates,
                # extract exchange session date directly via .date() to avoid pandas C-extension
                # DST conversion offset shift on custom USEastern tzinfo.
                sess_date = date_val.date()
            elif isinstance(date_val, (datetime, date)):
                if isinstance(date_val, datetime) and date_val.tzinfo is not None:
                    sess_date = to_eastern(date_val).date()
                else:
                    sess_date = date_val.date() if isinstance(date_val, datetime) else date_val
            elif hasattr(date_val, "tz") and date_val.tz is not None:
                sess_date = date_val.tz_convert("America/New_York").date()
            elif hasattr(date_val, "date"):
                sess_date = date_val.date()
            elif isinstance(date_val, str):
                sess_date = date.fromisoformat(date_val[:10])
            else:
                raise InvalidResponseError(f"Unparseable date '{date_val}' in bar record {i} for {ticker}.")

            # 2. Price extraction and null/malformed check
            o_val = b.get("open") if "open" in b else b.get("Open")
            h_val = b.get("high") if "high" in b else b.get("High")
            l_val = b.get("low") if "low" in b else b.get("Low")
            c_val = b.get("close") if "close" in b else b.get("Close")
            v_val = b.get("volume") if "volume" in b else b.get("Volume")

            # Check for null prices
            for name, val in [("open", o_val), ("high", h_val), ("low", l_val), ("close", c_val)]:
                if val is None:
                    raise InvalidPriceError(f"Candle {name} price cannot be None for {ticker} at {sess_date}.", ticker=ticker)
                if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
                    raise InvalidPriceError(f"Candle {name} price cannot be NaN/infinite for {ticker} at {sess_date}.", ticker=ticker)

            # Volume check
            if v_val is None:
                raise InvalidVolumeError(f"Candle volume cannot be None for {ticker} at {sess_date}.", ticker=ticker)
            if isinstance(v_val, (int, float, Decimal)) and v_val < 0:
                raise InvalidVolumeError(f"Candle volume cannot be negative ({v_val}) for {ticker} at {sess_date}.", ticker=ticker)

            # Check splits
            split_val = b.get("splits") if "splits" in b else b.get("Stock Splits", b.get("Stock_Splits", b.get("Splits", 0.0)))
            if split_val is not None:
                try:
                    s_dec = Decimal(str(split_val))
                    if s_dec > Decimal("0.0") and s_dec != Decimal("1.0"):
                        split_events.append((sess_date, s_dec))
                except Exception:
                    pass

            # Timestamp normalization (Requirement 6, 7, 8, 9)
            # Session start in Eastern time, converted to UTC
            start_et = datetime.combine(sess_date, MarketClock.REGULAR_OPEN, tzinfo=EASTERN)
            ts_utc = to_utc(start_et)

            # Construct candidate candle
            candidate = OHLCVCandle(
                ticker=ticker,
                interval=interval,
                timestamp=ts_utc,
                open=Decimal(str(o_val)),
                high=Decimal(str(h_val)),
                low=Decimal(str(l_val)),
                close=Decimal(str(c_val)),
                volume=int(v_val),
                status=CandleStatus.UNKNOWN,
                source=self.name,
                is_delayed=self.is_delayed,
                received_timestamp=now_utc,
            )

            # Invariant check on candidate prices
            validate_single_candle(candidate, current_time=now_utc)

            # Resolve lifecycle
            resolved_status = resolve_candle_lifecycle(candidate, current_time=now_utc)

            # Non-trading day observation on 1d (weekend/holiday/unsupported year)
            if interval == "1d" and resolved_status == CandleStatus.UNKNOWN:
                logger.warning(
                    f"Discarding non-trading day observation for {ticker} on {sess_date} (status UNKNOWN)."
                )
                continue

            # Normalized candle with resolved status
            candle = OHLCVCandle(
                ticker=ticker,
                interval=interval,
                timestamp=ts_utc,
                open=candidate.open,
                high=candidate.high,
                low=candidate.low,
                close=candidate.close,
                volume=candidate.volume,
                status=resolved_status,
                source=self.name,
                is_delayed=self.is_delayed,
                received_timestamp=now_utc,
                bar_end_timestamp=candidate.bar_end_timestamp,
            )
            validate_single_candle(candle, current_time=now_utc)
            parsed_candles.append(candle)

        if not parsed_candles:
            raise NoDataError(f"No valid candles parsed for {ticker} in specified interval.")

        # Validate sequence invariants (ordering, duplicate prevention, interval consistency)
        validate_candle_sequence(parsed_candles, expected_interval=interval, current_time=now_utc)

        # Split continuity verification gate
        self._verify_split_continuity(parsed_candles, split_events, ticker)

        return parsed_candles

    def fetch_historical_bars(
        self,
        ticker: str,
        interval: str,
        start: datetime,
        end: datetime,
        current_time: Optional[datetime] = None,
    ) -> List[OHLCVCandle]:
        """Fetch historical completed and forming OHLCV candles from yfinance.

        Note: When downloading from yfinance, the `end` date boundary is treated by
        yfinance as exclusive (< end 00:00:00). To include a completed session date T,
        the caller must supply an exclusive upper bound of at least T + 1 calendar day.
        """
        sym = ticker.upper().strip()
        inv = interval.lower().strip()
        start_str = start.strftime("%Y-%m-%d") if isinstance(start, (datetime, date)) else str(start)
        end_str = end.strftime("%Y-%m-%d") if isinstance(end, (datetime, date)) else str(end)

        raw_data = self._execute_download_with_retries(sym, start_str, end_str, inv)
        return self._parse_and_validate_data(raw_data, sym, inv, current_time=current_time)

    def fetch_latest_bar(
        self,
        ticker: str,
        interval: str,
        current_time: Optional[datetime] = None,
    ) -> Optional[OHLCVCandle]:
        """Fetch the single most recent candle for the specified ticker and interval."""
        now_utc = to_utc(current_time) if current_time is not None else datetime.now(UTC)
        lookback_days = 60 if interval == "1d" else 7
        start_dt = now_utc - timedelta(days=lookback_days)
        try:
            bars = self.fetch_historical_bars(ticker, interval, start=start_dt, end=now_utc, current_time=current_time)
            return bars[-1] if bars else None
        except NoDataError:
            return None

    def get_provider_metadata(self) -> Dict[str, Any]:
        """Return provider properties, delay disclosures, and endpoint health metadata."""
        return {
            "provider_name": self.name,
            "is_delayed": self.is_delayed,
            "delay_status": self.delay_status,
            "delay_minutes": self.delay_minutes,
            "native_adjustment_type": self.native_adjustment_type,
            "max_retries": self.max_retries,
            "backoff_intervals": list(self.backoff_intervals),
            "timeout_seconds": self.timeout_seconds,
            "cache_db_path": self.cache_db_path,
            "disclosure": (
                "Yahoo Finance / yfinance provides delayed market data with unverified latency. "
                "Quotes and historical bars are split-adjusted (auto_adjust=False, actions=True) "
                "and are not guaranteed real-time or suitable for live order routing."
            ),
        }
