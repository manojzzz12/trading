"""Deterministic offline integration test suite for Phase 7.2.4 Dual-Source Sync Gateway.

Verifies:
1. Unified Data Gateway & Daily Candle Synchronization (Alpaca prices + Yahoo consolidated volume).
2. Authoritative price OHLC: Price strictly from Alpaca; Yahoo prices NEVER substituted.
3. Authoritative volume: Volume strictly from Yahoo; Alpaca IEX volume isolated in .auxiliary_iex_volume.
4. Mandatory volume & price invariants:
   - Alpaca failure aborts scan (FEED_UNAVAILABLE / FEED_DEGRADED); NO silent failover.
   - Yahoo volume missing or zero transitions to VOLUME_UNVERIFIABLE; screening blocked.
   - Session date mismatch (D_alpaca != D_yahoo) blocks screening.
5. Intraday Forming Stream updates:
   - Minute bar ('b') and bar update ('u') in-place maintenance.
   - Invariant: CandleStatus.FORMING and is_consolidated_volume=False.
   - Forming candles NEVER overwrite completed historical daily candles.
   - Forming candles NEVER receive completed Yahoo daily volume.
   - Forming bars strictly blocked from new-long screening.
   - Deduplication of identical timestamps.
   - Rejection of out-of-order timestamps.
   - Prohibition of promotion to daily candle.
6. Market Clock and Session Timing:
   - Regular hours (09:30-16:00 ET).
   - Pre-market (04:00-09:30 ET) and after-hours (16:00-20:00 ET).
   - Early closes (13:00 ET on 2026-11-27).
   - Holidays (2026-11-26 Thanksgiving Day).
   - DST transitions (EDT UTC-4 vs EST UTC-5).
7. Explicit 8-State Data Readiness Matrix & Permissions:
   - Exact compliance with Phase 7.2.1 Section 6 contract.
8. Partial source recovery safety:
   - Never promotes incomplete data into screening eligibility.
9. WebSocket client reconnection and subscription restoration bridge.
10. Multi-ticker universe synchronization & readiness evaluation.
"""

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import json
import os
import sys
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union
import unittest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SWING_DIR = os.path.join(ROOT_DIR, ".agents", "skills", "trader-swing")
for path in (ROOT_DIR, SWING_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

from market_data_models import (
    CandleStatus,
    DataFreshness,
    EASTERN,
    MarketClock,
    MarketSessionState,
    OHLCVCandle,
    to_eastern,
    to_utc,
    UTC,
)
from market_data_validation import (
    InvalidPriceError,
    InvalidVolumeError,
    ProviderError,
    ProviderUnavailableError,
    RateLimitError,
    SequenceValidationError,
)
from feed_adapter import (
    FeedReadinessResult,
    MarketDataReadinessEvaluator,
    ReadinessStatus,
    UnconsolidatedVolumeError,
)
from dual_source_aggregator import (
    DualSourceAggregator,
    DualSourceCandle,
    MissingVolumeError,
    SessionMismatchError,
)
from providers.base import MarketDataProvider
from providers.alpaca_provider import AlpacaCandle, AlpacaMarketDataProvider
from providers.alpaca_websocket import (
    AlpacaStreamBar,
    AlpacaWebSocketClient,
    FormingCandleManager,
    WebSocketState,
)
from dual_source_sync_gateway import (
    DailySyncResult,
    DualSourceSyncGateway,
    FeedState,
    ScreeningEligibilityResult,
    SessionTimingInfo,
    STATE_PERMISSIONS,
    StatePermission,
)


class MockProvider(MarketDataProvider):
    """Deterministic offline mock provider with programmable responses and call tracking."""

    def __init__(
        self,
        bars_map: Optional[dict] = None,
        side_effect: Optional[Any] = None,
        metadata: Optional[dict] = None,
    ):
        self.bars_map = bars_map or {}
        self.side_effect = side_effect
        self.metadata = metadata or {"provider": "MockProvider"}
        self.fetch_calls = []

    def fetch_historical_bars(
        self,
        ticker: str,
        interval: str,
        start: datetime,
        end: datetime,
        current_time: Optional[datetime] = None,
    ):
        self.fetch_calls.append({
            "ticker": ticker,
            "interval": interval,
            "start": start,
            "end": end,
            "current_time": current_time,
        })
        if self.side_effect:
            if isinstance(self.side_effect, Exception):
                raise self.side_effect
            elif callable(self.side_effect):
                return self.side_effect(ticker, interval, start, end, current_time)
        return list(self.bars_map.get(ticker, []))

    def fetch_latest_bar(
        self,
        ticker: str,
        interval: str,
        current_time: Optional[datetime] = None,
    ) -> Optional[OHLCVCandle]:
        bars = self.bars_map.get(ticker, [])
        return bars[-1] if bars else None

    def get_provider_metadata(self):
        return dict(self.metadata)


class TestDualSourceSyncGateway(unittest.TestCase):
    """Deterministic offline unit and integration test suite for DualSourceSyncGateway."""

    @classmethod
    def setUpClass(cls):
        fixture_path = os.path.join(
            ROOT_DIR,
            "tests",
            "fixtures",
            "trader-swing",
            "sync_gateway",
            "gateway_fixtures.json",
        )
        with open(fixture_path, "r", encoding="utf-8") as f:
            cls.fixtures = json.load(f)

    def setUp(self):
        self.clock = MarketClock()
        self.eval_now = datetime(2026, 11, 16, 21, 5, tzinfo=UTC)

        # Parse Alpaca daily bars from fixtures
        self.alpaca_daily_bars = []
        for raw in self.fixtures["alpaca_completed_daily_spy"]:
            c = AlpacaCandle(
                ticker=raw["ticker"],
                interval=raw["interval"],
                timestamp=datetime.fromisoformat(raw["timestamp"]),
                open=Decimal(str(raw["open"])),
                high=Decimal(str(raw["high"])),
                low=Decimal(str(raw["low"])),
                close=Decimal(str(raw["close"])),
                volume=raw["volume"],
                status=CandleStatus.COMPLETE,
                source=raw["source"],
                is_delayed=False,
                vwap=Decimal(str(raw["vwap"])),
                trade_count=raw["trade_count"],
                feed_classification=raw["feed_classification"],
                session_date=date.fromisoformat(raw["session_date"]),
                adjustment_type="SPLIT_ADJUSTED",
                is_consolidated_volume=raw["is_consolidated_volume"],
            )
            self.alpaca_daily_bars.append(c)

        # Parse Yahoo daily bars from fixtures
        self.yahoo_daily_bars = []
        for raw in self.fixtures["yahoo_consolidated_daily_spy"]:
            c = OHLCVCandle(
                ticker=raw["ticker"],
                interval=raw["interval"],
                timestamp=datetime.fromisoformat(raw["timestamp"]),
                open=Decimal(str(raw["open"])),
                high=Decimal(str(raw["high"])),
                low=Decimal(str(raw["low"])),
                close=Decimal(str(raw["close"])),
                volume=raw["volume"],
                status=CandleStatus.COMPLETE,
                source=raw["source"],
                is_delayed=True,
            )
            c.session_date = date.fromisoformat(raw["session_date"])
            c.is_consolidated_volume = raw["is_consolidated_volume"]
            self.yahoo_daily_bars.append(c)

        # Setup standard mock providers
        self.alpaca_provider = MockProvider(
            bars_map={"SPY": self.alpaca_daily_bars},
            metadata={"provider": "Alpaca_Mock", "feed": "IEX"},
        )
        self.yahoo_provider = MockProvider(
            bars_map={"SPY": self.yahoo_daily_bars},
            metadata={"provider": "Yahoo_Mock", "feed": "SIP_CONSOLIDATED"},
        )

        self.gateway = DualSourceSyncGateway(
            alpaca_provider=self.alpaca_provider,
            yahoo_provider=self.yahoo_provider,
            clock=self.clock,
        )

    # -------------------------------------------------------------------------
    # 1. Daily Candle Synchronization: Happy Path
    # -------------------------------------------------------------------------
    def test_01_synchronize_completed_daily_happy_path(self):
        """Synchronizing completed daily candles merges Alpaca prices with Yahoo consolidated volume."""
        start = datetime(2026, 11, 10, tzinfo=UTC)
        end = datetime(2026, 11, 17, tzinfo=UTC)

        result = self.gateway.synchronize_completed_daily(
            symbol="SPY", start=start, end=end, current_time=self.eval_now
        )

        self.assertTrue(result.success)
        self.assertEqual(result.state, FeedState.LIVE_IEX_HYBRID)
        self.assertEqual(len(result.candles), 3)

        # Verify latest candle attributes
        latest = result.candles[-1]
        self.assertEqual(latest.ticker, "SPY")
        # Authoritative price action from Alpaca
        self.assertEqual(latest.open, Decimal("745.20"))
        self.assertEqual(latest.high, Decimal("747.80"))
        self.assertEqual(latest.low, Decimal("738.50"))
        self.assertEqual(latest.close, Decimal("739.96"))
        self.assertEqual(latest.vwap, Decimal("742.15"))
        self.assertEqual(latest.trade_count, 19450)

        # Authoritative volume strictly from Yahoo consolidated tape
        self.assertEqual(latest.volume, 71800000)
        # Alpaca IEX volume isolated in auxiliary_iex_volume
        self.assertEqual(latest.auxiliary_iex_volume, 1695400)
        self.assertTrue(latest.is_consolidated_volume)
        self.assertEqual(latest.price_source, "ALPACA_IEX")
        self.assertEqual(latest.volume_source, "YAHOO_CONSOLIDATED")

        # Screening eligibility check
        eligibility = self.gateway.get_screening_eligibility("SPY", current_time=self.eval_now)
        self.assertTrue(eligibility.is_eligible)
        self.assertEqual(eligibility.state, FeedState.LIVE_IEX_HYBRID)
        self.assertTrue(eligibility.is_consolidated_volume)
        self.assertEqual(eligibility.candle_status, CandleStatus.COMPLETE)
        self.assertEqual(eligibility.bar_count, 3)

    # -------------------------------------------------------------------------
    # 2. Strict Price Invariant: Alpaca Failure Aborts Scan (No Yahoo Price Failover)
    # -------------------------------------------------------------------------
    def test_02_alpaca_failure_aborts_scan_no_yahoo_price_failover(self):
        """When Alpaca price fetch fails, scan aborts with FEED_UNAVAILABLE; never substitutes Yahoo prices."""
        failing_alpaca = MockProvider(
            side_effect=ProviderUnavailableError("Alpaca REST service unavailable (HTTP 503)")
        )
        gateway = DualSourceSyncGateway(
            alpaca_provider=failing_alpaca,
            yahoo_provider=self.yahoo_provider,
            clock=self.clock,
        )

        start = datetime(2026, 11, 10, tzinfo=UTC)
        end = datetime(2026, 11, 17, tzinfo=UTC)

        result = gateway.synchronize_completed_daily(
            symbol="SPY", start=start, end=end, current_time=self.eval_now
        )

        self.assertFalse(result.success)
        self.assertEqual(result.state, FeedState.FEED_UNAVAILABLE)
        self.assertIn("Silent failover to Yahoo price is prohibited", result.error_message)

        # Verify Yahoo fetch was NEVER called for prices (or scan was aborted)
        self.assertEqual(len(self.yahoo_provider.fetch_calls), 0)

        # Verify screening is strictly blocked
        eligibility = gateway.get_screening_eligibility("SPY", current_time=self.eval_now)
        self.assertFalse(eligibility.is_eligible)
        self.assertEqual(eligibility.state, FeedState.FEED_UNAVAILABLE)

    # -------------------------------------------------------------------------
    # 3. Rate Limit Degradation
    # -------------------------------------------------------------------------
    def test_03_alpaca_rate_limit_degrades_feed(self):
        """When Alpaca returns HTTP 429, feed degrades to FEED_DEGRADED and blocks screening."""
        rate_limited_alpaca = MockProvider(
            side_effect=RateLimitError("Alpaca rate limit exceeded: 180 req/min")
        )
        gateway = DualSourceSyncGateway(
            alpaca_provider=rate_limited_alpaca,
            yahoo_provider=self.yahoo_provider,
            clock=self.clock,
        )

        start = datetime(2026, 11, 10, tzinfo=UTC)
        end = datetime(2026, 11, 17, tzinfo=UTC)

        result = gateway.synchronize_completed_daily("SPY", start=start, end=end, current_time=self.eval_now)
        self.assertFalse(result.success)
        self.assertEqual(result.state, FeedState.FEED_DEGRADED)

        eligibility = gateway.get_screening_eligibility("SPY", current_time=self.eval_now)
        self.assertFalse(eligibility.is_eligible)
        self.assertEqual(eligibility.state, FeedState.FEED_DEGRADED)

    # -------------------------------------------------------------------------
    # 4. Strict Volume Invariant: Missing Yahoo Volume -> VOLUME_UNVERIFIABLE
    # -------------------------------------------------------------------------
    def test_04_yahoo_volume_failure_transitions_to_volume_unverifiable(self):
        """When Yahoo volume is unavailable, state transitions to VOLUME_UNVERIFIABLE and blocks screening."""
        failing_yahoo = MockProvider(
            side_effect=ProviderError("Yahoo Finance composite tape unavailable")
        )
        gateway = DualSourceSyncGateway(
            alpaca_provider=self.alpaca_provider,
            yahoo_provider=failing_yahoo,
            clock=self.clock,
        )

        start = datetime(2026, 11, 10, tzinfo=UTC)
        end = datetime(2026, 11, 17, tzinfo=UTC)

        result = gateway.synchronize_completed_daily("SPY", start=start, end=end, current_time=self.eval_now)
        self.assertFalse(result.success)
        self.assertEqual(result.state, FeedState.VOLUME_UNVERIFIABLE)

        eligibility = gateway.get_screening_eligibility("SPY", current_time=self.eval_now)
        self.assertFalse(eligibility.is_eligible)
        self.assertEqual(eligibility.state, FeedState.VOLUME_UNVERIFIABLE)

    # -------------------------------------------------------------------------
    # 5. Strict Volume Invariant: Non-Positive Yahoo Volume Rejected
    # -------------------------------------------------------------------------
    def test_05_yahoo_zero_volume_rejected(self):
        """Zero volume in Yahoo feed transitions to VOLUME_UNVERIFIABLE and blocks screening."""
        zero_vol_raw = self.fixtures["yahoo_zero_volume_spy"][0]
        zero_vol_candle = OHLCVCandle(
            ticker=zero_vol_raw["ticker"],
            interval=zero_vol_raw["interval"],
            timestamp=datetime.fromisoformat(zero_vol_raw["timestamp"]),
            open=Decimal(str(zero_vol_raw["open"])),
            high=Decimal(str(zero_vol_raw["high"])),
            low=Decimal(str(zero_vol_raw["low"])),
            close=Decimal(str(zero_vol_raw["close"])),
            volume=0,  # Zero volume defect
            status=CandleStatus.COMPLETE,
            source="yahoo",
        )
        zero_vol_candle.session_date = date.fromisoformat(zero_vol_raw["session_date"])
        zero_vol_candle.is_consolidated_volume = True

        yahoo_provider = MockProvider(bars_map={"SPY": [zero_vol_candle]})
        alpaca_provider = MockProvider(bars_map={"SPY": [self.alpaca_daily_bars[-1]]})

        gateway = DualSourceSyncGateway(
            alpaca_provider=alpaca_provider,
            yahoo_provider=yahoo_provider,
            clock=self.clock,
        )

        start = datetime(2026, 11, 16, tzinfo=UTC)
        end = datetime(2026, 11, 17, tzinfo=UTC)

        result = gateway.synchronize_completed_daily("SPY", start=start, end=end, current_time=self.eval_now)
        self.assertFalse(result.success)
        self.assertEqual(result.state, FeedState.VOLUME_UNVERIFIABLE)
        self.assertIn("non-positive", result.error_message)

    # -------------------------------------------------------------------------
    # 6. Session Date Mismatch Blocks Screening
    # -------------------------------------------------------------------------
    def test_06_session_date_mismatch_blocks_screening(self):
        """Alpaca and Yahoo session date divergence transitions to FEED_DEGRADED and blocks screening."""
        mismatched_raw = self.fixtures["yahoo_mismatched_date_spy"][0]
        mismatched_candle = OHLCVCandle(
            ticker=mismatched_raw["ticker"],
            interval=mismatched_raw["interval"],
            timestamp=datetime.fromisoformat(mismatched_raw["timestamp"]),
            open=Decimal(str(mismatched_raw["open"])),
            high=Decimal(str(mismatched_raw["high"])),
            low=Decimal(str(mismatched_raw["low"])),
            close=Decimal(str(mismatched_raw["close"])),
            volume=mismatched_raw["volume"],
            status=CandleStatus.COMPLETE,
            source="yahoo",
        )
        mismatched_candle.session_date = date.fromisoformat(mismatched_raw["session_date"])  # 2026-11-17
        mismatched_candle.is_consolidated_volume = True

        yahoo_provider = MockProvider(bars_map={"SPY": [mismatched_candle]})
        alpaca_provider = MockProvider(bars_map={"SPY": [self.alpaca_daily_bars[-1]]})  # 2026-11-16

        gateway = DualSourceSyncGateway(
            alpaca_provider=alpaca_provider,
            yahoo_provider=yahoo_provider,
            clock=self.clock,
        )

        start = datetime(2026, 11, 16, tzinfo=UTC)
        end = datetime(2026, 11, 17, tzinfo=UTC)

        result = gateway.synchronize_completed_daily("SPY", start=start, end=end, current_time=self.eval_now)
        self.assertFalse(result.success)
        self.assertEqual(result.state, FeedState.FEED_DEGRADED)
        self.assertIn("Session date mismatch", result.error_message)

        eligibility = gateway.get_screening_eligibility("SPY", current_time=self.eval_now)
        self.assertFalse(eligibility.is_eligible)

    # -------------------------------------------------------------------------
    # 7. Intraday Forming Stream Updates & Invariants
    # -------------------------------------------------------------------------
    def test_07_intraday_streaming_bars_maintain_forming_state(self):
        """Streaming minute bars maintain FORMING_BAR state, isolate IEX volume, and block screening."""
        bars_data = self.fixtures["intraday_streaming_bars_spy"]

        # Bar 1 ('b'): 15:30:00Z
        bar1 = self.gateway.process_intraday_stream_bar(bars_data[0])
        self.assertEqual(bar1.status, CandleStatus.FORMING)
        self.assertFalse(bar1.is_consolidated_volume)
        self.assertEqual(bar1.close, Decimal("580.95"))
        self.assertEqual(bar1.volume, 15400)
        self.assertEqual(self.gateway.get_feed_state("SPY"), FeedState.FORMING_BAR)

        # Bar update ('u'): in-place modification of 15:30:00Z bar
        bar_update = self.gateway.process_intraday_stream_bar(bars_data[1])
        self.assertEqual(bar_update.status, CandleStatus.FORMING)
        self.assertFalse(bar_update.is_consolidated_volume)
        self.assertEqual(bar_update.close, Decimal("581.85"))
        self.assertEqual(bar_update.volume, 24200)

        # Bar 2 ('b'): 15:31:00Z archives bar1 into history
        bar2 = self.gateway.process_intraday_stream_bar(bars_data[2])
        self.assertEqual(bar2.status, CandleStatus.FORMING)
        self.assertEqual(bar2.close, Decimal("582.10"))
        self.assertEqual(len(self.gateway.get_forming_history("SPY")), 1)

        # Verify historical daily cache is untouched
        self.assertEqual(len(self.gateway.get_completed_daily_candles("SPY")), 0)

        # Verify screening is strictly blocked
        eligibility = self.gateway.get_screening_eligibility("SPY")
        self.assertFalse(eligibility.is_eligible)
        self.assertEqual(eligibility.state, FeedState.FORMING_BAR)
        self.assertIn("strictly blocks", eligibility.reason)

    # -------------------------------------------------------------------------
    # 8. Streaming Bar Deduplication and Out-of-Order Rejection
    # -------------------------------------------------------------------------
    def test_08_duplicate_and_out_of_order_streaming_bars(self):
        """Duplicate streaming bars are deduplicated; out-of-order bars are rejected."""
        bars_data = self.fixtures["intraday_streaming_bars_spy"]

        # Initial bar
        self.gateway.process_intraday_stream_bar(bars_data[0])

        # Exact duplicate timestamp (15:30:00Z) with identical type ('b')
        dup_bar = dict(bars_data[0])
        res_dup = self.gateway.process_intraday_stream_bar(dup_bar)
        self.assertEqual(res_dup.close, Decimal("580.95"))

        # Advance to 15:31:00Z
        self.gateway.process_intraday_stream_bar(bars_data[2])

        # Out-of-order bar with timestamp earlier than latest (15:29:00Z) is discarded
        stale_bar = {
            "T": "b",
            "S": "SPY",
            "o": 580.00,
            "h": 580.50,
            "l": 579.50,
            "c": 580.20,
            "v": 10000,
            "t": "2026-11-17T15:29:00Z",
        }
        res_stale = self.gateway.process_intraday_stream_bar(stale_bar)
        # Active candle timestamp must remain 15:31:00Z
        self.assertEqual(res_stale.timestamp, datetime(2026, 11, 17, 15, 31, 0, tzinfo=UTC))
        self.assertEqual(self.gateway.forming_manager.out_of_order_count, 1)

    # -------------------------------------------------------------------------
    # 9. Prohibit Intraday Bar Promotion to Daily Candle
    # -------------------------------------------------------------------------
    def test_09_prohibit_intraday_promotion_to_daily(self):
        """Attempting to promote an intraday IEX candle to a completed daily candle raises InvalidVolumeError."""
        bars_data = self.fixtures["intraday_streaming_bars_spy"]
        self.gateway.process_intraday_stream_bar(bars_data[0])

        with self.assertRaises(InvalidVolumeError):
            self.gateway.forming_manager.promote_to_daily_candle("SPY")

    # -------------------------------------------------------------------------
    # 10. Market Clock Session Timing: Regular Hours
    # -------------------------------------------------------------------------
    def test_10_market_clock_regular_hours(self):
        """Regular trading session (11:00 ET) evaluates to REGULAR and market open."""
        # 2026-11-16 11:00:00 EST = 16:00:00 UTC
        eval_dt = datetime(2026, 11, 16, 16, 0, 0, tzinfo=UTC)
        timing = self.gateway.evaluate_session_timing(eval_dt)

        self.assertTrue(timing.is_trading_day)
        self.assertFalse(timing.is_holiday)
        self.assertFalse(timing.is_early_close)
        self.assertTrue(timing.is_market_open)
        self.assertEqual(timing.session_state, MarketSessionState.REGULAR)
        self.assertEqual(timing.open_time, time(9, 30))
        self.assertEqual(timing.close_time, time(16, 0))

    # -------------------------------------------------------------------------
    # 11. Market Clock Session Timing: Pre-Market & After-Hours
    # -------------------------------------------------------------------------
    def test_11_market_clock_pre_market_and_after_hours(self):
        """Pre-market (07:00 ET) and after-hours (17:00 ET) resolve correctly with market closed."""
        # Pre-market: 07:00:00 EST = 12:00:00 UTC
        pre_dt = datetime(2026, 11, 16, 12, 0, 0, tzinfo=UTC)
        pre_timing = self.gateway.evaluate_session_timing(pre_dt)
        self.assertEqual(pre_timing.session_state, MarketSessionState.PRE_MARKET)
        self.assertFalse(pre_timing.is_market_open)

        # After-hours: 17:00:00 EST = 22:00:00 UTC
        post_dt = datetime(2026, 11, 16, 22, 0, 0, tzinfo=UTC)
        post_timing = self.gateway.evaluate_session_timing(post_dt)
        self.assertEqual(post_timing.session_state, MarketSessionState.POST_MARKET)
        self.assertFalse(post_timing.is_market_open)

    # -------------------------------------------------------------------------
    # 12. Market Clock Session Timing: Early Close (13:00 ET)
    # -------------------------------------------------------------------------
    def test_12_market_clock_early_close(self):
        """Day after Thanksgiving (2026-11-27) resolves to early close at 13:00 ET."""
        # 12:30 ET (market open): 17:30 UTC
        open_dt = datetime(2026, 11, 27, 17, 30, 0, tzinfo=UTC)
        open_timing = self.gateway.evaluate_session_timing(open_dt)
        self.assertTrue(open_timing.is_early_close)
        self.assertEqual(open_timing.close_time, time(13, 0))
        self.assertEqual(open_timing.session_state, MarketSessionState.REGULAR)
        self.assertTrue(open_timing.is_market_open)

        # 13:05 ET (market closed early): 18:05 UTC
        closed_dt = datetime(2026, 11, 27, 18, 5, 0, tzinfo=UTC)
        closed_timing = self.gateway.evaluate_session_timing(closed_dt)
        self.assertTrue(closed_timing.is_early_close)
        self.assertEqual(closed_timing.session_state, MarketSessionState.POST_MARKET)
        self.assertFalse(closed_timing.is_market_open)

    # -------------------------------------------------------------------------
    # 13. Market Clock Session Timing: Market Holiday
    # -------------------------------------------------------------------------
    def test_13_market_clock_holiday(self):
        """Thanksgiving Day (2026-11-26) resolves to holiday and market closed."""
        holiday_dt = datetime(2026, 11, 26, 16, 0, 0, tzinfo=UTC)
        timing = self.gateway.evaluate_session_timing(holiday_dt)
        self.assertFalse(timing.is_trading_day)
        self.assertTrue(timing.is_holiday)
        self.assertEqual(timing.holiday_name, "Thanksgiving Day")
        self.assertEqual(timing.session_state, MarketSessionState.CLOSED)
        self.assertFalse(timing.is_market_open)

    # -------------------------------------------------------------------------
    # 14. Market Clock Session Timing: DST Transition
    # -------------------------------------------------------------------------
    def test_14_market_clock_dst_transition(self):
        """Summer (EDT UTC-4) and Winter (EST UTC-5) timestamps resolve correct Eastern hours."""
        # Summer EDT: 2026-07-15 14:00 UTC = 10:00 EDT (Regular open)
        edt_dt = datetime(2026, 7, 15, 14, 0, 0, tzinfo=UTC)
        edt_timing = self.gateway.evaluate_session_timing(edt_dt)
        self.assertEqual(edt_timing.as_of_eastern.hour, 10)
        self.assertEqual(edt_timing.session_state, MarketSessionState.REGULAR)

        # Winter EST: 2026-12-15 15:00 UTC = 10:00 EST (Regular open)
        est_dt = datetime(2026, 12, 15, 15, 0, 0, tzinfo=UTC)
        est_timing = self.gateway.evaluate_session_timing(est_dt)
        self.assertEqual(est_timing.as_of_eastern.hour, 10)
        self.assertEqual(est_timing.session_state, MarketSessionState.REGULAR)

    # -------------------------------------------------------------------------
    # 15. Approved 8-State Readiness Matrix & Permission Enforcement
    # -------------------------------------------------------------------------
    def test_15_permission_matrix_all_8_states(self):
        """All 8 approved states adhere strictly to the Phase 7.2.1 Section 6 permission matrix."""
        # 1. COMPLETE_BAR
        p = self.gateway.get_state_permissions(FeedState.COMPLETE_BAR)
        self.assertTrue(p.permits_display)
        self.assertTrue(p.permits_historical_analysis)
        self.assertTrue(p.permits_new_long_screening)

        # 2. FORMING_BAR
        p = self.gateway.get_state_permissions(FeedState.FORMING_BAR)
        self.assertTrue(p.permits_display)
        self.assertFalse(p.permits_historical_analysis)
        self.assertFalse(p.permits_new_long_screening)

        # 3. LIVE_IEX_HYBRID
        p = self.gateway.get_state_permissions(FeedState.LIVE_IEX_HYBRID)
        self.assertTrue(p.permits_display)
        self.assertTrue(p.permits_historical_analysis)
        self.assertTrue(p.permits_new_long_screening)

        # 4. LIVE_PRICE_UNVERIFIED
        p = self.gateway.get_state_permissions(FeedState.LIVE_PRICE_UNVERIFIED)
        self.assertTrue(p.permits_display)
        self.assertFalse(p.permits_historical_analysis)
        self.assertFalse(p.permits_new_long_screening)

        # 5. DELAYED_UNVERIFIED
        p = self.gateway.get_state_permissions(FeedState.DELAYED_UNVERIFIED)
        self.assertTrue(p.permits_display)
        self.assertTrue(p.permits_historical_analysis)
        self.assertTrue(p.permits_new_long_screening)

        # 6. FEED_DEGRADED
        p = self.gateway.get_state_permissions(FeedState.FEED_DEGRADED)
        self.assertTrue(p.permits_display)
        self.assertFalse(p.permits_historical_analysis)
        self.assertFalse(p.permits_new_long_screening)

        # 7. FEED_UNAVAILABLE
        p = self.gateway.get_state_permissions(FeedState.FEED_UNAVAILABLE)
        self.assertTrue(p.permits_display)
        self.assertFalse(p.permits_historical_analysis)
        self.assertFalse(p.permits_new_long_screening)

        # 8. VOLUME_UNVERIFIABLE
        p = self.gateway.get_state_permissions(FeedState.VOLUME_UNVERIFIABLE)
        self.assertTrue(p.permits_display)
        self.assertFalse(p.permits_historical_analysis)
        self.assertFalse(p.permits_new_long_screening)

    # -------------------------------------------------------------------------
    # 16. Partial Source Recovery Safety
    # -------------------------------------------------------------------------
    def test_16_partial_recovery_does_not_promote_incomplete_data(self):
        """When only one source recovers, screening eligibility remains strictly blocked."""
        # Initial state: Both failing
        alpaca_mock = MockProvider(side_effect=ProviderUnavailableError("Alpaca down"))
        yahoo_mock = MockProvider(side_effect=ProviderError("Yahoo down"))
        gateway = DualSourceSyncGateway(
            alpaca_provider=alpaca_mock,
            yahoo_provider=yahoo_mock,
            clock=self.clock,
        )

        start = datetime(2026, 11, 10, tzinfo=UTC)
        end = datetime(2026, 11, 17, tzinfo=UTC)

        # 1. Both fail -> FEED_UNAVAILABLE
        res1 = gateway.synchronize_completed_daily("SPY", start=start, end=end, current_time=self.eval_now)
        self.assertFalse(res1.success)
        self.assertFalse(gateway.get_screening_eligibility("SPY", current_time=self.eval_now).is_eligible)

        # 2. Alpaca recovers, but Yahoo still down -> VOLUME_UNVERIFIABLE
        gateway.alpaca_provider = MockProvider(bars_map={"SPY": self.alpaca_daily_bars})
        res2 = gateway.synchronize_completed_daily("SPY", start=start, end=end, current_time=self.eval_now)
        self.assertFalse(res2.success)
        self.assertEqual(res2.state, FeedState.VOLUME_UNVERIFIABLE)
        self.assertFalse(gateway.get_screening_eligibility("SPY", current_time=self.eval_now).is_eligible)

        # 3. Yahoo recovers too -> Promotes to LIVE_IEX_HYBRID and eligible
        gateway.yahoo_provider = MockProvider(bars_map={"SPY": self.yahoo_daily_bars})
        res3 = gateway.synchronize_completed_daily("SPY", start=start, end=end, current_time=self.eval_now)
        self.assertTrue(res3.success)
        self.assertEqual(res3.state, FeedState.LIVE_IEX_HYBRID)
        self.assertTrue(gateway.get_screening_eligibility("SPY", current_time=self.eval_now).is_eligible)

    # -------------------------------------------------------------------------
    # 17. WebSocket Client Reconnection Bridge
    # -------------------------------------------------------------------------
    def test_17_websocket_reconnection_bridge(self):
        """Triggering reconnect on gateway restores WebSocket client and active subscriptions."""
        class MockWSClient:
            def __init__(self):
                self.reconnect_called = False
                self.state = WebSocketState.SUBSCRIBED
                self.subscribed_channels = {"bars": {"SPY"}}

            def reconnect(self):
                self.reconnect_called = True
                return True

        mock_ws = MockWSClient()
        gateway = DualSourceSyncGateway(
            alpaca_provider=self.alpaca_provider,
            yahoo_provider=self.yahoo_provider,
            ws_client=mock_ws,
            clock=self.clock,
        )

        success = gateway.reconnect_websocket()
        self.assertTrue(success)
        self.assertTrue(mock_ws.reconnect_called)

    # -------------------------------------------------------------------------
    # 18. Multi-Ticker Universe Synchronization & Readiness
    # -------------------------------------------------------------------------
    def test_18_universe_synchronization_with_evaluator(self):
        """Universe synchronization evaluates SPY proxy and candidate stock readiness."""
        # Generate 200 synthetic historical daily bars for SPY to satisfy evaluator
        synthetic_spy_alpaca = []
        synthetic_spy_yahoo = []
        base_date = date(2026, 11, 16)
        day_counter = 0

        # Step back through trading days to build 205 valid bars
        curr_d = base_date
        while len(synthetic_spy_alpaca) < 205:
            is_trade, _ = self.clock.is_trading_day(curr_d)
            if not is_trade and self.clock.is_historical_year_supported(curr_d.year):
                is_trade, _ = self.clock.is_historical_trading_day(curr_d)
            if is_trade:
                dt_utc = datetime.combine(curr_d, time(14, 30), tzinfo=UTC)
                a_cand = AlpacaCandle(
                    ticker="SPY",
                    interval="1d",
                    timestamp=dt_utc,
                    open=Decimal("500.00"),
                    high=Decimal("505.00"),
                    low=Decimal("495.00"),
                    close=Decimal("502.00"),
                    volume=1500000,
                    status=CandleStatus.COMPLETE,
                    source="alpaca_iex",
                    session_date=curr_d,
                    is_consolidated_volume=False,
                )
                y_cand = OHLCVCandle(
                    ticker="SPY",
                    interval="1d",
                    timestamp=dt_utc,
                    open=Decimal("500.00"),
                    high=Decimal("505.00"),
                    low=Decimal("495.00"),
                    close=Decimal("502.00"),
                    volume=55000000,
                    status=CandleStatus.COMPLETE,
                    source="yahoo",
                )
                y_cand.session_date = curr_d
                y_cand.is_consolidated_volume = True
                synthetic_spy_alpaca.append(a_cand)
                synthetic_spy_yahoo.append(y_cand)
            curr_d -= timedelta(days=1)

        # Sort chronologically ascending
        synthetic_spy_alpaca.reverse()
        synthetic_spy_yahoo.reverse()

        alpaca_mock = MockProvider(bars_map={"SPY": synthetic_spy_alpaca})
        yahoo_mock = MockProvider(bars_map={"SPY": synthetic_spy_yahoo})

        gateway = DualSourceSyncGateway(
            alpaca_provider=alpaca_mock,
            yahoo_provider=yahoo_mock,
            clock=self.clock,
        )

        start = synthetic_spy_alpaca[0].timestamp
        end = synthetic_spy_alpaca[-1].timestamp + timedelta(days=1)

        readiness = gateway.synchronize_universe(
            stock_universe=[],
            start=start,
            end=end,
            current_time=self.eval_now,
        )

        self.assertTrue(readiness.is_ready)
        self.assertEqual(readiness.status, ReadinessStatus.READY)
        self.assertIn("SPY", readiness.proxy_candles)
        self.assertGreaterEqual(len(readiness.proxy_candles["SPY"]), 200)

    # -------------------------------------------------------------------------
    # 19. Missing SPY Proxy Produces HARD_FAIL
    # -------------------------------------------------------------------------
    def test_19_missing_spy_causes_hard_fail(self):
        """Failure to synchronize mandatory SPY proxy produces ReadinessStatus.HARD_FAIL."""
        failing_alpaca = MockProvider(side_effect=ProviderUnavailableError("Alpaca endpoint down"))
        gateway = DualSourceSyncGateway(
            alpaca_provider=failing_alpaca,
            yahoo_provider=self.yahoo_provider,
            clock=self.clock,
        )

        start = datetime(2026, 11, 10, tzinfo=UTC)
        end = datetime(2026, 11, 17, tzinfo=UTC)

        readiness = gateway.synchronize_universe(
            stock_universe=["AAPL"],
            start=start,
            end=end,
            current_time=self.eval_now,
        )

        self.assertFalse(readiness.is_ready)
        self.assertEqual(readiness.status, ReadinessStatus.HARD_FAIL)
        self.assertTrue(any("SPY" in r for r in readiness.failure_reasons))

    # -------------------------------------------------------------------------
    # 20. Raw Alpaca Candle Cannot Pass Screening Eligibility Gate
    # -------------------------------------------------------------------------
    def test_20_raw_alpaca_cannot_pass_screening_eligibility(self):
        """Even if state is set to LIVE_IEX_HYBRID, an unconsolidated candle is blocked."""
        # Manually inject raw Alpaca candle with is_consolidated_volume=False
        self.gateway._completed_daily_candles["SPY"] = [self.alpaca_daily_bars[-1]]
        self.gateway.set_feed_state("SPY", FeedState.LIVE_IEX_HYBRID)

        eligibility = self.gateway.get_screening_eligibility("SPY", current_time=self.eval_now)
        self.assertFalse(eligibility.is_eligible)
        self.assertEqual(eligibility.state, FeedState.VOLUME_UNVERIFIABLE)
        self.assertIn("unconsolidated volume", eligibility.reason)


if __name__ == "__main__":
    unittest.main()
