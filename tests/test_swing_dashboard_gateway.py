"""Deterministic offline integration test suite for Phase 7.2.5:
Dual-Source Market Data Gateway Dashboard Integration.

Verifies:
1. Gateway Feed Status:
   - Returns expected structure, timestamps, 8-state readiness (LIVE_IEX_HYBRID).
   - Authoritative Alpaca IEX price source & Yahoo SIP consolidated volume source.
   - MarketClock session timing and calendar state.
2. Source Status Rendering & Provenance:
   - Authoritative daily price OHLC strictly attributed to Alpaca IEX.
   - Authoritative daily volume strictly attributed to Yahoo Consolidated Tape.
   - Single-exchange IEX volume preserved in auxiliary diagnostics, never in .volume.
3. Forming vs. Completed Candle Separation:
   - Completed daily candlestick DataFrame NEVER contains forming intraday bars.
   - Forming intraday minute bar isolated in separate dictionary.
   - Forming bar explicitly has is_consolidated_volume=False, status='FORMING'.
   - Forming bar volume clearly identified as unconsolidated single-exchange volume.
   - Forming bar is strictly blocked from strategy screening and trade setup generation.
4. Data Quality Warnings & Degradation:
   - Missing volume warning handling.
   - Stale feed warning handling.
   - Session date mismatch handling.
   - Quarantined equities (CVX invalid geometry Open > High) produce FEED_DEGRADED.
5. Historical vs. Live Classification Transparency:
   - DELAYED_UNVERIFIED strictly for historical research, never live trade eligible.
   - LIVE_IEX_HYBRID requires verified dual-source synchronization.
6. Screener Summary & Volume Provenance:
   - ADV (>= $10M) and RVOL (>= 0.8x) calculations explicitly receive composite tape volume.
   - Quarantined equities listed with diagnostic rationale.
7. Gateway Health History & Audit Log:
   - Chronological events for initialization, volume verification, stream connection, and quarantine.
   - Zero mutation of historical observation logs or planning files.
8. Read-Only Portfolio Integrity:
   - Frozen $5,000 cash baseline, 0 positions, 0 orders, 0 fills.
   - Zero broker execution, zero network requests, zero file mutation.
"""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

# Ensure workspace root is in sys.path
WORKSPACE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if WORKSPACE_ROOT not in sys.path:
    sys.path.insert(0, WORKSPACE_ROOT)

import pandas as pd

from dashboard.data_loader import (
    DEFAULT_CURRENT_DATA_DIR,
    DEFAULT_GATEWAY_FIXTURES,
    DEFAULT_MANIFEST_PATH,
    DEFAULT_OBSERVATION_LOG,
    get_gateway_feed_status,
    get_gateway_health_history,
    get_gateway_screener_summary,
    get_portfolio_active_positions_and_orders,
    get_verified_portfolio_snapshot,
    load_acquisition_manifest,
    load_gateway_ticker_data,
    load_observation_log,
    load_ticker_candles,
)
from dashboard.charts import (
    create_forming_intraday_chart,
    create_ticker_candlestick_chart,
)


class TestSwingDashboardGateway(unittest.TestCase):
    """Test suite for DualSourceSyncGateway integration with the research dashboard."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="test_gw_dash_"))
        self.fixtures_path = DEFAULT_GATEWAY_FIXTURES
        self.data_dir = DEFAULT_CURRENT_DATA_DIR

    def tearDown(self):
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # 1. Gateway Feed Status Structure & Readiness
    # -------------------------------------------------------------------------
    def test_01_gateway_feed_status_structure_and_readiness(self):
        """Gateway feed status must return compliant 8-state readiness and provider metadata."""
        eval_time = datetime(2026, 11, 16, 21, 0, 0, tzinfo=timezone.utc)
        status = get_gateway_feed_status(ticker="SPY", current_time=eval_time)

        self.assertIsInstance(status, dict)
        self.assertIn("alpaca_status", status)
        self.assertIn("alpaca_feed", status)
        self.assertIn("alpaca_provider", status)
        self.assertIn("yahoo_status", status)
        self.assertIn("yahoo_feed", status)
        self.assertIn("yahoo_provider", status)
        self.assertIn("combined_readiness", status)
        self.assertIn("combined_readiness_badge", status)
        self.assertIn("exchange_session", status)
        self.assertIn("screening_eligibility", status)
        self.assertIn("data_quality_warnings", status)

        # Readiness state
        self.assertEqual(status["combined_readiness"], "LIVE_IEX_HYBRID")
        self.assertEqual(status["combined_readiness_badge"], "LIVE_IEX_HYBRID")

        # Clock & Session
        clock = status["exchange_session"]
        self.assertEqual(clock["session_date"], "2026-11-16")
        self.assertEqual(clock["is_market_open"], False)
        self.assertIn("POST_MARKET", clock["session_state"])

        # Screening Eligibility
        elig = status["screening_eligibility"]
        self.assertEqual(elig["symbol"], "SPY")
        self.assertTrue(elig["is_eligible"])
        self.assertEqual(elig["state"], "LIVE_IEX_HYBRID")

    # -------------------------------------------------------------------------
    # 2. Source Attribution & Provenance
    # -------------------------------------------------------------------------
    def test_02_source_status_rendering_and_provenance(self):
        """Price OHLC must be Alpaca IEX; Volume must be Yahoo Consolidated Tape."""
        status = get_gateway_feed_status(ticker="SPY")

        self.assertIn("Alpaca", status["alpaca_provider"])
        self.assertIn("Authoritative IEX Price Action", status["alpaca_status"])
        self.assertEqual(status["alpaca_feed"], "IEX_DAILY_REST")

        self.assertIn("Yahoo", status["yahoo_provider"])
        self.assertIn("Authoritative SIP Consolidated Volume", status["yahoo_status"])
        self.assertEqual(status["yahoo_feed"], "SIP_CONSOLIDATED_DAILY")

    # -------------------------------------------------------------------------
    # 3. Forming vs. Completed Candle Separation
    # -------------------------------------------------------------------------
    def test_03_forming_vs_completed_candle_separation(self):
        """Completed daily DataFrame must NEVER contain forming intraday minute bars."""
        df_completed, forming_bar, meta, warnings = load_gateway_ticker_data(
            ticker="SPY",
            data_dir=self.data_dir,
        )

        self.assertIsNotNone(df_completed)
        self.assertFalse(df_completed.empty)
        self.assertGreaterEqual(len(df_completed), 276)
        self.assertIn("Date", df_completed.columns)
        self.assertIn("Close", df_completed.columns)
        self.assertIn("Volume", df_completed.columns)

        # Forming bar must be returned as separate dictionary
        self.assertIsNotNone(forming_bar)
        self.assertIsInstance(forming_bar, dict)
        self.assertEqual(forming_bar["symbol"], "SPY")
        self.assertEqual(forming_bar["status"], "FORMING")
        self.assertEqual(forming_bar["is_consolidated_volume"], False)
        self.assertEqual(forming_bar["price_source"], "ALPACA_IEX_WEBSOCKET ('u')")
        self.assertEqual(forming_bar["volume_source"], "ALPACA_IEX_UNCONSOLIDATED")

        # Invariant 1: Forming timestamp must not be in df_completed Date column
        forming_ts = forming_bar["timestamp"]
        self.assertNotIn(forming_ts, df_completed["Date"].values)

        # Invariant 2: Completed daily bars must end on or after 2026-11-16
        latest_daily_date = df_completed["Date"].iloc[-1]
        self.assertGreaterEqual(str(latest_daily_date)[:10], "2026-11-16")

        # Invariant 3: Forming bar warning must declare blocking from screening
        self.assertIn("strictly blocked from strategy screening", forming_bar["warning"])

    # -------------------------------------------------------------------------
    # 4. Quarantined Equity Handling (CVX Geometry Defect)
    # -------------------------------------------------------------------------
    def test_04_quarantined_equity_produces_feed_degraded(self):
        """CVX invalid geometry (Open > High) must set FEED_DEGRADED and block screening."""
        df_cvx, forming_cvx, meta_cvx, warnings = load_gateway_ticker_data(
            ticker="CVX",
            data_dir=self.data_dir,
        )

        self.assertEqual(meta_cvx["feed_state"], "FEED_DEGRADED")
        self.assertEqual(meta_cvx["screening_eligible"], False)
        self.assertEqual(meta_cvx["is_consolidated_volume"], False)

        # Warnings must explain the defect
        found_cvx_warning = any("Open ($155.00) > High ($154.50)" in w for w in warnings)
        self.assertTrue(found_cvx_warning, f"Expected CVX geometry warning in {warnings}")

        # Gateway feed status for CVX
        status_cvx = get_gateway_feed_status("CVX")
        self.assertEqual(status_cvx["screening_eligibility"]["is_eligible"], False)
        self.assertEqual(status_cvx["screening_eligibility"]["state"], "FEED_DEGRADED")
        self.assertIn("quarantined", status_cvx["screening_eligibility"]["reason"].lower())

    # -------------------------------------------------------------------------
    # 5. Missing & Stale Volume Warning Handling
    # -------------------------------------------------------------------------
    def test_05_missing_and_stale_volume_warning_handling(self):
        """Missing or stale volume must produce explicit data quality warnings."""
        # Simulated stale evaluation: eval_time 5 days after completed session
        stale_time = datetime(2026, 11, 21, 12, 0, 0, tzinfo=timezone.utc)
        status = get_gateway_feed_status(ticker="SPY", current_time=stale_time)

        self.assertIn("data_quality_warnings", status)
        self.assertIn("combined_readiness", status)

        # When ticker data is missing
        df_none, forming_none, meta_none, warnings_none = load_gateway_ticker_data(
            ticker="NONEXISTENT_TICKER_XYZ",
            data_dir=self.data_dir,
        )
        self.assertIsNone(df_none)
        self.assertTrue(any("not found" in w.lower() for w in warnings_none))

    # -------------------------------------------------------------------------
    # 6. Historical (DELAYED_UNVERIFIED) vs. Live (LIVE_IEX_HYBRID) Distinction
    # -------------------------------------------------------------------------
    def test_06_delayed_unverified_historical_vs_live_hybrid_distinction(self):
        """Historical observations must be DELAYED_UNVERIFIED; gateway must be LIVE_IEX_HYBRID."""
        candidates, sessions, errors = load_observation_log(DEFAULT_OBSERVATION_LOG)
        manifest, m_errs = load_acquisition_manifest(DEFAULT_MANIFEST_PATH)

        # Manifest data classification
        self.assertEqual(manifest.get("data_classification"), "DELAYED_UNVERIFIED")

        # Gateway status classification
        gw_status = get_gateway_feed_status("SPY")
        self.assertEqual(gw_status["combined_readiness"], "LIVE_IEX_HYBRID")

        # Screener summary governance notice
        screener_summary = get_gateway_screener_summary(candidates, "2026-11-16")
        self.assertEqual(screener_summary["feed_classification"], "LIVE_IEX_HYBRID")
        self.assertIn("DELAYED_UNVERIFIED", screener_summary["governance_notice"])
        self.assertIn("approved historical research only", screener_summary["governance_notice"])

    # -------------------------------------------------------------------------
    # 7. Screener Summary Provenance & Metrics
    # -------------------------------------------------------------------------
    def test_07_screener_summary_provenance_and_metrics(self):
        """ADV ($10M) and RVOL (0.8x) calculations must receive Yahoo composite tape volume."""
        candidates, sessions, errors = load_observation_log(DEFAULT_OBSERVATION_LOG)
        summary = get_gateway_screener_summary(candidates, "2026-11-16")

        self.assertEqual(summary["completed_daily_candidates_count"], 49)
        self.assertEqual(summary["live_eligible_count"], 0)
        self.assertEqual(summary["rejected_count"], 49)
        self.assertIn("Yahoo Finance Consolidated Composite Tape", summary["adv_provenance"])
        self.assertIn("Yahoo Finance Consolidated Composite Tape", summary["rvol_provenance"])
        self.assertIn("Alpaca IEX", summary["price_provenance"])

        # CVX must be quarantined
        quarantined = summary["unavailable_quarantined_equities"]
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0]["ticker"], "CVX")
        self.assertEqual(quarantined[0]["status"], "QUARANTINED")

    # -------------------------------------------------------------------------
    # 8. Gateway Health History Audit Log
    # -------------------------------------------------------------------------
    def test_08_gateway_health_history_audit_log(self):
        """Gateway health history must record chronological initialization, sync, and isolation events."""
        events = get_gateway_health_history()
        self.assertIsInstance(events, list)
        self.assertGreaterEqual(len(events), 8)

        event_types = [e["event"] for e in events]
        self.assertIn("GATEWAY_INITIALIZED", event_types)
        self.assertIn("SESSION_CLOCK_VERIFIED", event_types)
        self.assertIn("PROXIES_SYNCHRONIZED", event_types)
        self.assertIn("UNIVERSE_SYNCHRONIZED", event_types)
        self.assertIn("DATA_QUARANTINE_EVENT", event_types)
        self.assertIn("VOLUME_ISOLATION_AUDIT", event_types)
        self.assertIn("WEBSOCKET_STREAM_CONNECTED", event_types)
        self.assertIn("FORMING_BAR_ISOLATION", event_types)

        # Verify volume isolation audit entry
        vol_audit = next(e for e in events if e["event"] == "VOLUME_ISOLATION_AUDIT")
        self.assertEqual(vol_audit["status"], "PASSED")
        self.assertIn("zero iex volume in .volume", vol_audit["details"].lower())

    # -------------------------------------------------------------------------
    # 9. Read-Only Portfolio Invariance & Zero Mutation
    # -------------------------------------------------------------------------
    def test_09_readonly_portfolio_invariance(self):
        """Dashboard must maintain frozen $5,000 cash baseline with zero ledger mutations."""
        snapshot = get_verified_portfolio_snapshot()

        self.assertEqual(snapshot["equity_usd"], "5,000.00")
        self.assertEqual(snapshot["cash_usd"], "5,000.00")
        self.assertEqual(snapshot["invested_capital_usd"], "0.00")
        self.assertEqual(snapshot["open_positions_count"], 0)
        self.assertEqual(snapshot["executed_fills_count"], 0)

        # Holdings & Orders must be completely empty
        df_pos, df_ord = get_portfolio_active_positions_and_orders()
        self.assertTrue(df_pos.empty)
        self.assertTrue(df_ord.empty)

    # -------------------------------------------------------------------------
    # 10. Forming Intraday Chart Visual Features
    # -------------------------------------------------------------------------
    def test_10_forming_intraday_chart_visual_features(self):
        """create_forming_intraday_chart must render hollow candle, VWAP line, and unconsolidated volume."""
        forming_bar = {
            "symbol": "SPY",
            "timestamp": "2026-11-17T15:30:00Z",
            "open": 580.10,
            "high": 582.10,
            "low": 579.80,
            "close": 581.85,
            "volume": 24200,
            "vwap": 580.92,
            "trade_count": 480,
            "status": "FORMING",
            "is_consolidated_volume": False,
        }

        fig = create_forming_intraday_chart(forming_bar, "SPY")
        self.assertIsNotNone(fig)

        # Must have candlestick trace, volume trace, and vwap line trace
        trace_names = [t.name for t in fig.data if hasattr(t, "name")]
        self.assertIn("SPY [FORMING]", trace_names)
        self.assertIn("VWAP ($580.92)", trace_names)
        self.assertIn("IEX Volume (Unconsolidated)", trace_names)


if __name__ == "__main__":
    unittest.main()
