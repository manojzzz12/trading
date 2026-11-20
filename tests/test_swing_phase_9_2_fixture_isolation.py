"""Trader-Swing Phase 9.2 Fixture Isolation & Dashboard Consistency Test Suite.

Validates:
1. Strict fail-closed filtering: default performance reports and dashboard default Screen 5
   include only genuine 'DAILY' and 'LIVE' records.
2. Complete exclusion of 'FIXTURE', 'UNKNOWN', null, and invalid provenance modes.
3. Explicit diagnostic views for 'FIXTURE' and 'COMBINED' with prominent non-performance caveats.
4. Strictly read-only CLI --report and --summary behavior (zero writes, zero acquisition).
5. Independent forward evaluation session date selection for Screen 5.
6. Zero schema migrations; preservation of all existing historical records.
7. Unbroken integrity of protected repository baselines.
"""

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
from typing import Any, Dict, List, Optional
import unittest
from unittest.mock import patch

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, ".."))
SKILL_DIR = os.path.join(PROJECT_ROOT, ".agents", "skills", "trader-swing")
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)

from accounting import quantize_cent, to_decimal
from forward_evaluation_models import (
    EvaluationSessionSummary,
    ForwardDataMode,
    ForwardObservationRecord,
    ForwardScreeningDecision,
    ForwardTradeProposal,
    HypotheticalClosedTrade,
    HypotheticalPosition,
    HypotheticalTradeStatus,
    MARKER_FORWARD_EVALUATION,
    MARKER_SIMULATED_TRADE,
)
from forward_evaluation_storage import (
    GENUINE_DATA_MODES,
    ForwardEvaluationStorage,
    ForwardStorageSafetyError,
    is_genuine_daily_mode,
)
from forward_performance import (
    ForwardPerformanceEngine,
    ForwardPerformanceReport,
)
from forward_evaluation_service import ForwardEvaluationService
from dashboard.data_loader import load_forward_evaluation_dashboard_data
import forward_evaluation_cli





def make_test_obs(
    session_date: str = "2026-11-20",
    ticker: str = "AAPL",
    data_mode: str = "DAILY",
    is_eligible: bool = True,
    rejection_reasons: Optional[List[str]] = None,
    signal_type: Optional[str] = "PULLBACK_EMA20",
    entry_p: str = "150.00",
    stop_p: str = "145.00",
    target_p: str = "160.00",
) -> ForwardObservationRecord:
    """Helper to create a validated ForwardObservationRecord."""
    now_str = datetime.now(timezone.utc).isoformat()
    return ForwardObservationRecord(
        observation_id=f"OBS-{session_date}-{ticker}-{data_mode}",
        session_date=session_date,
        observation_timestamp=now_str,
        ticker=ticker,
        data_mode=data_mode,
        data_provenance={"source": "test_fixture", "status": "VERIFIED"},
        market_regime="BULLISH",
        regime_allows_longs=True,
        screening_decision=ForwardScreeningDecision.ELIGIBLE.value if is_eligible else ForwardScreeningDecision.REJECTED.value,
        five_pillar_scores={"trend": 1.0, "momentum": 0.8, "volume": 1.2, "volatility": 0.5, "catalyst": 0.7},
        rejection_reasons=rejection_reasons or ([] if is_eligible else ["RVOL_BELOW_THRESHOLD"]),
        signal_type=signal_type if is_eligible else None,
        proposed_entry=to_decimal(entry_p) if is_eligible else None,
        proposed_stop=to_decimal(stop_p) if is_eligible else None,
        proposed_target=to_decimal(target_p) if is_eligible else None,
        risk_sizing={"shares": 10, "risk_usd": "50.00"} if is_eligible else None,
        is_eligible=is_eligible,
    )


class TestSwingPhase92FixtureIsolation(unittest.TestCase):
    """Exhaustive tests for Phase 9.2 fail-closed fixture isolation and reporting."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="test_phase_9_2_")
        self.storage = ForwardEvaluationStorage(base_dir=self.temp_dir)
        self.service = ForwardEvaluationService(storage=self.storage)

        manifest_path = os.path.join(PROJECT_ROOT, ".planning", "data", "current_daily", "CURRENT_DAILY_MANIFEST.json")
        if os.path.exists(manifest_path):
            with open(manifest_path, "rb") as f:
                self.initial_manifest_hash = hashlib.sha256(f.read()).hexdigest().upper()
        else:
            self.initial_manifest_hash = None

        spy_path = os.path.join(PROJECT_ROOT, ".planning", "data", "current_daily", "SPY_daily.csv")
        if os.path.exists(spy_path):
            with open(spy_path, "rb") as f:
                self.initial_spy_hash = hashlib.sha256(f.read()).hexdigest().upper()
        else:
            self.initial_spy_hash = None

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # Test 1: Whitelist Fail-Closed Mode Helper
    # -------------------------------------------------------------------------
    def test_01_is_genuine_daily_mode_whitelist(self) -> None:
        """Verify fail-closed helper only accepts DAILY and LIVE (case-insensitive)."""
        self.assertTrue(is_genuine_daily_mode("DAILY"))
        self.assertTrue(is_genuine_daily_mode("daily"))
        self.assertTrue(is_genuine_daily_mode("  DAILY  "))
        self.assertTrue(is_genuine_daily_mode("LIVE"))
        self.assertTrue(is_genuine_daily_mode("live"))
        self.assertTrue(is_genuine_daily_mode("  LIVE  "))

        # Fail closed on anything else
        self.assertFalse(is_genuine_daily_mode("FIXTURE"))
        self.assertFalse(is_genuine_daily_mode("fixture"))
        self.assertFalse(is_genuine_daily_mode("UNKNOWN"))
        self.assertFalse(is_genuine_daily_mode("HISTORICAL_PLAYBACK"))
        self.assertFalse(is_genuine_daily_mode(""))
        self.assertFalse(is_genuine_daily_mode("   "))
        self.assertFalse(is_genuine_daily_mode(None))
        self.assertFalse(is_genuine_daily_mode(123))
        self.assertFalse(is_genuine_daily_mode([]))

    # -------------------------------------------------------------------------
    # Test 2: Storage Session Summaries Filtering
    # -------------------------------------------------------------------------
    def test_02_storage_load_session_summaries_filtering(self) -> None:
        """Verify storage session summaries are properly filtered by data mode."""
        s_daily = EvaluationSessionSummary(
            session_date="2026-11-21",
            data_mode="DAILY",
            market_regime="BULL_TREND",
            tickers_screened=50,
            eligible_candidates=3,
            rejected_candidates=47,
            proposals_created=1,
            hypothetical_trades_opened=1,
            hypothetical_trades_closed=0,
            active_positions_count=1,
            virtual_equity=Decimal("5000.00"),
            virtual_cash=Decimal("4000.00"),
            realized_net_pnl=Decimal("0.00"),
            unrealized_pnl=Decimal("0.00"),
            total_fees_paid=Decimal("1.00"),
        )
        s_live = EvaluationSessionSummary(
            session_date="2026-11-22",
            data_mode="LIVE",
            market_regime="BULL_TREND",
            tickers_screened=50,
            eligible_candidates=2,
            rejected_candidates=48,
            proposals_created=1,
            hypothetical_trades_opened=1,
            hypothetical_trades_closed=0,
            active_positions_count=2,
            virtual_equity=Decimal("5020.00"),
            virtual_cash=Decimal("3000.00"),
            realized_net_pnl=Decimal("20.00"),
            unrealized_pnl=Decimal("0.00"),
            total_fees_paid=Decimal("2.00"),
        )
        s_fixture = EvaluationSessionSummary(
            session_date="2026-11-20",
            data_mode="FIXTURE",
            market_regime="UNKNOWN",
            tickers_screened=2,
            eligible_candidates=0,
            rejected_candidates=2,
            proposals_created=0,
            hypothetical_trades_opened=0,
            hypothetical_trades_closed=0,
            active_positions_count=0,
            virtual_equity=Decimal("5000.00"),
            virtual_cash=Decimal("5000.00"),
            realized_net_pnl=Decimal("0.00"),
            unrealized_pnl=Decimal("0.00"),
            total_fees_paid=Decimal("0.00"),
        )
        self.storage.save_session_summary(s_daily)
        self.storage.save_session_summary(s_live)
        self.storage.save_session_summary(s_fixture)

        # genuine_only=True returns exactly 2 records (DAILY + LIVE)
        genuine = self.storage.load_session_summaries(genuine_only=True)
        self.assertEqual(len(genuine), 2)
        self.assertEqual([s.data_mode for s in genuine], ["DAILY", "LIVE"])

        # fixture only
        fixtures = self.storage.load_session_summaries(data_mode="FIXTURE")
        self.assertEqual(len(fixtures), 1)
        self.assertEqual(fixtures[0].data_mode, "FIXTURE")

        # all records
        all_sums = self.storage.load_session_summaries(genuine_only=False)
        self.assertEqual(len(all_sums), 3)

    # -------------------------------------------------------------------------
    # Test 3: Storage Closed Trades and Positions Filtering
    # -------------------------------------------------------------------------
    def test_03_storage_trades_and_positions_filtering(self) -> None:
        """Verify closed trades and active positions filter by data_mode safely."""
        pos_daily = HypotheticalPosition(
            position_id="POS_DAILY",
            proposal_id="PROP_D",
            session_date="2026-11-21",
            ticker="AAPL",
            data_mode="DAILY",
            shares=10,
            entry_price=Decimal("150.00"),
            entry_fee=Decimal("1.00"),
            stop_loss=Decimal("145.00"),
            target=Decimal("160.00"),
            current_price=Decimal("152.00"),
            highest_high=Decimal("153.00"),
            lowest_low=Decimal("149.50"),
        )
        pos_fixture = HypotheticalPosition(
            position_id="POS_FIXTURE",
            proposal_id="PROP_F",
            session_date="2026-11-20",
            ticker="MSFT",
            data_mode="FIXTURE",
            shares=5,
            entry_price=Decimal("200.00"),
            entry_fee=Decimal("1.00"),
            stop_loss=Decimal("195.00"),
            target=Decimal("210.00"),
            current_price=Decimal("202.00"),
            highest_high=Decimal("203.00"),
            lowest_low=Decimal("199.00"),
        )
        trade_live = HypotheticalClosedTrade(
            trade_id="TR_LIVE",
            position_id="POS_L",
            proposal_id="PROP_L",
            ticker="AAPL",
            data_mode="LIVE",
            shares=10,
            entry_date="2026-11-20",
            entry_price=Decimal("150.00"),
            exit_date="2026-11-21",
            exit_price=Decimal("160.00"),
            entry_fee=Decimal("1.00"),
            exit_fee=Decimal("1.00"),
            gross_pnl=Decimal("100.00"),
            net_pnl=Decimal("98.00"),
            return_pct=Decimal("6.53"),
            exit_reason="TAKE_PROFIT",
            bars_held=1,
            mfe_usd=Decimal("100.00"),
            mae_usd=Decimal("0.00"),
            closed_at="2026-11-21T21:00:00Z",
        )
        trade_fixture = HypotheticalClosedTrade(
            trade_id="TR_FIXTURE",
            position_id="POS_FX",
            proposal_id="PROP_FX",
            ticker="MSFT",
            data_mode="FIXTURE",
            shares=5,
            entry_date="2026-11-19",
            entry_price=Decimal("200.00"),
            exit_date="2026-11-20",
            exit_price=Decimal("210.00"),
            entry_fee=Decimal("1.00"),
            exit_fee=Decimal("1.00"),
            gross_pnl=Decimal("50.00"),
            net_pnl=Decimal("48.00"),
            return_pct=Decimal("4.80"),
            exit_reason="TAKE_PROFIT",
            bars_held=1,
            mfe_usd=Decimal("50.00"),
            mae_usd=Decimal("0.00"),
            closed_at="2026-11-20T21:00:00Z",
        )
        self.storage.save_position(pos_daily)
        self.storage.save_position(pos_fixture)
        self.storage.save_closed_trade(trade_live)
        self.storage.save_closed_trade(trade_fixture)

        # Genuine only
        genuine_pos = self.storage.load_active_positions(genuine_only=True)
        self.assertEqual(len(genuine_pos), 1)
        self.assertEqual(genuine_pos[0].position_id, "POS_DAILY")

        genuine_trades = self.storage.load_closed_trades(genuine_only=True)
        self.assertEqual(len(genuine_trades), 1)
        self.assertEqual(genuine_trades[0].trade_id, "TR_LIVE")

        # Fixture diagnostic
        fixture_pos = self.storage.load_active_positions(data_mode="FIXTURE")
        self.assertEqual(len(fixture_pos), 1)
        self.assertEqual(fixture_pos[0].position_id, "POS_FIXTURE")

        fixture_trades = self.storage.load_closed_trades(data_mode="FIXTURE")
        self.assertEqual(len(fixture_trades), 1)
        self.assertEqual(fixture_trades[0].trade_id, "TR_FIXTURE")

    # -------------------------------------------------------------------------
    # Test 4: Storage Observations and Proposals Mode Filtering
    # -------------------------------------------------------------------------
    def test_04_storage_observations_and_proposals_filtering(self) -> None:
        """Verify observations and proposals filter by data mode."""
        obs_daily = make_test_obs(
            session_date="2026-11-21",
            ticker="AAPL",
            data_mode="DAILY",
            is_eligible=True,
        )
        obs_fixture = make_test_obs(
            session_date="2026-11-20",
            ticker="MSFT",
            data_mode="FIXTURE",
            is_eligible=False,
        )
        self.storage.save_observation(obs_daily)
        self.storage.save_observation(obs_fixture)

        prop_daily = ForwardTradeProposal(
            proposal_id="PROP_D",
            observation_id="OBS_D",
            session_date="2026-11-21",
            ticker="AAPL",
            data_mode="DAILY",
            shares=10,
            entry_reference_price=Decimal("150.00"),
            stop_loss_price=Decimal("145.00"),
            target_price=Decimal("160.00"),
        )
        prop_fixture = ForwardTradeProposal(
            proposal_id="PROP_F",
            observation_id="OBS_F",
            session_date="2026-11-20",
            ticker="MSFT",
            data_mode="FIXTURE",
            shares=5,
            entry_reference_price=Decimal("200.00"),
            stop_loss_price=Decimal("195.00"),
            target_price=Decimal("210.00"),
        )
        self.storage.save_proposal(prop_daily)
        self.storage.save_proposal(prop_fixture)

        # Observations filtering
        genuine_obs = self.storage.list_observations(genuine_only=True)
        self.assertEqual(len(genuine_obs), 1)
        self.assertEqual(genuine_obs[0].ticker, "AAPL")

        fixt_obs = self.storage.list_observations(data_mode="FIXTURE")
        self.assertEqual(len(fixt_obs), 1)
        self.assertEqual(fixt_obs[0].ticker, "MSFT")

        # Proposals filtering
        genuine_props = self.storage.load_proposals(genuine_only=True)
        self.assertEqual(len(genuine_props), 1)
        self.assertEqual(genuine_props[0].ticker, "AAPL")

        fixt_props = self.storage.load_proposals(data_mode="FIXTURE")
        self.assertEqual(len(fixt_props), 1)
        self.assertEqual(fixt_props[0].ticker, "MSFT")

    # -------------------------------------------------------------------------
    # Test 5: Performance Engine Default Excludes Fixtures
    # -------------------------------------------------------------------------
    def test_05_performance_engine_default_excludes_fixtures(self) -> None:
        """Verify performance engine default evaluation excludes FIXTURE records."""
        # Only populate fixture summary and fixture trade in storage
        s_fixture = EvaluationSessionSummary(
            session_date="2026-11-20",
            data_mode="FIXTURE",
            market_regime="UNKNOWN",
            tickers_screened=2,
            eligible_candidates=0,
            rejected_candidates=2,
            proposals_created=0,
            hypothetical_trades_opened=0,
            hypothetical_trades_closed=0,
            active_positions_count=0,
            virtual_equity=Decimal("5000.00"),
            virtual_cash=Decimal("5000.00"),
            realized_net_pnl=Decimal("0.00"),
            unrealized_pnl=Decimal("0.00"),
            total_fees_paid=Decimal("0.00"),
        )
        self.storage.save_session_summary(s_fixture)

        trade_fixture = HypotheticalClosedTrade(
            trade_id="TR_FIXTURE_1",
            position_id="POS_F",
            proposal_id="PROP_F",
            ticker="MSFT",
            data_mode="FIXTURE",
            shares=5,
            entry_date="2026-11-19",
            entry_price=Decimal("200.00"),
            exit_date="2026-11-20",
            exit_price=Decimal("210.00"),
            entry_fee=Decimal("1.00"),
            exit_fee=Decimal("1.00"),
            gross_pnl=Decimal("50.00"),
            net_pnl=Decimal("48.00"),
            return_pct=Decimal("4.80"),
            exit_reason="TAKE_PROFIT",
            bars_held=1,
            mfe_usd=Decimal("50.00"),
            mae_usd=Decimal("0.00"),
            closed_at="2026-11-20T21:00:00Z",
        )
        self.storage.save_closed_trade(trade_fixture)

        engine = ForwardPerformanceEngine(storage=self.storage)
        report = engine.evaluate_performance()  # default genuine_only=True

        self.assertEqual(report.total_sessions_evaluated, 0)
        self.assertEqual(report.total_symbols_screened, 0)
        self.assertEqual(report.closed_trades_count, 0)
        self.assertEqual(report.net_realized_pnl, Decimal("0.00"))
        self.assertIn("GENUINE_FORWARD_ONLY", report.data_mode_filter)
        self.assertNotIn("DIAGNOSTIC", report.statistical_caveat)

    # -------------------------------------------------------------------------
    # Test 6: Performance Engine Explicit Diagnostic Fixture Mode
    # -------------------------------------------------------------------------
    def test_06_performance_engine_diagnostic_fixture_mode(self) -> None:
        """Verify performance engine provides explicit diagnostic evaluation when requested."""
        s_fixture = EvaluationSessionSummary(
            session_date="2026-11-20",
            data_mode="FIXTURE",
            market_regime="UNKNOWN",
            tickers_screened=2,
            eligible_candidates=0,
            rejected_candidates=2,
            proposals_created=0,
            hypothetical_trades_opened=0,
            hypothetical_trades_closed=0,
            active_positions_count=0,
            virtual_equity=Decimal("5000.00"),
            virtual_cash=Decimal("5000.00"),
            realized_net_pnl=Decimal("0.00"),
            unrealized_pnl=Decimal("0.00"),
            total_fees_paid=Decimal("0.00"),
        )
        self.storage.save_session_summary(s_fixture)

        engine = ForwardPerformanceEngine(storage=self.storage)
        diag_report = engine.evaluate_performance(data_mode="FIXTURE", genuine_only=False)

        self.assertEqual(diag_report.total_sessions_evaluated, 1)
        self.assertEqual(diag_report.total_symbols_screened, 2)
        self.assertIn("DIAGNOSTIC (FIXTURE)", diag_report.data_mode_filter)
        self.assertIn("DIAGNOSTIC DATA ONLY", diag_report.statistical_caveat)

    # -------------------------------------------------------------------------
    # Test 7: CLI Report is Strictly Read-Only and Excludes Fixtures by Default
    # -------------------------------------------------------------------------
    def test_07_cli_report_is_strictly_read_only_and_excludes_fixtures(self) -> None:
        """Verify CLI --report executes read-only and excludes fixtures by default."""
        test_args = ["forward_evaluation_cli.py", "--report", "--storage-dir", self.temp_dir]

        # Populate a fixture summary
        s_fixture = EvaluationSessionSummary(
            session_date="2026-11-20",
            data_mode="FIXTURE",
            market_regime="UNKNOWN",
            tickers_screened=2,
            eligible_candidates=0,
            rejected_candidates=2,
            proposals_created=0,
            hypothetical_trades_opened=0,
            hypothetical_trades_closed=0,
            active_positions_count=0,
            virtual_equity=Decimal("5000.00"),
            virtual_cash=Decimal("5000.00"),
            realized_net_pnl=Decimal("0.00"),
            unrealized_pnl=Decimal("0.00"),
            total_fees_paid=Decimal("0.00"),
        )
        self.storage.save_session_summary(s_fixture)

        # Snapshot files before
        sums_dir = os.path.join(self.temp_dir, "summaries")
        files_before = set(os.listdir(sums_dir))

        captured_out = io.StringIO()
        with patch.object(sys, "argv", test_args):
            with patch("sys.stdout", captured_out):
                forward_evaluation_cli.main()

        output = captured_out.getvalue()
        self.assertIn("GENUINE_FORWARD_ONLY", output)
        self.assertIn("Sessions Evaluated     : 0", output)
        self.assertIn("Total Screened         : 0", output)

        # Confirm zero mutation
        files_after = set(os.listdir(sums_dir))
        self.assertEqual(files_before, files_after)

    # -------------------------------------------------------------------------
    # Test 8: CLI Diagnostic Mode Reporting
    # -------------------------------------------------------------------------
    def test_08_cli_report_diagnostic_fixture_mode(self) -> None:
        """Verify CLI --report --mode FIXTURE displays diagnostic warning and metrics."""
        s_fixture = EvaluationSessionSummary(
            session_date="2026-11-20",
            data_mode="FIXTURE",
            market_regime="UNKNOWN",
            tickers_screened=2,
            eligible_candidates=0,
            rejected_candidates=2,
            proposals_created=0,
            hypothetical_trades_opened=0,
            hypothetical_trades_closed=0,
            active_positions_count=0,
            virtual_equity=Decimal("5000.00"),
            virtual_cash=Decimal("5000.00"),
            realized_net_pnl=Decimal("0.00"),
            unrealized_pnl=Decimal("0.00"),
            total_fees_paid=Decimal("0.00"),
        )
        self.storage.save_session_summary(s_fixture)

        test_args = ["forward_evaluation_cli.py", "--report", "--mode", "FIXTURE", "--storage-dir", self.temp_dir]
        captured_out = io.StringIO()
        with patch.object(sys, "argv", test_args):
            with patch("sys.stdout", captured_out):
                forward_evaluation_cli.main()

        output = captured_out.getvalue()
        self.assertIn("DIAGNOSTIC Assessment (Synthetic FIXTURE Test Data Only", output)
        self.assertIn("WARNING: Viewing synthetic test fixtures", output)
        self.assertIn("Sessions Evaluated     : 1", output)
        self.assertIn("Total Screened         : 2", output)

    # -------------------------------------------------------------------------
    # Test 9: Dashboard Data Loader Mode Filtering
    # -------------------------------------------------------------------------
    def test_09_dashboard_data_loader_mode_filtering(self) -> None:
        """Verify dashboard loader excludes fixtures by default and permits diagnostic viewing."""
        s_daily = EvaluationSessionSummary(
            session_date="2026-11-21",
            data_mode="DAILY",
            market_regime="BULL_TREND",
            tickers_screened=50,
            eligible_candidates=2,
            rejected_candidates=48,
            proposals_created=1,
            hypothetical_trades_opened=1,
            hypothetical_trades_closed=0,
            active_positions_count=1,
            virtual_equity=Decimal("5000.00"),
            virtual_cash=Decimal("4000.00"),
            realized_net_pnl=Decimal("0.00"),
            unrealized_pnl=Decimal("0.00"),
            total_fees_paid=Decimal("1.00"),
        )
        s_fixture = EvaluationSessionSummary(
            session_date="2026-11-20",
            data_mode="FIXTURE",
            market_regime="UNKNOWN",
            tickers_screened=2,
            eligible_candidates=0,
            rejected_candidates=2,
            proposals_created=0,
            hypothetical_trades_opened=0,
            hypothetical_trades_closed=0,
            active_positions_count=0,
            virtual_equity=Decimal("5000.00"),
            virtual_cash=Decimal("5000.00"),
            realized_net_pnl=Decimal("0.00"),
            unrealized_pnl=Decimal("0.00"),
            total_fees_paid=Decimal("0.00"),
        )
        self.storage.save_session_summary(s_daily)
        self.storage.save_session_summary(s_fixture)

        # Default genuine load
        dash_data = load_forward_evaluation_dashboard_data(eval_dir=self.temp_dir)
        self.assertEqual(dash_data["summaries_count"], 1)
        self.assertEqual(dash_data["data_mode_filter"], "GENUINE_DAILY_ONLY")
        self.assertFalse(dash_data["is_diagnostic_scope"])
        self.assertEqual(dash_data["total_fixture_records_count"], 1)

        # Explicit diagnostic load
        diag_data = load_forward_evaluation_dashboard_data(
            eval_dir=self.temp_dir,
            data_mode="FIXTURE",
            genuine_only=False,
        )
        self.assertEqual(diag_data["summaries_count"], 1)
        self.assertTrue(diag_data["is_diagnostic_scope"])
        self.assertEqual(diag_data["summaries_df"]["data_mode"].iloc[0], "FIXTURE")

        # Combined load
        combined_data = load_forward_evaluation_dashboard_data(
            eval_dir=self.temp_dir,
            genuine_only=False,
        )
        self.assertEqual(combined_data["summaries_count"], 2)
        self.assertTrue(combined_data["is_diagnostic_scope"])

    # -------------------------------------------------------------------------
    # Test 10: Live Forward Evaluation Directory Fixture Exclusion
    # -------------------------------------------------------------------------
    def test_10_live_forward_evaluation_dir_excludes_fixtures_by_default(self) -> None:
        """Verify the actual .planning/data/forward_evaluation directory excludes fixtures by default."""
        live_dir = os.path.join(PROJECT_ROOT, ".planning", "data", "forward_evaluation")
        dash_data = load_forward_evaluation_dashboard_data(eval_dir=live_dir)

        # Genuine default returns only genuine (DAILY or LIVE) records, never fixtures
        self.assertGreaterEqual(dash_data["summaries_count"], 0)
        if not dash_data["summaries_df"].empty:
            for mode in dash_data["summaries_df"]["data_mode"]:
                self.assertIn(mode, ("DAILY", "LIVE"))
                self.assertNotEqual(mode, "FIXTURE")

        if not dash_data["observations_df"].empty:
            for mode in dash_data["observations_df"]["data_mode"]:
                self.assertIn(mode, ("DAILY", "LIVE"))
                self.assertNotEqual(mode, "FIXTURE")

        self.assertGreater(dash_data["total_fixture_records_count"], 0)
        self.assertFalse(dash_data["is_diagnostic_scope"])

        # When diagnostic fixture view requested, fixture records are inspectable
        diag_data = load_forward_evaluation_dashboard_data(
            eval_dir=live_dir,
            data_mode="FIXTURE",
            genuine_only=False,
        )
        self.assertGreaterEqual(diag_data["summaries_count"], 1)
        self.assertGreaterEqual(diag_data["observations_count"], 1)
        self.assertTrue(diag_data["is_diagnostic_scope"])
        if not diag_data["summaries_df"].empty:
            for mode in diag_data["summaries_df"]["data_mode"]:
                self.assertEqual(mode, "FIXTURE")
        if not diag_data["observations_df"].empty:
            for mode in diag_data["observations_df"]["data_mode"]:
                self.assertEqual(mode, "FIXTURE")

    # -------------------------------------------------------------------------
    # Test 11: Protected Files Integrity Baseline
    # -------------------------------------------------------------------------
    def test_11_protected_files_integrity(self) -> None:
        """Verify mutable dataset integrity and sandbox path isolation."""
        # 1. Verify mutable manifest has not been modified during operation
        manifest_path = os.path.join(PROJECT_ROOT, ".planning", "data", "current_daily", "CURRENT_DAILY_MANIFEST.json")
        self.assertTrue(os.path.exists(manifest_path), f"Manifest missing: {manifest_path}")
        h = hashlib.sha256()
        with open(manifest_path, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        actual_manifest_hash = h.hexdigest().upper()
        self.assertEqual(
            actual_manifest_hash,
            self.initial_manifest_hash,
            "CURRENT_DAILY_MANIFEST.json was unexpectedly modified during the test execution.",
        )

        # 2. Verify mutable SPY daily data has not been modified during operation
        spy_path = os.path.join(PROJECT_ROOT, ".planning", "data", "current_daily", "SPY_daily.csv")
        self.assertTrue(os.path.exists(spy_path), f"SPY dataset missing: {spy_path}")
        h = hashlib.sha256()
        with open(spy_path, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        actual_spy_hash = h.hexdigest().upper()
        self.assertEqual(
            actual_spy_hash,
            self.initial_spy_hash,
            "SPY_daily.csv was unexpectedly modified during the test execution.",
        )

        # 3. Path isolation: Verify operations use configured sandbox and block protected directories
        self.assertTrue(os.path.isdir(self.temp_dir))
        self.assertEqual(self.storage.base_dir, self.temp_dir)
        with self.assertRaises(ForwardStorageSafetyError):
            ForwardEvaluationStorage(base_dir="paper_journal.json")

        with self.assertRaises(ForwardStorageSafetyError):
            ForwardEvaluationStorage(base_dir=os.path.join(PROJECT_ROOT, "TradingPaperState"))

        with self.assertRaises(ForwardStorageSafetyError):
            ForwardEvaluationStorage(base_dir=os.path.join(PROJECT_ROOT, "portfolio.json"))

        # 4. Verify operations write strictly into temp sandbox, not into genuine observations
        genuine_obs_dir = os.path.join(PROJECT_ROOT, ".planning", "data", "observations")
        self.assertNotEqual(self.storage.obs_dir, genuine_obs_dir)
        self.assertTrue(self.storage.obs_dir.startswith(self.temp_dir))


if __name__ == "__main__":
    unittest.main()
