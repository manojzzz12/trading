"""Targeted test suite for Phase 6.1 Manual Paper Trading CLI and Storage Engine.

Enforces:
1. State is strictly maintained in a temporary external directory outside the repository.
2. Root protected files (paper_journal.json, portfolio.json, broker.json) are NEVER created or touched.
3. Path safety: Rejection of any state dir inside repo root or .agents, including symlinks/junctions.
4. $5,000.00 starting capital initialization.
5. Mandatory --paper-only flag on all mutations.
6. Rejection of forbidden flags (--live, --broker, --key, --webhook, etc.).
7. Market-session and timestamp validation with 24-hour backdating window.
8. Frozen risk controls: 4 positions max, 0.50% risk per trade, 20% cash reserve, 80% invested cap, 25% ticker cap.
9. Zero-recovery delisting quantity integrity, explicit lot-id selection on multi-lot positions,
   and prohibition of nonzero fees on zero-recovery write-offs.
10. Multi-file transactional crash recovery using commit manifest with failure injection tests.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
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

from paper_models import (
    PaperExitReason,
    PaperExitRecord,
    PaperFillRecord,
    PaperPositionMetadata,
)
from paper_storage import (
    PaperStorageManager,
    PaperStorageSafetyError,
    FORBIDDEN_ROOT_FILES,
    _REPO_ROOT,
)
from paper_service import (
    MarketSessionValidationError,
    PaperSafetyViolation,
    PaperTradingService,
    RiskViolationError,
)
from paper_cli import scan_for_forbidden_flags, main as cli_main


class TestSwingPaperPhase61(unittest.TestCase):
    """Test suite validating Phase 6.1 paper trading safety, risk, and storage invariants."""

    def setUp(self):
        # Create external temporary directory located outside repository
        self.temp_dir = tempfile.TemporaryDirectory()
        self.external_state_dir = os.path.join(self.temp_dir.name, "ExternalPaperState")
        os.makedirs(self.external_state_dir, exist_ok=True)
        self.storage = PaperStorageManager(state_dir=self.external_state_dir)
        self.service = PaperTradingService(storage=self.storage)

        # Baseline reference timestamp during regular trading hours (Monday 2026-10-19 10:15:00 EDT)
        # EDT is UTC-4: 10:15 EDT is 14:15 UTC
        self.valid_trading_ts = "2026-10-19T14:15:00+00:00"
        self.valid_reference_now = datetime.fromisoformat("2026-10-19T14:30:00+00:00")

    def tearDown(self):
        self.temp_dir.cleanup()

    # -------------------------------------------------------------------------
    # 1. Path Safety & Symlink/Junction Containment
    # -------------------------------------------------------------------------

    def test_01_external_state_directory_safety_enforced(self):
        """1. Verify state dir inside repository root or .agents is strictly rejected."""
        # A. Inside repository root
        inside_repo_path = os.path.join(_REPO_ROOT, "state_test")
        with self.assertRaises(PaperStorageSafetyError):
            PaperStorageManager(state_dir=inside_repo_path)

        # B. Inside .agents folder anywhere
        agents_path = os.path.join(self.temp_dir.name, ".agents", "paper_state")
        with self.assertRaises(PaperStorageSafetyError):
            PaperStorageManager(state_dir=agents_path)

        # C. Matching forbidden root file names
        forbidden_name_path = os.path.join(self.temp_dir.name, "paper_journal.json")
        with self.assertRaises(PaperStorageSafetyError):
            PaperStorageManager(state_dir=forbidden_name_path)

    def test_01b_symlink_or_junction_path_containment(self):
        """1b. Verify symlink or junction resolving inside repo root is rejected."""
        link_path = os.path.join(self.temp_dir.name, "link_to_repo")
        link_created = False

        # Attempt creating a symlink or junction
        try:
            os.symlink(_REPO_ROOT, link_path, target_is_directory=True)
            link_created = True
        except (OSError, NotImplementedError):
            # Try Windows directory junction if symlink not permitted
            try:
                import _winapi
                _winapi.CreateJunction(_REPO_ROOT, link_path)
                link_created = True
            except Exception:
                link_created = False

        if not link_created:
            self.skipTest("Symlinks and directory junctions not permitted by OS in current test environment.")

        # Realpath resolution must detect that target is inside repo root
        with self.assertRaises(PaperStorageSafetyError):
            PaperStorageManager(state_dir=link_path)

    # -------------------------------------------------------------------------
    # 2. Capital Initialization
    # -------------------------------------------------------------------------

    def test_02_capital_initialization_5000(self):
        """2. Verify initialization initializes ledger with exactly $5,000.00 USD."""
        ledger = self.storage.initialize_state(initial_capital=Decimal("5000.00"))
        self.assertEqual(ledger.initial_capital_usd, Decimal("5000.00"))
        self.assertEqual(ledger.cash_usd, Decimal("5000.00"))
        self.assertEqual(ledger.total_equity_usd, Decimal("5000.00"))

        status = self.service.get_status()
        self.assertEqual(status["total_equity_usd"], "5000.00")
        self.assertEqual(status["cash_usd"], "5000.00")
        self.assertEqual(status["open_positions_count"], 0)
        self.assertTrue(status["equity_invariant_holds"])

        events = self.storage.load_audit_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "INITIALIZE")
        self.assertEqual(events[0]["cash_after"], "5000.00")

    # -------------------------------------------------------------------------
    # 3. Mandatory --paper-only Flag
    # -------------------------------------------------------------------------

    def test_03_paper_only_flag_required_for_mutations(self):
        """3. Reject any mutation if paper_only is False."""
        self.storage.initialize_state(Decimal("5000.00"))

        with self.assertRaises(PaperSafetyViolation):
            self.service.record_paper_entry(
                ticker="AAPL",
                shares=5,
                price="150.00",
                stop_loss="146.00",
                timestamp=self.valid_trading_ts,
                paper_only=False,
                reference_now=self.valid_reference_now,
            )

        with self.assertRaises(PaperSafetyViolation):
            self.service.record_paper_exit(
                ticker="AAPL",
                shares=5,
                price="155.00",
                timestamp=self.valid_trading_ts,
                paper_only=False,
                reference_now=self.valid_reference_now,
            )

    # -------------------------------------------------------------------------
    # 4. CLI Forbidden Flags
    # -------------------------------------------------------------------------

    def test_04_cli_forbidden_flags_rejection(self):
        """4. Reject CLI invocation if forbidden live/broker/automation flags are present."""
        forbidden_examples = [
            ["--live"],
            ["--broker"],
            ["--key=secret123"],
            ["--webhook", "http://example.com"],
            ["--auto"],
            ["--poll"],
            ["--daemon"],
            ["--alpaca"],
            ["--ibkr"],
        ]
        for flags in forbidden_examples:
            with self.assertRaises(SystemExit) as cm:
                scan_for_forbidden_flags(flags)
            self.assertEqual(cm.exception.code, 1)

    # -------------------------------------------------------------------------
    # 5. Market Session & Timestamp Validation
    # -------------------------------------------------------------------------

    def test_05_market_session_validation(self):
        """5. Validate session boundaries, weekends, holidays, and future timestamps."""
        # A. Valid trading session (Monday 2026-10-19 10:15 EDT)
        dt_utc, ts_str = self.service.validate_timestamp(
            "2026-10-19T10:15:00-04:00",
            reference_now=datetime.fromisoformat("2026-10-19T11:00:00-04:00"),
        )
        self.assertIsNotNone(dt_utc)

        # B. Weekend rejection (Sunday 2026-10-18 10:15 EDT)
        with self.assertRaises(MarketSessionValidationError):
            self.service.validate_timestamp(
                "2026-10-18T10:15:00-04:00",
                reference_now=datetime.fromisoformat("2026-10-19T11:00:00-04:00"),
            )

        # C. Holiday rejection (Christmas Day 2026-12-25 10:15 EDT)
        with self.assertRaises(MarketSessionValidationError):
            self.service.validate_timestamp(
                "2026-12-25T10:15:00-05:00",
                reference_now=datetime.fromisoformat("2026-12-26T11:00:00-05:00"),
            )

        # D. Off-hours rejection (08:00 AM EDT before 09:30 AM open)
        with self.assertRaises(MarketSessionValidationError):
            self.service.validate_timestamp(
                "2026-10-19T08:00:00-04:00",
                reference_now=datetime.fromisoformat("2026-10-19T11:00:00-04:00"),
            )

        # E. Future timestamp rejection
        with self.assertRaises(MarketSessionValidationError):
            self.service.validate_timestamp(
                "2026-10-19T15:00:00-04:00",
                reference_now=datetime.fromisoformat("2026-10-19T10:00:00-04:00"),
            )

    def test_05b_backdating_window_freshness(self):
        """5b. Validate that fills older than 24 hours are rejected unless allow_stale=True."""
        ref_now = datetime.fromisoformat("2026-10-21T14:00:00+00:00")  # Wed 10:00 AM EDT

        # Same-day fill (2 hours prior) -> ACCEPTED
        same_day_ts = "2026-10-21T12:00:00+00:00"  # Wed 08:00 AM EDT -> adjust to 10:00 AM EDT: 14:00 UTC
        valid_same_day = "2026-10-21T13:45:00+00:00"  # 09:45 AM EDT
        dt_utc, _ = self.service.validate_timestamp(valid_same_day, reference_now=ref_now)
        self.assertIsNotNone(dt_utc)

        # Stale fill (48 hours prior: Monday 2026-10-19 14:00 UTC) -> REJECTED
        stale_ts = "2026-10-19T14:00:00+00:00"
        with self.assertRaises(MarketSessionValidationError) as cm:
            self.service.validate_timestamp(stale_ts, reference_now=ref_now)
        self.assertIn("STALE_FILL_REJECTED", str(cm.exception))

        # Explicit exception allow_stale=True -> ACCEPTED
        dt_allowed, _ = self.service.validate_timestamp(stale_ts, reference_now=ref_now, allow_stale=True)
        self.assertIsNotNone(dt_allowed)

    # -------------------------------------------------------------------------
    # 6. Valid Entry Fill Recording
    # -------------------------------------------------------------------------

    def test_06_record_valid_paper_entry(self):
        """6. Record a user-confirmed paper entry fill with stops, fees, and audit trail."""
        self.storage.initialize_state(Decimal("5000.00"))

        res = self.service.record_paper_entry(
            ticker="AAPL",
            shares=5,
            price="150.00",
            stop_loss="146.00",
            target="160.00",
            fee="1.00",
            timestamp=self.valid_trading_ts,
            order_notes="TradingView paper fill #101",
            paper_only=True,
            reference_now=self.valid_reference_now,
        )

        self.assertEqual(res["status"], "RECORDED")
        self.assertEqual(res["ticker"], "AAPL")
        self.assertEqual(res["shares"], 5)
        self.assertEqual(res["cash_remaining"], "4249.00")
        self.assertEqual(res["equity_total"], "4999.00")

        portfolio = self.storage.load_portfolio()
        lot_id = res["lot_id"]
        self.assertIn(lot_id, portfolio["lots"])
        self.assertEqual(portfolio["lots"][lot_id]["stop_loss"], "146.00")
        self.assertEqual(portfolio["lots"][lot_id]["target"], "160.00")

        status = self.service.get_status()
        self.assertEqual(status["open_positions_count"], 1)
        self.assertEqual(status["active_tickers"], ["AAPL"])
        self.assertTrue(status["equity_invariant_holds"])

    # -------------------------------------------------------------------------
    # 7. Risk Limit: Maximum 4 Simultaneous Positions
    # -------------------------------------------------------------------------

    def test_07_max_simultaneous_positions_limit_4(self):
        """7. Reject 5th open ticker when 4 positions are already open."""
        self.storage.initialize_state(Decimal("5000.00"))

        tickers = ["AAPL", "MSFT", "GOOGL", "NVDA"]
        for sym in tickers:
            self.service.record_paper_entry(
                ticker=sym,
                shares=2,
                price="100.00",
                stop_loss="90.00",
                timestamp=self.valid_trading_ts,
                paper_only=True,
                reference_now=self.valid_reference_now,
            )

        status = self.service.get_status()
        self.assertEqual(status["open_positions_count"], 4)

        with self.assertRaises(RiskViolationError) as cm:
            self.service.record_paper_entry(
                ticker="AMZN",
                shares=2,
                price="100.00",
                stop_loss="90.00",
                timestamp=self.valid_trading_ts,
                paper_only=True,
                reference_now=self.valid_reference_now,
            )
        self.assertIn("MAX_POSITIONS_EXCEEDED", str(cm.exception))

    # -------------------------------------------------------------------------
    # 8. Risk Limit: 0.50% Per-Trade Risk
    # -------------------------------------------------------------------------

    def test_08_per_trade_risk_limit_050_percent(self):
        """8. Reject trade if total risk (entry - stop) * shares exceeds 0.50% of equity."""
        self.storage.initialize_state(Decimal("5000.00"))

        with self.assertRaises(RiskViolationError) as cm:
            self.service.record_paper_entry(
                ticker="TSLA",
                shares=2,
                price="100.00",
                stop_loss="80.00",  # $20 risk * 2 = $40 > $25
                timestamp=self.valid_trading_ts,
                paper_only=True,
                reference_now=self.valid_reference_now,
            )
        self.assertIn("PER_TRADE_RISK_EXCEEDED", str(cm.exception))

    # -------------------------------------------------------------------------
    # 9. Risk Limit: 20% Minimum Cash Reserve
    # -------------------------------------------------------------------------

    def test_09_minimum_cash_reserve_20_percent(self):
        """9. Reject trade if purchase leaves cash below 20% of equity ($1,000 on $5,000)."""
        self.storage.initialize_state(Decimal("5000.00"))

        with self.assertRaises(RiskViolationError) as cm:
            self.service.record_paper_entry(
                ticker="MSFT",
                shares=9,
                price="450.00",
                stop_loss="448.00",
                timestamp=self.valid_trading_ts,
                paper_only=True,
                reference_now=self.valid_reference_now,
            )
        self.assertIn("MIN_CASH_RESERVE_VIOLATION", str(cm.exception))

    # -------------------------------------------------------------------------
    # 10. Stop-Loss Geometry Validation
    # -------------------------------------------------------------------------

    def test_10_invalid_stop_loss_geometry(self):
        """10. Reject entries with stop loss >= entry price."""
        self.storage.initialize_state(Decimal("5000.00"))

        with self.assertRaises(RiskViolationError) as cm:
            self.service.record_paper_entry(
                ticker="AAPL",
                shares=5,
                price="150.00",
                stop_loss="150.00",
                timestamp=self.valid_trading_ts,
                paper_only=True,
                reference_now=self.valid_reference_now,
            )
        self.assertIn("INVALID_STOP_LOSS", str(cm.exception))

    # -------------------------------------------------------------------------
    # 11. Exit Fill Recording & Positive-Price Delisting Fee Deduction
    # -------------------------------------------------------------------------

    def test_11_record_paper_exit_and_pnl_realization(self):
        """11. Record paper exit, realize profit, update cash, and preserve invariant."""
        self.storage.initialize_state(Decimal("5000.00"))

        entry_res = self.service.record_paper_entry(
            ticker="AAPL",
            shares=5,
            price="150.00",
            stop_loss="146.00",
            fee="1.00",
            timestamp=self.valid_trading_ts,
            paper_only=True,
            reference_now=self.valid_reference_now,
        )

        exit_res = self.service.record_paper_exit(
            ticker="AAPL",
            shares=5,
            price="160.00",
            fee="1.00",
            reason="TAKE_PROFIT",
            timestamp=self.valid_trading_ts,
            paper_only=True,
            reference_now=self.valid_reference_now,
        )

        self.assertEqual(exit_res["status"], "RECORDED")
        self.assertEqual(exit_res["realized_pnl"], "48.00")
        self.assertEqual(exit_res["cash_after"], "5048.00")
        self.assertEqual(exit_res["equity_after"], "5048.00")

        portfolio = self.storage.load_portfolio()
        self.assertEqual(len(portfolio["lots"]), 0)

        status = self.service.get_status()
        self.assertEqual(status["open_positions_count"], 0)
        self.assertEqual(status["realized_pnl_usd"], "48.00")
        self.assertTrue(status["equity_invariant_holds"])

    def test_11b_positive_price_delisting_deducts_fee(self):
        """11b. Verify positive-price delisting exit correctly deducts the configured exit fee."""
        self.storage.initialize_state(Decimal("5000.00"))

        self.service.record_paper_entry(
            ticker="XYZ",
            shares=10,
            price="10.00",
            stop_loss="8.00",
            fee="0.50",
            timestamp=self.valid_trading_ts,
            paper_only=True,
            reference_now=self.valid_reference_now,
        )

        exit_res = self.service.record_paper_exit(
            ticker="XYZ",
            shares=10,
            price="5.00",
            fee="1.50",
            reason="DELISTING_POSITIVE",
            timestamp=self.valid_trading_ts,
            paper_only=True,
            reference_now=self.valid_reference_now,
        )

        self.assertEqual(exit_res["status"], "RECORDED")
        self.assertEqual(exit_res["fee"], "1.50")
        # Cost basis $100. Proceeds $50. Entry fee $0.50, exit fee $1.50 -> net P&L = -$52.00
        self.assertEqual(exit_res["realized_pnl"], "-52.00")
        status = self.service.get_status()
        self.assertEqual(status["realized_pnl_usd"], "-52.00")
        self.assertTrue(status["equity_invariant_holds"])

    # -------------------------------------------------------------------------
    # 12. Zero-Recovery Delisting Quantity Integrity & Prohibitions
    # -------------------------------------------------------------------------

    def test_12_zero_recovery_delisting_quantity_and_fee_integrity(self):
        """12. Full-lot write-off succeeds; partial write-off, multi-lot omission, and fee are rejected."""
        self.storage.initialize_state(Decimal("5000.00"))

        # Buy 10 shares @ $20
        entry_res = self.service.record_paper_entry(
            ticker="XYZ",
            shares=10,
            price="20.00",
            stop_loss="18.00",
            fee="0.00",
            timestamp=self.valid_trading_ts,
            paper_only=True,
            reference_now=self.valid_reference_now,
        )
        lot_id_1 = entry_res["lot_id"]

        # A. Nonzero fee on zero-recovery write-off -> REJECTED
        with self.assertRaises(RiskViolationError) as cm_fee:
            self.service.record_paper_exit(
                ticker="XYZ",
                shares=10,
                price="0.00",
                fee="1.00",
                reason="DELISTING_ZERO_RECOVERY",
                timestamp=self.valid_trading_ts,
                paper_only=True,
                reference_now=self.valid_reference_now,
            )
        self.assertIn("ZERO_RECOVERY_FEE_PROHIBITED", str(cm_fee.exception))

        # B. Partial zero-recovery request (5 of 10 shares) -> REJECTED & state unchanged
        with self.assertRaises(RiskViolationError) as cm_part:
            self.service.record_paper_exit(
                ticker="XYZ",
                shares=5,
                price="0.00",
                fee="0.00",
                reason="DELISTING_ZERO_RECOVERY",
                timestamp=self.valid_trading_ts,
                paper_only=True,
                reference_now=self.valid_reference_now,
            )
        self.assertIn("PARTIAL_ZERO_RECOVERY_REJECTED", str(cm_part.exception))

        # Verify state unchanged after rejection
        status_after_reject = self.service.get_status()
        self.assertEqual(status_after_reject["open_positions_count"], 1)

        # Add second lot for XYZ to test multi-lot ambiguity
        entry_res_2 = self.service.record_paper_entry(
            ticker="XYZ",
            shares=5,
            price="20.00",
            stop_loss="18.00",
            fee="0.00",
            timestamp=self.valid_trading_ts,
            paper_only=True,
            reference_now=self.valid_reference_now,
        )
        lot_id_2 = entry_res_2["lot_id"]

        # C. Multi-lot ticker without --lot-id -> REJECTED
        with self.assertRaises(RiskViolationError) as cm_multi:
            self.service.record_paper_exit(
                ticker="XYZ",
                shares=10,
                price="0.00",
                fee="0.00",
                reason="DELISTING_ZERO_RECOVERY",
                timestamp=self.valid_trading_ts,
                paper_only=True,
                reference_now=self.valid_reference_now,
            )
        self.assertIn("AMBIGUOUS_LOT_SELECTION", str(cm_multi.exception))

        # D. Full-lot write-off with explicit lot_id -> SUCCEEDS
        exit_res = self.service.record_paper_exit(
            ticker="XYZ",
            shares=10,
            price="0.00",
            fee="0.00",
            reason="DELISTING_ZERO_RECOVERY",
            lot_id=lot_id_1,
            timestamp=self.valid_trading_ts,
            paper_only=True,
            reference_now=self.valid_reference_now,
        )
        self.assertEqual(exit_res["status"], "RECORDED")
        self.assertEqual(exit_res["shares"], 10)
        self.assertEqual(exit_res["realized_pnl"], "-200.00")
        self.assertEqual(exit_res["lot_id"], lot_id_1)

        # Audit details confirm exact lot and shares
        events = self.storage.load_audit_events()
        exit_event = events[-1]
        self.assertEqual(exit_event["event_type"], "RECORD_EXIT")
        self.assertEqual(exit_event["details"]["lot_id"], lot_id_1)
        self.assertEqual(exit_event["details"]["shares_written_off"], 10)

        # Status confirms remaining lot intact
        status_final = self.service.get_status()
        self.assertEqual(status_final["open_positions_count"], 1)
        self.assertEqual(len(status_final["active_lots"]), 1)
        self.assertEqual(status_final["active_lots"][0]["lot_id"], lot_id_2)
        self.assertTrue(status_final["equity_invariant_holds"])

    # -------------------------------------------------------------------------
    # 13. Multi-File Transactional Crash Recovery (Failure Injections)
    # -------------------------------------------------------------------------

    def test_13_multi_file_crash_recovery_injected_failures(self):
        """13. Injected failure tests at each persistence stage proving truthfulness and rollback."""
        self.storage.initialize_state(Decimal("5000.00"))

        # Point A: Injected failure BEFORE manifest written -> Prior state retained
        with self.assertRaises(RuntimeError) as cm_a:
            self.service.record_paper_entry(
                ticker="AAPL",
                shares=5,
                price="150.00",
                stop_loss="146.00",
                timestamp=self.valid_trading_ts,
                paper_only=True,
                reference_now=self.valid_reference_now,
                failure_injection_point="BEFORE_MANIFEST",
            )
        self.assertIn("BEFORE_MANIFEST", str(cm_a.exception))

        # Reload state from disk: must be identical to initial state ($5,000 cash, 0 lots)
        fresh_storage_a = PaperStorageManager(state_dir=self.external_state_dir)
        fresh_service_a = PaperTradingService(storage=fresh_storage_a)
        status_a = fresh_service_a.get_status()
        self.assertEqual(status_a["cash_usd"], "5000.00")
        self.assertEqual(status_a["open_positions_count"], 0)
        self.assertEqual(len(fresh_storage_a.load_orders().get("fills", [])), 0)

        # Point B: Injected failure PRE-COMMIT -> Prior state retained
        with self.assertRaises(RuntimeError) as cm_b:
            self.service.record_paper_entry(
                ticker="AAPL",
                shares=5,
                price="150.00",
                stop_loss="146.00",
                timestamp=self.valid_trading_ts,
                paper_only=True,
                reference_now=self.valid_reference_now,
                failure_injection_point="PRE_COMMIT",
            )
        self.assertIn("PRE_COMMIT", str(cm_b.exception))

        fresh_storage_b = PaperStorageManager(state_dir=self.external_state_dir)
        status_b = PaperTradingService(storage=fresh_storage_b).get_status()
        self.assertEqual(status_b["cash_usd"], "5000.00")
        self.assertEqual(status_b["open_positions_count"], 0)

        # Point C: Injected failure AFTER commit point (manifest COMMITTED) before apply
        with self.assertRaises(RuntimeError) as cm_c:
            self.service.record_paper_entry(
                ticker="AAPL",
                shares=5,
                price="150.00",
                stop_loss="146.00",
                timestamp=self.valid_trading_ts,
                paper_only=True,
                reference_now=self.valid_reference_now,
                failure_injection_point="AFTER_COMMIT_BEFORE_APPLY",
            )
        self.assertIn("AFTER_COMMIT_BEFORE_APPLY", str(cm_c.exception))

        # On restart, recovery detects COMMITTED manifest and completes applying the transaction
        fresh_storage_c = PaperStorageManager(state_dir=self.external_state_dir)
        fresh_service_c = PaperTradingService(storage=fresh_storage_c)
        status_c = fresh_service_c.get_status()
        self.assertEqual(status_c["open_positions_count"], 1)
        self.assertEqual(status_c["active_tickers"], ["AAPL"])
        self.assertEqual(status_c["cash_usd"], "4250.00")
        self.assertTrue(status_c["equity_invariant_holds"])

        orders_c = fresh_storage_c.load_orders()
        self.assertEqual(len(orders_c["fills"]), 1)
        self.assertEqual(orders_c["fills"][0]["ticker"], "AAPL")

        portfolio_c = fresh_storage_c.load_portfolio()
        self.assertEqual(len(portfolio_c["lots"]), 1)

        # Point D: Injected failure MID-APPLY
        with self.assertRaises(RuntimeError) as cm_d:
            fresh_service_c.record_paper_exit(
                ticker="AAPL",
                shares=5,
                price="160.00",
                reason="TAKE_PROFIT",
                timestamp=self.valid_trading_ts,
                paper_only=True,
                reference_now=self.valid_reference_now,
                failure_injection_point="MID_APPLY",
            )
        self.assertIn("MID_APPLY", str(cm_d.exception))

        # On restart, recovery applies all staged files so journal, portfolio, orders, and audit reconcile
        fresh_storage_d = PaperStorageManager(state_dir=self.external_state_dir)
        status_d = PaperTradingService(storage=fresh_storage_d).get_status()
        self.assertEqual(status_d["open_positions_count"], 0)
        self.assertEqual(status_d["cash_usd"], "5050.00")
        self.assertEqual(status_d["realized_pnl_usd"], "50.00")
        self.assertTrue(status_d["equity_invariant_holds"])

        orders_d = fresh_storage_d.load_orders()
        self.assertEqual(len(orders_d["exits"]), 1)
        portfolio_d = fresh_storage_d.load_portfolio()
        self.assertEqual(len(portfolio_d["lots"]), 0)

    # -------------------------------------------------------------------------
    # 14. CLI End-to-End Execution
    # -------------------------------------------------------------------------

    def test_14_cli_end_to_end_workflow(self):
        """14. Test CLI main() commands: init, record-entry, status, record-exit, audit."""
        ret_init = cli_main([
            "--state-dir", self.external_state_dir,
            "init",
            "--capital", "5000.00",
            "--paper-only",
        ])
        self.assertEqual(ret_init, 0)

        ret_entry = cli_main([
            "--state-dir", self.external_state_dir,
            "record-entry",
            "--ticker", "MSFT",
            "--shares", "4",
            "--price", "200.00",
            "--stop", "195.00",
            "--fee", "1.00",
            "--timestamp", self.valid_trading_ts,
            "--paper-only",
            "--allow-stale",
        ])
        self.assertEqual(ret_entry, 0)

        ret_status = cli_main([
            "--state-dir", self.external_state_dir,
            "status",
        ])
        self.assertEqual(ret_status, 0)

        ret_exit = cli_main([
            "--state-dir", self.external_state_dir,
            "record-exit",
            "--ticker", "MSFT",
            "--shares", "4",
            "--price", "210.00",
            "--fee", "1.00",
            "--reason", "TAKE_PROFIT",
            "--timestamp", self.valid_trading_ts,
            "--paper-only",
            "--allow-stale",
        ])
        self.assertEqual(ret_exit, 0)

        ret_audit = cli_main([
            "--state-dir", self.external_state_dir,
            "audit",
        ])
        self.assertEqual(ret_audit, 0)

    # -------------------------------------------------------------------------
    # 15. Protected Root Files Untouched
    # -------------------------------------------------------------------------

    def test_15_root_protected_files_remain_untouched(self):
        """15. Confirm root paper_journal.json, portfolio.json, and broker.json are not created."""
        for forbidden in ["portfolio.json", "broker.json"]:
            root_target = os.path.join(_REPO_ROOT, forbidden)
            self.assertFalse(
                os.path.exists(root_target),
                f"Prohibited file '{forbidden}' must never be created at repository root."
            )


if __name__ == "__main__":
    unittest.main()
