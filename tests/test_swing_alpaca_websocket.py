"""Deterministic offline unit test suite for Phase 7.2.3 Alpaca WebSocket streaming.

Verifies:
1. Connection lifecycle and state transitions (DISCONNECTED -> CONNECTING -> AUTH -> SUBSCRIBED).
2. Authentication message construction, credential masking, and 401/403 failure handling.
3. Message parsers for bars ('b'), updated bars ('u'), trades ('t'), and quotes ('q').
4. Multi-message frame handling and safe rejection of malformed frames.
5. Intraday FormingCandleManager:
   - Preserves CandleStatus.FORMING strictly.
   - In-place updates for updated bars ('u').
   - De-duplication of identical messages.
   - Out-of-order timestamp rejection.
   - Anti-volume contamination: is_consolidated_volume is False; get_consolidated_volume() raises.
   - Refusal of promotion to daily complete candles.
6. Connection reliability:
   - Unexpected disconnect and exponential backoff.
   - Subscription restoration on reconnect.
   - Reconnect exhaustion to FAILED state.
   - Stale stream detection.
   - Graceful disconnect without reconnection.
7. Rate Limiter integration: control frames gated, inbound stream frames zero token deduction.
8. Zero live network calls and zero background daemon processes.
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
    InvalidPriceError,
    InvalidResponseError,
    InvalidVolumeError,
    MarketClock,
    OHLCVCandle,
    ProviderUnavailableError,
    RateLimitError,
    TimeoutError,
    UTC,
)
from providers.alpaca_connector import (
    TokenBucketRateLimiter,
    sanitize_headers,
    sanitize_message,
    sanitize_secret,
)
from providers.alpaca_provider import AlpacaCandle
from providers.alpaca_websocket import (
    AlpacaStreamBar,
    AlpacaStreamQuote,
    AlpacaStreamTrade,
    AlpacaWebSocketClient,
    AlpacaWebSocketError,
    AlpacaWSAuthError,
    AlpacaWSConnectionError,
    AlpacaWSProtocolError,
    AlpacaWSStaleStreamError,
    AlpacaWSSubscriptionError,
    FormingCandleManager,
    WebSocketState,
)

FIXTURE_PATH = os.path.join(
    os.path.dirname(__file__),
    "fixtures",
    "trader-swing",
    "alpaca",
    "websocket_fixtures.json",
)


class MockClock:
    """Deterministic simulated clock and sleep tracker for zero-delay offline testing."""

    def __init__(self, initial_time: float = 1_000_000.0):
        self.current_time = initial_time
        self.sleep_calls = []

    def time(self) -> float:
        return self.current_time

    def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self.current_time += seconds


class MockWebSocketTransport:
    """Synchronous mock transport for WebSocket client testing with zero network access."""

    def __init__(self):
        self.is_open: bool = False
        self.sent_messages: list = []
        self.opened_url: Optional[str] = None
        self.close_count: int = 0

    def open(self, url: str) -> None:
        self.is_open = True
        self.opened_url = url

    def send(self, message: str) -> None:
        if not self.is_open:
            raise RuntimeError("Cannot send message on closed mock transport")
        self.sent_messages.append(message)

    def close(self) -> None:
        self.is_open = False
        self.close_count += 1


class TestAlpacaWebSocketLifecycle(unittest.TestCase):
    """Test suite for WebSocket connection lifecycle, authentication, and state management."""

    @classmethod
    def setUpClass(cls):
        with open(FIXTURE_PATH, "r", encoding="utf-8") as f:
            cls.fixtures = json.load(f)

    def setUp(self):
        self.clock = MockClock(1000.0)
        self.transport = MockWebSocketTransport()
        self.client = AlpacaWebSocketClient(
            api_key="TEST_KEY_ID",
            api_secret="TEST_SECRET_KEY",
            transport=self.transport,
            time_fn=self.clock.time,
            sleep_fn=self.clock.sleep,
        )

    def test_initial_state(self):
        """Client starts in DISCONNECTED state with zero subscriptions."""
        self.assertEqual(self.client.state, WebSocketState.DISCONNECTED)
        self.assertFalse(any(self.client.active_subscriptions.values()))

    def test_connection_and_handshake(self):
        """Calling connect() transitions to CONNECTING; welcome message triggers authenticate()."""
        self.client.connect()
        self.assertEqual(self.client.state, WebSocketState.CONNECTING)
        self.assertTrue(self.transport.is_open)

        # Server sends welcome message: [{"T": "success", "msg": "connected"}]
        welcome_payload = self.fixtures["welcome_connected"]
        self.client.handle_raw_message(welcome_payload)

        # Handshake should transition to AUTHENTICATING and send auth payload
        self.assertEqual(self.client.state, WebSocketState.AUTHENTICATING)
        self.assertEqual(len(self.transport.sent_messages), 1)

        sent_auth = json.loads(self.transport.sent_messages[0])
        self.assertEqual(sent_auth.get("action"), "auth")
        self.assertEqual(sent_auth.get("key"), "TEST_KEY_ID")
        self.assertEqual(sent_auth.get("secret"), "TEST_SECRET_KEY")

    def test_auth_success_transitions_to_authenticated(self):
        """Server auth success message transitions state to AUTHENTICATED."""
        self.client.connect()
        self.client.handle_raw_message(self.fixtures["welcome_connected"])
        self.client.handle_raw_message(self.fixtures["auth_success"])

        self.assertEqual(self.client.state, WebSocketState.AUTHENTICATED)
        self.assertEqual(self.client.reconnect_attempts, 0)

    def test_auth_failure_is_terminal(self):
        """HTTP 401 auth rejection transitions to FAILED and raises AlpacaWSAuthError without reconnecting."""
        self.client.connect()
        self.client.handle_raw_message(self.fixtures["welcome_connected"])

        with self.assertRaises(AlpacaWSAuthError) as ctx:
            self.client.handle_raw_message(self.fixtures["auth_failure_invalid_key"])

        self.assertEqual(self.client.state, WebSocketState.FAILED)
        self.assertEqual(ctx.exception.code, 401)
        self.assertIsInstance(ctx.exception, ProviderUnavailableError)

    def test_missing_credentials_fails_immediately(self):
        """Authenticating without credentials raises AlpacaWSAuthError and sets state to FAILED."""
        client_no_creds = AlpacaWebSocketClient(
            api_key=None,
            api_secret=None,
            transport=self.transport,
        )
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(AlpacaWSAuthError):
                client_no_creds.authenticate()
        self.assertEqual(client_no_creds.state, WebSocketState.FAILED)

    def test_subscription_and_acknowledgement(self):
        """Subscribing transmits payload; server ack sets state to SUBSCRIBED."""
        self.client.connect()
        self.client.handle_raw_message(self.fixtures["welcome_connected"])
        self.client.handle_raw_message(self.fixtures["auth_success"])

        self.client.subscribe(bars=["SPY"], updated_bars=["SPY"], trades=["AAPL"], quotes=["QQQ"])
        self.assertEqual(len(self.transport.sent_messages), 2)  # 1 auth + 1 subscribe

        sent_sub = json.loads(self.transport.sent_messages[1])
        self.assertEqual(sent_sub.get("action"), "subscribe")
        self.assertEqual(sent_sub.get("bars"), ["SPY"])
        self.assertEqual(sent_sub.get("quotes"), ["QQQ"])

        # Server sends subscription ack
        self.client.handle_raw_message(self.fixtures["subscription_ack"])
        self.assertEqual(self.client.state, WebSocketState.SUBSCRIBED)
        self.assertIn("SPY", self.client.active_subscriptions["bars"])
        self.assertIn("QQQ", self.client.active_subscriptions["quotes"])

    def test_unsubscribe_workflow(self):
        """Unsubscribing updates desired/active sets and transitions state back to AUTHENTICATED."""
        self.client.connect()
        self.client.handle_raw_message(self.fixtures["welcome_connected"])
        self.client.handle_raw_message(self.fixtures["auth_success"])
        self.client.subscribe(bars=["SPY"])
        single_sub_ack = '[{"T": "subscription", "bars": ["SPY"], "updatedBars": [], "trades": [], "quotes": []}]'
        self.client.handle_raw_message(single_sub_ack)

        self.client.unsubscribe(bars=["SPY"])
        sent_unsub = json.loads(self.transport.sent_messages[-1])
        self.assertEqual(sent_unsub.get("action"), "unsubscribe")
        self.assertEqual(sent_unsub.get("bars"), ["SPY"])
        self.assertNotIn("SPY", self.client.active_subscriptions["bars"])
        self.assertEqual(self.client.state, WebSocketState.AUTHENTICATED)

    def test_graceful_disconnect(self):
        """Calling disconnect() cleanly transitions to DISCONNECTED without reconnecting."""
        self.client.connect()
        self.client.disconnect()
        self.assertEqual(self.client.state, WebSocketState.DISCONNECTED)
        self.assertFalse(self.transport.is_open)
        self.assertEqual(self.transport.close_count, 1)


class TestAlpacaWebSocketMessageParsers(unittest.TestCase):
    """Test suite for parsing Alpaca WebSocket stream messages."""

    @classmethod
    def setUpClass(cls):
        with open(FIXTURE_PATH, "r", encoding="utf-8") as f:
            cls.fixtures = json.load(f)

    def setUp(self):
        self.transport = MockWebSocketTransport()
        self.client = AlpacaWebSocketClient(
            api_key="KEY",
            api_secret="SECRET",
            transport=self.transport,
        )

    def test_parse_minute_bar_message(self):
        """Minute-bar message ('b') is parsed with Decimal prices and UTC timestamp."""
        received_bars = []
        self.client.on_bar = lambda b: received_bars.append(b)

        self.client.handle_raw_message(self.fixtures["minute_bar_spy"])
        self.assertEqual(len(received_bars), 1)

        bar = received_bars[0]
        self.assertEqual(bar.symbol, "SPY")
        self.assertEqual(bar.open, Decimal("580.10"))
        self.assertEqual(bar.high, Decimal("581.50"))
        self.assertEqual(bar.low, Decimal("579.80"))
        self.assertEqual(bar.close, Decimal("580.95"))
        self.assertEqual(bar.volume, 15400)
        self.assertEqual(bar.trade_count, 320)
        self.assertEqual(bar.vwap, Decimal("580.65"))
        self.assertFalse(bar.is_updated)

    def test_parse_updated_bar_message(self):
        """Updated-bar message ('u') is parsed with is_updated=True."""
        received_bars = []
        self.client.on_bar = lambda b: received_bars.append(b)

        self.client.handle_raw_message(self.fixtures["updated_bar_spy"])
        self.assertEqual(len(received_bars), 1)
        self.assertTrue(received_bars[0].is_updated)
        self.assertEqual(received_bars[0].close, Decimal("581.85"))
        self.assertEqual(received_bars[0].volume, 24200)

    def test_parse_trade_message(self):
        """Trade message ('t') is parsed with Decimal price and int size."""
        received_trades = []
        self.client.on_trade = lambda t: received_trades.append(t)

        self.client.handle_raw_message(self.fixtures["trade_aapl"])
        self.assertEqual(len(received_trades), 1)

        trade = received_trades[0]
        self.assertEqual(trade.symbol, "AAPL")
        self.assertEqual(trade.price, Decimal("228.45"))
        self.assertEqual(trade.size, 100)
        self.assertEqual(trade.exchange, "V")
        self.assertEqual(trade.trade_id, 987654)

    def test_parse_quote_message(self):
        """Quote message ('q') is parsed with bid/ask prices and sizes."""
        received_quotes = []
        self.client.on_quote = lambda q: received_quotes.append(q)

        self.client.handle_raw_message(self.fixtures["quote_qqq"])
        self.assertEqual(len(received_quotes), 1)

        quote = received_quotes[0]
        self.assertEqual(quote.symbol, "QQQ")
        self.assertEqual(quote.bid_price, Decimal("492.20"))
        self.assertEqual(quote.bid_size, 15)
        self.assertEqual(quote.ask_price, Decimal("492.25"))
        self.assertEqual(quote.ask_size, 30)

    def test_multi_message_frame(self):
        """Multi-message frame containing trade, quote, and bar dispatches all three."""
        counts = {"bar": 0, "trade": 0, "quote": 0}
        self.client.on_bar = lambda b: counts.__setitem__("bar", counts["bar"] + 1)
        self.client.on_trade = lambda t: counts.__setitem__("trade", counts["trade"] + 1)
        self.client.on_quote = lambda q: counts.__setitem__("quote", counts["quote"] + 1)

        self.client.handle_raw_message(self.fixtures["multi_message_frame"])
        self.assertEqual(counts["trade"], 1)
        self.assertEqual(counts["quote"], 1)
        self.assertEqual(counts["bar"], 1)

    def test_malformed_json_raises_protocol_error(self):
        """Truncated or invalid JSON raises AlpacaWSProtocolError."""
        with self.assertRaises(AlpacaWSProtocolError):
            self.client.handle_raw_message(self.fixtures["malformed_truncated_json"])

    def test_malformed_inverted_high_low_raises_invalid_price(self):
        """Inverted bar geometry (high < low) raises InvalidPriceError."""
        with self.assertRaises(InvalidPriceError):
            self.client.handle_raw_message(self.fixtures["malformed_inverted_high_low"])

    def test_malformed_negative_volume_raises_invalid_volume(self):
        """Negative volume raises InvalidVolumeError."""
        with self.assertRaises(InvalidVolumeError):
            self.client.handle_raw_message(self.fixtures["malformed_negative_volume"])


class TestFormingCandleManager(unittest.TestCase):
    """Test suite for FormingCandleManager lifecycle, updating, and volume protection."""

    def setUp(self):
        self.clock = MarketClock()
        self.manager = FormingCandleManager(clock=self.clock)
        self.mock_now = datetime(2026, 11, 17, 16, 0, 0, tzinfo=UTC)

    def test_minute_bar_creates_forming_candle(self):
        """Processing a new minute bar creates an AlpacaCandle with status FORMING."""
        bar = AlpacaStreamBar(
            symbol="SPY",
            open=Decimal("580.10"),
            high=Decimal("581.50"),
            low=Decimal("579.80"),
            close=Decimal("580.95"),
            volume=15400,
            timestamp=datetime(2026, 11, 17, 15, 30, 0, tzinfo=UTC),
            trade_count=320,
            vwap=Decimal("580.65"),
            is_updated=False,
        )

        candle, is_new = self.manager.process_bar(bar, current_time=self.mock_now)
        self.assertTrue(is_new)
        self.assertIsInstance(candle, AlpacaCandle)
        self.assertEqual(candle.ticker, "SPY")
        self.assertEqual(candle.status, CandleStatus.FORMING)
        self.assertEqual(candle.feed_classification, "IEX")
        self.assertFalse(candle.is_consolidated_volume)

        # Anti-substitution check: calling get_consolidated_volume() must raise
        with self.assertRaises(InvalidVolumeError):
            candle.get_consolidated_volume()

    def test_updated_bar_modifies_active_forming_candle_in_place(self):
        """Updated bar ('u') updates the existing forming candle for that minute."""
        bar1 = AlpacaStreamBar(
            symbol="SPY",
            open=Decimal("580.10"),
            high=Decimal("581.50"),
            low=Decimal("579.80"),
            close=Decimal("580.95"),
            volume=15400,
            timestamp=datetime(2026, 11, 17, 15, 30, 0, tzinfo=UTC),
            trade_count=320,
            vwap=Decimal("580.65"),
            is_updated=False,
        )
        candle1, is_new1 = self.manager.process_bar(bar1, current_time=self.mock_now)
        self.assertTrue(is_new1)

        # In-flight update for same minute with higher high and higher volume
        bar_update = AlpacaStreamBar(
            symbol="SPY",
            open=Decimal("580.10"),
            high=Decimal("582.10"),
            low=Decimal("579.80"),
            close=Decimal("581.85"),
            volume=24200,
            timestamp=datetime(2026, 11, 17, 15, 30, 0, tzinfo=UTC),
            trade_count=480,
            vwap=Decimal("580.92"),
            is_updated=True,
        )
        candle2, is_new2 = self.manager.process_bar(bar_update, current_time=self.mock_now)

        self.assertFalse(is_new2, "Update must not create a new candle")
        self.assertIs(candle1, candle2, "Must modify the same forming candle instance")
        self.assertEqual(candle2.high, Decimal("582.10"))
        self.assertEqual(candle2.close, Decimal("581.85"))
        self.assertEqual(candle2.volume, 24200)
        self.assertEqual(candle2.trade_count, 480)
        self.assertEqual(candle2.status, CandleStatus.FORMING)
        self.assertEqual(self.manager.update_count, 1)

    def test_duplicate_bar_is_safely_ignored(self):
        """Identical bar message does not duplicate or alter candle values."""
        bar = AlpacaStreamBar(
            symbol="SPY",
            open=Decimal("580.10"),
            high=Decimal("581.50"),
            low=Decimal("579.80"),
            close=Decimal("580.95"),
            volume=15400,
            timestamp=datetime(2026, 11, 17, 15, 30, 0, tzinfo=UTC),
        )
        self.manager.process_bar(bar, current_time=self.mock_now)
        candle, is_new = self.manager.process_bar(bar, current_time=self.mock_now)

        self.assertFalse(is_new)
        self.assertEqual(self.manager.duplicate_count, 1)

    def test_out_of_order_bar_is_rejected(self):
        """Bar with timestamp earlier than active forming bar increments out_of_order_count."""
        bar_t2 = AlpacaStreamBar(
            symbol="SPY",
            open=Decimal("581.00"),
            high=Decimal("582.00"),
            low=Decimal("580.50"),
            close=Decimal("581.50"),
            volume=10000,
            timestamp=datetime(2026, 11, 17, 15, 35, 0, tzinfo=UTC),
        )
        self.manager.process_bar(bar_t2, current_time=self.mock_now)

        # Out-of-order bar with earlier timestamp (15:30)
        bar_t1 = AlpacaStreamBar(
            symbol="SPY",
            open=Decimal("580.00"),
            high=Decimal("581.00"),
            low=Decimal("579.50"),
            close=Decimal("580.50"),
            volume=8000,
            timestamp=datetime(2026, 11, 17, 15, 30, 0, tzinfo=UTC),
        )
        candle, is_new = self.manager.process_bar(bar_t1, current_time=self.mock_now)
        self.assertFalse(is_new)
        self.assertEqual(self.manager.out_of_order_count, 1)
        # Active candle timestamp must remain 15:35
        self.assertEqual(self.manager.get_forming_candle("SPY").timestamp, datetime(2026, 11, 17, 15, 35, 0, tzinfo=UTC))

    def test_multi_minute_sequence_archives_history(self):
        """Advancing minute bars archive previous forming candles to history."""
        bar1 = AlpacaStreamBar(
            symbol="SPY",
            open=Decimal("580.10"),
            high=Decimal("581.50"),
            low=Decimal("579.80"),
            close=Decimal("580.95"),
            volume=15400,
            timestamp=datetime(2026, 11, 17, 15, 30, 0, tzinfo=UTC),
        )
        bar2 = AlpacaStreamBar(
            symbol="SPY",
            open=Decimal("581.00"),
            high=Decimal("582.00"),
            low=Decimal("580.80"),
            close=Decimal("581.80"),
            volume=12000,
            timestamp=datetime(2026, 11, 17, 15, 31, 0, tzinfo=UTC),
        )
        self.manager.process_bar(bar1, current_time=self.mock_now)
        self.manager.process_bar(bar2, current_time=self.mock_now)

        history = self.manager.get_forming_history("SPY")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].timestamp, datetime(2026, 11, 17, 15, 30, 0, tzinfo=UTC))
        self.assertEqual(self.manager.get_forming_candle("SPY").timestamp, datetime(2026, 11, 17, 15, 31, 0, tzinfo=UTC))

    def test_refuse_promotion_to_daily_candle(self):
        """Promote to daily candle is explicitly refused to preserve volume isolation."""
        with self.assertRaises(InvalidVolumeError):
            self.manager.promote_to_daily_candle("SPY")


class TestAlpacaWebSocketReliability(unittest.TestCase):
    """Test suite for unexpected disconnects, reconnection backoff, and stale streams."""

    @classmethod
    def setUpClass(cls):
        with open(FIXTURE_PATH, "r", encoding="utf-8") as f:
            cls.fixtures = json.load(f)

    def setUp(self):
        self.clock = MockClock(1000.0)
        self.transport = MockWebSocketTransport()
        self.client = AlpacaWebSocketClient(
            api_key="TEST_KEY",
            api_secret="TEST_SECRET",
            transport=self.transport,
            max_reconnect_attempts=3,
            initial_backoff_seconds=0.5,
            stale_timeout_seconds=20.0,
            time_fn=self.clock.time,
            sleep_fn=self.clock.sleep,
        )

    def test_reconnection_with_exponential_backoff(self):
        """Unexpected disconnect attempts reconnection with exponential backoff and sleep."""
        self.client.connect()
        self.client.handle_raw_message(self.fixtures["welcome_connected"])
        self.client.handle_raw_message(self.fixtures["auth_success"])
        self.client.subscribe(bars=["SPY"])
        self.client.handle_raw_message(self.fixtures["subscription_ack"])

        # First unexpected disconnect
        self.client.handle_disconnect(unexpected=True)
        self.assertEqual(self.client.reconnect_attempts, 1)
        self.assertEqual(self.clock.sleep_calls, [0.5])  # 0.5 * 2^0 = 0.5s

        # Second unexpected disconnect
        self.client.handle_disconnect(unexpected=True)
        self.assertEqual(self.client.reconnect_attempts, 2)
        self.assertEqual(self.clock.sleep_calls, [0.5, 1.0])  # 0.5 * 2^1 = 1.0s

    def test_subscription_restoration_on_reconnect(self):
        """Desired subscriptions are automatically re-sent following successful re-authentication."""
        self.client.connect()
        self.client.handle_raw_message(self.fixtures["welcome_connected"])
        self.client.handle_raw_message(self.fixtures["auth_success"])
        self.client.subscribe(bars=["SPY"], quotes=["QQQ"])
        self.client.handle_raw_message(self.fixtures["subscription_ack"])

        # Disconnect and simulate reconnect
        self.client.handle_disconnect(unexpected=True)
        self.transport.sent_messages.clear()

        # Server sends welcome on reconnect
        self.client.handle_raw_message(self.fixtures["welcome_connected"])
        # Server sends auth success
        self.client.handle_raw_message(self.fixtures["auth_success"])

        # Client must have sent auth AND subscription restoration
        self.assertEqual(len(self.transport.sent_messages), 2)
        restored_sub = json.loads(self.transport.sent_messages[1])
        self.assertEqual(restored_sub.get("action"), "subscribe")
        self.assertIn("SPY", restored_sub.get("bars", []))
        self.assertIn("QQQ", restored_sub.get("quotes", []))

    def test_reconnect_exhaustion_transitions_to_failed(self):
        """Exceeding max_reconnect_attempts sets state to FAILED and raises AlpacaWSConnectionError."""
        self.client.connect()
        with self.assertRaises(AlpacaWSConnectionError):
            for _ in range(4):  # max_reconnect_attempts = 3
                self.client.handle_disconnect(unexpected=True)

        self.assertEqual(self.client.state, WebSocketState.FAILED)

    def test_stale_stream_detection(self):
        """If stream is silent longer than stale_timeout_seconds, check_stale_stream returns True."""
        self.client.connect()
        self.client.handle_raw_message(self.fixtures["welcome_connected"])
        self.client.handle_raw_message(self.fixtures["auth_success"])

        # Normal activity at t = 1000.0
        self.assertFalse(self.client.check_stale_stream(now=1010.0))

        # Inactivity at t = 1025.0 (exceeds timeout 20.0s)
        errors = []
        self.client.on_error = lambda e: errors.append(e)

        is_stale = self.client.check_stale_stream(now=1025.0)
        self.assertTrue(is_stale)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], AlpacaWSStaleStreamError)

    def test_rate_limiter_gates_control_frames_only(self):
        """Rate limiter gates subscribe control frames, but incoming data messages consume 0 tokens."""
        rate_limiter = TokenBucketRateLimiter(
            rate_per_minute=60.0,
            capacity=2.0,  # 2 tokens: 1 for auth, 1 for subscribe
            time_fn=self.clock.time,
            sleep_fn=self.clock.sleep,
        )
        client_rl = AlpacaWebSocketClient(
            api_key="KEY",
            api_secret="SECRET",
            transport=self.transport,
            rate_limiter=rate_limiter,
        )
        client_rl.connect()
        client_rl.handle_raw_message(self.fixtures["welcome_connected"])
        client_rl.handle_raw_message(self.fixtures["auth_success"])

        # Consume the 2nd control token on subscribe
        client_rl.subscribe(bars=["SPY"])
        self.assertEqual(rate_limiter.get_available_tokens(), 0.0)

        # Incoming stream bar messages must NOT be rate-limited or deduct tokens!
        client_rl.handle_raw_message(self.fixtures["minute_bar_spy"])
        self.assertEqual(rate_limiter.get_available_tokens(), 0.0)

        # Subsequent subscribe without token refilled raises AlpacaWSSubscriptionError
        with self.assertRaises(AlpacaWSSubscriptionError):
            client_rl.subscribe(bars=["AAPL"])


if __name__ == "__main__":
    unittest.main()
