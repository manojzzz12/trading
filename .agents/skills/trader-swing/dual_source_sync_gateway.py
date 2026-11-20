"""Dual-Source Real-Time Synchronization Gateway for trader-swing.

Phase 7.2.4: Offline synchronization gateway integrating Alpaca REST daily price
candles, Alpaca WebSocket intraday price updates, Yahoo consolidated daily volume,
and the DualSourceAggregator into a unified, deterministic market data pipeline.

Key Guarantees:
1. Strict Volume Isolation:
   - Yahoo consolidated volume is authoritative for all daily ADV / RVOL screening.
   - Alpaca single-exchange IEX volume is preserved strictly in .auxiliary_iex_volume
     for diagnostics; it is NEVER copied into .volume or used in liquidity screening.
2. No Silent Price Failover:
   - Alpaca price action is authoritative. If Alpaca REST is unavailable, stale, or
     invalid, the scan for that symbol aborts and transitions to FEED_UNAVAILABLE or
     FEED_DEGRADED. Yahoo prices are NEVER substituted for missing Alpaca prices.
3. Intraday Forming Stream Isolation:
   - Incoming minute bars ('b') and bar updates ('u') from Alpaca WebSocket are
     managed via FormingCandleManager.
   - Forming candles retain CandleStatus.FORMING and is_consolidated_volume=False.
   - Forming bars NEVER overwrite completed historical daily candles.
   - Forming bars NEVER receive completed Yahoo daily volume.
   - Forming bars are strictly blocked from new-long screening.
4. Session and Calendar Alignment:
   - Session dates must match exactly between Alpaca and Yahoo (D_alpaca == D_yahoo).
   - MarketClock governs regular sessions (09:30-16:00 ET), early closes (13:00 ET),
     pre-market (04:00-09:30 ET), post-market (16:00-20:00 ET), weekends, holidays,
     and DST transitions.
5. Explicit 8-State Data Readiness Matrix:
   - LIVE_PRICE_UNVERIFIED, LIVE_IEX_HYBRID, DELAYED_UNVERIFIED, FEED_DEGRADED,
     FEED_UNAVAILABLE, VOLUME_UNVERIFIABLE, FORMING_BAR, COMPLETE_BAR.
   - Each state enforces deterministic permissions for display, historical analysis,
     and new-long screening.
6. Offline and Thread-Safe Operation:
   - Operates synchronously with deterministic mock providers and fixtures.
   - Zero background threads or persistent daemon processes.
   - Zero unapproved live network calls.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from enum import Enum
import logging
import os
import sys
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

# Ensure local imports work regardless of execution context
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from market_data_models import (
    CandleStatus,
    DataFreshness,
    EASTERN,
    HISTORICAL_NYSE_EARLY_CLOSE,
    HISTORICAL_NYSE_HOLIDAYS,
    MarketClock,
    MarketDataError,
    MarketSessionState,
    NYSE_EARLY_CLOSE,
    NYSE_HOLIDAYS,
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
    StaleDataError,
    TimeoutError,
    resolve_candle_lifecycle,
    validate_candle_sequence,
    validate_single_candle,
)
from feed_adapter import (
    FeedAdapterError,
    FeedReadinessResult,
    HardReadinessFailureError,
    IncompatibleAdjustmentBasisError,
    MarketDataFeedAdapter,
    MarketDataReadinessEvaluator,
    ReadinessStatus,
    StockExclusion,
    TickerSummary,
    UnconsolidatedVolumeError,
)
from dual_source_aggregator import (
    AdjustmentMismatchError,
    AggregatorError,
    DualSourceAggregator,
    DualSourceCandle,
    MissingVolumeError,
    SessionMismatchError,
    SymbolMismatchError,
)
from providers.base import MarketDataProvider
from providers.alpaca_provider import AlpacaCandle, AlpacaMarketDataProvider
from providers.alpaca_websocket import (
    AlpacaStreamBar,
    AlpacaStreamQuote,
    AlpacaStreamTrade,
    AlpacaWebSocketClient,
    FormingCandleManager,
    WebSocketState,
)

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# 8 Approved Data Readiness States
# -----------------------------------------------------------------------------
class FeedState(str, Enum):
    """Explicit 8-state market data feed classification."""
    LIVE_PRICE_UNVERIFIED = "LIVE_PRICE_UNVERIFIED"
    LIVE_IEX_HYBRID = "LIVE_IEX_HYBRID"
    DELAYED_UNVERIFIED = "DELAYED_UNVERIFIED"
    FEED_DEGRADED = "FEED_DEGRADED"
    FEED_UNAVAILABLE = "FEED_UNAVAILABLE"
    VOLUME_UNVERIFIABLE = "VOLUME_UNVERIFIABLE"
    FORMING_BAR = "FORMING_BAR"
    COMPLETE_BAR = "COMPLETE_BAR"


@dataclass(frozen=True)
class StatePermission:
    """Operational permissions associated with a FeedState."""
    permits_display: bool
    permits_historical_analysis: bool
    permits_new_long_screening: bool


# Exact permission matrix defined in Phase 7.2.1 Section 6 / Phase 7.2.1.1
STATE_PERMISSIONS: Dict[FeedState, StatePermission] = {
    FeedState.COMPLETE_BAR: StatePermission(
        permits_display=True,
        permits_historical_analysis=True,
        permits_new_long_screening=True,  # Conditioned on verified consolidated volume
    ),
    FeedState.FORMING_BAR: StatePermission(
        permits_display=True,
        permits_historical_analysis=False,
        permits_new_long_screening=False,  # Strictly blocked
    ),
    FeedState.LIVE_IEX_HYBRID: StatePermission(
        permits_display=True,
        permits_historical_analysis=True,   # For completed bars
        permits_new_long_screening=True,   # For completed bars only
    ),
    FeedState.LIVE_PRICE_UNVERIFIED: StatePermission(
        permits_display=True,
        permits_historical_analysis=False,
        permits_new_long_screening=False,  # Strictly blocked
    ),
    FeedState.DELAYED_UNVERIFIED: StatePermission(
        permits_display=True,
        permits_historical_analysis=True,
        permits_new_long_screening=True,   # Research baseline
    ),
    FeedState.FEED_DEGRADED: StatePermission(
        permits_display=True,
        permits_historical_analysis=False,
        permits_new_long_screening=False,  # Strictly blocked
    ),
    FeedState.FEED_UNAVAILABLE: StatePermission(
        permits_display=True,
        permits_historical_analysis=False,
        permits_new_long_screening=False,  # Scan aborted
    ),
    FeedState.VOLUME_UNVERIFIABLE: StatePermission(
        permits_display=True,
        permits_historical_analysis=False,
        permits_new_long_screening=False,  # Strictly blocked
    ),
}


# -----------------------------------------------------------------------------
# Structured Result Models
# -----------------------------------------------------------------------------
@dataclass
class SessionTimingInfo:
    """Market session timing analysis derived from MarketClock."""
    as_of: datetime
    as_of_eastern: datetime
    session_date: date
    session_state: MarketSessionState
    is_market_open: bool
    open_time: Optional[time]
    close_time: Optional[time]
    is_early_close: bool
    is_holiday: bool
    holiday_name: Optional[str]
    is_trading_day: bool


@dataclass
class ScreeningEligibilityResult:
    """Structured evaluation of whether a symbol's feed permits new-long screening."""
    symbol: str
    is_eligible: bool
    state: FeedState
    reason: str
    is_consolidated_volume: bool
    session_date: Optional[date] = None
    candle_status: Optional[CandleStatus] = None
    bar_count: int = 0


@dataclass
class DailySyncResult:
    """Result of completed daily candle synchronization for a single ticker."""
    symbol: str
    state: FeedState
    success: bool
    candles: List[DualSourceCandle] = field(default_factory=list)
    error_message: Optional[str] = None
    alpaca_candle_count: int = 0
    yahoo_candle_count: int = 0
    merged_candle_count: int = 0
    session_date: Optional[date] = None
    provenance: Dict[str, str] = field(default_factory=dict)


# -----------------------------------------------------------------------------
# Gateway Exception Hierarchy
# -----------------------------------------------------------------------------
class GatewayError(MarketDataError):
    """Base exception for synchronization gateway operations."""
    pass


class GatewayFeedUnavailableError(GatewayError):
    """Raised when Alpaca REST or price provider is unavailable."""
    pass


class GatewayFeedDegradedError(GatewayError):
    """Raised when market data feed is degraded (e.g. rate limit, latency, mismatch)."""
    pass


class GatewayVolumeUnverifiableError(GatewayError):
    """Raised when Yahoo consolidated volume is missing, zero, or unverifiable."""
    pass


class GatewaySessionMismatchError(GatewayError):
    """Raised when Alpaca price candle and Yahoo volume candle dates diverge."""
    pass


class GatewayIncompleteDataError(GatewayError):
    """Raised when attempting to screen or analyze forming or incomplete data."""
    pass


# -----------------------------------------------------------------------------
# Dual-Source Real-Time Synchronization Gateway
# -----------------------------------------------------------------------------
class DualSourceSyncGateway:
    """Unified offline synchronization gateway for Trader-Swing.

    Coordinates:
    - Alpaca REST daily price candles (authoritative price OHLC).
    - Alpaca WebSocket intraday price updates (forming candles via FormingCandleManager).
    - Yahoo consolidated daily volume (authoritative consolidated composite tape volume).
    - DualSourceAggregator (merges Alpaca prices with Yahoo consolidated volume).
    - MarketDataReadinessEvaluator (validates full series integrity and universe readiness).
    - MarketClock (resolves session state, early closes, holidays, DST).
    """

    def __init__(
        self,
        alpaca_provider: Optional[MarketDataProvider] = None,
        yahoo_provider: Optional[MarketDataProvider] = None,
        ws_client: Optional[AlpacaWebSocketClient] = None,
        forming_manager: Optional[FormingCandleManager] = None,
        aggregator: Optional[DualSourceAggregator] = None,
        evaluator: Optional[MarketDataReadinessEvaluator] = None,
        clock: Optional[MarketClock] = None,
    ):
        self.clock = clock or MarketClock()
        self.alpaca_provider = alpaca_provider
        self.yahoo_provider = yahoo_provider
        self.aggregator = aggregator or DualSourceAggregator(
            alpaca_provider=alpaca_provider,
            yahoo_provider=yahoo_provider,
            clock=self.clock,
        )
        self.evaluator = evaluator or MarketDataReadinessEvaluator(clock=self.clock)
        self.ws_client = ws_client
        self.forming_manager = forming_manager or (
            getattr(ws_client, "forming_candle_manager", None)
            or getattr(ws_client, "forming_manager", None)
            or FormingCandleManager()
        )

        # In-memory tracking per symbol
        self._feed_states: Dict[str, FeedState] = {}
        self._sync_errors: Dict[str, str] = {}
        self._completed_daily_candles: Dict[str, List[DualSourceCandle]] = {}
        self._latest_sync_times: Dict[str, datetime] = {}

    # -------------------------------------------------------------------------
    # State and Permission Management
    # -------------------------------------------------------------------------
    def get_feed_state(self, symbol: str) -> FeedState:
        """Return the current feed state for a symbol, defaulting to FEED_UNAVAILABLE."""
        return self._feed_states.get(symbol.upper().strip(), FeedState.FEED_UNAVAILABLE)

    def set_feed_state(
        self,
        symbol: str,
        state: FeedState,
        error_message: Optional[str] = None,
    ) -> None:
        """Update the feed state and optional error message for a symbol."""
        sym_clean = symbol.upper().strip()
        self._feed_states[sym_clean] = state
        if error_message:
            self._sync_errors[sym_clean] = error_message
        elif sym_clean in self._sync_errors and state in (FeedState.LIVE_IEX_HYBRID, FeedState.COMPLETE_BAR):
            del self._sync_errors[sym_clean]

    def get_sync_error(self, symbol: str) -> Optional[str]:
        """Return the latest recorded synchronization error for a symbol."""
        return self._sync_errors.get(symbol.upper().strip())

    @staticmethod
    def get_state_permissions(state: FeedState) -> StatePermission:
        """Return the operational permissions for an approved FeedState."""
        return STATE_PERMISSIONS[state]

    # -------------------------------------------------------------------------
    # Session Timing Evaluation
    # -------------------------------------------------------------------------
    def evaluate_session_timing(
        self,
        dt: Optional[datetime] = None,
    ) -> SessionTimingInfo:
        """Evaluate session state, market open status, early closes, and holidays via MarketClock."""
        now_utc = to_utc(dt) if dt is not None else datetime.now(UTC)
        et = to_eastern(now_utc)
        curr_date = et.date()
        iso_str = curr_date.isoformat()

        # Check trading day and holiday
        is_trading_day, day_reason = self.clock.is_trading_day(curr_date)
        if not is_trading_day and self.clock.is_historical_year_supported(curr_date.year):
            is_trading_day, day_reason = self.clock.is_historical_trading_day(curr_date)

        is_holiday = "Holiday:" in day_reason
        holiday_name = day_reason.split("Holiday: ")[-1] if is_holiday else None

        # Check early close
        is_early_close = (
            iso_str in NYSE_EARLY_CLOSE or iso_str in HISTORICAL_NYSE_EARLY_CLOSE
        )

        # Get session hours
        sess = self.clock.get_session(curr_date)
        if sess is None and self.clock.is_historical_year_supported(curr_date.year):
            sess = self.clock.get_historical_session(curr_date)

        open_time = sess[0] if sess else None
        close_time = sess[1] if sess else None

        # Resolve session state
        session_state = self.clock.get_session_state(now_utc)
        is_market_open = (session_state == MarketSessionState.REGULAR)

        return SessionTimingInfo(
            as_of=now_utc,
            as_of_eastern=et,
            session_date=curr_date,
            session_state=session_state,
            is_market_open=is_market_open,
            open_time=open_time,
            close_time=close_time,
            is_early_close=is_early_close,
            is_holiday=is_holiday,
            holiday_name=holiday_name,
            is_trading_day=is_trading_day,
        )

    # -------------------------------------------------------------------------
    # Completed Daily Candle Synchronization
    # -------------------------------------------------------------------------
    def synchronize_completed_daily(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        current_time: Optional[datetime] = None,
    ) -> DailySyncResult:
        """Synchronize Alpaca price candles with Yahoo consolidated volume.

        Strict Requirements:
        1. Authoritative price action from Alpaca IEX. If Alpaca fails, abort scan.
           Never substitute Yahoo price data (no silent failover).
        2. Authoritative consolidated volume from Yahoo Finance composite tape.
           If Yahoo volume is missing, non-positive, or unconsolidated, reject symbol.
        3. Exact session date match: D_alpaca == D_yahoo.
        4. Strict IEX volume protection: Alpaca IEX volume is preserved in .auxiliary_iex_volume;
           never copied into .volume.
        5. Session completion validation: Only completed sessions become LIVE_IEX_HYBRID / COMPLETE_BAR.
        """
        sym_clean = symbol.upper().strip()
        now_utc = to_utc(current_time) if current_time is not None else datetime.now(UTC)

        # 1. Step 1: Alpaca price retrieval (authoritative price action)
        if self.alpaca_provider is None:
            err_msg = f"Alpaca provider unconfigured; cannot fetch price data for {sym_clean}."
            self.set_feed_state(sym_clean, FeedState.FEED_UNAVAILABLE, err_msg)
            return DailySyncResult(
                symbol=sym_clean,
                state=FeedState.FEED_UNAVAILABLE,
                success=False,
                error_message=err_msg,
            )

        try:
            alpaca_bars = self.alpaca_provider.fetch_historical_bars(
                ticker=sym_clean,
                interval="1d",
                start=start,
                end=end,
                current_time=now_utc,
            )
            if not alpaca_bars:
                err_msg = f"Alpaca returned empty bar series for {sym_clean}."
                self.set_feed_state(sym_clean, FeedState.FEED_UNAVAILABLE, err_msg)
                return DailySyncResult(
                    symbol=sym_clean,
                    state=FeedState.FEED_UNAVAILABLE,
                    success=False,
                    error_message=err_msg,
                )
        except RateLimitError as e:
            err_msg = f"Alpaca rate limit exceeded during price fetch for {sym_clean}: {e}"
            self.set_feed_state(sym_clean, FeedState.FEED_DEGRADED, err_msg)
            return DailySyncResult(
                symbol=sym_clean,
                state=FeedState.FEED_DEGRADED,
                success=False,
                error_message=err_msg,
            )
        except (ProviderUnavailableError, TimeoutError, NoDataError, ProviderError, Exception) as e:
            # Phase 7.2.1.1 Mandatory Rule: NEVER failover to Yahoo price! Abort scan.
            err_msg = f"Alpaca price fetch failed for {sym_clean}: {e}. Silent failover to Yahoo price is prohibited."
            self.set_feed_state(sym_clean, FeedState.FEED_UNAVAILABLE, err_msg)
            return DailySyncResult(
                symbol=sym_clean,
                state=FeedState.FEED_UNAVAILABLE,
                success=False,
                error_message=err_msg,
            )

        # 2. Step 2: Yahoo volume retrieval (authoritative consolidated tape volume)
        if self.yahoo_provider is None:
            err_msg = f"Yahoo provider unconfigured; missing consolidated volume for {sym_clean}."
            self.set_feed_state(sym_clean, FeedState.VOLUME_UNVERIFIABLE, err_msg)
            return DailySyncResult(
                symbol=sym_clean,
                state=FeedState.VOLUME_UNVERIFIABLE,
                success=False,
                alpaca_candle_count=len(alpaca_bars),
                error_message=err_msg,
            )

        try:
            yahoo_bars = self.yahoo_provider.fetch_historical_bars(
                ticker=sym_clean,
                interval="1d",
                start=start,
                end=end,
                current_time=now_utc,
            )
            if not yahoo_bars:
                err_msg = f"Yahoo returned empty candle series for {sym_clean}; missing consolidated volume."
                self.set_feed_state(sym_clean, FeedState.VOLUME_UNVERIFIABLE, err_msg)
                return DailySyncResult(
                    symbol=sym_clean,
                    state=FeedState.VOLUME_UNVERIFIABLE,
                    success=False,
                    alpaca_candle_count=len(alpaca_bars),
                    error_message=err_msg,
                )
        except Exception as e:
            err_msg = f"Yahoo consolidated volume fetch failed for {sym_clean}: {e}"
            self.set_feed_state(sym_clean, FeedState.VOLUME_UNVERIFIABLE, err_msg)
            return DailySyncResult(
                symbol=sym_clean,
                state=FeedState.VOLUME_UNVERIFIABLE,
                success=False,
                alpaca_candle_count=len(alpaca_bars),
                error_message=err_msg,
            )

        # 3. Step 3: Date Alignment Verification
        last_a = alpaca_bars[-1]
        last_y = yahoo_bars[-1]
        a_date = getattr(last_a, "session_date", None) or to_eastern(last_a.timestamp).date()
        y_date = getattr(last_y, "session_date", None) or to_eastern(last_y.timestamp).date()

        if a_date != y_date:
            err_msg = (
                f"Session date mismatch for {sym_clean}: Alpaca latest session={a_date}, "
                f"Yahoo latest session={y_date}. Screening blocked."
            )
            self.set_feed_state(sym_clean, FeedState.FEED_DEGRADED, err_msg)
            return DailySyncResult(
                symbol=sym_clean,
                state=FeedState.FEED_DEGRADED,
                success=False,
                alpaca_candle_count=len(alpaca_bars),
                yahoo_candle_count=len(yahoo_bars),
                error_message=err_msg,
            )

        # 4. Step 4: Validate positive consolidated volume
        if last_y.volume is None or last_y.volume <= 0:
            err_msg = (
                f"Yahoo consolidated volume for {sym_clean} on {y_date} is invalid or non-positive "
                f"({last_y.volume}). Cannot enter liquidity calculations."
            )
            self.set_feed_state(sym_clean, FeedState.VOLUME_UNVERIFIABLE, err_msg)
            return DailySyncResult(
                symbol=sym_clean,
                state=FeedState.VOLUME_UNVERIFIABLE,
                success=False,
                alpaca_candle_count=len(alpaca_bars),
                yahoo_candle_count=len(yahoo_bars),
                error_message=err_msg,
            )

        # 5. Step 5: Merge via DualSourceAggregator
        try:
            merged_candles = self.aggregator.merge_candle_series(
                ticker=sym_clean,
                alpaca_candles=alpaca_bars,
                yahoo_candles=yahoo_bars,
                current_time=now_utc,
            )
        except SessionMismatchError as e:
            err_msg = f"Session mismatch during aggregation for {sym_clean}: {e}"
            self.set_feed_state(sym_clean, FeedState.FEED_DEGRADED, err_msg)
            return DailySyncResult(
                symbol=sym_clean,
                state=FeedState.FEED_DEGRADED,
                success=False,
                alpaca_candle_count=len(alpaca_bars),
                yahoo_candle_count=len(yahoo_bars),
                error_message=err_msg,
            )
        except (MissingVolumeError, InvalidVolumeError) as e:
            err_msg = f"Volume validation failed during aggregation for {sym_clean}: {e}"
            self.set_feed_state(sym_clean, FeedState.VOLUME_UNVERIFIABLE, err_msg)
            return DailySyncResult(
                symbol=sym_clean,
                state=FeedState.VOLUME_UNVERIFIABLE,
                success=False,
                alpaca_candle_count=len(alpaca_bars),
                yahoo_candle_count=len(yahoo_bars),
                error_message=err_msg,
            )
        except (InvalidPriceError, CandleValidationError, SequenceValidationError, Exception) as e:
            err_msg = f"Candle validation failed during aggregation for {sym_clean}: {e}"
            self.set_feed_state(sym_clean, FeedState.FEED_DEGRADED, err_msg)
            return DailySyncResult(
                symbol=sym_clean,
                state=FeedState.FEED_DEGRADED,
                success=False,
                alpaca_candle_count=len(alpaca_bars),
                yahoo_candle_count=len(yahoo_bars),
                error_message=err_msg,
            )

        # 6. Step 6: Verify completion of latest session
        latest_candle = merged_candles[-1]
        resolved_lifecycle = resolve_candle_lifecycle(latest_candle, current_time=now_utc)

        if resolved_lifecycle != CandleStatus.COMPLETE:
            # The session has not concluded; this candle is still forming
            self.set_feed_state(sym_clean, FeedState.FORMING_BAR)
            return DailySyncResult(
                symbol=sym_clean,
                state=FeedState.FORMING_BAR,
                success=True,
                candles=merged_candles,
                alpaca_candle_count=len(alpaca_bars),
                yahoo_candle_count=len(yahoo_bars),
                merged_candle_count=len(merged_candles),
                session_date=a_date,
                provenance={
                    "price_source": "ALPACA_IEX",
                    "volume_source": "YAHOO_CONSOLIDATED",
                    "feed_classification": "FORMING_INTRADAY",
                },
            )

        # Completed candle: Store in cache and set LIVE_IEX_HYBRID
        self._completed_daily_candles[sym_clean] = merged_candles
        self._latest_sync_times[sym_clean] = now_utc
        self.set_feed_state(sym_clean, FeedState.LIVE_IEX_HYBRID)

        return DailySyncResult(
            symbol=sym_clean,
            state=FeedState.LIVE_IEX_HYBRID,
            success=True,
            candles=merged_candles,
            alpaca_candle_count=len(alpaca_bars),
            yahoo_candle_count=len(yahoo_bars),
            merged_candle_count=len(merged_candles),
            session_date=a_date,
            provenance={
                "price_source": "ALPACA_IEX",
                "volume_source": "YAHOO_CONSOLIDATED",
                "feed_classification": "DUAL_SOURCE_HYBRID",
            },
        )

    # -------------------------------------------------------------------------
    # Intraday Forming Bar Updates
    # -------------------------------------------------------------------------
    def process_intraday_stream_bar(
        self,
        bar: Union[AlpacaStreamBar, Dict[str, Any]],
        current_time: Optional[datetime] = None,
    ) -> AlpacaCandle:
        """Route incoming Alpaca WebSocket minute bar updates to FormingCandleManager.

        Strict Invariants:
        1. Maintains forming intraday candle in-place without modifying historical daily candles.
        2. Incoming streaming volume remains single-exchange IEX volume (is_consolidated_volume=False).
        3. Never attaches completed Yahoo daily volume to a forming intraday bar.
        4. Transitions the symbol's feed state to FORMING_BAR.
        5. Rejects out-of-order bars and safely deduplicates duplicate minute bars.
        """
        now_utc = to_utc(current_time) if current_time is not None else datetime.now(UTC)

        # Parse message if dict
        stream_bar: AlpacaStreamBar
        if isinstance(bar, dict):
            msg_type = bar.get("T", "b")
            stream_bar = AlpacaStreamBar(
                symbol=str(bar.get("S", "")).upper().strip(),
                open=Decimal(str(bar["o"])),
                high=Decimal(str(bar["h"])),
                low=Decimal(str(bar["l"])),
                close=Decimal(str(bar["c"])),
                volume=int(bar["v"]),
                timestamp=parse_iso_timestamp(str(bar["t"])),
                trade_count=int(bar["n"]) if "n" in bar and bar["n"] is not None else None,
                vwap=Decimal(str(bar["vw"])) if "vw" in bar and bar["vw"] is not None else None,
                is_updated=(msg_type == "u"),
            )
        elif isinstance(bar, AlpacaStreamBar):
            stream_bar = bar
        else:
            raise GatewayError(f"Unsupported stream bar type: {type(bar)}")

        sym_clean = stream_bar.symbol.upper().strip()

        # Route through FormingCandleManager
        forming_candle, is_new = self.forming_manager.process_bar(
            stream_bar, current_time=now_utc
        )

        # Mark symbol feed state as FORMING_BAR
        self.set_feed_state(sym_clean, FeedState.FORMING_BAR)

        # Enforce that forming candle NEVER touches completed daily series
        assert forming_candle.is_consolidated_volume is False, (
            "Invariant violation: forming intraday candle must declare is_consolidated_volume=False."
        )
        assert forming_candle.status == CandleStatus.FORMING, (
            "Invariant violation: forming intraday candle must maintain CandleStatus.FORMING."
        )

        return forming_candle

    def get_forming_candle(self, symbol: str) -> Optional[AlpacaCandle]:
        """Return the current active forming candle for a symbol."""
        return self.forming_manager.get_forming_candle(symbol.upper().strip())

    def get_forming_history(self, symbol: str) -> List[AlpacaCandle]:
        """Return archived forming candle history for a symbol."""
        return self.forming_manager.get_forming_history(symbol.upper().strip())

    def get_completed_daily_candles(self, symbol: str) -> List[DualSourceCandle]:
        """Return cached completed daily candles for a symbol."""
        return list(self._completed_daily_candles.get(symbol.upper().strip(), []))

    # -------------------------------------------------------------------------
    # Screening Eligibility Gate
    # -------------------------------------------------------------------------
    def get_screening_eligibility(
        self,
        symbol: str,
        current_time: Optional[datetime] = None,
    ) -> ScreeningEligibilityResult:
        """Evaluate whether a symbol satisfies all preconditions for new-long screening.

        Strict Preconditions:
        1. FeedState must permit new-long screening (LIVE_IEX_HYBRID, COMPLETE_BAR, DELAYED_UNVERIFIED).
        2. Symbol must have synchronized completed daily candles.
        3. Latest candle must have status == CandleStatus.COMPLETE.
        4. Latest candle must have verified consolidated volume (is_consolidated_volume=True).
        5. Latest candle volume must be strictly positive (> 0).
        6. All forming bars, unverified prices, degraded feeds, or volume unverifiable states are BLOCKED.
        """
        sym_clean = symbol.upper().strip()
        state = self.get_feed_state(sym_clean)
        permissions = STATE_PERMISSIONS[state]

        # 1. State-level permission check
        if not permissions.permits_new_long_screening:
            return ScreeningEligibilityResult(
                symbol=sym_clean,
                is_eligible=False,
                state=state,
                reason=f"Feed state '{state.value}' strictly blocks new-long screening.",
                is_consolidated_volume=False,
            )

        # 2. Check for synchronized candle series
        candles = self._completed_daily_candles.get(sym_clean)
        if not candles:
            return ScreeningEligibilityResult(
                symbol=sym_clean,
                is_eligible=False,
                state=state,
                reason=f"No synchronized completed daily candles available for {sym_clean}.",
                is_consolidated_volume=False,
            )

        # 3. Inspect latest candle properties
        latest = candles[-1]
        session_d = getattr(latest, "session_date", None) or to_eastern(latest.timestamp).date()

        if latest.status != CandleStatus.COMPLETE:
            return ScreeningEligibilityResult(
                symbol=sym_clean,
                is_eligible=False,
                state=FeedState.FORMING_BAR,
                reason=f"Latest candle on {session_d} is '{latest.status.value}'; only COMPLETE candles are eligible.",
                is_consolidated_volume=False,
                session_date=session_d,
                candle_status=latest.status,
                bar_count=len(candles),
            )

        if not getattr(latest, "is_consolidated_volume", False):
            return ScreeningEligibilityResult(
                symbol=sym_clean,
                is_eligible=False,
                state=FeedState.VOLUME_UNVERIFIABLE,
                reason=f"Latest candle on {session_d} has unconsolidated volume (IEX). Consolidated SIP volume required.",
                is_consolidated_volume=False,
                session_date=session_d,
                candle_status=latest.status,
                bar_count=len(candles),
            )

        if latest.volume is None or latest.volume <= 0:
            return ScreeningEligibilityResult(
                symbol=sym_clean,
                is_eligible=False,
                state=FeedState.VOLUME_UNVERIFIABLE,
                reason=f"Latest candle on {session_d} has invalid non-positive volume ({latest.volume}).",
                is_consolidated_volume=True,
                session_date=session_d,
                candle_status=latest.status,
                bar_count=len(candles),
            )

        # All gates passed
        return ScreeningEligibilityResult(
            symbol=sym_clean,
            is_eligible=True,
            state=state,
            reason="Verified dual-source hybrid candle with authoritative consolidated volume.",
            is_consolidated_volume=True,
            session_date=session_d,
            candle_status=latest.status,
            bar_count=len(candles),
        )

    # -------------------------------------------------------------------------
    # Multi-Ticker Universe Synchronization & Readiness
    # -------------------------------------------------------------------------
    def synchronize_universe(
        self,
        stock_universe: Sequence[str],
        start: datetime,
        end: datetime,
        current_time: Optional[datetime] = None,
    ) -> FeedReadinessResult:
        """Synchronize proxies and candidate stock universe, evaluating comprehensive readiness."""
        now_utc = to_utc(current_time) if current_time is not None else datetime.now(UTC)

        proxy_map: Dict[str, List[OHLCVCandle]] = {}
        stock_map: Dict[str, List[OHLCVCandle]] = {}
        initial_warnings: List[str] = []

        # 1. Synchronize proxies (SPY mandatory, QQQ, IWM secondary)
        for proxy in ("SPY", "QQQ", "IWM"):
            res = self.synchronize_completed_daily(
                symbol=proxy, start=start, end=end, current_time=now_utc
            )
            if res.success and res.candles:
                proxy_map[proxy] = list(res.candles)
            else:
                initial_warnings.append(
                    f"Failed to synchronize proxy {proxy}: {res.error_message or res.state.value}"
                )

        # 2. Synchronize candidate stock universe
        for stock in stock_universe:
            sym = stock.upper().strip()
            if sym in proxy_map:
                stock_map[sym] = proxy_map[sym]
                continue

            res = self.synchronize_completed_daily(
                symbol=sym, start=start, end=end, current_time=now_utc
            )
            if res.success and res.candles:
                stock_map[sym] = list(res.candles)
            else:
                stock_map[sym] = []
                initial_warnings.append(
                    f"Failed to synchronize stock {sym}: {res.error_message or res.state.value}"
                )

        # 3. Evaluate readiness via MarketDataReadinessEvaluator
        provider_meta = {
            "gateway": "DualSourceSyncGateway",
            "price_source": "Alpaca IEX",
            "volume_source": "Yahoo Consolidated",
            "is_offline_mock": True,
        }

        return self.evaluator.evaluate_readiness(
            proxy_candles_map=proxy_map,
            stock_candles_map=stock_map,
            current_time=now_utc,
            provider_metadata=provider_meta,
            initial_warnings=initial_warnings,
        )

    # -------------------------------------------------------------------------
    # WebSocket Reconnection Bridge
    # -------------------------------------------------------------------------
    def reconnect_websocket(self) -> bool:
        """Trigger offline client reconnection with subscription restoration."""
        if self.ws_client is None:
            logger.warning("No WebSocket client configured on sync gateway.")
            return False
        return self.ws_client.reconnect()
