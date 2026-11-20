"""Targeted test suite for Phase 6.4.1 Manual Paper Observation Workflow.

Verifies:
1. Running an observation research scan does NOT mutate paper-trading state or ledgers.
2. Observation records are explicitly marked as forward observations and cannot silently become executed fills.
3. Missing mandatory proxy (SPY) or insufficient data produces a HARD_FAIL with clear diagnostic reporting.
4. Stale or insufficient stock candles are excluded from the active universe and reported with explicit reasons.
5. Eligible candidates have deterministic Phase 2 risk calculations (shares, capital, buffered risk).
6. Scanner and risk rejection reasons are explicitly reported.
7. Prohibited flags (--live, --broker, --auto, --stream) trigger immediate safety rejection.
8. Daily reports contain mandatory disclosures distinguishing historical backtests from forward observations.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest

# Ensure skills are importable
SKILLS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", ".agents", "skills", "trader-swing")
)
if SKILLS_DIR not in sys.path:
    sys.path.insert(0, SKILLS_DIR)

from market_data_models import CandleStatus, MarketClock, OHLCVCandle, to_utc, EASTERN
from observation_workflow import (
    DailyResearchReport,
    ObservationRecord,
    format_observation_report,
    load_daily_candles_from_dir,
    run_observation_scan,
)
from paper_cli import FORBIDDEN_CLI_FLAGS, scan_for_forbidden_flags
from paper_service import PaperTradingService, PaperSafetyViolation
from paper_storage import PaperStorageManager, DEFAULT_PAPER_STATE_DIR


class TestSwingObservationWorkflow(unittest.TestCase):
    """Test suite for manual paper observation workflow in Phase 6.4.1."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_obs_wf_")
        self.data_dir = os.path.join(self.test_dir, "data")
        self.state_dir = os.path.join(self.test_dir, "paper_state")
        self.record_file = os.path.join(self.test_dir, "observations", "obs_log.jsonl")

        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(self.state_dir, exist_ok=True)

        # Initialize mock paper trading state in temporary directory
        self.storage = PaperStorageManager(state_dir=self.state_dir)
        self.storage.initialize_state(initial_capital=Decimal("5000.00"), force=True)

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def _generate_synthetic_daily_csv(
        self,
        ticker: str,
        start_date: date,
        num_sessions: int,
        base_price: Decimal,
        trend: Decimal = Decimal("0.05"),
    ) -> str:
        """Helper to create valid synthetic daily CSV data."""
        csv_path = os.path.join(self.data_dir, f"{ticker}_daily.csv")
        curr_d = start_date
        sessions_written = 0
        px = base_price

        with open(csv_path, "w", encoding="utf-8") as f:
            f.write("Date,Open,High,Low,Close,Volume\n")
            while sessions_written < num_sessions:
                # Skip weekends and exchange holidays
                is_trade, _ = MarketClock.is_historical_trading_day(curr_d)
                if curr_d.weekday() < 5 and is_trade:
                    op = px
                    hi = op + Decimal("1.50")
                    lo = op - Decimal("1.00")
                    cl = op + Decimal("0.80")
                    vol = 25000000 if ticker in ("SPY", "QQQ", "IWM") else 5000000
                    f.write(f"{curr_d.isoformat()},{op:.2f},{hi:.2f},{lo:.2f},{cl:.2f},{vol}\n")
                    sessions_written += 1
                    px += trend
                curr_d += timedelta(days=1)

        return csv_path

    def _compute_state_hashes(self) -> dict:
        """Compute SHA256 hashes of all state files in state_dir."""
        hashes = {}
        for fname in os.listdir(self.state_dir):
            fpath = os.path.join(self.state_dir, fname)
            if os.path.isfile(fpath):
                with open(fpath, "rb") as f:
                    hashes[fname] = hashlib.sha256(f.read()).hexdigest()
        return hashes

    # -------------------------------------------------------------------------
    # Test 1: Zero Mutation Invariant
    # -------------------------------------------------------------------------
    def test_01_observation_scan_does_not_mutate_paper_state(self):
        """Verify that running an observation research scan NEVER mutates paper state files."""
        # Setup synthetic proxies and stock
        start_d = date(2023, 1, 3)
        self._generate_synthetic_daily_csv("SPY", start_d, 220, Decimal("400.00"))
        self._generate_synthetic_daily_csv("QQQ", start_d, 220, Decimal("300.00"))
        self._generate_synthetic_daily_csv("IWM", start_d, 220, Decimal("180.00"))
        self._generate_synthetic_daily_csv("AAPL", start_d, 220, Decimal("150.00"))

        pre_hashes = self._compute_state_hashes()
        self.assertIn("trading_paper_journal.json", pre_hashes)
        self.assertIn("audit_events.jsonl", pre_hashes)

        # Run observation scan
        report = run_observation_scan(
            data_dir=self.data_dir,
            state_dir=self.state_dir,
            record_file=self.record_file,
            write_records=True,
        )

        post_hashes = self._compute_state_hashes()

        # Verify state directory files are byte-for-byte unchanged
        self.assertEqual(pre_hashes, post_hashes, "Paper trading state files were mutated by observation scan!")

        # Verify ledger contents unchanged
        journal = self.storage.load_journal()
        self.assertEqual(journal.ledger.total_equity_usd, Decimal("5000.00"))
        self.assertEqual(journal.ledger.cash_usd, Decimal("5000.00"))
        self.assertEqual(len(journal.ledger.positions), 0)
        self.assertEqual(len(journal.ledger.closed_trades), 0)

        # Verify observation record log was written separately
        self.assertTrue(os.path.exists(self.record_file), "Observation log file was not created.")

    # -------------------------------------------------------------------------
    # Test 2: Observation Records vs Executed Fills Distinction
    # -------------------------------------------------------------------------
    def test_02_observation_records_cannot_become_executed_fills(self):
        """Verify ObservationRecords are strictly observation-only and cannot become fills."""
        rec = ObservationRecord(
            date="2023-12-29",
            ticker="NVDA",
            market_regime="STRONG_BULLISH",
            signal="ENTRY_SIGNAL",
            entry_reference="49.52",
            stop_reference="47.72",
            risk_estimate="$24.78 total risk",
            decision="OBSERVE_ONLY",
            notes="Testing observation isolation",
        )

        # 1. Structural guarantees
        self.assertEqual(rec.record_type, "FORWARD_OBSERVATION")
        self.assertFalse(rec.executed_fill, "ObservationRecord executed_fill must always be False")
        self.assertEqual(rec.decision, "OBSERVE_ONLY")

        # 2. Verify that PaperTradingService requires explicit paper_only=True and cannot take ObservationRecord
        service = PaperTradingService(storage=self.storage)
        with self.assertRaises(PaperSafetyViolation):
            # Attempt to record fill without paper_only confirmation
            service.record_paper_entry(
                ticker=rec.ticker,
                shares=10,
                price=rec.entry_reference,
                stop_loss=rec.stop_reference,
                timestamp="2023-12-29 10:00:00",
                paper_only=False,
            )

        # Confirm ledger still has zero positions
        status = service.get_status()
        self.assertEqual(status["open_positions_count"], 0)

    # -------------------------------------------------------------------------
    # Test 3: Missing SPY Produces HARD_FAIL
    # -------------------------------------------------------------------------
    def test_03_missing_spy_causes_hard_fail(self):
        """Verify that missing mandatory proxy SPY produces HARD_FAIL and no fabricated data."""
        # Data dir with only stock, no SPY
        start_d = date(2023, 1, 3)
        self._generate_synthetic_daily_csv("AAPL", start_d, 60, Decimal("150.00"))

        report = run_observation_scan(
            data_dir=self.data_dir,
            state_dir=self.state_dir,
            record_file=self.record_file,
            write_records=False,
        )

        self.assertEqual(report.feed_status, "HARD_FAIL")
        self.assertIsNone(report.market_regime)
        self.assertEqual(len(report.eligible_candidates), 0)
        self.assertTrue(any("SPY" in r for r in report.failure_reasons))

    # -------------------------------------------------------------------------
    # Test 4: Stale and Insufficient Stock Data Reported
    # -------------------------------------------------------------------------
    def test_04_stale_or_insufficient_stock_data_reported(self):
        """Verify that stale stock candles or <50 bars are excluded with clear reasons."""
        start_d = date(2023, 1, 3)
        # SPY, QQQ, IWM: 220 sessions up to ~Nov 2023
        self._generate_synthetic_daily_csv("SPY", start_d, 220, Decimal("400.00"))
        self._generate_synthetic_daily_csv("QQQ", start_d, 220, Decimal("300.00"))
        self._generate_synthetic_daily_csv("IWM", start_d, 220, Decimal("180.00"))

        # Stock A: only 30 bars (insufficient history)
        self._generate_synthetic_daily_csv("SHORT_HIST", start_d, 30, Decimal("50.00"))

        # Stock B: 100 bars, but stops early (stale data relative to SPY)
        self._generate_synthetic_daily_csv("STALE_STK", start_d, 100, Decimal("100.00"))

        report = run_observation_scan(
            data_dir=self.data_dir,
            state_dir=self.state_dir,
            record_file=self.record_file,
            write_records=False,
        )

        self.assertEqual(report.feed_status, "READY")
        excluded_map = {e.ticker: e.reason for e in report.feed_excluded_tickers}

        self.assertIn("SHORT_HIST", excluded_map)
        self.assertIn("STALE_STK", excluded_map)
        self.assertIn("bars", excluded_map["SHORT_HIST"].lower())
        self.assertIn("stale", excluded_map["STALE_STK"].lower())

    # -------------------------------------------------------------------------
    # Test 5: Risk Sizing and Rejection Reporting
    # -------------------------------------------------------------------------
    def test_05_rejection_reasons_and_risk_sizing_calculations(self):
        """Verify eligible candidates have deterministic risk sizing and rejections have reasons."""
        # Run on the primary corrected partition for session 2023-12-29
        real_partition = os.path.abspath(
            r".planning\data\historical_partitions\2021_2023_primary_corrected"
        )
        if not os.path.exists(real_partition):
            self.skipTest(f"Historical partition {real_partition} not found.")

        report = run_observation_scan(
            data_dir=real_partition,
            as_of_date=date(2023, 12, 29),
            tickers=["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL"],
            state_dir=self.state_dir,
            record_file=self.record_file,
            write_records=True,
        )

        self.assertEqual(report.feed_status, "READY")
        self.assertIsNotNone(report.market_regime)

        # Check eligible candidates have sized risk within 0.50% ($25.00)
        for plan in report.eligible_candidates:
            self.assertGreater(plan.shares, 0)
            self.assertGreater(plan.entry_price, Decimal("0.00"))
            self.assertLess(plan.stop_loss_price, plan.entry_price)
            self.assertLessEqual(plan.total_buffered_risk, Decimal("25.00"))

        # Check scanner rejected candidates have non-empty rejection reasons
        self.assertGreater(len(report.scanner_rejected_candidates), 0)
        for ticker, reasons in report.scanner_rejected_candidates.items():
            self.assertGreater(len(reasons), 0, f"Ticker {ticker} rejected without documented reason.")

    # -------------------------------------------------------------------------
    # Test 6: Forbidden Flags Rejection
    # -------------------------------------------------------------------------
    def test_06_forbidden_flags_rejected(self):
        """Verify that live trading, broker, credential, and polling flags are rejected."""
        test_flags = [
            "--live",
            "--broker",
            "--auto",
            "--poll",
            "--stream",
            "--key=SECRET123",
            "--alpaca",
            "--ibkr",
        ]
        for flag in test_flags:
            with self.assertRaises(SystemExit):
                scan_for_forbidden_flags([flag])

    # -------------------------------------------------------------------------
    # Test 7: Report Disclosures Distinguish Backtest vs Forward Observation
    # -------------------------------------------------------------------------
    def test_07_distinguish_historical_backtest_from_forward_observation(self):
        """Verify report clearly demarcates historical backtests from forward observations."""
        rep = DailyResearchReport(
            as_of_date="2023-12-29",
            scan_timestamp="2026-11-16T22:00:00+00:00",
            data_dir=self.data_dir,
            feed_status="READY",
            market_regime=None,
            portfolio_status={
                "total_equity_usd": "5000.00",
                "cash_usd": "5000.00",
                "open_positions_count": 0,
            },
            eligible_candidates=[],
            risk_rejected_candidates=[],
            scanner_rejected_candidates={},
            feed_excluded_tickers=[],
            exit_signals=[],
            observation_records=[],
            warnings=[],
            failure_reasons=[],
        )

        formatted = format_observation_report(rep)
        self.assertIn("FORWARD PAPER OBSERVATION ONLY", formatted)
        self.assertIn("Historical Backtests (2022-2023): In-sample baseline", formatted)
        self.assertIn("Forward Observation: Pure observation tracking", formatted)
        self.assertIn("NO MUTATION TO LEDGER", formatted)


if __name__ == "__main__":
    unittest.main()
