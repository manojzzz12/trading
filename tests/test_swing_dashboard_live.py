"""Deterministic offline integration test suite for Phase 7.3.5:
Trader-Swing Dashboard Live Data Bridge.

Verifies:
1. Gateway data loading and source attribution (Alpaca IEX price + Yahoo consolidated volume).
2. Historical vs. Live data isolation (never mixes historical fixtures with live gateway).
3. Correct readiness badges across all 8 approved states:
   - COMPLETE_BAR, FORMING_BAR, LIVE_IEX_HYBRID, LIVE_PRICE_UNVERIFIED,
     DELAYED_UNVERIFIED, FEED_DEGRADED, FEED_UNAVAILABLE, VOLUME_UNVERIFIABLE.
4. No false LIVE_IEX_HYBRID status for cached, fixture, stale, or offline data.
5. Forming candles strictly excluded from screening and daily technical indicators.
6. 500-item circular buffer capacity, FIFO eviction, bounded memory, and clean disconnect.
7. No IEX volume leakage into liquidity calculations or daily .volume.
8. Missing volume, stale data, and gateway errors fail closed (no silent fallback).
9. Portfolio remains strictly read-only ($5,000 cash baseline, 0 positions, 0 orders, 0 fills).
10. Strategy and risk parameters strictly preserved (RVOL=0.8000, risk=0.50%).
"""

from collections import deque
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock

# Ensure workspace root is in sys.path
WORKSPACE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if WORKSPACE_ROOT not in sys.path:
    sys.path.insert(0, WORKSPACE_ROOT)

SWING_DIR = os.path.join(WORKSPACE_ROOT, ".agents", "skills", "trader-swing")
if SWING_DIR not in sys.path:
    sys.path.insert(0, SWING_DIR)

import pandas as pd

from dashboard.data_loader import (
    DEFAULT_CURRENT_DATA_DIR,
    DEFAULT_GATEWAY_FIXTURES,
    DEFAULT_MANIFEST_PATH,
    DEFAULT_OBSERVATION_LOG,
    READINESS_BADGE_MAP,
    StreamingMessageBuffer,
    candles_to_dataframe,
    get_gateway_feed_status,
    get_gateway_screener_summary,
    get_readiness_badge,
    get_streaming_buffer,
    get_verified_portfolio_snapshot,
    get_portfolio_active_positions_and_orders,
    load_gateway_ticker_data,
    load_live_gateway_data,
)

from dual_source_sync_gateway import (
    DailySyncResult,
    DualSourceSyncGateway,
    FeedState,
    ScreeningEligibilityResult,
)
from dual_source_aggregator import DualSourceCandle
from market_data_models import CandleStatus, OHLCVCandle
from risk import RiskConfig
from scanner_models import ScreeningCriteria


def _make_mock_dual_source_candle(
    ticker: str,
    dt: datetime,
    close: float = 580.0,
    volume: int = 50000000,
    iex_volume: int = 1500000,
    is_consolidated: bool = True,
    status: CandleStatus = CandleStatus.COMPLETE,
) -> DualSourceCandle:
    """Helper to generate mock DualSourceCandle for offline testing."""
    return DualSourceCandle(
        ticker=ticker,
        interval="1d",
        timestamp=dt,
        open=Decimal(str(close - 1.0)),
        high=Decimal(str(close + 2.0)),
        low=Decimal(str(close - 2.0)),
        close=Decimal(str(close)),
        volume=volume,
        auxiliary_iex_volume=iex_volume,
        is_consolidated_volume=is_consolidated,
        status=status,
    )


class TestSwingDashboardLiveBridge(unittest.TestCase):
    """Test suite for Phase 7.3.5 Dashboard Live Data Bridge integration."""

    def setUp(self):
        self.data_dir = DEFAULT_CURRENT_DATA_DIR
        self.now_utc = datetime(2026, 11, 16, 21, 5, 0, tzinfo=timezone.utc)

    # -------------------------------------------------------------------------
    # 1. Gateway Data Loading & Source Attribution
    # -------------------------------------------------------------------------
    def test_01_gateway_data_loading_and_source_attribution(self):
        """Live gateway loading must preserve Alpaca IEX price + Yahoo consolidated volume provenance."""
        mock_gateway = MagicMock(spec=DualSourceSyncGateway)
        candles = [
            _make_mock_dual_source_candle(
                "SPY",
                datetime(2026, 11, 16, 14, 30, tzinfo=timezone.utc) - timedelta(days=i),
                close=580.0 - i,
                volume=60000000 + i * 100000,
                iex_volume=1500000,
                is_consolidated=True,
                status=CandleStatus.COMPLETE,
            )
            for i in range(30, -1, -1)
        ]

        mock_gateway.synchronize_completed_daily.return_value = DailySyncResult(
            symbol="SPY",
            state=FeedState.LIVE_IEX_HYBRID,
            success=True,
            candles=candles,
            alpaca_candle_count=len(candles),
            yahoo_candle_count=len(candles),
        )
        mock_gateway.get_feed_state.return_value = FeedState.LIVE_IEX_HYBRID
        mock_gateway.get_screening_eligibility.return_value = ScreeningEligibilityResult(
            symbol="SPY",
            is_eligible=True,
            state=FeedState.LIVE_IEX_HYBRID,
            reason="Verified dual-source hybrid candle.",
            is_consolidated_volume=True,
        )
        mock_gateway.forming_manager = None

        df_completed, forming_bar, meta, warnings = load_live_gateway_data(
            ticker="SPY",
            gateway=mock_gateway,
            current_time=self.now_utc,
        )

        self.assertIsNotNone(df_completed)
        self.assertEqual(len(df_completed), 31)
        self.assertIsNone(forming_bar)
        self.assertEqual(meta["feed_state"], "LIVE_IEX_HYBRID")
        self.assertEqual(meta["screening_eligible"], True)
        self.assertEqual(meta["is_consolidated_volume"], True)
        self.assertIn("Alpaca IEX", meta["price_source"])
        self.assertIn("Yahoo Composite Tape", meta["volume_source"])

        # Check technical overlays computed on completed Close prices
        self.assertIn("SMA20", df_completed.columns)
        self.assertIn("RSI14", df_completed.columns)
        self.assertIsNotNone(df_completed["SMA20"].iloc[-1])
        self.assertIsNotNone(df_completed["RSI14"].iloc[-1])

    # -------------------------------------------------------------------------
    # 2. Historical vs. Live Data Isolation
    # -------------------------------------------------------------------------
    def test_02_historical_and_live_data_isolation(self):
        """Historical data mode must strictly report DELAYED_UNVERIFIED and never touch live gateway."""
        df_hist, forming_hist, meta_hist, warnings_hist = load_gateway_ticker_data(
            ticker="SPY",
            data_dir=self.data_dir,
            data_mode="HISTORICAL",
        )

        self.assertIsNotNone(df_hist)
        self.assertIsNone(forming_hist)
        self.assertEqual(meta_hist["feed_state"], "DELAYED_UNVERIFIED")
        self.assertEqual(meta_hist["screening_eligible"], True)
        self.assertIn("Historical", meta_hist["price_source"])
        self.assertIn("Historical", meta_hist["volume_source"])

        # Live mode must require gateway or live fixtures
        status_hist = get_gateway_feed_status(ticker="SPY", data_mode="HISTORICAL")
        self.assertEqual(status_hist["combined_readiness"], "DELAYED_UNVERIFIED")
        self.assertEqual(status_hist["combined_readiness_badge"], "DELAYED_UNVERIFIED")
        self.assertIn("OFFLINE_ARCHIVE", status_hist["alpaca_status"])

    # -------------------------------------------------------------------------
    # 3. Correct Readiness Badges for All 8 Approved States
    # -------------------------------------------------------------------------
    def test_03_correct_readiness_badges_for_all_approved_states(self):
        """All 8 approved feed readiness states must map to valid CSS badge classes and exact labels."""
        expected_states = [
            ("COMPLETE_BAR", "badge-complete", "COMPLETE_BAR"),
            ("FORMING_BAR", "badge-forming", "FORMING_BAR"),
            ("LIVE_IEX_HYBRID", "badge-hybrid", "LIVE_IEX_HYBRID"),
            ("LIVE_PRICE_UNVERIFIED", "badge-live-price", "LIVE_PRICE_UNVERIFIED"),
            ("DELAYED_UNVERIFIED", "badge-info", "DELAYED_UNVERIFIED"),
            ("FEED_DEGRADED", "badge-degraded", "FEED_DEGRADED"),
            ("FEED_UNAVAILABLE", "badge-danger", "FEED_UNAVAILABLE"),
            ("VOLUME_UNVERIFIABLE", "badge-unverifiable", "VOLUME_UNVERIFIABLE"),
        ]

        for state_str, exp_cls, exp_lbl in expected_states:
            css_cls, lbl = get_readiness_badge(state_str)
            self.assertEqual(css_cls, exp_cls, f"Mismatch CSS for state {state_str}")
            self.assertEqual(lbl, exp_lbl, f"Mismatch label for state {state_str}")

            # Test Enum variant as well
            enum_val = FeedState(state_str)
            css_cls_enum, lbl_enum = get_readiness_badge(enum_val)
            self.assertEqual(css_cls_enum, exp_cls)
            self.assertEqual(lbl_enum, exp_lbl)

    # -------------------------------------------------------------------------
    # 4. No False LIVE_IEX_HYBRID for Cached or Offline Data
    # -------------------------------------------------------------------------
    def test_04_no_false_live_iex_hybrid_for_cached_or_offline_data(self):
        """Offline, cached, or historical data must NEVER be classified as LIVE_IEX_HYBRID."""
        # Offline flag
        st_offline = get_gateway_feed_status(ticker="SPY", is_offline=True)
        self.assertEqual(st_offline["combined_readiness"], "DELAYED_UNVERIFIED")
        self.assertNotEqual(st_offline["combined_readiness"], "LIVE_IEX_HYBRID")

        # Cached flag
        st_cached = get_gateway_feed_status(ticker="SPY", is_cached=True)
        self.assertEqual(st_cached["combined_readiness"], "DELAYED_UNVERIFIED")
        self.assertNotEqual(st_cached["combined_readiness"], "LIVE_IEX_HYBRID")

        # Historical mode
        st_hist = get_gateway_feed_status(ticker="SPY", data_mode="HISTORICAL")
        self.assertEqual(st_hist["combined_readiness"], "DELAYED_UNVERIFIED")
        self.assertNotEqual(st_hist["combined_readiness"], "LIVE_IEX_HYBRID")

        # Ticker loader historical mode
        _, _, meta_h, _ = load_gateway_ticker_data("SPY", data_dir=self.data_dir, data_mode="HISTORICAL")
        self.assertEqual(meta_h["feed_state"], "DELAYED_UNVERIFIED")
        self.assertNotEqual(meta_h["feed_state"], "LIVE_IEX_HYBRID")

    # -------------------------------------------------------------------------
    # 5. Forming Candles Excluded from Screening & Daily Indicators
    # -------------------------------------------------------------------------
    def test_05_forming_candles_excluded_from_screening_and_daily_indicators(self):
        """Forming minute bars must be isolated in forming_bar with is_consolidated_volume=False."""
        mock_gateway = MagicMock(spec=DualSourceSyncGateway)
        completed_candle = _make_mock_dual_source_candle(
            "SPY",
            datetime(2026, 11, 16, 14, 30, tzinfo=timezone.utc),
            close=580.0,
            volume=50000000,
            is_consolidated=True,
            status=CandleStatus.COMPLETE,
        )

        mock_gateway.synchronize_completed_daily.return_value = DailySyncResult(
            symbol="SPY",
            state=FeedState.LIVE_IEX_HYBRID,
            success=True,
            candles=[completed_candle],
        )

        mock_forming_mgr = MagicMock()
        mock_forming_candle = MagicMock()
        mock_forming_candle.timestamp = datetime(2026, 11, 17, 15, 30, tzinfo=timezone.utc)
        mock_forming_candle.open = Decimal("580.10")
        mock_forming_candle.high = Decimal("582.10")
        mock_forming_candle.low = Decimal("579.80")
        mock_forming_candle.close = Decimal("581.85")
        mock_forming_candle.volume = 24200  # IEX unconsolidated
        mock_forming_candle.vwap = Decimal("580.92")
        mock_forming_candle.trade_count = 480
        mock_forming_mgr.get_forming_candle.return_value = mock_forming_candle
        mock_gateway.forming_manager = mock_forming_mgr

        df_completed, forming_bar, meta, warnings = load_live_gateway_data(
            ticker="SPY",
            gateway=mock_gateway,
            current_time=self.now_utc,
        )

        self.assertIsNotNone(forming_bar)
        self.assertEqual(forming_bar["symbol"], "SPY")
        self.assertEqual(forming_bar["status"], "FORMING")
        self.assertEqual(forming_bar["is_consolidated_volume"], False)
        self.assertIn("ALPACA_IEX_UNCONSOLIDATED", forming_bar["volume_source"])
        self.assertIn("strictly blocked from strategy screening", forming_bar["warning"])

        # Forming bar timestamp must NOT be in df_completed
        self.assertEqual(len(df_completed), 1)
        self.assertEqual(str(df_completed["Date"].iloc[-1].date()), "2026-11-16")

    # -------------------------------------------------------------------------
    # 6. Streaming Circular Buffer Capacity & Eviction (500 Items)
    # -------------------------------------------------------------------------
    def test_06_streaming_buffer_500_item_capacity_and_fifo_eviction(self):
        """StreamingMessageBuffer must enforce strictly bounded 500-item memory and FIFO eviction."""
        buf = StreamingMessageBuffer(max_capacity=500)
        self.assertEqual(buf.capacity, 500)
        self.assertEqual(buf.current_size, 0)
        self.assertEqual(buf.dropped_count, 0)
        self.assertFalse(buf.is_connected)

        # Connection handling
        buf.set_connected(True)
        self.assertTrue(buf.is_connected)

        # Append exactly 500 items
        for i in range(500):
            buf.append({
                "symbol": "SPY",
                "seq": i,
                "price": 580.0 + (i * 0.01),
                "volume": 1000 + i,
            })

        self.assertEqual(buf.current_size, 500)
        self.assertEqual(buf.total_received, 500)
        self.assertEqual(buf.dropped_count, 0)

        # Append 100 more items (total 600) -> 100 items must be deterministically evicted
        for j in range(500, 600):
            buf.append({
                "symbol": "SPY",
                "seq": j,
                "price": 580.0 + (j * 0.01),
                "volume": 1000 + j,
            })

        self.assertEqual(buf.current_size, 500)  # Strictly bounded at 500
        self.assertEqual(buf.total_received, 600)
        self.assertEqual(buf.dropped_count, 100)

        # Verify FIFO eviction: oldest 100 items (seq 0-99) were dropped, first item is seq 100
        messages_chrono = buf.get_messages(reverse=False)
        self.assertEqual(messages_chrono[0]["seq"], 100)
        self.assertEqual(messages_chrono[-1]["seq"], 599)

        # Verify newest-first retrieval
        messages_newest = buf.get_messages(reverse=True)
        self.assertEqual(messages_newest[0]["seq"], 599)

        # DataFrame conversion
        df_buf = buf.to_dataframe(limit=10)
        self.assertIsInstance(df_buf, pd.DataFrame)
        self.assertEqual(len(df_buf), 10)
        self.assertEqual(df_buf.iloc[0]["seq"], 599)

        # Clean disconnect
        buf.set_connected(False)
        self.assertFalse(buf.is_connected)

        # Clear
        buf.clear()
        self.assertEqual(buf.current_size, 0)
        self.assertEqual(buf.dropped_count, 0)

    # -------------------------------------------------------------------------
    # 7. No IEX Volume Leakage into Liquidity Calculations
    # -------------------------------------------------------------------------
    def test_07_no_iex_volume_leakage_into_liquidity_calculations(self):
        """Raw single-exchange IEX volume must never leak into candle.volume or liquidity."""
        candle = _make_mock_dual_source_candle(
            "SPY",
            datetime(2026, 11, 16, 14, 30, tzinfo=timezone.utc),
            close=580.0,
            volume=71800000,         # Consolidated SIP volume
            iex_volume=1695400,       # Single-exchange IEX volume
            is_consolidated=True,
        )

        df = candles_to_dataframe([candle])
        self.assertEqual(df["Volume"].iloc[0], 71800000)
        self.assertNotEqual(df["Volume"].iloc[0], 1695400)

        # If a candle with is_consolidated_volume=False is passed, fail closed
        bad_candle = _make_mock_dual_source_candle(
            "SPY",
            datetime(2026, 11, 16, 14, 30, tzinfo=timezone.utc),
            close=580.0,
            volume=1695400,
            iex_volume=1695400,
            is_consolidated=False,  # Unconsolidated!
        )

        mock_gateway = MagicMock()
        mock_gateway.synchronize_completed_daily.return_value = DailySyncResult(
            symbol="SPY",
            state=FeedState.LIVE_IEX_HYBRID,
            success=True,
            candles=[bad_candle],
        )

        df_bad, _, meta_bad, warnings_bad = load_live_gateway_data("SPY", mock_gateway, self.now_utc)
        self.assertIsNone(df_bad)
        self.assertEqual(meta_bad["feed_state"], "VOLUME_UNVERIFIABLE")
        self.assertEqual(meta_bad["screening_eligible"], False)
        self.assertTrue(any("Unconsolidated volume detected" in w for w in warnings_bad))

    # -------------------------------------------------------------------------
    # 8. Missing Volume, Stale Data, and Gateway Errors Fail Closed
    # -------------------------------------------------------------------------
    def test_08_missing_volume_stale_data_and_gateway_errors(self):
        """Gateway failure states must fail closed without silent fallback to fixtures."""
        mock_gateway = MagicMock(spec=DualSourceSyncGateway)

        # 1. Missing volume (VOLUME_UNVERIFIABLE)
        mock_gateway.synchronize_completed_daily.return_value = DailySyncResult(
            symbol="SPY",
            state=FeedState.VOLUME_UNVERIFIABLE,
            success=False,
            error_message="Yahoo consolidated volume missing for session 2026-11-16",
        )
        df, forming, meta, warnings = load_live_gateway_data("SPY", mock_gateway, self.now_utc)
        self.assertIsNone(df)
        self.assertIsNone(forming)
        self.assertEqual(meta["feed_state"], "VOLUME_UNVERIFIABLE")
        self.assertEqual(meta["screening_eligible"], False)
        self.assertTrue(any("Yahoo consolidated volume missing" in w for w in warnings))

        # 2. Alpaca price unavailable (FEED_UNAVAILABLE) - No Yahoo price failover
        mock_gateway.synchronize_completed_daily.return_value = DailySyncResult(
            symbol="SPY",
            state=FeedState.FEED_UNAVAILABLE,
            success=False,
            error_message="Alpaca REST price endpoint unavailable; silent failover prohibited",
        )
        df_u, _, meta_u, warnings_u = load_live_gateway_data("SPY", mock_gateway, self.now_utc)
        self.assertIsNone(df_u)
        self.assertEqual(meta_u["feed_state"], "FEED_UNAVAILABLE")
        self.assertEqual(meta_u["screening_eligible"], False)

        # 3. Upstream geometry defect (FEED_DEGRADED)
        mock_gateway.synchronize_completed_daily.return_value = DailySyncResult(
            symbol="CVX",
            state=FeedState.FEED_DEGRADED,
            success=False,
            error_message="Open > High invalid geometry defect",
        )
        df_d, _, meta_d, warnings_d = load_live_gateway_data("CVX", mock_gateway, self.now_utc)
        self.assertIsNone(df_d)
        self.assertEqual(meta_d["feed_state"], "FEED_DEGRADED")
        self.assertEqual(meta_d["screening_eligible"], False)

    # -------------------------------------------------------------------------
    # 9. Portfolio Remains Strictly Read-Only
    # -------------------------------------------------------------------------
    def test_09_portfolio_remains_strictly_readonly(self):
        """Dashboard must maintain frozen $5,000 cash baseline with zero ledger mutations."""
        snapshot = get_verified_portfolio_snapshot()
        self.assertEqual(snapshot["equity_usd"], "5,000.00")
        self.assertEqual(snapshot["cash_usd"], "5,000.00")
        self.assertEqual(snapshot["invested_capital_usd"], "0.00")
        self.assertEqual(snapshot["open_positions_count"], 0)
        self.assertEqual(snapshot["executed_fills_count"], 0)
        self.assertEqual(snapshot["max_simultaneous_positions_limit"], 4)
        self.assertEqual(snapshot["risk_limit_per_trade_pct"], "0.50%")
        self.assertEqual(snapshot["max_risk_per_trade_usd"], "$25.00")

        # Active positions and orders tables must be completely empty
        df_pos, df_ord = get_portfolio_active_positions_and_orders()
        self.assertTrue(df_pos.empty)
        self.assertTrue(df_ord.empty)

    # -------------------------------------------------------------------------
    # 10. Strategy & Risk Parameters Invariance
    # -------------------------------------------------------------------------
    def test_10_strategy_and_risk_parameters_invariance(self):
        """Strategy parameters and risk sizing rules must remain frozen."""
        risk_cfg = RiskConfig()
        self.assertEqual(risk_cfg.risk_per_trade_pct, Decimal("0.005"))
        self.assertEqual(risk_cfg.starting_virtual_capital_usd, Decimal("5000.00"))
        self.assertEqual(risk_cfg.max_simultaneous_positions, 4)

        criteria = ScreeningCriteria()
        self.assertEqual(criteria.min_rvol, Decimal("0.8000"))
        self.assertEqual(criteria.min_price, Decimal("10.00"))
        self.assertEqual(criteria.max_price, Decimal("2000.00"))


if __name__ == "__main__":
    unittest.main()
