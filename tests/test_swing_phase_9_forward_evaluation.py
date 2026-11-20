"""Comprehensive Acceptance and Verification Suite for Trader-Swing Phase 9.

Phase 9: Forward Paper Trading Evaluation Acceptance Tests:
1.  test_01_valid_observation_recording
2.  test_02_duplicate_observation_rejection_same_date_ticker_mode
3.  test_03_same_ticker_different_sessions_accepted
4.  test_04_same_session_different_data_modes_accepted
5.  test_05_invalid_or_stale_data_rejection_fails_closed
6.  test_06_forming_bar_exclusion
7.  test_07_rejected_candidate_cannot_create_trade
8.  test_08_eligible_candidate_creates_hypothetical_proposal_only
9.  test_09_stop_loss_evaluation_exit
10. test_10_take_profit_evaluation_exit
11. test_11_same_bar_stop_target_ambiguity_conservative_stop_first
12. test_12_holding_period_tracking_and_max_duration_exit
13. test_13_mfe_and_mae_tracking
14. test_14_fee_and_pnl_calculations
15. test_15_position_and_exposure_constraints_max_4_and_cash_reserve
16. test_16_crash_recovery_and_atomic_persistence
17. test_17_benchmark_alignment_spy_comparison
18. test_18_open_vs_closed_trade_separation
19. test_19_dashboard_read_only_behavior_and_marker
20. test_20_historical_live_partition_isolation
21. test_21_no_modification_to_existing_portfolio_journal_orders
22. test_22_wilson_score_and_sample_size_statistical_honesty
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
import shutil
import sys
import tempfile
from typing import Any, Dict, List, Optional, Sequence
import unittest

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, ".."))
SKILLS_DIR = os.path.join(PROJECT_ROOT, ".agents", "skills", "trader-swing")
if SKILLS_DIR not in sys.path:
    sys.path.insert(0, SKILLS_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from accounting import AccountingLedger, quantize_cent, to_decimal
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
    DuplicateObservationError,
    ForwardEvaluationStorage,
    ForwardStorageSafetyError,
)
from forward_trade_tracker import ForwardTradeTracker
from forward_performance import (
    ForwardPerformanceEngine,
    ForwardPerformanceReport,
    compute_wilson_score_interval,
)
from forward_evaluation_service import ForwardEvaluationService
from market_data_models import CandleStatus, OHLCVCandle, UTC
from risk import RiskConfig
from dashboard.data_loader import load_forward_evaluation_dashboard_data





def make_eval_candle(
    ticker: str,
    dt_str: str,
    open_p: str,
    high_p: str,
    low_p: str,
    close_p: str,
    volume: int = 1_500_000,
    status: CandleStatus = CandleStatus.COMPLETE,
) -> OHLCVCandle:
    """Helper to build a strongly-typed OHLCVCandle."""
    dt = datetime.strptime(dt_str, "%Y-%m-%d").replace(tzinfo=UTC)
    return OHLCVCandle(
        ticker=ticker,
        interval="1d",
        timestamp=dt,
        open=to_decimal(open_p),
        high=to_decimal(high_p),
        low=to_decimal(low_p),
        close=to_decimal(close_p),
        volume=volume,
        status=status,
    )


def make_test_obs(
    session_date: str = "2026-11-20",
    ticker: str = "AAPL",
    data_mode: str = ForwardDataMode.LIVE.value,
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


class TestSwingPhase9ForwardEvaluation(unittest.TestCase):
    """Rigorous acceptance test suite for Phase 9 Forward Paper Trading Evaluation."""

    def setUp(self) -> None:
        """Create fresh isolated evaluation sandbox."""
        self.test_dir = tempfile.mkdtemp(prefix="trader_swing_phase9_test_")
        self.storage = ForwardEvaluationStorage(base_dir=self.test_dir)
        self.risk_config = RiskConfig()
        self.tracker = ForwardTradeTracker(
            storage=self.storage,
            initial_capital=Decimal("5000.00"),
            risk_config=self.risk_config,
        )

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
        """Clean up isolated test sandbox."""
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # Test 1: Valid observation recording
    # -------------------------------------------------------------------------
    def test_01_valid_observation_recording(self) -> None:
        record = make_test_obs(
            session_date="2026-11-20",
            ticker="AAPL",
            data_mode=ForwardDataMode.LIVE.value,
            is_eligible=True,
        )
        self.storage.save_observation(record)

        loaded = self.storage.list_observations(session_date="2026-11-20")
        self.assertEqual(len(loaded), 1)
        obs = loaded[0]
        self.assertEqual(obs.ticker, "AAPL")
        self.assertEqual(obs.session_date, "2026-11-20")
        self.assertEqual(obs.record_marker, MARKER_FORWARD_EVALUATION)
        self.assertEqual(obs.disclaimer, MARKER_SIMULATED_TRADE)
        self.assertTrue(obs.is_eligible)
        self.assertEqual(obs.signal_type, "PULLBACK_EMA20")

    # -------------------------------------------------------------------------
    # Test 2: Duplicate observation rejection (same date, ticker, data_mode)
    # -------------------------------------------------------------------------
    def test_02_duplicate_observation_rejection_same_date_ticker_mode(self) -> None:
        record = make_test_obs(
            session_date="2026-11-20",
            ticker="AAPL",
            data_mode=ForwardDataMode.LIVE.value,
            is_eligible=True,
        )
        self.storage.save_observation(record)

        with self.assertRaises(DuplicateObservationError) as ctx:
            self.storage.save_observation(record)
        self.assertIn("Duplicate observation rejected", str(ctx.exception))

    # -------------------------------------------------------------------------
    # Test 3: Same ticker different sessions accepted
    # -------------------------------------------------------------------------
    def test_03_same_ticker_different_sessions_accepted(self) -> None:
        rec1 = make_test_obs(session_date="2026-11-19", ticker="AAPL")
        rec2 = make_test_obs(session_date="2026-11-20", ticker="AAPL")
        self.storage.save_observation(rec1)
        self.storage.save_observation(rec2)

        all_obs = self.storage.list_observations()
        self.assertEqual(len(all_obs), 2)
        dates = {o.session_date for o in all_obs}
        self.assertEqual(dates, {"2026-11-19", "2026-11-20"})

    # -------------------------------------------------------------------------
    # Test 4: Same session different data modes accepted
    # -------------------------------------------------------------------------
    def test_04_same_session_different_data_modes_accepted(self) -> None:
        rec_live = make_test_obs(session_date="2026-11-20", ticker="AAPL", data_mode=ForwardDataMode.LIVE.value)
        rec_fixt = make_test_obs(session_date="2026-11-20", ticker="AAPL", data_mode=ForwardDataMode.FIXTURE.value)
        self.storage.save_observation(rec_live)
        self.storage.save_observation(rec_fixt)

        loaded_live = self.storage.list_observations(session_date="2026-11-20", data_mode=ForwardDataMode.LIVE.value)
        loaded_fixt = self.storage.list_observations(session_date="2026-11-20", data_mode=ForwardDataMode.FIXTURE.value)
        self.assertEqual(len(loaded_live), 1)
        self.assertEqual(len(loaded_fixt), 1)

    # -------------------------------------------------------------------------
    # Test 5: Invalid or stale data rejection fails closed
    # -------------------------------------------------------------------------
    def test_05_invalid_or_stale_data_rejection_fails_closed(self) -> None:
        service = ForwardEvaluationService(
            storage=self.storage,
            virtual_starting_capital=Decimal("5000.00"),
        )
        # 1. Missing candles raises ValueError
        with self.assertRaises(ValueError) as ctx1:
            service.run_forward_session(
                session_date="2026-11-20",
                proxy_candles_map={"SPY": [make_eval_candle("SPY", "2026-11-20", "580", "585", "579", "582")]},
                stock_candles_map={"AAPL": []},
            )
        self.assertIn("Zero candles provided", str(ctx1.exception))

        # 2. Malformed OHLC geometry: Low > High raises ValueError
        bad_candle = make_eval_candle("AAPL", "2026-11-20", "150.00", "145.00", "155.00", "150.00")
        with self.assertRaises(ValueError) as ctx2:
            service.run_forward_session(
                session_date="2026-11-20",
                proxy_candles_map={"SPY": [make_eval_candle("SPY", "2026-11-20", "580", "585", "579", "582")]},
                stock_candles_map={"AAPL": [bad_candle]},
            )
        self.assertIn("Malformed OHLC geometry", str(ctx2.exception))

    # -------------------------------------------------------------------------
    # Test 6: Forming bar exclusion
    # -------------------------------------------------------------------------
    def test_06_forming_bar_exclusion(self) -> None:
        service = ForwardEvaluationService(
            storage=self.storage,
            virtual_starting_capital=Decimal("5000.00"),
        )
        forming_candle = make_eval_candle(
            "AAPL", "2026-11-20", "150.00", "155.00", "149.00", "153.00", status=CandleStatus.FORMING
        )
        with self.assertRaises(ValueError) as ctx:
            service.run_forward_session(
                session_date="2026-11-20",
                proxy_candles_map={"SPY": [make_eval_candle("SPY", "2026-11-20", "580", "585", "579", "582")]},
                stock_candles_map={"AAPL": [forming_candle]},
            )
        self.assertIn("Incomplete or forming candle detected", str(ctx.exception))

    # -------------------------------------------------------------------------
    # Test 7: Rejected candidate cannot create trade
    # -------------------------------------------------------------------------
    def test_07_rejected_candidate_cannot_create_trade(self) -> None:
        # Invalid parameters (e.g. stop >= entry) must reject trade proposal creation
        prop, pos, reason = self.tracker.create_proposal_and_open_position(
            observation_id="OBS-2026-11-20-TSLA-LIVE",
            session_date="2026-11-20",
            ticker="TSLA",
            data_mode=ForwardDataMode.LIVE.value,
            shares=10,
            entry_price=Decimal("198.00"),
            stop_loss=Decimal("200.00"),  # Stop above entry -> invalid
            target=Decimal("214.00"),
            virtual_available_cash=Decimal("5000.00"),
            active_positions=[],
        )
        self.assertIsNone(prop)
        self.assertIsNone(pos)
        self.assertIn("INVALID_STOP", reason)
        self.assertEqual(len(self.storage.load_active_positions()), 0)

    # -------------------------------------------------------------------------
    # Test 8: Eligible candidate creates hypothetical proposal only
    # -------------------------------------------------------------------------
    def test_08_eligible_candidate_creates_hypothetical_proposal_only(self) -> None:
        prop, pos, reason = self.tracker.create_proposal_and_open_position(
            observation_id="OBS-2026-11-20-AAPL-LIVE",
            session_date="2026-11-20",
            ticker="AAPL",
            data_mode=ForwardDataMode.LIVE.value,
            shares=8,
            entry_price=Decimal("150.00"),
            stop_loss=Decimal("145.00"),
            target=Decimal("160.00"),
            virtual_available_cash=Decimal("5000.00"),
            active_positions=[],
        )
        self.assertIsNotNone(prop)
        self.assertIsNotNone(pos)
        self.assertIsNone(reason)
        self.assertEqual(pos.ticker, "AAPL")
        self.assertEqual(pos.shares, 8)
        self.assertEqual(pos.marker, MARKER_SIMULATED_TRADE)

        # Verify proposal file saved in proposals/
        proposals = self.storage.load_proposals()
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].marker, MARKER_SIMULATED_TRADE)

    # -------------------------------------------------------------------------
    # Test 9: Stop-loss evaluation exit
    # -------------------------------------------------------------------------
    def test_09_stop_loss_evaluation_exit(self) -> None:
        _, pos, _ = self.tracker.create_proposal_and_open_position(
            observation_id="OBS-2026-11-20-AAPL-LIVE",
            session_date="2026-11-20",
            ticker="AAPL",
            data_mode=ForwardDataMode.LIVE.value,
            shares=8,
            entry_price=Decimal("150.00"),
            stop_loss=Decimal("145.00"),
            target=Decimal("160.00"),
            virtual_available_cash=Decimal("5000.00"),
            active_positions=[],
        )
        # Next session: low breaches stop ($144.00 <= $145.00)
        c_exit = make_eval_candle("AAPL", "2026-11-21", "148.00", "149.00", "144.00", "146.00")
        closed_trade, updated_pos = self.tracker.update_position_with_bar(
            position=pos,
            candle=c_exit,
        )
        self.assertIsNotNone(closed_trade)
        self.assertEqual(closed_trade.exit_reason, "STOP_LOSS_TRIGGERED")
        self.assertEqual(closed_trade.exit_price, Decimal("145.00"))
        # Gross loss: 8 * (145 - 150) = -40.00; total fees = 2.00; net pnl = -42.00
        self.assertEqual(closed_trade.gross_pnl, Decimal("-40.00"))
        self.assertEqual(closed_trade.net_pnl, Decimal("-42.00"))
        self.assertEqual(len(self.storage.load_active_positions()), 0)

    # -------------------------------------------------------------------------
    # Test 10: Take-profit evaluation exit
    # -------------------------------------------------------------------------
    def test_10_take_profit_evaluation_exit(self) -> None:
        _, pos, _ = self.tracker.create_proposal_and_open_position(
            observation_id="OBS-2026-11-20-MSFT-LIVE",
            session_date="2026-11-20",
            ticker="MSFT",
            data_mode=ForwardDataMode.LIVE.value,
            shares=4,
            entry_price=Decimal("300.00"),
            stop_loss=Decimal("290.00"),
            target=Decimal("320.00"),
            virtual_available_cash=Decimal("5000.00"),
            active_positions=[],
        )
        # Next session: high touches target ($321.00 >= $320.00)
        c_exit = make_eval_candle("MSFT", "2026-11-21", "305.00", "321.00", "304.00", "318.00")
        closed_trade, updated_pos = self.tracker.update_position_with_bar(
            position=pos,
            candle=c_exit,
        )
        self.assertIsNotNone(closed_trade)
        self.assertEqual(closed_trade.exit_reason, "TAKE_PROFIT_TRIGGERED")
        self.assertEqual(closed_trade.exit_price, Decimal("320.00"))
        # Gross gain: 4 * (320 - 300) = 80.00; fees = 2.00; net pnl = 78.00
        self.assertEqual(closed_trade.gross_pnl, Decimal("80.00"))
        self.assertEqual(closed_trade.net_pnl, Decimal("78.00"))
        self.assertEqual(len(self.storage.load_active_positions()), 0)

    # -------------------------------------------------------------------------
    # Test 11: Same-bar stop/target ambiguity resolves conservatively to stop-first
    # -------------------------------------------------------------------------
    def test_11_same_bar_stop_target_ambiguity_conservative_stop_first(self) -> None:
        _, pos, _ = self.tracker.create_proposal_and_open_position(
            observation_id="OBS-2026-11-20-AMZN-LIVE",
            session_date="2026-11-20",
            ticker="AMZN",
            data_mode=ForwardDataMode.LIVE.value,
            shares=10,
            entry_price=Decimal("100.00"),
            stop_loss=Decimal("95.00"),
            target=Decimal("110.00"),
            virtual_available_cash=Decimal("5000.00"),
            active_positions=[],
        )
        # Ambiguous session candle: breaches both stop ($94.00 <= $95.00) AND target ($112.00 >= $110.00)
        c_ambiguous = make_eval_candle("AMZN", "2026-11-21", "101.00", "112.00", "94.00", "104.00")
        closed_trade, _ = self.tracker.update_position_with_bar(
            position=pos,
            candle=c_ambiguous,
        )
        self.assertIsNotNone(closed_trade)
        # Conservative resolution MUST be stop first to prevent upward bias
        self.assertEqual(closed_trade.exit_reason, "STOP_LOSS_CONSERVATIVE_AMBIGUITY")
        self.assertEqual(closed_trade.exit_price, Decimal("95.00"))
        self.assertEqual(closed_trade.net_pnl, Decimal("-52.00"))

    # -------------------------------------------------------------------------
    # Test 12: Holding period tracking and max duration exit (10 bars)
    # -------------------------------------------------------------------------
    def test_12_holding_period_tracking_and_max_duration_exit(self) -> None:
        _, pos, _ = self.tracker.create_proposal_and_open_position(
            observation_id="OBS-2026-11-01-GOOG-LIVE",
            session_date="2026-11-01",
            ticker="GOOG",
            data_mode=ForwardDataMode.LIVE.value,
            shares=10,
            entry_price=Decimal("100.00"),
            stop_loss=Decimal("90.00"),
            target=Decimal("120.00"),
            virtual_available_cash=Decimal("5000.00"),
            active_positions=[],
        )
        # Feed 9 neutral daily candles: bars_held increments from 1 to 9
        current_pos = pos
        for day in range(2, 11):
            dt_str = f"2026-11-{day:02d}"
            c = make_eval_candle("GOOG", dt_str, "100.00", "103.00", "98.00", "101.00")
            closed, updated = self.tracker.update_position_with_bar(current_pos, c)
            self.assertIsNone(closed)
            self.assertEqual(updated.bars_held, day - 1)
            current_pos = updated

        # 10th candle closes trade on MAX_HOLDING_PERIOD_EXPIRED
        c_day10 = make_eval_candle("GOOG", "2026-11-11", "101.00", "106.00", "99.00", "105.00")
        closed_trade, _ = self.tracker.update_position_with_bar(current_pos, c_day10)
        self.assertIsNotNone(closed_trade)
        self.assertEqual(closed_trade.exit_reason, "MAX_HOLDING_PERIOD_EXPIRED")
        self.assertEqual(closed_trade.exit_price, Decimal("105.00"))
        self.assertEqual(closed_trade.bars_held, 10)
        # Gross: 10 * (105 - 100) = 50.00; net: 50 - 2 = 48.00
        self.assertEqual(closed_trade.net_pnl, Decimal("48.00"))

    # -------------------------------------------------------------------------
    # Test 13: MFE and MAE tracking
    # -------------------------------------------------------------------------
    def test_13_mfe_and_mae_tracking(self) -> None:
        _, pos, _ = self.tracker.create_proposal_and_open_position(
            observation_id="OBS-2026-11-20-NVDA-LIVE",
            session_date="2026-11-20",
            ticker="NVDA",
            data_mode=ForwardDataMode.LIVE.value,
            shares=10,
            entry_price=Decimal("100.00"),
            stop_loss=Decimal("90.00"),
            target=Decimal("120.00"),
            virtual_available_cash=Decimal("5000.00"),
            active_positions=[],
        )
        # Day 1: High 108 (+80 USD), Low 97 (-30 USD)
        c1 = make_eval_candle("NVDA", "2026-11-21", "101.00", "108.00", "97.00", "104.00")
        _, p1 = self.tracker.update_position_with_bar(pos, c1)
        self.assertEqual(p1.mfe_usd, Decimal("80.00"))
        self.assertEqual(p1.mae_usd, Decimal("30.00"))

        # Day 2: High 106, Low 95 (-50 USD)
        c2 = make_eval_candle("NVDA", "2026-11-22", "104.00", "106.00", "95.00", "102.00")
        _, p2 = self.tracker.update_position_with_bar(p1, c2)
        self.assertEqual(p2.mfe_usd, Decimal("80.00"))
        self.assertEqual(p2.mae_usd, Decimal("50.00"))

    # -------------------------------------------------------------------------
    # Test 14: Fee and PnL calculations
    # -------------------------------------------------------------------------
    def test_14_fee_and_pnl_calculations(self) -> None:
        _, pos, _ = self.tracker.create_proposal_and_open_position(
            observation_id="OBS-2026-11-20-META-LIVE",
            session_date="2026-11-20",
            ticker="META",
            data_mode=ForwardDataMode.LIVE.value,
            shares=5,
            entry_price=Decimal("200.00"),
            stop_loss=Decimal("190.00"),
            target=Decimal("220.00"),
            virtual_available_cash=Decimal("5000.00"),
            active_positions=[],
        )
        c = make_eval_candle("META", "2026-11-21", "210.00", "225.00", "208.00", "222.00")
        ct, _ = self.tracker.update_position_with_bar(pos, c)
        self.assertIsNotNone(ct)
        self.assertEqual(ct.entry_fee, Decimal("1.00"))
        self.assertEqual(ct.exit_fee, Decimal("1.00"))
        self.assertEqual(ct.gross_pnl, Decimal("100.00"))
        self.assertEqual(ct.net_pnl, Decimal("98.00"))
        # Capital invested = 1001.00; return_pct = 98 / 1001 = 0.0979
        self.assertEqual(ct.return_pct, quantize_cent(Decimal("98.00") / Decimal("1001.00")))

    # -------------------------------------------------------------------------
    # Test 15: Position and exposure constraints (Max 4 and Cash Reserve)
    # -------------------------------------------------------------------------
    def test_15_position_and_exposure_constraints_max_4_and_cash_reserve(self) -> None:
        # Open 4 positions ($800 each = $3,200 total invested, leaving $1,800 cash)
        active = []
        for i, sym in enumerate(["SYM1", "SYM2", "SYM3", "SYM4"]):
            _, p, _ = self.tracker.create_proposal_and_open_position(
                observation_id=f"OBS-2026-11-20-{sym}-LIVE",
                session_date="2026-11-20",
                ticker=sym,
                data_mode=ForwardDataMode.LIVE.value,
                shares=10,
                entry_price=Decimal("80.00"),
                stop_loss=Decimal("75.00"),
                target=Decimal("90.00"),
                virtual_available_cash=Decimal("5000.00") - (Decimal(i) * Decimal("801.00")),
                active_positions=active,
            )
            self.assertIsNotNone(p)
            active.append(p)
        self.assertEqual(len(active), 4)

        # 5th position should return rejection reason MAX_POSITIONS_REACHED
        _, p5, reason5 = self.tracker.create_proposal_and_open_position(
            observation_id="OBS-2026-11-20-SYM5-LIVE",
            session_date="2026-11-20",
            ticker="SYM5",
            data_mode=ForwardDataMode.LIVE.value,
            shares=10,
            entry_price=Decimal("80.00"),
            stop_loss=Decimal("75.00"),
            target=Decimal("90.00"),
            virtual_available_cash=Decimal("1796.00"),
            active_positions=active,
        )
        self.assertIsNone(p5)
        self.assertIn("MAX_POSITIONS_REACHED", reason5)

        # Cash reserve test: Attempt an order that would leave cash < $1,000.00 (20% reserve)
        _, p_cr, reason_cr = self.tracker.create_proposal_and_open_position(
            observation_id="OBS-2026-11-20-BIG1-LIVE",
            session_date="2026-11-20",
            ticker="BIG1",
            data_mode=ForwardDataMode.LIVE.value,
            shares=1,
            entry_price=Decimal("1200.00"),
            stop_loss=Decimal("1150.00"),
            target=Decimal("1300.00"),
            virtual_available_cash=Decimal("1500.00"),  # leaves $299.00 < $1,000 reserve
            active_positions=[],
        )
        self.assertIsNone(p_cr)
        self.assertIn("CASH_RESERVE_BREACH", reason_cr)

    # -------------------------------------------------------------------------
    # Test 16: Crash recovery and atomic persistence
    # -------------------------------------------------------------------------
    def test_16_crash_recovery_and_atomic_persistence(self) -> None:
        self.tracker.create_proposal_and_open_position(
            observation_id="OBS-2026-11-20-AAPL-LIVE",
            session_date="2026-11-20",
            ticker="AAPL",
            data_mode=ForwardDataMode.LIVE.value,
            shares=8,
            entry_price=Decimal("150.00"),
            stop_loss=Decimal("145.00"),
            target=Decimal("160.00"),
            virtual_available_cash=Decimal("5000.00"),
            active_positions=[],
        )
        # Verify no .tmp_ files remain in positions dir
        pos_dir = os.path.join(self.test_dir, "positions")
        tmp_files = [f for f in os.listdir(pos_dir) if f.startswith(".tmp_")]
        self.assertEqual(len(tmp_files), 0)

        # Create new storage and reload (simulating restart)
        restarted_storage = ForwardEvaluationStorage(base_dir=self.test_dir)
        reloaded_positions = restarted_storage.load_active_positions()
        self.assertEqual(len(reloaded_positions), 1)
        reloaded_pos = reloaded_positions[0]
        self.assertEqual(reloaded_pos.ticker, "AAPL")
        self.assertEqual(reloaded_pos.shares, 8)
        self.assertEqual(reloaded_pos.entry_price, Decimal("150.00"))

    # -------------------------------------------------------------------------
    # Test 17: Benchmark alignment SPY comparison
    # -------------------------------------------------------------------------
    def test_17_benchmark_alignment_spy_comparison(self) -> None:
        spy_c1 = make_eval_candle("SPY", "2026-11-16", "580.00", "585.00", "578.00", "580.00")
        spy_c2 = make_eval_candle("SPY", "2026-11-20", "585.00", "595.00", "584.00", "591.60")

        engine = ForwardPerformanceEngine(
            storage=self.storage,
            virtual_starting_capital=Decimal("5000.00"),
        )
        report = engine.evaluate_performance(
            session_summaries=[],
            closed_trades=[],
            active_positions=[],
            spy_benchmark_candles=[spy_c1, spy_c2],
        )
        # Expected return: (591.60 - 580.00) / 580.00 = 11.60 / 580.00 = +2.00%
        self.assertEqual(report.benchmark_spy_return_pct, Decimal("2.00"))

    # -------------------------------------------------------------------------
    # Test 18: Open vs closed trade separation
    # -------------------------------------------------------------------------
    def test_18_open_vs_closed_trade_separation(self) -> None:
        _, pos, _ = self.tracker.create_proposal_and_open_position(
            observation_id="OBS-2026-11-20-AAPL-LIVE",
            session_date="2026-11-20",
            ticker="AAPL",
            data_mode=ForwardDataMode.LIVE.value,
            shares=5,
            entry_price=Decimal("150.00"),
            stop_loss=Decimal("145.00"),
            target=Decimal("160.00"),
            virtual_available_cash=Decimal("5000.00"),
            active_positions=[],
        )
        # AAPL is in active positions, not in closed trades
        self.assertEqual(len(self.storage.load_active_positions()), 1)
        self.assertEqual(len(self.storage.load_closed_trades()), 0)

        # Close position
        c = make_eval_candle("AAPL", "2026-11-21", "161.00", "165.00", "160.00", "163.00")
        self.tracker.update_position_with_bar(pos, c)

        # Now AAPL is in closed trades, deleted from active positions
        self.assertEqual(len(self.storage.load_active_positions()), 0)
        self.assertEqual(len(self.storage.load_closed_trades()), 1)

    # -------------------------------------------------------------------------
    # Test 19: Dashboard read-only behavior and marker
    # -------------------------------------------------------------------------
    def test_19_dashboard_read_only_behavior_and_marker(self) -> None:
        data = load_forward_evaluation_dashboard_data(eval_dir=self.test_dir)
        self.assertEqual(data["environment"], "FORWARD_EVALUATION_ONLY")
        self.assertFalse(data["is_real_execution"])
        self.assertEqual(data["disclaimer"], "SIMULATED OBSERVATION — NOT EXECUTED")
        self.assertTrue(data["is_initialized"])
        self.assertIn("summaries_df", data)
        self.assertIn("positions_df", data)
        self.assertIn("trades_df", data)
        self.assertIn("observations_df", data)

    # -------------------------------------------------------------------------
    # Test 20: Historical and live partition isolation
    # -------------------------------------------------------------------------
    def test_20_historical_live_partition_isolation(self) -> None:
        rec_fixt = make_test_obs(session_date="2026-11-20", ticker="QQQ", data_mode=ForwardDataMode.FIXTURE.value)
        rec_live = make_test_obs(session_date="2026-11-20", ticker="SPY", data_mode=ForwardDataMode.LIVE.value)
        self.storage.save_observation(rec_fixt)
        self.storage.save_observation(rec_live)

        fixt_only = self.storage.list_observations(data_mode=ForwardDataMode.FIXTURE.value)
        live_only = self.storage.list_observations(data_mode=ForwardDataMode.LIVE.value)
        self.assertEqual(len(fixt_only), 1)
        self.assertEqual(fixt_only[0].ticker, "QQQ")
        self.assertEqual(len(live_only), 1)
        self.assertEqual(live_only[0].ticker, "SPY")

    # -------------------------------------------------------------------------
    # Test 21: No modification to existing portfolio, journal, orders
    # -------------------------------------------------------------------------
    def test_21_no_modification_to_existing_portfolio_journal_orders(self) -> None:
        # 1. ForwardEvaluationStorage blocks unsafe directories
        with self.assertRaises(ForwardStorageSafetyError):
            ForwardEvaluationStorage(base_dir="paper_journal.json")

        with self.assertRaises(ForwardStorageSafetyError):
            ForwardEvaluationStorage(base_dir=os.path.join(PROJECT_ROOT, "TradingPaperState"))

        with self.assertRaises(ForwardStorageSafetyError):
            ForwardEvaluationStorage(base_dir=os.path.join(PROJECT_ROOT, "portfolio.json"))

        # 2. Mutable CURRENT_DAILY_MANIFEST.json integrity check (no modification during test)
        manifest_path = os.path.join(PROJECT_ROOT, ".planning", "data", "current_daily", "CURRENT_DAILY_MANIFEST.json")
        self.assertTrue(os.path.exists(manifest_path), f"Manifest missing: {manifest_path}")
        hasher = hashlib.sha256()
        with open(manifest_path, "rb") as f:
            hasher.update(f.read())
        actual_manifest_hash = hasher.hexdigest().upper()
        self.assertEqual(
            actual_manifest_hash,
            self.initial_manifest_hash,
            "CURRENT_DAILY_MANIFEST.json was unexpectedly modified during the test execution.",
        )

        # 3. Mutable SPY daily data integrity check (no modification during test)
        spy_path = os.path.join(PROJECT_ROOT, ".planning", "data", "current_daily", "SPY_daily.csv")
        self.assertTrue(os.path.exists(spy_path), f"SPY dataset missing: {spy_path}")
        hasher = hashlib.sha256()
        with open(spy_path, "rb") as f:
            hasher.update(f.read())
        actual_spy_hash = hasher.hexdigest().upper()
        self.assertEqual(
            actual_spy_hash,
            self.initial_spy_hash,
            "SPY_daily.csv was unexpectedly modified during the test execution.",
        )

        # 4. Path isolation: Verify operations use configured sandbox and do not write to genuine observations
        self.assertTrue(os.path.isdir(self.test_dir))
        self.assertEqual(self.storage.base_dir, self.test_dir)
        genuine_obs_dir = os.path.join(PROJECT_ROOT, ".planning", "data", "observations")
        self.assertNotEqual(self.storage.obs_dir, genuine_obs_dir)
        self.assertTrue(self.storage.obs_dir.startswith(self.test_dir))

    # -------------------------------------------------------------------------
    # Test 22: Wilson score and sample size statistical honesty
    # -------------------------------------------------------------------------
    def test_22_wilson_score_and_sample_size_statistical_honesty(self) -> None:
        # Test Wilson score calculation
        lower, upper = compute_wilson_score_interval(successes=15, total=20, z=1.96)
        self.assertGreater(lower, Decimal("0.50"))
        self.assertLess(upper, Decimal("0.95"))
        self.assertLess(lower, upper)

        # Test small sample size (< 30 trades): flags SAMPLE INSUFFICIENT
        small_trades = []
        for i in range(10):
            small_trades.append(
                HypotheticalClosedTrade(
                    trade_id=f"T_{i}",
                    position_id=f"POS_{i}",
                    proposal_id=f"PROP_{i}",
                    ticker="AAPL",
                    data_mode=ForwardDataMode.LIVE.value,
                    shares=10,
                    entry_date="2026-11-20",
                    entry_price=Decimal("150.00"),
                    exit_date="2026-11-22",
                    exit_price=Decimal("160.00") if i % 2 == 0 else Decimal("145.00"),
                    entry_fee=Decimal("1.00"),
                    exit_fee=Decimal("1.00"),
                    gross_pnl=Decimal("100.00") if i % 2 == 0 else Decimal("-50.00"),
                    net_pnl=Decimal("98.00") if i % 2 == 0 else Decimal("-52.00"),
                    return_pct=Decimal("6.53") if i % 2 == 0 else Decimal("-3.47"),
                    exit_reason="TAKE_PROFIT_TRIGGERED" if i % 2 == 0 else "STOP_LOSS_TRIGGERED",
                    bars_held=2,
                    mfe_usd=Decimal("100.00"),
                    mae_usd=Decimal("0.00"),
                    closed_at="2026-11-22T20:00:00Z",
                    marker=MARKER_SIMULATED_TRADE,
                )
            )
        engine = ForwardPerformanceEngine(
            storage=self.storage,
            virtual_starting_capital=Decimal("5000.00"),
        )
        small_report = engine.evaluate_performance(
            session_summaries=[],
            closed_trades=small_trades,
            active_positions=[],
        )
        self.assertFalse(small_report.is_sample_statistically_significant)
        self.assertIn("SAMPLE INSUFFICIENT", small_report.statistical_caveat)

        # Test sample size >= 30 trades: statistically significant flag is True
        large_trades = small_trades * 4  # 40 trades
        large_report = engine.evaluate_performance(
            session_summaries=[],
            closed_trades=large_trades,
            active_positions=[],
        )
        self.assertTrue(large_report.is_sample_statistically_significant)
        self.assertIn("STATISTICALLY ROBUST", large_report.statistical_caveat)


if __name__ == "__main__":
    unittest.main()
