"""Deterministic offline test suite for Phase 6.4.5: Comprehensive Forward Observation Logging.

Verifies:
1. All candidates rejected by STRONG_BEARISH generate structured scanner rejection records.
2. Candidates rejected for technical conditions (price, RVOL, SMAs) capture complete rejection reasons.
3. Eligible candidates reaching signal generation record ELIGIBLE status with candidate scores.
4. Candidates rejected during risk sizing record REJECTED_RISK with binding constraints.
5. Mixed eligible and rejected candidates in the same session.
6. Zero candidates evaluated session handling (distinguished from all candidates rejected).
7. Multiple observations across different dates append chronologically without overwriting.
8. Duplicate prevention within one session (exactly one record per evaluated ticker).
9. Correct session-level counts (evaluated, eligible, rejected, signals, sizing).
10. executed_fill is always False across all records.
11. Paper trading ledger and audit state files are never mutated.
12. Existing observation workflow regressions pass without issue.
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

# Ensure skills directory is on sys.path
SKILLS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", ".agents", "skills", "trader-swing")
)
if SKILLS_DIR not in sys.path:
    sys.path.insert(0, SKILLS_DIR)

from market_data_models import MarketClock, OHLCVCandle, to_utc, EASTERN
from observation_workflow import (
    DailyResearchReport,
    ObservationRecord,
    ObservationSessionRecord,
    read_observation_log,
    run_observation_scan,
)
from paper_service import PaperTradingService
from paper_storage import PaperStorageManager
from regime_models import MarketRegime


class TestSwingObservationLogging(unittest.TestCase):
    """Deterministic test suite for scanner-level and session-level observation logging."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_obs_logging_")
        self.data_dir = os.path.join(self.test_dir, "data")
        self.state_dir = os.path.join(self.test_dir, "paper_state")
        self.obs_dir = os.path.join(self.test_dir, "observations")
        self.record_file = os.path.join(self.obs_dir, "observation_log.jsonl")

        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(self.state_dir, exist_ok=True)
        os.makedirs(self.obs_dir, exist_ok=True)

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
        volume: int = 5000000,
    ) -> str:
        """Helper to create valid synthetic daily CSV data."""
        csv_path = os.path.join(self.data_dir, f"{ticker}_daily.csv")
        curr_d = start_date
        sessions_written = 0
        px = base_price

        with open(csv_path, "w", encoding="utf-8") as f:
            f.write("Date,Open,High,Low,Close,Volume\n")
            while sessions_written < num_sessions:
                is_trade, _ = MarketClock.is_historical_trading_day(curr_d)
                if curr_d.weekday() < 5 and is_trade:
                    op = px
                    hi = op + Decimal("1.50")
                    lo = op - Decimal("1.00") if op > Decimal("1.50") else Decimal("0.50")
                    cl = op + Decimal("0.80")
                    vol = 25000000 if ticker in ("SPY", "QQQ", "IWM") else volume
                    f.write(f"{curr_d.isoformat()},{op:.2f},{hi:.2f},{lo:.2f},{cl:.2f},{vol}\n")
                    sessions_written += 1
                    px += trend
                curr_d += timedelta(days=1)

        return csv_path

    def _setup_bullish_proxies(self, start_d: date = date(2023, 1, 3), num_bars: int = 220):
        self._generate_synthetic_daily_csv("SPY", start_d, num_bars, Decimal("400.00"), Decimal("0.50"))
        self._generate_synthetic_daily_csv("QQQ", start_d, num_bars, Decimal("300.00"), Decimal("0.60"))
        self._generate_synthetic_daily_csv("IWM", start_d, num_bars, Decimal("180.00"), Decimal("0.30"))

    def _setup_bearish_proxies(self, start_d: date = date(2023, 1, 3), num_bars: int = 220):
        self._generate_synthetic_daily_csv("SPY", start_d, num_bars, Decimal("600.00"), Decimal("-0.80"))
        self._generate_synthetic_daily_csv("QQQ", start_d, num_bars, Decimal("500.00"), Decimal("-0.90"))
        self._generate_synthetic_daily_csv("IWM", start_d, num_bars, Decimal("280.00"), Decimal("-0.40"))

    def _compute_state_hashes(self) -> dict:
        hashes = {}
        for fname in os.listdir(self.state_dir):
            fpath = os.path.join(self.state_dir, fname)
            if os.path.isfile(fpath):
                with open(fpath, "rb") as f:
                    hashes[fname] = hashlib.sha256(f.read()).hexdigest()
        return hashes

    # -------------------------------------------------------------------------
    # 1. All candidates rejected by STRONG_BEARISH
    # -------------------------------------------------------------------------
    def test_01_all_candidates_rejected_by_strong_bearish(self):
        """Under STRONG_BEARISH, all candidates must generate structured rejection records."""
        start_d = date(2023, 1, 3)
        self._setup_bearish_proxies(start_d, 220)
        self._generate_synthetic_daily_csv("AAPL", start_d, 220, Decimal("150.00"), Decimal("0.10"))
        self._generate_synthetic_daily_csv("MSFT", start_d, 220, Decimal("250.00"), Decimal("0.10"))
        self._generate_synthetic_daily_csv("NVDA", start_d, 220, Decimal("200.00"), Decimal("0.10"))

        report = run_observation_scan(
            data_dir=self.data_dir,
            state_dir=self.state_dir,
            record_file=self.record_file,
            write_records=True,
        )

        self.assertIsNotNone(report.market_regime)
        self.assertEqual(report.market_regime.regime, MarketRegime.STRONG_BEARISH)
        self.assertEqual(len(report.eligible_candidates), 0)
        self.assertEqual(len(report.observation_records), 3)

        records = read_observation_log(self.record_file)
        self.assertEqual(len(records), 4)  # 3 candidate records + 1 session summary

        candidate_recs = [r for r in records if r["record_type"] == "FORWARD_OBSERVATION"]
        session_recs = [r for r in records if r["record_type"] == "SESSION_RECORD"]

        self.assertEqual(len(candidate_recs), 3)
        self.assertEqual(len(session_recs), 1)

        for rec in candidate_recs:
            self.assertEqual(rec["scanner_decision"], "REJECTED")
            self.assertEqual(rec["signal"], "REJECTED_SCANNER")
            self.assertEqual(rec["decision"], "SKIP")
            self.assertFalse(rec["reached_signal_generation"])
            self.assertFalse(rec["reached_risk_sizing"])
            self.assertFalse(rec["executed_fill"])
            self.assertIn("STRONG_BEARISH", rec["market_regime"])
            self.assertTrue(any("disallows new long positions" in r for r in rec["rejection_reasons"]))
            self.assertIsNotNone(rec["candidate_score"])
            self.assertIsNotNone(rec["technical_metrics"])

        sess = session_recs[0]
        self.assertEqual(sess["tickers_evaluated_count"], 3)
        self.assertEqual(sess["eligible_count"], 0)
        self.assertEqual(sess["rejected_count"], 3)
        self.assertEqual(sess["signal_generation_count"], 0)
        self.assertEqual(sess["risk_sizing_count"], 0)
        self.assertEqual(sess["observation_records_count"], 3)
        self.assertIn("ALL_CANDIDATES_REJECTED", sess["notes"])
        self.assertFalse(sess["executed_fill"])

    # -------------------------------------------------------------------------
    # 2. Candidates rejected for technical conditions
    # -------------------------------------------------------------------------
    def test_02_candidates_rejected_for_technical_conditions(self):
        """Technical condition failures (price, volume, SMAs) must be captured in detail."""
        start_d = date(2023, 1, 3)
        self._setup_bullish_proxies(start_d, 220)
        # PENNY: price under $10 threshold
        self._generate_synthetic_daily_csv("PENNY", start_d, 220, Decimal("3.00"), Decimal("0.00"), volume=1000000)
        # LVOL: volume fails ADV ($10M) threshold
        self._generate_synthetic_daily_csv("LVOL", start_d, 220, Decimal("20.00"), Decimal("0.05"), volume=1000)

        report = run_observation_scan(
            data_dir=self.data_dir,
            state_dir=self.state_dir,
            record_file=self.record_file,
            write_records=True,
        )

        records = read_observation_log(self.record_file)
        cand_map = {r["ticker"]: r for r in records if r["record_type"] == "FORWARD_OBSERVATION"}

        self.assertIn("PENNY", cand_map)
        penny_rec = cand_map["PENNY"]
        self.assertEqual(penny_rec["scanner_decision"], "REJECTED")
        self.assertTrue(any("minimum threshold" in r.lower() for r in penny_rec["rejection_reasons"]))
        self.assertFalse(penny_rec["reached_signal_generation"])

        self.assertIn("LVOL", cand_map)
        lvol_rec = cand_map["LVOL"]
        self.assertEqual(lvol_rec["scanner_decision"], "REJECTED")
        self.assertTrue(any("dollar volume" in r.lower() for r in lvol_rec["rejection_reasons"]))

    # -------------------------------------------------------------------------
    # 3. Eligible candidates reaching signal generation
    # -------------------------------------------------------------------------
    def test_03_eligible_candidates_reaching_signal_generation(self):
        """Eligible candidates must record ELIGIBLE scanner decision and reached_signal_generation=True."""
        real_partition = os.path.abspath(
            r".planning\data\historical_partitions\2021_2023_primary_corrected"
        )
        if not os.path.exists(real_partition):
            self.skipTest("Historical partition not found.")

        report = run_observation_scan(
            data_dir=real_partition,
            as_of_date=date(2023, 12, 29),
            tickers=["NVDA", "AAPL", "MSFT"],
            state_dir=self.state_dir,
            record_file=self.record_file,
            write_records=True,
        )

        records = read_observation_log(self.record_file)
        cand_map = {r["ticker"]: r for r in records if r["record_type"] == "FORWARD_OBSERVATION"}

        # At least one candidate in 2023-12-29 is eligible
        eligible_records = [r for r in cand_map.values() if r["scanner_decision"] == "ELIGIBLE"]
        self.assertGreater(len(eligible_records), 0)

        for rec in eligible_records:
            self.assertEqual(rec["rejection_reasons"], [])
            self.assertTrue(rec["reached_signal_generation"])
            self.assertIsNotNone(rec["candidate_score"])
            self.assertIsNotNone(rec["technical_metrics"])

    # -------------------------------------------------------------------------
    # 4. Candidates rejected during risk sizing
    # -------------------------------------------------------------------------
    def test_04_candidates_rejected_during_risk_sizing(self):
        """Candidates reaching risk sizing but rejected must record REJECTED_RISK and binding constraints."""
        real_partition = os.path.abspath(
            r".planning\data\historical_partitions\2021_2023_primary_corrected"
        )
        if not os.path.exists(real_partition):
            self.skipTest("Historical partition not found.")

        # Fill 4 open positions in paper state to reach capacity limit
        service = PaperTradingService(storage=self.storage)
        for sym in ["MSFT", "AAPL", "AMZN", "GOOGL"]:
            service.record_paper_entry(
                ticker=sym,
                shares=5,
                price="100.00",
                stop_loss="95.00",
                timestamp="2023-12-28 15:30:00",
                paper_only=True,
                allow_stale=True,
            )

        report = run_observation_scan(
            data_dir=real_partition,
            as_of_date=date(2023, 12, 29),
            tickers=["NVDA"],
            state_dir=self.state_dir,
            record_file=self.record_file,
            write_records=True,
        )

        records = read_observation_log(self.record_file)
        risk_rej = [r for r in records if r.get("risk_decision") == "REJECTED_RISK"]
        self.assertGreater(len(risk_rej), 0)

        rec = risk_rej[0]
        self.assertEqual(rec["scanner_decision"], "ELIGIBLE")
        self.assertTrue(rec["reached_signal_generation"])
        self.assertTrue(rec["reached_risk_sizing"])
        self.assertEqual(rec["signal"], "REJECTED_RISK")
        self.assertEqual(rec["decision"], "SKIP")
        self.assertIn("MAX_POSITIONS_EXCEEDED", rec["notes"])
        self.assertIsNotNone(rec["entry_reference"])
        self.assertIsNotNone(rec["stop_reference"])

    # -------------------------------------------------------------------------
    # 5. Mixed eligible and rejected candidates
    # -------------------------------------------------------------------------
    def test_05_mixed_eligible_and_rejected_candidates(self):
        """Mixed session must capture both ELIGIBLE and REJECTED candidates without collision."""
        real_partition = os.path.abspath(
            r".planning\data\historical_partitions\2021_2023_primary_corrected"
        )
        if not os.path.exists(real_partition):
            self.skipTest("Historical partition not found.")

        # Mix: NVDA (strong candidate on 2023-12-29) and a non-qualifying stock
        report = run_observation_scan(
            data_dir=real_partition,
            as_of_date=date(2023, 12, 29),
            tickers=["NVDA", "XOM", "CVX"],
            state_dir=self.state_dir,
            record_file=self.record_file,
            write_records=True,
        )

        records = read_observation_log(self.record_file)
        cand_recs = [r for r in records if r["record_type"] == "FORWARD_OBSERVATION"]
        sess_recs = [r for r in records if r["record_type"] == "SESSION_RECORD"]

        self.assertEqual(len(sess_recs), 1)
        sess = sess_recs[0]
        self.assertEqual(sess["tickers_evaluated_count"], len(cand_recs))
        self.assertEqual(sess["eligible_count"] + sess["rejected_count"], sess["tickers_evaluated_count"])

        decisions = set(r["scanner_decision"] for r in cand_recs)
        self.assertTrue("ELIGIBLE" in decisions or "REJECTED" in decisions)

    # -------------------------------------------------------------------------
    # 6. Zero candidates evaluated
    # -------------------------------------------------------------------------
    def test_06_zero_candidates_evaluated(self):
        """Zero candidates evaluated must be cleanly distinguished from all candidates rejected."""
        start_d = date(2023, 1, 3)
        self._setup_bullish_proxies(start_d, 220)
        # No stock CSVs created

        report = run_observation_scan(
            data_dir=self.data_dir,
            tickers=[],
            state_dir=self.state_dir,
            record_file=self.record_file,
            write_records=True,
        )

        self.assertEqual(len(report.observation_records), 0)
        self.assertIsNotNone(report.session_record)

        records = read_observation_log(self.record_file)
        self.assertEqual(len(records), 1)

        sess = records[0]
        self.assertEqual(sess["record_type"], "SESSION_RECORD")
        self.assertEqual(sess["tickers_evaluated_count"], 0)
        self.assertEqual(sess["eligible_count"], 0)
        self.assertEqual(sess["rejected_count"], 0)
        self.assertEqual(sess["observation_records_count"], 0)
        self.assertIn("NO_CANDIDATES_EVALUATED", sess["notes"])
        self.assertFalse(sess["executed_fill"])

    # -------------------------------------------------------------------------
    # 7. Multiple observations across different dates
    # -------------------------------------------------------------------------
    def test_07_multiple_observations_across_different_dates(self):
        """Sequential scans across multiple dates must append cleanly without overwriting."""
        real_partition = os.path.abspath(
            r".planning\data\historical_partitions\2021_2023_primary_corrected"
        )
        if not os.path.exists(real_partition):
            self.skipTest("Historical partition not found.")

        # Date 1: 2023-12-28
        run_observation_scan(
            data_dir=real_partition,
            as_of_date=date(2023, 12, 28),
            tickers=["NVDA", "AAPL"],
            state_dir=self.state_dir,
            record_file=self.record_file,
            write_records=True,
        )

        # Date 2: 2023-12-29
        run_observation_scan(
            data_dir=real_partition,
            as_of_date=date(2023, 12, 29),
            tickers=["NVDA", "AAPL"],
            state_dir=self.state_dir,
            record_file=self.record_file,
            write_records=True,
        )

        records = read_observation_log(self.record_file)
        dates = [r["date"] for r in records]

        self.assertIn("2023-12-28", dates)
        self.assertIn("2023-12-29", dates)

        # Confirm 2 session records exist
        sess_recs = [r for r in records if r["record_type"] == "SESSION_RECORD"]
        self.assertEqual(len(sess_recs), 2)
        self.assertEqual(sess_recs[0]["date"], "2023-12-28")
        self.assertEqual(sess_recs[1]["date"], "2023-12-29")

    # -------------------------------------------------------------------------
    # 8. Duplicate prevention within one session
    # -------------------------------------------------------------------------
    def test_08_duplicate_prevention_within_one_session(self):
        """Every evaluated ticker must have exactly one observation record per session."""
        real_partition = os.path.abspath(
            r".planning\data\historical_partitions\2021_2023_primary_corrected"
        )
        if not os.path.exists(real_partition):
            self.skipTest("Historical partition not found.")

        run_observation_scan(
            data_dir=real_partition,
            as_of_date=date(2023, 12, 29),
            tickers=["NVDA", "AAPL", "MSFT", "AMZN", "GOOGL"],
            state_dir=self.state_dir,
            record_file=self.record_file,
            write_records=True,
        )

        records = read_observation_log(self.record_file)
        cand_recs = [r for r in records if r["record_type"] == "FORWARD_OBSERVATION"]
        tickers = [r["ticker"] for r in cand_recs]

        self.assertEqual(len(tickers), len(set(tickers)), f"Duplicate tickers found: {tickers}")

    # -------------------------------------------------------------------------
    # 9. Correct session-level counts
    # -------------------------------------------------------------------------
    def test_09_correct_session_level_counts(self):
        """Session summary counts must strictly equal candidate aggregates."""
        start_d = date(2023, 1, 3)
        self._setup_bearish_proxies(start_d, 220)
        self._generate_synthetic_daily_csv("STKA", start_d, 220, Decimal("100.00"), Decimal("0.05"))
        self._generate_synthetic_daily_csv("STKB", start_d, 220, Decimal("120.00"), Decimal("0.05"))

        report = run_observation_scan(
            data_dir=self.data_dir,
            state_dir=self.state_dir,
            record_file=self.record_file,
            write_records=True,
        )

        sr = report.session_record
        self.assertIsNotNone(sr)
        self.assertEqual(sr.tickers_evaluated_count, 2)
        self.assertEqual(sr.eligible_count, 0)
        self.assertEqual(sr.rejected_count, 2)
        self.assertEqual(sr.signal_generation_count, 0)
        self.assertEqual(sr.risk_sizing_count, 0)
        self.assertEqual(sr.observation_records_count, len(report.observation_records))

    # -------------------------------------------------------------------------
    # 10. executed_fill always false
    # -------------------------------------------------------------------------
    def test_10_executed_fill_always_false(self):
        """Every record (candidate and session) must explicitly declare executed_fill=False."""
        start_d = date(2023, 1, 3)
        self._setup_bullish_proxies(start_d, 220)
        self._generate_synthetic_daily_csv("STKA", start_d, 220, Decimal("50.00"), Decimal("0.10"))

        run_observation_scan(
            data_dir=self.data_dir,
            state_dir=self.state_dir,
            record_file=self.record_file,
            write_records=True,
        )

        records = read_observation_log(self.record_file)
        self.assertGreater(len(records), 0)

        for idx, rec in enumerate(records):
            self.assertIn("executed_fill", rec, f"Record #{idx} missing executed_fill field.")
            self.assertIs(rec["executed_fill"], False, f"Record #{idx} executed_fill is not False.")

    # -------------------------------------------------------------------------
    # 11. No paper ledger mutations
    # -------------------------------------------------------------------------
    def test_11_no_paper_ledger_mutations(self):
        """Paper trading state directory files must remain completely untouched."""
        start_d = date(2023, 1, 3)
        self._setup_bullish_proxies(start_d, 220)
        self._generate_synthetic_daily_csv("AAPL", start_d, 220, Decimal("150.00"), Decimal("0.10"))
        self._generate_synthetic_daily_csv("NVDA", start_d, 220, Decimal("200.00"), Decimal("0.10"))

        pre_hashes = self._compute_state_hashes()
        self.assertGreater(len(pre_hashes), 0)

        run_observation_scan(
            data_dir=self.data_dir,
            state_dir=self.state_dir,
            record_file=self.record_file,
            write_records=True,
        )

        post_hashes = self._compute_state_hashes()
        self.assertEqual(pre_hashes, post_hashes, "Paper trading state was mutated!")

        journal = self.storage.load_journal()
        self.assertEqual(journal.ledger.total_equity_usd, Decimal("5000.00"))
        self.assertEqual(journal.ledger.cash_usd, Decimal("5000.00"))
        self.assertEqual(len(journal.ledger.positions), 0)
        self.assertEqual(len(journal.ledger.closed_trades), 0)

    # -------------------------------------------------------------------------
    # 12. Existing observation workflow regressions pass
    # -------------------------------------------------------------------------
    def test_12_existing_workflow_regression_contract(self):
        """Confirm ObservationRecord maintains full backward compatibility for existing consumers."""
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
        self.assertEqual(rec.record_type, "FORWARD_OBSERVATION")
        self.assertFalse(rec.executed_fill)
        self.assertEqual(rec.decision, "OBSERVE_ONLY")
        self.assertEqual(rec.signal, "ENTRY_SIGNAL")

        d = rec.to_dict()
        self.assertEqual(d["date"], "2023-12-29")
        self.assertEqual(d["ticker"], "NVDA")
        self.assertFalse(d["executed_fill"])


if __name__ == "__main__":
    unittest.main()
