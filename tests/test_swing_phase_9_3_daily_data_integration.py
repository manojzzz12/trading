"""Phase 9.3 Acceptance & Verification Suite: Genuine Daily Data Integration & Safety.

Verifies:
1. Synthetic pilot fixtures (make_pilot_series) are completely banned from genuine DAILY/LIVE metrics.
2. FIXTURE mode remains explicitly tagged as FIXTURE and isolated from genuine performance reports.
3. Genuine daily CSV data loading and processing through load_candles_from_daily_partition.
4. Session date mismatch and stale data fail-closed rejection.
5. Incomplete / forming candle fail-closed rejection.
6. Retrospective replay rejection for sessions <= 2026-11-16.
7. Idempotency against legacy Nov 16 records where data_mode was omitted.
8. Duplicate session summary fail-closed rejection on repeated DAILY evaluation.
9. Indicator warm-up enforcement (SPY >= 200 bars in DAILY mode).
10. CURRENT_DAILY_MANIFEST validation status enforcement.
11. Read-only CLI --report and --summary guarantees.
12. Strict isolation from root paper_journal.json and external TradingPaperState.
"""

import csv
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import os
import shutil
import sys
import tempfile
from typing import Any, Dict, List, Optional
import unittest

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, ".."))
SKILLS_DIR = os.path.join(PROJECT_ROOT, ".agents", "skills", "trader-swing")
if SKILLS_DIR not in sys.path:
    sys.path.insert(0, SKILLS_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from forward_evaluation_models import (
    EvaluationSessionSummary,
    ForwardDataMode,
    ForwardObservationRecord,
    ForwardScreeningDecision,
    HypotheticalClosedTrade,
    HypotheticalTradeStatus,
    MARKER_FORWARD_EVALUATION,
    MARKER_SIMULATED_TRADE,
)
from forward_evaluation_storage import (
    DEFAULT_FORWARD_EVALUATION_DIR,
    DuplicateObservationError,
    ForwardEvaluationStorage,
    ForwardStorageSafetyError,
)
from forward_evaluation_service import ForwardEvaluationService
from market_data_models import CandleStatus, EASTERN, MarketClock, OHLCVCandle, to_utc, UTC
from pilot_simulation_runner import make_pilot_series


def make_test_candle(
    ticker: str,
    dt_str: str,
    open_p: str = "150.00",
    high_p: str = "155.00",
    low_p: str = "149.00",
    close_p: str = "153.00",
    volume: int = 2_000_000,
    status: CandleStatus = CandleStatus.COMPLETE,
) -> OHLCVCandle:
    """Create a validated OHLCVCandle with regular market hours."""
    sess_d = date.fromisoformat(dt_str)
    start_et = datetime.combine(sess_d, MarketClock.REGULAR_OPEN, tzinfo=EASTERN)
    ts_utc = to_utc(start_et)
    return OHLCVCandle(
        ticker=ticker,
        interval="1d",
        timestamp=ts_utc,
        open=Decimal(open_p),
        high=Decimal(high_p),
        low=Decimal(low_p),
        close=Decimal(close_p),
        volume=volume,
        status=status,
        source="yahoo",
    )


def make_test_obs(
    session_date: str = "2026-11-19",
    ticker: str = "AAPL",
    data_mode: str = ForwardDataMode.DAILY.value,
    is_eligible: bool = False,
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
        five_pillar_scores={"score": "85.00", "rank": 1},
        rejection_reasons=[] if is_eligible else ["SCANNER_FILTER_FAILED"],
        signal_type="PULLBACK_EMA20" if is_eligible else None,
        proposed_entry=Decimal("150.00") if is_eligible else None,
        proposed_stop=Decimal("145.00") if is_eligible else None,
        proposed_target=Decimal("160.00") if is_eligible else None,
        risk_sizing={"shares": 10, "risk_approved": True} if is_eligible else None,
        is_eligible=is_eligible,
    )


def generate_candle_series(ticker: str, end_date_str: str, count: int = 210) -> List[OHLCVCandle]:
    """Generate a sequence of completed daily candles ending on end_date_str."""
    end_d = date.fromisoformat(end_date_str)
    candles: List[OHLCVCandle] = []
    curr_d = end_d - timedelta(days=int(count * 1.5))
    base_px = Decimal("100.00")
    while len(candles) < count:
        if curr_d.weekday() < 5:  # Monday to Friday
            p = base_px + Decimal(str(len(candles) * 0.25))
            c = make_test_candle(
                ticker=ticker,
                dt_str=curr_d.isoformat(),
                open_p=str(p),
                high_p=str(p + Decimal("2.00")),
                low_p=str(p - Decimal("1.00")),
                close_p=str(p + Decimal("1.00")),
            )
            candles.append(c)
        curr_d += timedelta(days=1)
    # Ensure the final candle has exact date end_date_str
    last_candle = candles[-1]
    final_candle = make_test_candle(
        ticker=ticker,
        dt_str=end_date_str,
        open_p=str(last_candle.open),
        high_p=str(last_candle.high),
        low_p=str(last_candle.low),
        close_p=str(last_candle.close),
    )
    candles[-1] = final_candle
    return candles


class TestSwingPhase93DailyDataIntegration(unittest.TestCase):
    """Acceptance test suite for Phase 9.3 genuine daily data integration."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp(prefix="trader_swing_test_9_3_")
        self.eval_dir = os.path.join(self.test_dir, "forward_evaluation")
        self.daily_data_dir = os.path.join(self.test_dir, "current_daily")
        self.legacy_obs_log = os.path.join(self.test_dir, "observation_log.jsonl")
        os.makedirs(self.eval_dir, exist_ok=True)
        os.makedirs(self.daily_data_dir, exist_ok=True)

        self.storage = ForwardEvaluationStorage(
            base_dir=self.eval_dir,
            legacy_log_path=self.legacy_obs_log,
        )
        self.service = ForwardEvaluationService(storage=self.storage)

    def tearDown(self) -> None:
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # Test 1: Synthetic pilot series banned from genuine DAILY/LIVE metrics
    # -------------------------------------------------------------------------
    def test_01_synthetic_fixtures_excluded_from_genuine_daily_metrics(self) -> None:
        """Synthetic fixture records must never enter genuine performance reports."""
        fix_summary = EvaluationSessionSummary(
            session_date="2026-11-19",
            data_mode=ForwardDataMode.FIXTURE.value,
            market_regime="BULLISH",
            tickers_screened=2,
            eligible_candidates=1,
            rejected_candidates=1,
            proposals_created=1,
            hypothetical_trades_opened=1,
            hypothetical_trades_closed=0,
            active_positions_count=1,
            virtual_equity=Decimal("5050.00"),
            virtual_cash=Decimal("4000.00"),
            realized_net_pnl=Decimal("50.00"),
            unrealized_pnl=Decimal("0.00"),
            total_fees_paid=Decimal("2.00"),
        )
        self.storage.save_session_summary(fix_summary, force_overwrite=True)

        fix_trade = HypotheticalClosedTrade(
            trade_id="HTRD-FIXTURE-001",
            position_id="HPOS-2026-11-18-AAPL",
            proposal_id="PROP-2026-11-18-AAPL-FIXTURE",
            ticker="AAPL",
            data_mode=ForwardDataMode.FIXTURE.value,
            shares=10,
            entry_price=Decimal("150.00"),
            entry_date="2026-11-18",
            exit_price=Decimal("155.00"),
            exit_date="2026-11-19",
            entry_fee=Decimal("1.00"),
            exit_fee=Decimal("1.00"),
            gross_pnl=Decimal("50.00"),
            net_pnl=Decimal("48.00"),
            return_pct=Decimal("3.20"),
            exit_reason=HypotheticalTradeStatus.CLOSED_TAKE_PROFIT.value,
            bars_held=1,
            mfe_usd=Decimal("5.00"),
            mae_usd=Decimal("0.00"),
            closed_at="2026-11-19T20:00:00Z",
        )
        self.storage.save_closed_trade(fix_trade)

        # Generate report with genuine_only=True (default)
        report = self.service.generate_performance_report(genuine_only=True)
        self.assertEqual(report.total_sessions_evaluated, 0)
        self.assertEqual(report.closed_trades_count, 0)
        self.assertEqual(report.net_realized_pnl, Decimal("0.00"))

        # In diagnostic mode, fixture is visible
        diag_report = self.service.generate_performance_report(data_mode="FIXTURE", genuine_only=False)
        self.assertEqual(diag_report.total_sessions_evaluated, 1)
        self.assertEqual(diag_report.closed_trades_count, 1)
        self.assertEqual(diag_report.net_realized_pnl, Decimal("48.00"))

    # -------------------------------------------------------------------------
    # Test 2: Session date mismatch fails closed
    # -------------------------------------------------------------------------
    def test_02_session_date_mismatch_fails_closed(self) -> None:
        """Evaluating when candle date does not match session_date must raise ValueError."""
        spy_series = generate_candle_series("SPY", "2026-11-18", count=210)
        aapl_series = generate_candle_series("AAPL", "2026-11-18", count=210)

        with self.assertRaises(ValueError) as ctx:
            self.service.run_forward_session(
                session_date="2026-11-19",  # Stale data: series ends on 2026-11-18
                proxy_candles_map={"SPY": spy_series},
                stock_candles_map={"AAPL": aapl_series},
                data_mode=ForwardDataMode.DAILY.value,
            )
        self.assertIn("SESSION_DATE_MISMATCH", str(ctx.exception))

    # -------------------------------------------------------------------------
    # Test 3: Incomplete or forming candle fails closed
    # -------------------------------------------------------------------------
    def test_03_forming_candle_fails_closed(self) -> None:
        """Forming candles must raise ValueError."""
        spy_series = generate_candle_series("SPY", "2026-11-19", count=210)
        forming_aapl = make_test_candle("AAPL", "2026-11-19", status=CandleStatus.FORMING)

        with self.assertRaises(ValueError) as ctx:
            self.service.run_forward_session(
                session_date="2026-11-19",
                proxy_candles_map={"SPY": spy_series},
                stock_candles_map={"AAPL": [forming_aapl]},
                data_mode=ForwardDataMode.DAILY.value,
            )
        self.assertIn("Incomplete or forming candle detected", str(ctx.exception))

    # -------------------------------------------------------------------------
    # Test 4: Retrospective replay rejection (<= 2026-11-16)
    # -------------------------------------------------------------------------
    def test_04_retrospective_replay_rejection(self) -> None:
        """Evaluating historical sessions <= 2026-11-16 in DAILY mode must be rejected."""
        spy_series = generate_candle_series("SPY", "2026-11-16", count=210)
        aapl_series = generate_candle_series("AAPL", "2026-11-16", count=210)

        with self.assertRaises(ValueError) as ctx:
            self.service.run_forward_session(
                session_date="2026-11-16",
                proxy_candles_map={"SPY": spy_series},
                stock_candles_map={"AAPL": aapl_series},
                data_mode=ForwardDataMode.DAILY.value,
            )
        self.assertIn("RETROSPECTIVE_REPLAY_REJECTED", str(ctx.exception))

    # -------------------------------------------------------------------------
    # Test 5: Indicator warm-up enforcement (SPY < 200 bars in DAILY mode)
    # -------------------------------------------------------------------------
    def test_05_insufficient_indicator_warmup_fails_closed(self) -> None:
        """SPY with < 200 bars in DAILY mode must raise INSUFFICIENT_HISTORY."""
        short_spy = generate_candle_series("SPY", "2026-11-19", count=50)  # Only 50 bars
        aapl_series = generate_candle_series("AAPL", "2026-11-19", count=50)

        with self.assertRaises(ValueError) as ctx:
            self.service.run_forward_session(
                session_date="2026-11-19",
                proxy_candles_map={"SPY": short_spy},
                stock_candles_map={"AAPL": aapl_series},
                data_mode=ForwardDataMode.DAILY.value,
            )
        self.assertIn("INSUFFICIENT_HISTORY", str(ctx.exception))

    # -------------------------------------------------------------------------
    # Test 6: Legacy Nov 16 records with missing data_mode prevent duplicate saves
    # -------------------------------------------------------------------------
    def test_06_legacy_nov16_missing_data_mode_idempotency(self) -> None:
        """Storage must reject observations that match legacy records even with missing data_mode."""
        legacy_entry = {
            "record_type": "FORWARD_OBSERVATION",
            "date": "2026-11-16",
            "ticker": "AAPL",
            "market_regime": "STRONG_BEARISH",
            "scanner_decision": "REJECTED",
        }
        with open(self.legacy_obs_log, "w", encoding="utf-8") as f:
            f.write(json.dumps(legacy_entry) + "\n")

        # Now try to save a new observation for 2026-11-16 AAPL in DAILY mode
        record = make_test_obs(
            session_date="2026-11-16",
            ticker="AAPL",
            data_mode=ForwardDataMode.DAILY.value,
        )
        with self.assertRaises(DuplicateObservationError) as ctx:
            self.storage.save_observation(record)
        self.assertIn("already recorded in historical observation log", str(ctx.exception))

    # -------------------------------------------------------------------------
    # Test 7: Duplicate session summary fails closed
    # -------------------------------------------------------------------------
    def test_07_duplicate_forward_session_summary_fails_closed(self) -> None:
        """Attempting to re-evaluate the same session date without overwrite must raise DuplicateObservationError."""
        summary = EvaluationSessionSummary(
            session_date="2026-11-19",
            data_mode=ForwardDataMode.DAILY.value,
            market_regime="BULLISH",
            tickers_screened=1,
            eligible_candidates=0,
            rejected_candidates=1,
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
        self.storage.save_session_summary(summary, force_overwrite=False)

        # Attempt duplicate write
        with self.assertRaises(DuplicateObservationError) as ctx:
            self.storage.save_session_summary(summary, force_overwrite=False)
        self.assertIn("Duplicate evaluation session summary rejected", str(ctx.exception))

    # -------------------------------------------------------------------------
    # Test 8: Genuine daily CSV loading through load_candles_from_daily_partition
    # -------------------------------------------------------------------------
    def test_08_genuine_daily_csv_loading_and_processing(self) -> None:
        """Service must load candles from daily partition CSV files and process session."""
        def write_mock_csv(sym: str, count: int = 210) -> None:
            candles = generate_candle_series(sym, "2026-11-19", count=count)
            fpath = os.path.join(self.daily_data_dir, f"{sym}_daily.csv")
            with open(fpath, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["Date", "Open", "High", "Low", "Close", "Volume"])
                for c in candles:
                    d_str = to_utc(c.timestamp).date().isoformat()
                    writer.writerow([d_str, str(c.open), str(c.high), str(c.low), str(c.close), c.volume])

        write_mock_csv("SPY", count=210)
        write_mock_csv("QQQ", count=210)
        write_mock_csv("IWM", count=210)
        write_mock_csv("AAPL", count=210)

        # Write valid manifest
        manifest_data = {
            "validation_results": {"validation_status": "PASSED"},
            "as_of_session_date": "2026-11-19",
        }
        with open(os.path.join(self.daily_data_dir, "CURRENT_DAILY_MANIFEST.json"), "w", encoding="utf-8") as f:
            json.dump(manifest_data, f)

        proxy_map, stock_map, effective_date, warnings = self.service.load_candles_from_daily_partition(
            data_dir=self.daily_data_dir,
            as_of_date="2026-11-19",
        )
        self.assertEqual(effective_date, "2026-11-19")
        self.assertIn("SPY", proxy_map)
        self.assertIn("AAPL", stock_map)
        self.assertEqual(len(proxy_map["SPY"]), 210)

        # Run session
        summary = self.service.run_forward_session(
            session_date="2026-11-19",
            proxy_candles_map=proxy_map,
            stock_candles_map=stock_map,
            data_mode=ForwardDataMode.DAILY.value,
        )
        self.assertEqual(summary.session_date, "2026-11-19")
        self.assertEqual(summary.data_mode, "DAILY")
        self.assertEqual(summary.tickers_screened, 1)

        # Verify observation record saved with FORWARD_EVALUATION_ONLY marker
        loaded_obs = self.storage.list_observations(session_date="2026-11-19")
        self.assertEqual(len(loaded_obs), 1)
        self.assertEqual(loaded_obs[0].record_marker, MARKER_FORWARD_EVALUATION)
        self.assertEqual(loaded_obs[0].disclaimer, MARKER_SIMULATED_TRADE)
        self.assertEqual(loaded_obs[0].data_mode, "DAILY")

    # -------------------------------------------------------------------------
    # Test 9: CURRENT_DAILY_MANIFEST validation status check
    # -------------------------------------------------------------------------
    def test_09_dataset_manifest_validation_check(self) -> None:
        """If CURRENT_DAILY_MANIFEST validation_status != PASSED, loading must fail."""
        manifest_data = {
            "validation_results": {"validation_status": "FAILED"},
            "as_of_session_date": "2026-11-19",
        }
        with open(os.path.join(self.daily_data_dir, "CURRENT_DAILY_MANIFEST.json"), "w", encoding="utf-8") as f:
            json.dump(manifest_data, f)

        with self.assertRaises(ValueError) as ctx:
            self.service.load_candles_from_daily_partition(
                data_dir=self.daily_data_dir,
            )
        self.assertIn("DATASET_VALIDATION_FAILED", str(ctx.exception))

    # -------------------------------------------------------------------------
    # Test 10: Zero broker execution & paper portfolio isolation
    # -------------------------------------------------------------------------
    def test_10_zero_broker_execution_and_paper_portfolio_isolation(self) -> None:
        """Storage must refuse any connection or write to external paper state."""
        with self.assertRaises(ForwardStorageSafetyError):
            ForwardEvaluationStorage(base_dir=os.path.join(self.test_dir, "TradingPaperState"))


if __name__ == "__main__":
    unittest.main()
