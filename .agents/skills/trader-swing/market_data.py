"""Public market data service and coordinator for trader-swing.

Exposes normalized public APIs:
- get_historical_bars(ticker, interval, start, end)
- get_latest_bar(ticker, interval)
- get_market_data_status(ticker, interval)
- validate_candles(candles)

Features:
1. Provider abstraction with Yahoo Finance default.
2. Local cache integration with transparent write-through and fallback during outages.
3. Strict candle validation before consumer delivery.
4. Intelligent stale data detection considering US market sessions and holidays.
5. Zero order placement, portfolio, or risk manipulation capabilities.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
import sys
from typing import Any, Dict, List, Optional, Sequence

# Ensure local imports work regardless of execution context
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from market_data_models import (
    CandleStatus,
    DataFreshness,
    MarketClock,
    MarketDataStatus,
    MarketSessionState,
    OHLCVCandle,
    interval_to_timedelta,
    to_utc,
    to_eastern,
    UTC,
    EASTERN,
)
from market_data_validation import (
    CandleValidationError,
    MarketDataError,
    NoDataError,
    ProviderError,
    ProviderUnavailableError,
    RateLimitError,
    SequenceValidationError,
    StaleDataError,
    TimeoutError,
    validate_candle_sequence,
    validate_single_candle,
)
from market_data_cache import MarketDataCache
from providers.base import MarketDataProvider
from providers.yahoo import YahooMarketDataProvider


class MarketDataService:
    """Coordinator connecting providers, local cache, validation, and session clock."""

    def __init__(
        self,
        provider: Optional[MarketDataProvider] = None,
        cache: Optional[MarketDataCache] = None,
        clock: Optional[MarketClock] = None,
        stale_threshold_seconds: Optional[int] = None,
    ):
        self.provider = provider or YahooMarketDataProvider()
        self.cache = cache or MarketDataCache()
        self.clock = clock or MarketClock()
        # Default stale threshold: 20 minutes (15 min Yahoo delay + 5 min tolerance)
        self.stale_threshold_seconds = stale_threshold_seconds or 1200

    def get_historical_bars(
        self,
        ticker: str,
        interval: str,
        start: datetime,
        end: datetime,
        use_cache: bool = True,
        current_time: Optional[datetime] = None,
    ) -> List[OHLCVCandle]:
        """Fetch historical validated OHLCV bars.

        Tries provider first; updates cache upon success.
        Falls back to cache if provider suffers a transient or connectivity outage.
        """
        sym = ticker.upper().strip()
        inv = interval.lower().strip()
        now_utc = to_utc(current_time) if current_time is not None else datetime.now(UTC)

        try:
            bars = self.provider.fetch_historical_bars(sym, inv, start, end, current_time=now_utc)
            # Add processing timestamp
            for b in bars:
                b.processing_timestamp = now_utc

            # Validate entire sequence before storing or delivering
            validate_candle_sequence(bars, expected_interval=inv, current_time=now_utc)

            # Update cache if enabled
            if use_cache and self.cache:
                self.cache.store_candles(bars)

            return bars

        except (ProviderUnavailableError, RateLimitError, TimeoutError, OSError) as e:
            # Outage encountered: attempt fallback to cache
            if use_cache and self.cache:
                cached = self.cache.get_historical_candles(sym, inv, start, end)
                if cached:
                    # Mark source as cache fallback
                    for c in cached:
                        c.source = f"{c.source}_cached_fallback"
                    return cached
            # If no cached data exists, re-raise structured provider error
            raise e

    def get_latest_bar(
        self,
        ticker: str,
        interval: str,
        use_cache: bool = True,
        current_time: Optional[datetime] = None,
    ) -> Optional[OHLCVCandle]:
        """Fetch the most recent validated bar for a ticker and interval."""
        sym = ticker.upper().strip()
        inv = interval.lower().strip()
        now_utc = to_utc(current_time) if current_time is not None else datetime.now(UTC)

        try:
            bar = self.provider.fetch_latest_bar(sym, inv, current_time=now_utc)
            if bar is not None:
                bar.processing_timestamp = now_utc
                validate_single_candle(bar, current_time=now_utc)
                if use_cache and self.cache:
                    self.cache.store_candles([bar])
                return bar
            return None

        except (ProviderUnavailableError, RateLimitError, TimeoutError, OSError) as e:
            if use_cache and self.cache:
                cached = self.cache.get_latest_candle(sym, inv)
                if cached:
                    cached.source = f"{cached.source}_cached_fallback"
                    return cached
            raise e

    def get_market_data_status(
        self,
        ticker: str,
        interval: str,
        current_time: Optional[datetime] = None,
    ) -> MarketDataStatus:
        """Evaluate data freshness, market session state, and provider latency."""
        sym = ticker.upper().strip()
        inv = interval.lower().strip()
        now_utc = to_utc(current_time) if current_time is not None else datetime.now(UTC)

        # Retrieve latest available bar (from cache or provider)
        latest_bar = None
        if self.cache:
            latest_bar = self.cache.get_latest_candle(sym, inv)

        if latest_bar is None:
            try:
                latest_bar = self.get_latest_bar(sym, inv, current_time=now_utc)
            except ProviderError:
                pass

        if latest_bar is None:
            return MarketDataStatus(
                ticker=sym,
                interval=inv,
                freshness=DataFreshness.UNKNOWN,
                latest_bar_timestamp=None,
                latest_bar_status=None,
                source=self.provider.name,
                is_delayed=self.provider.is_delayed,
                as_of=now_utc,
                message="No market data available for ticker/interval.",
            )

        bar_ts = to_utc(latest_bar.timestamp)
        bar_et = to_eastern(bar_ts)
        now_et = to_eastern(now_utc)
        session_state = self.clock.get_session_state(now_utc)

        # 1. Calendar coverage boundaries check (Fix O-4)
        if session_state == MarketSessionState.UNKNOWN or not self.clock.is_year_supported(now_et.year):
            return MarketDataStatus(
                ticker=sym,
                interval=inv,
                freshness=DataFreshness.UNKNOWN,
                latest_bar_timestamp=bar_ts,
                latest_bar_status=latest_bar.status,
                source=latest_bar.source,
                is_delayed=latest_bar.is_delayed,
                as_of=now_utc,
                message=f"Calendar coverage unverified: year {now_et.year} outside confirmed NYSE calendar (2026-2027).",
            )

        is_market_open = (session_state == MarketSessionState.REGULAR)

        # 2. Closed-market session-aware freshness (Fix D-1)
        if not is_market_open:
            last_sess = self.clock.get_last_completed_session(now_utc)
            if last_sess is None:
                freshness = DataFreshness.UNKNOWN
                msg = f"Cannot establish last completed trading session for {now_utc.isoformat()}."
            else:
                expected_date, exp_open, exp_close = last_sess

                if inv == "1d":
                    if bar_et.date() == expected_date:
                        freshness = DataFreshness.FRESH
                        msg = f"Market is {session_state.value}. Latest daily bar matches last completed session ({expected_date})."
                    elif bar_et.date() < expected_date:
                        freshness = DataFreshness.STALE
                        msg = f"Data is stale: latest daily bar is from {bar_et.date()}, but last completed session was {expected_date}."
                    else:
                        freshness = DataFreshness.UNKNOWN
                        msg = f"Candle date {bar_et.date()} is ahead of last completed session {expected_date}."

                elif inv == "1wk":
                    week_start = expected_date - timedelta(days=expected_date.weekday())
                    if bar_et.date() >= week_start:
                        freshness = DataFreshness.FRESH
                        msg = f"Market is {session_state.value}. Latest weekly bar matches current week."
                    else:
                        freshness = DataFreshness.STALE
                        msg = f"Data is stale: latest weekly bar is older than last completed week."

                else:
                    # Intraday intervals (1m, 5m, 15m, 30m, 1h, etc.)
                    try:
                        bar_dur = interval_to_timedelta(inv)
                    except ValueError:
                        bar_dur = timedelta(minutes=15)

                    if bar_et.date() < expected_date:
                        freshness = DataFreshness.STALE
                        msg = f"Data is stale: latest intraday bar is from {bar_et.date()}, but last completed session was {expected_date}."
                    elif bar_et.date() > expected_date:
                        age = (now_utc - bar_ts).total_seconds()
                        if age <= (self.stale_threshold_seconds + bar_dur.total_seconds()):
                            freshness = DataFreshness.DELAYED if self.provider.is_delayed else DataFreshness.FRESH
                            msg = f"Pre-market bar is current."
                        else:
                            freshness = DataFreshness.STALE
                            msg = f"Pre-market bar is stale: {age / 60:.1f} minutes old."
                    else:
                        # Bar is on expected_date: check if it reached regular session close
                        exp_close_dt = datetime.combine(expected_date, exp_close, tzinfo=EASTERN)
                        exp_latest_bar_start = exp_close_dt - bar_dur

                        if bar_et >= exp_latest_bar_start:
                            freshness = DataFreshness.FRESH
                            msg = f"Market is {session_state.value}. Latest intraday bar is from close of session {expected_date}."
                        else:
                            # Inactive, halted, or suspended ticker with no bar at session close
                            freshness = DataFreshness.STALE
                            msg = (
                                f"Data is stale: latest bar for {sym} is from {bar_et.time().isoformat()}, "
                                f"before expected session close bar ({exp_latest_bar_start.time().isoformat()})."
                            )
        else:
            # 3. Market IS open (REGULAR session)
            try:
                bar_dur = interval_to_timedelta(inv)
            except ValueError:
                bar_dur = timedelta(minutes=15)

            expected_latest_ts = now_utc - bar_dur
            age = (now_utc - bar_ts).total_seconds()

            if self.provider.is_delayed:
                # Expect ~15 min delay
                if age <= (self.stale_threshold_seconds + bar_dur.total_seconds()):
                    freshness = DataFreshness.DELAYED
                    msg = f"Data is delayed by ~15 min as expected for free {self.provider.name} endpoint."
                else:
                    freshness = DataFreshness.STALE
                    msg = f"Data is stale: latest bar is older than {age / 60:.1f} minutes during active session."
            else:
                if age <= (bar_dur.total_seconds() * 2):
                    freshness = DataFreshness.FRESH
                    msg = "Real-time market data is current."
                else:
                    freshness = DataFreshness.STALE
                    msg = f"Data is stale during active trading session."

        return MarketDataStatus(
            ticker=sym,
            interval=inv,
            freshness=freshness,
            latest_bar_timestamp=bar_ts,
            latest_bar_status=latest_bar.status,
            source=latest_bar.source,
            is_delayed=latest_bar.is_delayed,
            as_of=now_utc,
            message=msg,
        )

    def validate_candles(self, candles: Sequence[OHLCVCandle]) -> List[str]:
        """Validate a sequence of candles and return any diagnostic error messages."""
        errors: List[str] = []
        try:
            validate_candle_sequence(candles)
        except (CandleValidationError, SequenceValidationError) as e:
            errors.append(str(e))
        return errors
