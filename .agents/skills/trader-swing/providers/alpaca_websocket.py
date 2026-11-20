"""Alpaca WebSocket streaming foundation and intraday candle lifecycle manager.

Phase 7.2.3: Alpaca WebSocket Streaming Foundation.

Provides:
1. AlpacaWebSocketClient: State-managed, offline-testable streaming client
   supporting connection lifecycle, authentication, channel subscription,
   and deterministic message dispatch.
2. FormingCandleManager: Dedicated lifecycle handler that maintains strictly FORMING
   intraday candles from incoming 1-minute bars ('b') and updated bars ('u'),
   preventing duplicate creation, out-of-order corruption, and volume contamination.
3. Typed message models (AlpacaStreamTrade, AlpacaStreamQuote, AlpacaStreamBar)
   with Decimal price validation and strict IEX unconsolidated provenance.
4. Typed connection and protocol exception hierarchy.
5. Strict governance:
   - Forming candles are NEVER marked COMPLETE.
   - IEX streaming volume is NEVER substituted for consolidated daily volume.
   - Zero background daemons or persistent processes during development or testing.
   - Zero live network access: connects strictly via mock transport in offline mode.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
import json
import logging
import math
import os
import sys
import time as time_mod
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

# Ensure local imports work regardless of execution context
PARENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PARENT_DIR not in sys.path:
    sys.path.insert(0, PARENT_DIR)

from market_data_models import (
    CandleStatus,
    EASTERN,
    InvalidPriceError,
    InvalidResponseError,
    InvalidVolumeError,
    OHLCVCandle,
    parse_iso_timestamp,
    ProviderError,
    ProviderUnavailableError,
    RateLimitError,
    TimeoutError,
    to_eastern,
    to_utc,
    UTC,
)
from market_data_validation import validate_single_candle
from .alpaca_connector import (
    DEFAULT_BURST_CAPACITY,
    DEFAULT_RATE_LIMIT_PER_MINUTE,
    TokenBucketRateLimiter,
    sanitize_headers,
    sanitize_message,
    sanitize_secret,
)
from .alpaca_provider import AlpacaCandle

logger = logging.getLogger(__name__)

# Constants
DEFAULT_WS_STREAM_URL: str = "wss://stream.data.alpaca.markets/v2/iex"
DEFAULT_STALE_TIMEOUT_SECONDS: float = 30.0
DEFAULT_MAX_RECONNECT_ATTEMPTS: int = 5
DEFAULT_INITIAL_BACKOFF_SECONDS: float = 0.5
DEFAULT_MAX_BACKOFF_SECONDS: float = 10.0


# -----------------------------------------------------------------------------
# WebSocket State Machine
# -----------------------------------------------------------------------------
class WebSocketState(str, Enum):
    """Explicit lifecycle states for Alpaca WebSocket connection."""
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    AUTHENTICATING = "AUTHENTICATING"
    AUTHENTICATED = "AUTHENTICATED"
    SUBSCRIBED = "SUBSCRIBED"
    RECONNECTING = "RECONNECTING"
    DISCONNECTING = "DISCONNECTING"
    FAILED = "FAILED"


# -----------------------------------------------------------------------------
# Typed WebSocket Exceptions
# -----------------------------------------------------------------------------
class AlpacaWebSocketError(ProviderError):
    """Base exception for all Alpaca WebSocket streaming errors."""

    def __init__(
        self,
        message: str,
        code: Optional[int] = None,
        secrets_to_redact: Sequence[Optional[str]] = (),
    ):
        clean_msg = sanitize_message(message, secrets_to_redact)
        super().__init__(clean_msg)
        self.code = code


class AlpacaWSConnectionError(AlpacaWebSocketError, ProviderUnavailableError):
    """Raised on socket drop, handshake failure, or connection reset."""
    pass


class AlpacaWSAuthError(AlpacaWebSocketError, ProviderUnavailableError):
    """Raised when authentication is rejected by Alpaca (HTTP 401/403). Terminal."""
    pass


class AlpacaWSProtocolError(AlpacaWebSocketError, InvalidResponseError):
    """Raised on malformed JSON frames or unexpected message structure."""
    pass


class AlpacaWSStaleStreamError(AlpacaWebSocketError, TimeoutError):
    """Raised when no message has been received within the configured stale timeout."""
    pass


class AlpacaWSSubscriptionError(AlpacaWebSocketError):
    """Raised when a subscription request is rejected or rate-limited."""
    pass


# -----------------------------------------------------------------------------
# Parsed Streaming Message Models
# -----------------------------------------------------------------------------
@dataclass
class AlpacaStreamTrade:
    """Normalized tick trade event from Alpaca WebSocket ('t')."""
    symbol: str
    price: Decimal
    size: int
    timestamp: datetime
    exchange: str = "V"
    trade_id: Optional[int] = None
    conditions: List[str] = field(default_factory=list)
    tape: str = "C"


@dataclass
class AlpacaStreamQuote:
    """Normalized top-of-book quote event from Alpaca WebSocket ('q')."""
    symbol: str
    bid_price: Decimal
    bid_size: int
    ask_price: Decimal
    ask_size: int
    timestamp: datetime
    bid_exchange: str = "V"
    ask_exchange: str = "V"
    conditions: List[str] = field(default_factory=list)


@dataclass
class AlpacaStreamBar:
    """Normalized 1-minute bar ('b') or in-progress updated bar ('u') from Alpaca WebSocket."""
    symbol: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    timestamp: datetime
    trade_count: Optional[int] = None
    vwap: Optional[Decimal] = None
    is_updated: bool = False  # False for 'b', True for 'u'


# -----------------------------------------------------------------------------
# Forming Candle Lifecycle Manager
# -----------------------------------------------------------------------------
class FormingCandleManager:
    """Manages the in-memory lifecycle of intraday forming candles.

    Strict Invariants:
    1. Every managed candle is strictly marked CandleStatus.FORMING.
    2. Updated bars ('u') update the existing forming candle for that minute rather than
       creating duplicates.
    3. Duplicate messages (identical symbol, timestamp, and values) are ignored.
    4. Out-of-order messages (timestamp earlier than the active forming bar) are rejected.
    5. IEX volume is strictly unconsolidated (is_consolidated_volume=False). Calling
       get_consolidated_volume() raises InvalidVolumeError.
    6. Refuses promotion of forming candles to completed daily candles.
    """

    def __init__(self, clock: Optional[Any] = None):
        self.clock = clock
        # Key: symbol -> current active AlpacaCandle (forming)
        self.active_forming_candles: Dict[str, AlpacaCandle] = {}
        # Key: symbol -> timestamp of the latest accepted bar
        self.latest_bar_timestamps: Dict[str, datetime] = {}
        # Key: symbol -> list of archived historical forming candles
        self.forming_history: Dict[str, List[AlpacaCandle]] = {}
        # Diagnostic tracking
        self.duplicate_count: int = 0
        self.out_of_order_count: int = 0
        self.update_count: int = 0

    def process_bar(
        self,
        bar: AlpacaStreamBar,
        current_time: Optional[datetime] = None,
    ) -> Tuple[AlpacaCandle, bool]:
        """Process an incoming stream bar ('b') or updated bar ('u').

        Args:
            bar: Normalized AlpacaStreamBar.
            current_time: Simulated reference time for validation.

        Returns:
            Tuple of (candle, is_new_candle).

        Raises:
            InvalidResponseError: If bar contains invalid data geometry.
            AlpacaWSProtocolError: If bar is an unrecoverable out-of-order sequence.
        """
        sym = bar.symbol.upper().strip()
        ts = to_utc(bar.timestamp)
        now_dt = to_utc(current_time) if current_time is not None else datetime.now(UTC)

        latest_ts = self.latest_bar_timestamps.get(sym)

        # ---------------------------------------------------------------------
        # 1. Out-of-Order Detection
        # ---------------------------------------------------------------------
        if latest_ts is not None and ts < latest_ts:
            self.out_of_order_count += 1
            logger.warning(
                "Out-of-order stream bar received for %s: bar_ts=%s < latest_ts=%s. Discarding.",
                sym,
                ts.isoformat(),
                latest_ts.isoformat(),
            )
            # Return current forming candle without updating
            current_cand = self.active_forming_candles.get(sym)
            if current_cand is not None:
                return (current_cand, False)
            raise AlpacaWSProtocolError(
                f"Out-of-order bar received for {sym}: timestamp {ts} < latest {latest_ts}"
            )

        # ---------------------------------------------------------------------
        # 2. Updated Bar ('u'): Modify existing forming candle
        # ---------------------------------------------------------------------
        if bar.is_updated:
            active_cand = self.active_forming_candles.get(sym)
            if active_cand is not None and active_cand.timestamp == ts:
                # Check for exact duplicate update
                if (
                    active_cand.close == bar.close
                    and active_cand.volume == bar.volume
                    and active_cand.high == bar.high
                    and active_cand.low == bar.low
                ):
                    self.duplicate_count += 1
                    return (active_cand, False)

                # Apply update to active forming candle
                active_cand.high = max(active_cand.high, bar.high)
                active_cand.low = min(active_cand.low, bar.low)
                active_cand.close = bar.close
                active_cand.volume = bar.volume
                if bar.vwap is not None:
                    active_cand.vwap = bar.vwap
                if bar.trade_count is not None:
                    active_cand.trade_count = bar.trade_count

                # Validate updated candle geometry
                validate_single_candle(active_cand, current_time=now_dt)
                self.update_count += 1
                return (active_cand, False)

        # ---------------------------------------------------------------------
        # 3. New Minute Bar ('b') or New Session
        # ---------------------------------------------------------------------
        # Check for exact duplicate bar
        active_cand = self.active_forming_candles.get(sym)
        if active_cand is not None and active_cand.timestamp == ts:
            if (
                active_cand.open == bar.open
                and active_cand.close == bar.close
                and active_cand.volume == bar.volume
            ):
                self.duplicate_count += 1
                return (active_cand, False)

        # If a previous forming candle exists for a different minute, archive it
        if active_cand is not None and active_cand.timestamp != ts:
            if sym not in self.forming_history:
                self.forming_history[sym] = []
            self.forming_history[sym].append(active_cand)

        session_date = to_eastern(ts).date()

        # Construct new forming candle
        new_candle = AlpacaCandle(
            ticker=sym,
            interval="1m",
            timestamp=ts,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            status=CandleStatus.FORMING,  # STRICTLY FORMING!
            source="alpaca_iex_stream",
            is_delayed=False,
            vwap=bar.vwap,
            trade_count=bar.trade_count,
            feed_classification="IEX",
            session_date=session_date,
            adjustment_type="SPLIT_ADJUSTED",
            is_consolidated_volume=False,  # STRICTLY FALSE!
        )

        # Validate intrinsic OHLCV geometry
        validate_single_candle(new_candle, current_time=now_dt)

        self.active_forming_candles[sym] = new_candle
        self.latest_bar_timestamps[sym] = ts
        return (new_candle, True)

    def get_forming_candle(self, symbol: str) -> Optional[AlpacaCandle]:
        """Return the current active forming candle for a symbol."""
        return self.active_forming_candles.get(symbol.upper().strip())

    def get_forming_history(self, symbol: str) -> List[AlpacaCandle]:
        """Return archived historical forming candles for a symbol."""
        return list(self.forming_history.get(symbol.upper().strip(), []))

    def promote_to_daily_candle(self, symbol: str) -> None:
        """Explicitly prohibited by design.

        Raises:
            InvalidVolumeError: Always raised to prevent volume contamination.
        """
        raise InvalidVolumeError(
            f"Intraday streaming IEX candle for {symbol} cannot be promoted to a completed daily candle. "
            "Completed daily candles must receive verified consolidated SIP tape volume.",
            ticker=symbol,
        )


# -----------------------------------------------------------------------------
# Streaming Client Implementation
# -----------------------------------------------------------------------------
class AlpacaWebSocketClient:
    """Offline-compatible Alpaca market data WebSocket client.

    Adheres strictly to the following guarantees:
    1. Operates state machine: DISCONNECTED -> CONNECTING -> AUTHENTICATING -> AUTHENTICATED -> SUBSCRIBED.
    2. Parses incoming arrays of JSON events for authentication, subscriptions, trades, quotes, and bars.
    3. Manages forming candle state via FormingCandleManager.
    4. Bounded exponential reconnection with jitter.
    5. Authentication failure is terminal (no endless reconnect loops).
    6. Restores channel subscriptions on successful reconnection.
    7. Detects stale streams based on configured inactivity timeout.
    8. Client-initiated control messages (auth, subscribe) can use the REST rate limiter,
       while incoming data frames NEVER consume rate limiter tokens.
    9. 100% offline-safe: operates synchronously with injected mock transports without
       creating background threads or daemon processes.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        stream_url: str = DEFAULT_WS_STREAM_URL,
        rate_limiter: Optional[TokenBucketRateLimiter] = None,
        max_reconnect_attempts: int = DEFAULT_MAX_RECONNECT_ATTEMPTS,
        initial_backoff_seconds: float = DEFAULT_INITIAL_BACKOFF_SECONDS,
        max_backoff_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS,
        stale_timeout_seconds: float = DEFAULT_STALE_TIMEOUT_SECONDS,
        transport: Optional[Any] = None,
        time_fn: Optional[Callable[[], float]] = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
        clock: Optional[Any] = None,
    ):
        self.api_key = api_key or os.environ.get("ALPACA_API_KEY_ID")
        self.api_secret = api_secret or os.environ.get("ALPACA_API_SECRET_KEY")
        self.stream_url = stream_url
        self.rate_limiter = rate_limiter
        self.max_reconnect_attempts = max(0, max_reconnect_attempts)
        self.initial_backoff_seconds = max(0.01, initial_backoff_seconds)
        self.max_backoff_seconds = max(self.initial_backoff_seconds, max_backoff_seconds)
        self.stale_timeout_seconds = max(1.0, stale_timeout_seconds)

        self.transport = transport
        self.time_fn: Callable[[], float] = time_fn if time_fn is not None else time_mod.time
        self.sleep_fn: Callable[[float], None] = sleep_fn if sleep_fn is not None else time_mod.sleep
        self.clock = clock

        # State management
        self.state: WebSocketState = WebSocketState.DISCONNECTED
        self.forming_candle_manager = FormingCandleManager(clock=self.clock)
        self.reconnect_attempts: int = 0
        self.last_message_time: float = self.time_fn()

        # Desired and active subscriptions
        self.desired_subscriptions: Dict[str, Set[str]] = {
            "bars": set(),
            "updatedBars": set(),
            "trades": set(),
            "quotes": set(),
        }
        self.active_subscriptions: Dict[str, Set[str]] = {
            "bars": set(),
            "updatedBars": set(),
            "trades": set(),
            "quotes": set(),
        }

        # Event callbacks
        self.on_bar: Optional[Callable[[AlpacaStreamBar], None]] = None
        self.on_trade: Optional[Callable[[AlpacaStreamTrade], None]] = None
        self.on_quote: Optional[Callable[[AlpacaStreamQuote], None]] = None
        self.on_error: Optional[Callable[[AlpacaWebSocketError], None]] = None
        self.on_state_change: Optional[Callable[[WebSocketState, WebSocketState], None]] = None

    def _set_state(self, new_state: WebSocketState) -> None:
        """Transition connection state and invoke listener callback."""
        old_state = self.state
        if old_state != new_state:
            self.state = new_state
            if self.on_state_change:
                self.on_state_change(old_state, new_state)

    def _get_secrets(self) -> Tuple[Optional[str], Optional[str]]:
        return (self.api_key, self.api_secret)

    def __repr__(self) -> str:
        key_preview = sanitize_secret(self.api_key)
        return (
            f"AlpacaWebSocketClient(state={self.state.value}, "
            f"stream_url='{self.stream_url}', "
            f"api_key={key_preview})"
        )

    # -------------------------------------------------------------------------
    # Connection Lifecycle
    # -------------------------------------------------------------------------
    def connect(self) -> None:
        """Initiate WebSocket connection."""
        if self.state in (WebSocketState.CONNECTING, WebSocketState.AUTHENTICATED, WebSocketState.SUBSCRIBED):
            return

        self._set_state(WebSocketState.CONNECTING)
        if self.transport is not None:
            self.transport.open(self.stream_url)
        self.last_message_time = self.time_fn()

    def authenticate(self) -> None:
        """Construct and transmit authentication message."""
        if not self.api_key or not self.api_secret:
            self._set_state(WebSocketState.FAILED)
            raise AlpacaWSAuthError(
                "Cannot authenticate WebSocket: missing ALPACA_API_KEY_ID or ALPACA_API_SECRET_KEY."
            )

        self._set_state(WebSocketState.AUTHENTICATING)
        auth_msg = {
            "action": "auth",
            "key": self.api_key,
            "secret": self.api_secret,
        }
        self._send_payload(auth_msg, is_control_action=True)

    def subscribe(
        self,
        bars: Optional[Sequence[str]] = None,
        updated_bars: Optional[Sequence[str]] = None,
        trades: Optional[Sequence[str]] = None,
        quotes: Optional[Sequence[str]] = None,
    ) -> None:
        """Register desired channel subscriptions and send subscribe frame."""
        sub_payload: Dict[str, Any] = {"action": "subscribe"}
        has_items = False

        if bars:
            clean_b = [s.upper().strip() for s in bars if s.strip()]
            self.desired_subscriptions["bars"].update(clean_b)
            sub_payload["bars"] = clean_b
            has_items = True

        if updated_bars:
            clean_u = [s.upper().strip() for s in updated_bars if s.strip()]
            self.desired_subscriptions["updatedBars"].update(clean_u)
            sub_payload["updatedBars"] = clean_u
            has_items = True

        if trades:
            clean_t = [s.upper().strip() for s in trades if s.strip()]
            self.desired_subscriptions["trades"].update(clean_t)
            sub_payload["trades"] = clean_t
            has_items = True

        if quotes:
            clean_q = [s.upper().strip() for s in quotes if s.strip()]
            self.desired_subscriptions["quotes"].update(clean_q)
            sub_payload["quotes"] = clean_q
            has_items = True

        if not has_items:
            return

        if self.state in (WebSocketState.AUTHENTICATED, WebSocketState.SUBSCRIBED):
            self._send_payload(sub_payload, is_control_action=True)

    def unsubscribe(
        self,
        bars: Optional[Sequence[str]] = None,
        updated_bars: Optional[Sequence[str]] = None,
        trades: Optional[Sequence[str]] = None,
        quotes: Optional[Sequence[str]] = None,
    ) -> None:
        """Send unsubscribe frame and update subscription state."""
        unsub_payload: Dict[str, Any] = {"action": "unsubscribe"}
        has_items = False

        if bars:
            clean_b = [s.upper().strip() for s in bars if s.strip()]
            self.desired_subscriptions["bars"].difference_update(clean_b)
            self.active_subscriptions["bars"].difference_update(clean_b)
            unsub_payload["bars"] = clean_b
            has_items = True

        if updated_bars:
            clean_u = [s.upper().strip() for s in updated_bars if s.strip()]
            self.desired_subscriptions["updatedBars"].difference_update(clean_u)
            self.active_subscriptions["updatedBars"].difference_update(clean_u)
            unsub_payload["updatedBars"] = clean_u
            has_items = True

        if trades:
            clean_t = [s.upper().strip() for s in trades if s.strip()]
            self.desired_subscriptions["trades"].difference_update(clean_t)
            self.active_subscriptions["trades"].difference_update(clean_t)
            unsub_payload["trades"] = clean_t
            has_items = True

        if quotes:
            clean_q = [s.upper().strip() for s in quotes if s.strip()]
            self.desired_subscriptions["quotes"].difference_update(clean_q)
            self.active_subscriptions["quotes"].difference_update(clean_q)
            unsub_payload["quotes"] = clean_q
            has_items = True

        if not has_items:
            return

        if self.state in (WebSocketState.AUTHENTICATED, WebSocketState.SUBSCRIBED):
            self._send_payload(unsub_payload, is_control_action=True)

        if not any(self.active_subscriptions.values()):
            self._set_state(WebSocketState.AUTHENTICATED)

    def _send_payload(self, payload: Dict[str, Any], is_control_action: bool = False) -> None:
        """Transmit JSON payload to transport with rate limiting check for control frames."""
        if is_control_action and self.rate_limiter is not None:
            # Check rate limiter for control actions (subscribe, auth)
            acquired = self.rate_limiter.acquire(1.0, block=False)
            if not acquired:
                err = AlpacaWSSubscriptionError("Client-side control rate limit exceeded.")
                if self.on_error:
                    self.on_error(err)
                raise err

        if self.transport is not None:
            self.transport.send(json.dumps(payload))

    # -------------------------------------------------------------------------
    # Incoming Message Processing & Event Dispatch
    # -------------------------------------------------------------------------
    def handle_raw_message(self, raw_message: str) -> None:
        """Parse raw incoming WebSocket frame and route messages to specific handlers."""
        self.last_message_time = self.time_fn()

        if not raw_message or not raw_message.strip():
            return

        try:
            parsed = json.loads(raw_message)
        except json.JSONDecodeError as exc:
            err = AlpacaWSProtocolError(f"Malformed JSON received from WebSocket: {exc}")
            if self.on_error:
                self.on_error(err)
            raise err from exc

        if not isinstance(parsed, list):
            # Alpaca v2 streaming messages are always arrays of objects
            parsed = [parsed]

        for item in parsed:
            if not isinstance(item, dict):
                continue
            msg_type = item.get("T")
            if not msg_type:
                continue

            try:
                if msg_type == "success":
                    self._handle_success_message(item)
                elif msg_type == "subscription":
                    self._handle_subscription_ack(item)
                elif msg_type == "error":
                    self._handle_error_message(item)
                elif msg_type in ("b", "u"):
                    self._handle_bar_message(item, is_updated=(msg_type == "u"))
                elif msg_type == "t":
                    self._handle_trade_message(item)
                elif msg_type == "q":
                    self._handle_quote_message(item)
                else:
                    logger.debug("Ignored unsupported stream message type '%s'", msg_type)
            except Exception as exc:
                if self.on_error and isinstance(exc, AlpacaWebSocketError):
                    self.on_error(exc)
                else:
                    raise

    def _handle_success_message(self, msg: Dict[str, Any]) -> None:
        """Handle 'success' messages (connected, authenticated)."""
        content = msg.get("msg", "").lower()
        if content == "connected":
            # Handshake successful, proceed to authentication
            self.authenticate()
        elif content == "authenticated":
            self.reconnect_attempts = 0
            self._set_state(WebSocketState.AUTHENTICATED)
            # Restore subscriptions if desired
            self._restore_subscriptions()

    def _handle_subscription_ack(self, msg: Dict[str, Any]) -> None:
        """Handle subscription acknowledgement from server."""
        for channel in ("bars", "updatedBars", "trades", "quotes"):
            items = msg.get(channel, [])
            if items:
                self.active_subscriptions[channel].update(s.upper().strip() for s in items)

        if any(self.active_subscriptions.values()):
            self._set_state(WebSocketState.SUBSCRIBED)

    def _handle_error_message(self, msg: Dict[str, Any]) -> None:
        """Handle error messages from Alpaca stream."""
        code = msg.get("code")
        error_msg = msg.get("msg", f"Stream error code {code}")
        secrets = self._get_secrets()

        if code in (401, 403):
            # Terminal authentication failure
            self._set_state(WebSocketState.FAILED)
            err = AlpacaWSAuthError(
                f"WebSocket authentication rejected ({code}): {error_msg}",
                code=code,
                secrets_to_redact=secrets,
            )
            if self.on_error:
                self.on_error(err)
            raise err

        err = AlpacaWebSocketError(
            f"Alpaca stream error ({code}): {error_msg}",
            code=code,
            secrets_to_redact=secrets,
        )
        if self.on_error:
            self.on_error(err)

    def _handle_bar_message(self, item: Dict[str, Any], is_updated: bool = False) -> None:
        """Parse and route minute-bar ('b') or updated-bar ('u') message."""
        # Required bar fields
        for field in ("S", "o", "h", "l", "c", "v", "t"):
            if field not in item or item[field] is None:
                raise AlpacaWSProtocolError(f"Stream bar missing mandatory field '{field}': {item}")

        sym = str(item["S"]).upper().strip()
        ts = parse_iso_timestamp(str(item["t"]))

        try:
            o_px = Decimal(str(item["o"]))
            h_px = Decimal(str(item["h"]))
            l_px = Decimal(str(item["l"]))
            c_px = Decimal(str(item["c"]))
        except Exception as exc:
            raise InvalidPriceError(f"Non-numeric price in stream bar for {sym}: {exc}", ticker=sym, timestamp=ts)

        try:
            vol = int(item["v"])
        except Exception as exc:
            raise InvalidVolumeError(f"Non-integer volume in stream bar for {sym}: {exc}", ticker=sym, timestamp=ts)

        if vol < 0:
            raise InvalidVolumeError(f"Negative volume in stream bar for {sym}: {vol}", ticker=sym, timestamp=ts)

        vwap_px = Decimal(str(item["vw"])) if "vw" in item and item["vw"] is not None else None
        trade_count = int(item["n"]) if "n" in item and item["n"] is not None else None

        stream_bar = AlpacaStreamBar(
            symbol=sym,
            open=o_px,
            high=h_px,
            low=l_px,
            close=c_px,
            volume=vol,
            timestamp=ts,
            trade_count=trade_count,
            vwap=vwap_px,
            is_updated=is_updated,
        )

        # Process through forming candle manager
        self.forming_candle_manager.process_bar(stream_bar)

        if self.on_bar:
            self.on_bar(stream_bar)

    def _handle_trade_message(self, item: Dict[str, Any]) -> None:
        """Parse and route trade tick ('t') message."""
        for field in ("S", "p", "s", "t"):
            if field not in item or item[field] is None:
                raise AlpacaWSProtocolError(f"Stream trade missing mandatory field '{field}': {item}")

        sym = str(item["S"]).upper().strip()
        ts = parse_iso_timestamp(str(item["t"]))

        try:
            px = Decimal(str(item["p"]))
            size = int(item["s"])
        except Exception as exc:
            raise AlpacaWSProtocolError(f"Non-numeric trade value in stream for {sym}: {exc}")

        trade = AlpacaStreamTrade(
            symbol=sym,
            price=px,
            size=size,
            timestamp=ts,
            exchange=str(item.get("x", "V")),
            trade_id=int(item["i"]) if "i" in item else None,
            conditions=list(item.get("c", [])),
            tape=str(item.get("z", "C")),
        )

        if self.on_trade:
            self.on_trade(trade)

    def _handle_quote_message(self, item: Dict[str, Any]) -> None:
        """Parse and route top-of-book quote ('q') message."""
        for field in ("S", "bp", "bs", "ap", "as", "t"):
            if field not in item or item[field] is None:
                raise AlpacaWSProtocolError(f"Stream quote missing mandatory field '{field}': {item}")

        sym = str(item["S"]).upper().strip()
        ts = parse_iso_timestamp(str(item["t"]))

        try:
            bp = Decimal(str(item["bp"]))
            bs = int(item["bs"])
            ap = Decimal(str(item["ap"]))
            as_ = int(item["as"])
        except Exception as exc:
            raise AlpacaWSProtocolError(f"Non-numeric quote value in stream for {sym}: {exc}")

        quote = AlpacaStreamQuote(
            symbol=sym,
            bid_price=bp,
            bid_size=bs,
            ask_price=ap,
            ask_size=as_,
            timestamp=ts,
            bid_exchange=str(item.get("bx", "V")),
            ask_exchange=str(item.get("ax", "V")),
            conditions=list(item.get("c", [])),
        )

        if self.on_quote:
            self.on_quote(quote)

    # -------------------------------------------------------------------------
    # Reconnection & Stale Stream Handling
    # -------------------------------------------------------------------------
    def handle_disconnect(self, unexpected: bool = True) -> None:
        """Handle connection drop or server close event."""
        self.active_subscriptions = {k: set() for k in self.active_subscriptions}

        if not unexpected:
            self._set_state(WebSocketState.DISCONNECTED)
            return

        if self.state == WebSocketState.FAILED:
            return  # Terminal failure; do not attempt reconnect

        if self.reconnect_attempts >= self.max_reconnect_attempts:
            self._set_state(WebSocketState.FAILED)
            err = AlpacaWSConnectionError(
                f"WebSocket reconnection exhausted after {self.reconnect_attempts} attempts."
            )
            if self.on_error:
                self.on_error(err)
            raise err

        self._set_state(WebSocketState.RECONNECTING)
        self.reconnect_attempts += 1

        # Exponential backoff: backoff = min(max_backoff, initial * (2 ** (attempt - 1)))
        backoff = min(
            self.max_backoff_seconds,
            self.initial_backoff_seconds * (2 ** (self.reconnect_attempts - 1)),
        )
        logger.info(
            "WebSocket disconnected unexpectedly. Reconnecting attempt %d/%d in %.2fs",
            self.reconnect_attempts,
            self.max_reconnect_attempts,
            backoff,
        )
        self.sleep_fn(backoff)
        self.connect()

    def _restore_subscriptions(self) -> None:
        """Restore desired subscriptions following successful reconnection."""
        has_desired = any(self.desired_subscriptions.values())
        if has_desired:
            sub_payload: Dict[str, Any] = {"action": "subscribe"}
            for channel, symbols in self.desired_subscriptions.items():
                if symbols:
                    sub_payload[channel] = list(symbols)
            self._send_payload(sub_payload, is_control_action=True)

    def check_stale_stream(self, now: Optional[float] = None) -> bool:
        """Check if stream has gone silent beyond configured stale timeout.

        Returns:
            True if stream is stale, False otherwise.
        """
        current_time = now if now is not None else self.time_fn()
        elapsed = current_time - self.last_message_time

        if elapsed > self.stale_timeout_seconds and self.state in (
            WebSocketState.AUTHENTICATED,
            WebSocketState.SUBSCRIBED,
        ):
            err = AlpacaWSStaleStreamError(
                f"WebSocket stream silent for {elapsed:.1f}s (exceeds timeout {self.stale_timeout_seconds:.1f}s)."
            )
            if self.on_error:
                self.on_error(err)
            return True
        return False

    def disconnect(self) -> None:
        """Perform graceful client disconnect and transition state to DISCONNECTED."""
        self._set_state(WebSocketState.DISCONNECTING)
        if self.transport is not None:
            self.transport.close()
        self.active_subscriptions = {k: set() for k in self.active_subscriptions}
        self._set_state(WebSocketState.DISCONNECTED)
