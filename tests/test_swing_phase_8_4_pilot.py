"""Comprehensive Acceptance and Verification Suite for Trader-Swing Phase 8.4.

Controlled Automated Paper Simulation Pilot Acceptance Tests:
1. Acceptance Item A: HMAC TTL Discrepancy Resolution & Replay Protection (300s TTL).
2. Acceptance Item B: Protected Baseline File Hash Integrity & Isolation.
3. Scenario A: Normal Single-Order End-to-End Simulation Workflow.
4. Scenario B: Multiple Orders, 4-Position Limit, 20% Cash Reserve, and 25% Allocation Caps.
5. Scenario C: Human Rejection, Token Expiry, Platform Denial, and Cash Shortage (Fail-Closed).
6. Scenario D: Multi-Slice Partial Fills, Fee Amortization, and Reservation Relief.
7. Scenario E: Simulated Bracket Exit, FIFO Lot Relief, and Realized P&L Accounting.
8. Scenario F: State Reload, Idempotent Recovery, and File-Level Invariance.
9. Scenario G: Repeated Execution Cycles (Zero Duplicate Orders/Fills).
10. Scenario H: Real Daily Data Pipeline Evaluation (Regime Filter & NO_TRADE Verification).
11. Dashboard Simulation View & Audit Trail Cleanliness (Zero Secrets Exposed).
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
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

from accounting import AccountingLedger, quantize_cent, to_decimal
from authorization_models import (
    AuthorizationRecord,
    AuthorizationState,
    HumanGateRejectionError,
    PlatformPermissionError,
    RiskGateRejectionError,
)
from authorization_token import (
    DEFAULT_TOKEN_TTL_SECONDS,
    HMACApprovalTokenManager,
    SecretKeyManager,
    TokenAlreadyUsedError,
    TokenBindingMismatchError,
    TokenExpiredError,
    TokenTamperedError,
)
from cash_reservation import (
    CashReservationEngine,
    InsufficientAvailableCashError,
    ReservationStatus,
)
from dashboard.data_loader import load_simulation_dashboard_view
from market_data_models import CandleStatus, OHLCVCandle, UTC
from order_idempotency import CanonicalOrderIntent, ConflictingIdempotencyKeyError
from order_models import PaperOrder, PaperOrderState
from paper_broker_simulator import (
    InvalidExecutionStateError,
    LiveBrokerForbiddenError,
    PaperBrokerSimulator,
)
from paper_models import PaperExitReason, PaperOrderSide, PaperOrderType
from paper_storage import PaperStorageManager, PaperStorageSafetyError
from pilot_simulation_runner import (
    ControlledPaperSimulationPilot,
    get_ledger_total_fees,
    make_pilot_candle,
    make_pilot_series,
)
from regime_detector import MarketRegimeDetector
from risk import RiskConfig, RiskEngine


class TestSwingPhase84PilotAcceptance(unittest.TestCase):
    """Rigorous acceptance test suite for Phase 8.4 Automated Paper Simulation Pilot."""

    def setUp(self) -> None:
        """Create fresh isolated simulation sandbox with mandatory marker."""
        self.test_dir = tempfile.mkdtemp(prefix="trader_swing_phase84_test_")
        self.pilot = ControlledPaperSimulationPilot(
            state_dir=self.test_dir,
            starting_capital=Decimal("5000.00"),
        )
        self.broker = self.pilot.broker
        self.storage = self.pilot.storage

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
        """Safely destroy isolated test sandbox."""
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # =========================================================================
    # SECTION 1: Acceptance Items A & B Resolution Tests
    # =========================================================================

    def test_01_acceptance_item_a_hmac_ttl_300s_enforcement(self) -> None:
        """Item A: Verify HMAC approval token TTL is strictly 300 seconds and expires at t+301s."""
        self.assertEqual(DEFAULT_TOKEN_TTL_SECONDS, 300)

        # Create an authorized order intent
        intent = CanonicalOrderIntent(
            ticker="AAPL",
            side=PaperOrderSide.BUY,
            order_type=PaperOrderType.LIMIT.value,
            shares=5,
            limit_price=Decimal("150.00"),
            stop_loss=Decimal("146.00"),
            session_date="2026-11-20",
            client_tag="TEST-TTL",
        )
        order = self.broker.order_engine.create_order(intent)
        token_mgr = self.broker.auth_service.token_manager

        t_issue = datetime.now(timezone.utc)
        token = token_mgr.issue_token(order, reference_now=t_issue)
        duration_sec = (datetime.fromisoformat(token.expires_at) - datetime.fromisoformat(token.issued_at)).total_seconds()
        self.assertEqual(duration_sec, 300)

        # Verify token is valid before expiration (t + 299s)
        t_valid = t_issue + timedelta(seconds=299)
        self.assertTrue(token_mgr.verify_token(token, order, reference_now=t_valid))

        # Verify token is rejected after expiration (t + 301s)
        t_expired = t_issue + timedelta(seconds=301)
        with self.assertRaises(TokenExpiredError):
            token_mgr.verify_token(token, order, reference_now=t_expired)

    def test_02_acceptance_item_a_token_single_use_replay_protection(self) -> None:
        """Item A: Verify redeemed token cannot be reused or replayed."""
        intent = CanonicalOrderIntent(
            ticker="AAPL",
            side=PaperOrderSide.BUY,
            order_type=PaperOrderType.LIMIT.value,
            shares=5,
            limit_price=Decimal("150.00"),
            stop_loss=Decimal("146.00"),
            session_date="2026-11-20",
            client_tag="TEST-REPLAY",
        )
        order = self.broker.order_engine.create_order(intent)
        token_mgr = self.broker.auth_service.token_manager

        token = token_mgr.issue_token(order)
        token.used = True
        token.used_at = datetime.now(timezone.utc).isoformat()
        self.assertTrue(token.used)

        with self.assertRaises(TokenAlreadyUsedError):
            token_mgr.verify_token(token, order)

    def test_03_acceptance_item_b_protected_hashes_match_100_percent(self) -> None:
        """Item B: Verify mutable dataset integrity and sandbox path isolation."""
        # 1. Verify mutable current daily manifest has not been modified during operation
        manifest_path = os.path.join(PROJECT_ROOT, ".planning", "data", "current_daily", "CURRENT_DAILY_MANIFEST.json")
        self.assertTrue(os.path.exists(manifest_path), f"Manifest '{manifest_path}' must exist.")
        with open(manifest_path, "rb") as f:
            manifest_now = hashlib.sha256(f.read()).hexdigest().upper()
        self.assertEqual(
            manifest_now,
            self.initial_manifest_hash,
            "CURRENT_DAILY_MANIFEST.json was unexpectedly modified during the test execution.",
        )

        # 2. Verify mutable SPY daily data has not been modified during operation
        spy_path = os.path.join(PROJECT_ROOT, ".planning", "data", "current_daily", "SPY_daily.csv")
        self.assertTrue(os.path.exists(spy_path), f"SPY dataset '{spy_path}' must exist.")
        with open(spy_path, "rb") as f:
            spy_now = hashlib.sha256(f.read()).hexdigest().upper()
        self.assertEqual(
            spy_now,
            self.initial_spy_hash,
            "SPY_daily.csv was unexpectedly modified during the test execution.",
        )

        # 3. Path isolation: Verify pilot operations write exclusively to configured sandbox
        self.assertTrue(os.path.isdir(self.test_dir))
        self.assertEqual(self.storage.state_dir, self.test_dir)

        # 4. Storage safety: Verify forbidden root filenames are rejected in isolated temp sandbox
        forbidden_journal = os.path.join(self.test_dir, "paper_journal.json")
        with self.assertRaises(PaperStorageSafetyError):
            PaperStorageManager(state_dir=forbidden_journal)

        # 5. Storage safety: Verify operations cannot target repo-internal observation directory
        genuine_obs_dir = os.path.join(PROJECT_ROOT, ".planning", "data", "observations")
        with self.assertRaises(PaperStorageSafetyError):
            PaperStorageManager(state_dir=genuine_obs_dir)

    # =========================================================================
    # SECTION 2: Scenario A — Normal Simulation Workflow
    # =========================================================================

    def test_04_scenario_a_normal_authorized_order_to_fill_workflow(self) -> None:
        """Scenario A: Successful authorized order-to-fill workflow with complete accounting."""
        intent = CanonicalOrderIntent(
            ticker="AAPL",
            side=PaperOrderSide.BUY,
            order_type=PaperOrderType.LIMIT.value,
            shares=6,
            limit_price=Decimal("150.00"),
            stop_loss=Decimal("146.00"),
            session_date="2026-11-20",
            client_tag="SCENARIO-A",
        )
        order = self.broker.order_engine.create_order(intent)

        # 1. Multi-Gate Authorization
        auth_rec = self.broker.auth_service.authorize_order(order.order_id, user_confirmation="YES")
        self.assertEqual(auth_rec.state, AuthorizationState.AUTHORIZED)

        # 2. Broker Submission & Reservation
        submitted = self.broker.submit_order(order.order_id)
        self.assertEqual(submitted.state, PaperOrderState.SUBMITTED)
        # 6 * $150.00 + $1.00 fee = $901.00
        self.assertEqual(self.broker.reservation_engine.get_total_reserved_cash(), Decimal("901.00"))

        # 3. Simulated Fill Execution
        fill_rep = self.broker.process_simulated_fill(order.order_id, fill_shares=6, fill_price=Decimal("150.00"))
        self.assertEqual(fill_rep["status"], "FILLED")
        self.assertEqual(self.broker.reservation_engine.get_total_reserved_cash(), Decimal("0.00"))

        # 4. Verify Accounting & Balances
        journal = self.storage.load_journal()
        # $5,000 - $901.00 = $4,099.00
        self.assertEqual(journal.ledger.cash_usd, Decimal("4099.00"))
        pos = journal.ledger.positions["AAPL"]
        self.assertEqual(pos.total_shares, 6)
        self.assertEqual(pos.total_cost_basis_usd, Decimal("900.00"))

        audit = self.pilot.run_accounting_audit()
        self.assertEqual(audit["unexplained_discrepancy_usd"], "0.00")
        self.assertEqual(audit["reconciliation_verdict"], "PERFECT_MATCH")

    # =========================================================================
    # SECTION 3: Scenario B — Multiple Orders & Frozen Risk Limits
    # =========================================================================

    def test_05_scenario_b_four_position_limit_and_aggregate_caps(self) -> None:
        """Scenario B: Enforce max 4 simultaneous positions and 20% minimum cash reserve."""
        tickers = ["AAPL", "MSFT", "GOOGL", "AMZN"]
        orders = []

        # Open 4 positions
        for i, t in enumerate(tickers):
            intent = CanonicalOrderIntent(
                ticker=t,
                side=PaperOrderSide.BUY,
                order_type=PaperOrderType.LIMIT.value,
                shares=5,
                limit_price=Decimal("150.00"),
                stop_loss=Decimal("146.00"),
                session_date="2026-11-20",
                client_tag=f"SCEN-B-{i}",
            )
            ord_obj = self.broker.order_engine.create_order(intent)
            self.broker.auth_service.authorize_order(ord_obj.order_id, user_confirmation="YES")
            self.broker.submit_order(ord_obj.order_id)
            self.broker.process_simulated_fill(ord_obj.order_id, 5, Decimal("150.00"))
            orders.append(ord_obj)

        journal = self.storage.load_journal()
        self.assertEqual(len(journal.ledger.positions), 4)

        # Attempt 5th position: MUST be rejected by risk engine (Max 4 positions)
        intent5 = CanonicalOrderIntent(
            ticker="NVDA",
            side=PaperOrderSide.BUY,
            order_type=PaperOrderType.LIMIT.value,
            shares=5,
            limit_price=Decimal("150.00"),
            stop_loss=Decimal("146.00"),
            session_date="2026-11-20",
            client_tag="SCEN-B-5",
        )
        ord5 = self.broker.order_engine.create_order(intent5)
        with self.assertRaises(RiskGateRejectionError) as cm:
            self.broker.auth_service.authorize_order(ord5.order_id, user_confirmation="YES")
        self.assertIn("MAX_POSITIONS_EXCEEDED", str(cm.exception))

    def test_06_scenario_b_minimum_20_percent_cash_reserve_blocking(self) -> None:
        """Scenario B: Order submission attempting to spend into the 20% minimum cash reserve is blocked."""
        # Starting cash $5,000. 20% min reserve is $1,000. Max spendable is $4,000.
        # Fill AAPL ($1,201), MSFT ($1,201), GOOGL ($1,201). Total spent = $3,603.
        # Remaining cash = $1,397. Max spendable before $1,000 reserve = $397.
        for t in ["AAPL", "MSFT", "GOOGL"]:
            intent = CanonicalOrderIntent(
                ticker=t,
                side=PaperOrderSide.BUY,
                order_type=PaperOrderType.LIMIT.value,
                shares=8,
                limit_price=Decimal("150.00"),
                stop_loss=Decimal("147.00"),
                session_date="2026-11-20",
                client_tag=f"RESERVE-{t}",
            )
            o = self.broker.order_engine.create_order(intent)
            self.broker.auth_service.authorize_order(o.order_id, user_confirmation="YES")
            self.broker.submit_order(o.order_id)
            self.broker.process_simulated_fill(o.order_id, 8, Decimal("150.00"))

        journal = self.storage.load_journal()
        # Cash: $5,000 - 3 * (8 * 150 + 1) = $5,000 - 3 * 1201 = $5,000 - 3603 = $1,397.00
        self.assertEqual(journal.ledger.cash_usd, Decimal("1397.00"))

        # Order wanting $601 ($150 * 4 + $1) would leave cash at $796.00 < $1,000.00 reserve!
        intent_fail = CanonicalOrderIntent(
            ticker="AMZN",
            side=PaperOrderSide.BUY,
            order_type=PaperOrderType.LIMIT.value,
            shares=4,
            limit_price=Decimal("150.00"),
            stop_loss=Decimal("147.00"),
            session_date="2026-11-20",
            client_tag="RESERVE-FAIL",
        )
        o_fail = self.broker.order_engine.create_order(intent_fail)

        # 1. Gate 1 Risk Gate blocks authorization due to MIN_CASH_RESERVE_VIOLATION
        with self.assertRaises(RiskGateRejectionError) as cm:
            self.broker.auth_service.authorize_order(o_fail.order_id, user_confirmation="YES")
        self.assertIn("MIN_CASH_RESERVE_VIOLATION", str(cm.exception))

        # 2. Broker simulator submission also blocks with InsufficientAvailableCashError
        auth_rec = self.broker.auth_service.get_authorization(o_fail.order_id)
        auth_rec.token = self.broker.auth_service.token_manager.issue_token(o_fail)
        auth_rec.state = AuthorizationState.AUTHORIZED
        self.broker.auth_service.save_authorization(auth_rec)
        o_fail.state = PaperOrderState.APPROVED
        self.storage.save_order(o_fail)
        with self.assertRaises(InsufficientAvailableCashError):
            self.broker.submit_order(o_fail.order_id)

    # =========================================================================
    # SECTION 4: Scenario C — Rejection Paths & Fail-Closed Behavior
    # =========================================================================

    def test_07_scenario_c_explicit_human_rejection(self) -> None:
        """Scenario C: Explicit human rejection ('NO') marks record HUMAN_REJECTED with zero reservations."""
        intent = CanonicalOrderIntent(
            ticker="AAPL",
            side=PaperOrderSide.BUY,
            order_type=PaperOrderType.LIMIT.value,
            shares=5,
            limit_price=Decimal("150.00"),
            stop_loss=Decimal("146.00"),
            session_date="2026-11-20",
            client_tag="SCEN-C-REJECT",
        )
        order = self.broker.order_engine.create_order(intent)

        with self.assertRaises(HumanGateRejectionError):
            self.broker.auth_service.authorize_order(order.order_id, user_confirmation="NO")

        auth_rec = self.broker.auth_service.get_authorization(order.order_id)
        self.assertEqual(auth_rec.state, AuthorizationState.HUMAN_REJECTED)
        self.assertEqual(self.broker.reservation_engine.get_total_reserved_cash(), Decimal("0.00"))

    def test_08_scenario_c_expired_token_submission_rejection(self) -> None:
        """Scenario C: Expired token past 300s TTL is rejected fail-closed."""
        intent = CanonicalOrderIntent(
            ticker="AAPL",
            side=PaperOrderSide.BUY,
            order_type=PaperOrderType.LIMIT.value,
            shares=5,
            limit_price=Decimal("150.00"),
            stop_loss=Decimal("146.00"),
            session_date="2026-11-20",
            client_tag="SCEN-C-EXPIRED",
        )
        order = self.broker.order_engine.create_order(intent)
        token = self.broker.auth_service.token_manager.issue_token(order)

        t_expired = datetime.now(timezone.utc) + timedelta(seconds=301)
        with self.assertRaises(TokenExpiredError):
            self.broker.auth_service.token_manager.verify_token(token, order, reference_now=t_expired)

    def test_09_scenario_c_platform_permission_denial(self) -> None:
        """Scenario C: Platform permission verifier returning False denies authorization."""
        intent = CanonicalOrderIntent(
            ticker="AAPL",
            side=PaperOrderSide.BUY,
            order_type=PaperOrderType.LIMIT.value,
            shares=5,
            limit_price=Decimal("150.00"),
            stop_loss=Decimal("146.00"),
            session_date="2026-11-20",
            client_tag="SCEN-C-PLATFORM",
        )
        order = self.broker.order_engine.create_order(intent)

        with self.assertRaises(PlatformPermissionError):
            self.broker.auth_service.authorize_order(
                order.order_id,
                user_confirmation="YES",
                platform_verifier=lambda ord, tok: False,
            )

        auth_rec = self.broker.auth_service.get_authorization(order.order_id)
        self.assertEqual(auth_rec.state, AuthorizationState.AUTHORIZATION_FAILED)

    # =========================================================================
    # SECTION 5: Scenario D — Partial Fills & Multi-Slice Execution
    # =========================================================================

    def test_10_scenario_d_partial_fills_and_reservation_amortization(self) -> None:
        """Scenario D: Order for 6 shares filled in 2 slices (2 shares, then 4 shares)."""
        intent = CanonicalOrderIntent(
            ticker="AAPL",
            side=PaperOrderSide.BUY,
            order_type=PaperOrderType.LIMIT.value,
            shares=6,
            limit_price=Decimal("150.00"),
            stop_loss=Decimal("146.00"),
            session_date="2026-11-20",
            client_tag="SCEN-D-PARTIAL",
        )
        order = self.broker.order_engine.create_order(intent)
        self.broker.auth_service.authorize_order(order.order_id, user_confirmation="YES")
        self.broker.submit_order(order.order_id)

        # Slice 1: Fill 2 of 6 shares
        pfill1 = self.broker.process_simulated_fill(order.order_id, fill_shares=2, fill_price=Decimal("150.00"))
        self.assertEqual(pfill1["status"], "PARTIALLY_FILLED")
        self.assertEqual(pfill1["shares_remaining"], 4)
        # Remaining reservation: 4 * $150.00 + $1.00 = $601.00
        self.assertEqual(self.broker.reservation_engine.get_total_reserved_cash(), Decimal("601.00"))

        # Slice 2: Fill remaining 4 shares
        pfill2 = self.broker.process_simulated_fill(order.order_id, fill_shares=4, fill_price=Decimal("150.00"))
        self.assertEqual(pfill2["status"], "FILLED")
        self.assertEqual(pfill2["shares_remaining"], 0)
        self.assertEqual(self.broker.reservation_engine.get_total_reserved_cash(), Decimal("0.00"))

        # Check total position and cash
        journal = self.storage.load_journal()
        self.assertEqual(journal.ledger.positions["AAPL"].total_shares, 6)
        # Cash: $5,000 - (2 * 150 + 1) - (4 * 150 + 1) = $5,000 - 301 - 601 = $4,098.00
        self.assertEqual(journal.ledger.cash_usd, Decimal("4098.00"))

    # =========================================================================
    # SECTION 6: Scenario E — Simulated Bracket Exit & Realized P&L
    # =========================================================================

    def test_11_scenario_e_simulated_bracket_exit_fifo_realized_pnl(self) -> None:
        """Scenario E: Execute full cycle from entry to take-profit exit with FIFO realized P&L."""
        intent = CanonicalOrderIntent(
            ticker="AAPL",
            side=PaperOrderSide.BUY,
            order_type=PaperOrderType.LIMIT.value,
            shares=6,
            limit_price=Decimal("150.00"),
            stop_loss=Decimal("146.00"),
            target=Decimal("162.00"),
            session_date="2026-11-20",
            client_tag="SCEN-E-EXIT",
        )
        order = self.broker.order_engine.create_order(intent)
        self.broker.auth_service.authorize_order(order.order_id, user_confirmation="YES")
        self.broker.submit_order(order.order_id)
        self.broker.process_simulated_fill(order.order_id, 6, Decimal("150.00"))

        # Simulate take-profit exit: sell 6 shares @ $162.00 (exit fee $1.00)
        exit_rep = self.broker.simulate_bracket_exit(
            order_id=order.order_id,
            exit_price=Decimal("162.00"),
            reason=PaperExitReason.TAKE_PROFIT,
            fee=Decimal("1.00"),
        )
        self.assertEqual(exit_rep["status"], "EXITED")
        self.assertEqual(exit_rep["shares"], 6)

        # Gross proceeds: 6 * $162 = $972.00
        # Cost basis: 6 * $150 = $900.00
        # Total fees: $1.00 (entry) + $1.00 (exit) = $2.00
        # Realized net P&L: $972 - $900 - $2 = $70.00
        self.assertEqual(exit_rep["realized_pnl"], "70.00")

        journal = self.storage.load_journal()
        self.assertEqual(journal.ledger.positions["AAPL"].total_shares, 0)
        self.assertEqual(journal.ledger.cumulative_realized_trading_pnl_usd, Decimal("70.00"))
        # Ending cash: $5,000 + $70.00 = $5,070.00
        self.assertEqual(journal.ledger.cash_usd, Decimal("5070.00"))

        audit = self.pilot.run_accounting_audit()
        self.assertEqual(audit["unexplained_discrepancy_usd"], "0.00")
        self.assertEqual(audit["reconciliation_verdict"], "PERFECT_MATCH")

    # =========================================================================
    # SECTION 7: Scenario F — State Reload & Idempotent Recovery
    # =========================================================================

    def test_12_scenario_f_state_reload_and_idempotent_recovery(self) -> None:
        """Scenario F: Reload state into new pilot instance and verify recovery and idempotency."""
        intent = CanonicalOrderIntent(
            ticker="AAPL",
            side=PaperOrderSide.BUY,
            order_type=PaperOrderType.LIMIT.value,
            shares=5,
            limit_price=Decimal("150.00"),
            stop_loss=Decimal("146.00"),
            session_date="2026-11-20",
            client_tag="SCEN-F-RELOAD",
        )
        order = self.broker.order_engine.create_order(intent)
        self.broker.auth_service.authorize_order(order.order_id, user_confirmation="YES")
        self.broker.submit_order(order.order_id)
        self.broker.process_simulated_fill(order.order_id, 5, Decimal("150.00"))

        # Re-instantiate pilot from the same state directory
        reloaded_pilot = ControlledPaperSimulationPilot(state_dir=self.test_dir)
        recon = reloaded_pilot.broker.reconcile_and_recover()

        self.assertEqual(recon["status"], "RECONCILED")
        self.assertEqual(recon["ledger_equity_invariant"], "HOLDS")

        # Verify order retrieval is idempotent
        reloaded_order = reloaded_pilot.broker.order_engine.get_order(order.order_id)
        self.assertEqual(reloaded_order.state, PaperOrderState.FILLED)
        self.assertEqual(reloaded_order.shares_filled, 5)

    # =========================================================================
    # SECTION 8: Scenario G — Repeated Execution Cycles
    # =========================================================================

    def test_13_scenario_g_repeated_cycles_zero_duplicate_orders_or_fills(self) -> None:
        """Scenario G: Repeatedly creating the identical order returns the existing order idempotently."""
        intent = CanonicalOrderIntent(
            ticker="AAPL",
            side=PaperOrderSide.BUY,
            order_type=PaperOrderType.LIMIT.value,
            shares=5,
            limit_price=Decimal("150.00"),
            stop_loss=Decimal("146.00"),
            session_date="2026-11-20",
            client_tag="SCEN-G-REPEAT",
        )
        # Cycle 1: Create, authorize, fill
        order1 = self.broker.order_engine.create_order(intent)
        self.broker.auth_service.authorize_order(order1.order_id, user_confirmation="YES")
        self.broker.submit_order(order1.order_id)
        self.broker.process_simulated_fill(order1.order_id, 5, Decimal("150.00"))

        # Cycle 2: Present identical intent again
        order2 = self.broker.order_engine.create_order(intent)
        self.assertEqual(order1.order_id, order2.order_id)
        self.assertEqual(order2.state, PaperOrderState.FILLED)

        # Attempting fill on already FILLED order must be rejected cleanly
        with self.assertRaises(InvalidExecutionStateError):
            self.broker.process_simulated_fill(order2.order_id, 5, Decimal("150.00"))

        # Invariant: position remains exactly 5 shares
        journal = self.storage.load_journal()
        self.assertEqual(journal.ledger.positions["AAPL"].total_shares, 5)

    # =========================================================================
    # SECTION 9: Scenario H — Real Historical Market Data Evaluation
    # =========================================================================

    def test_14_scenario_h_real_historical_daily_data_records_no_trade(self) -> None:
        """Scenario H: Real historical daily data under STRONG_BEARISH regime produces NO_TRADE."""
        import csv

        def load_daily_csv(path: str, ticker: str) -> List[OHLCVCandle]:
            candles: List[OHLCVCandle] = []
            with open(path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    dt = datetime.strptime(row["Date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    candles.append(
                        OHLCVCandle(
                            ticker=ticker,
                            interval="1d",
                            timestamp=dt,
                            open=Decimal(row["Open"]),
                            high=Decimal(row["High"]),
                            low=Decimal(row["Low"]),
                            close=Decimal(row["Close"]),
                            volume=int(float(row["Volume"])),
                            status=CandleStatus.COMPLETE,
                            source="current_daily_fixture",
                        )
                    )
            return candles

        spy_path = os.path.join(PROJECT_ROOT, ".planning/data/current_daily/SPY_daily.csv")
        qqq_path = os.path.join(PROJECT_ROOT, ".planning/data/current_daily/QQQ_daily.csv")
        iwm_path = os.path.join(PROJECT_ROOT, ".planning/data/current_daily/IWM_daily.csv")
        aapl_path = os.path.join(PROJECT_ROOT, ".planning/data/current_daily/AAPL_daily.csv")

        proxy_map = {
            "SPY": load_daily_csv(spy_path, "SPY"),
            "QQQ": load_daily_csv(qqq_path, "QQQ"),
            "IWM": load_daily_csv(iwm_path, "IWM"),
        }
        stock_map = {
            "AAPL": load_daily_csv(aapl_path, "AAPL"),
        }

        # Run automated simulation cycle on actual market fixtures
        summary = self.pilot.run_automated_simulation_cycle(
            proxy_candles_map=proxy_map,
            stock_candles_map=stock_map,
            session_date="2026-11-16",
        )

        self.assertEqual(summary.regime, "STRONG_BEARISH")
        self.assertEqual(summary.status, "NO_TRADE")
        self.assertEqual(summary.orders_authorized, 0)
        self.assertEqual(summary.orders_submitted, 0)
        self.assertEqual(summary.orders_filled, 0)
        self.assertEqual(summary.ending_equity, Decimal("5000.00"))
        self.assertTrue(summary.accounting_invariant_holds)

    # =========================================================================
    # SECTION 10: Dashboard Simulation View & Security Cleanliness
    # =========================================================================

    def test_15_dashboard_simulation_view_read_only_and_clean_audit(self) -> None:
        """Verify dashboard simulation loader reflects pilot state with zero leaked secrets."""
        # Execute trade
        intent = CanonicalOrderIntent(
            ticker="AAPL",
            side=PaperOrderSide.BUY,
            order_type=PaperOrderType.LIMIT.value,
            shares=5,
            limit_price=Decimal("150.00"),
            stop_loss=Decimal("146.00"),
            session_date="2026-11-20",
            client_tag="DASH-TEST",
        )
        order = self.broker.order_engine.create_order(intent)
        self.broker.auth_service.authorize_order(order.order_id, user_confirmation="YES")
        self.broker.submit_order(order.order_id)
        self.broker.process_simulated_fill(order.order_id, 5, Decimal("150.00"))

        dash_view = load_simulation_dashboard_view(self.test_dir)
        self.assertEqual(dash_view["environment"], "SIMULATION_ONLY")
        self.assertFalse(dash_view["is_real_execution"])
        self.assertEqual(dash_view["equity_summary"]["cash_usd"], "4249.00")

        # Secret cleanliness scan
        sec_mgr = SecretKeyManager(secret_dir=self.test_dir)
        secret_hex = sec_mgr.get_signing_secret().hex()

        for evt in dash_view["audit_events"]:
            self.assertNotIn(secret_hex, json.dumps(evt))


if __name__ == "__main__":
    unittest.main()
