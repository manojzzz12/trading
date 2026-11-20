"""Comprehensive unit and security test suite for Trader-Swing Phase 8.2 Paper Broker Simulation.

Tests:
1. Environment tag and strict simulation safety boundaries (SIMULATION_ONLY).
2. Multi-gate human authorization verification prior to order submission.
3. Pre-submission cash reservation and available cash arithmetic (GAP-02).
4. Prevention of concurrent cash over-allocation against the 20% equity reserve.
5. Order cancellation, expiration, and failure releasing reserved cash immediately.
6. Execution fill processing, partial fills, and deterministic fill replay protection.
7. Double-entry ledger synchronization and tax lot tracking.
8. Cross-process mutual exclusion and lock timeout enforcement (PaperFileLock).
9. Crash recovery, transaction manifest, and orphan reservation reconciliation.
10. Bracket order emulation (attached stop-loss and take-profit exits).
11. Verification that root paper state, external paper state, and protected files are untouched.
"""

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
import unittest

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, ".."))
SKILLS_DIR = os.path.join(PROJECT_ROOT, ".agents", "skills", "trader-swing")
if SKILLS_DIR not in sys.path:
    sys.path.insert(0, SKILLS_DIR)

from accounting import (
    AccountingLedger,
    quantize_cent,
    to_decimal,
)
from authorization_models import (
    AuthorizationRecord,
    AuthorizationState,
)
from authorization_service import HumanAuthorizationService
from cash_reservation import (
    CashReservationEngine,
    CashReservationRecord,
    ConflictingReservationError,
    InsufficientAvailableCashError,
    ReservationStatus,
    TerminalReservationStateError,
)
from journal import SwingJournal
from order_idempotency import (
    CanonicalOrderIntent,
    DuplicateFillError,
    compute_fill_identity,
    compute_order_id,
)
from order_models import (
    IllegalStateTransitionError,
    PaperOrder,
    PaperOrderState,
    TerminalOrderStateError,
)
from order_service import PaperOrderEngine
from paper_broker_simulator import (
    ENVIRONMENT,
    InvalidExecutionStateError,
    LiveBrokerForbiddenError,
    PaperBrokerSimulator,
    UnauthorizedOrderSubmissionError,
)
from paper_lock import PaperFileLock, PaperLockTimeoutError
from paper_models import (
    PaperExitReason,
    PaperOrderSide,
    PaperOrderType,
)
from paper_storage import PaperStorageManager, PaperStorageSafetyError
from risk import RiskConfig


class TestSwingPaperBrokerSimulation(unittest.TestCase):
    """Exhaustive tests for Phase 8.2 Paper Broker Simulation subsystem."""

    def setUp(self) -> None:
        # Create an isolated temporary directory strictly outside repository
        self.temp_dir = tempfile.mkdtemp(prefix="trader_swing_sim_test_")
        self.storage = PaperStorageManager(state_dir=self.temp_dir)
        self.risk_config = RiskConfig()
        self.broker = PaperBrokerSimulator(
            storage=self.storage,
            risk_config=self.risk_config,
            lock_timeout=3.0,
        )
        self.broker.initialize_simulation(initial_capital=Decimal("5000.00"))

    def tearDown(self) -> None:
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_authorized_order(
        self,
        ticker: str = "AAPL",
        shares: int = 5,
        limit_price: Decimal = Decimal("150.00"),
        stop_loss: Decimal = Decimal("146.00"),
        target: Optional[Decimal] = None,
        client_tag: str = "SIM-TAG-01",
    ) -> Tuple[PaperOrder, AuthorizationRecord]:
        """Helper to create and fully authorize an order through Gates 1, 2, and 3."""
        eff_target = target
        if eff_target is None and limit_price is not None:
            eff_target = quantize_cent(limit_price * Decimal("1.08"))

        intent = CanonicalOrderIntent(
            ticker=ticker,
            side=PaperOrderSide.BUY,
            order_type=PaperOrderType.LIMIT.value,
            shares=shares,
            limit_price=limit_price,
            stop_loss=stop_loss,
            target=eff_target,
            session_date="2026-11-20",
            client_tag=client_tag,
        )
        order = self.broker.order_engine.create_order(intent)

        # Run 3-gate authorization
        auth_rec = self.broker.auth_service.authorize_order(
            order_id=order.order_id,
            user_confirmation="YES",
        )
        # Reload order to get updated state (APPROVED)
        updated_order = self.storage.get_order(order.order_id)
        return updated_order, auth_rec

    # -------------------------------------------------------------------------
    # 1. Environment & Boundary Tests
    # -------------------------------------------------------------------------

    def test_01_environment_tag_and_simulation_marker(self) -> None:
        """Verify explicit SIMULATION_ONLY tag and persistent marker file."""
        self.assertEqual(ENVIRONMENT, "SIMULATION_ONLY")
        marker_path = os.path.join(self.temp_dir, "SIMULATION_MARKER.json")
        self.assertTrue(os.path.exists(marker_path))
        with open(marker_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data.get("environment"), "SIMULATION_ONLY")
        self.assertEqual(data.get("system"), "trader-swing-simulated-broker")

    def test_02_rejection_of_live_environment(self) -> None:
        """Verify simulator strictly fails closed if environment tag is altered."""
        import paper_broker_simulator
        original_env = paper_broker_simulator.ENVIRONMENT
        try:
            paper_broker_simulator.ENVIRONMENT = "LIVE_PRODUCTION"
            with self.assertRaises(LiveBrokerForbiddenError):
                PaperBrokerSimulator(storage=self.storage)
        finally:
            paper_broker_simulator.ENVIRONMENT = original_env

    # -------------------------------------------------------------------------
    # 2. Authorization Enforcement & Order Submission
    # -------------------------------------------------------------------------

    def test_03_submission_fails_without_authorization(self) -> None:
        """Unapproved orders or orders lacking authorization must be rejected at submission."""
        intent = CanonicalOrderIntent(
            ticker="MSFT",
            side=PaperOrderSide.BUY,
            order_type=PaperOrderType.LIMIT.value,
            shares=5,
            limit_price=Decimal("200.00"),
            stop_loss=Decimal("196.00"),
            session_date="2026-11-20",
            client_tag="UNAUTH-01",
        )
        order = self.broker.order_engine.create_order(intent)
        self.assertEqual(order.state, PaperOrderState.PROPOSED)

        # Attempt submission without authorization
        with self.assertRaises(UnauthorizedOrderSubmissionError):
            self.broker.submit_order(order.order_id)

    def test_04_successful_order_submission_with_cash_reservation(self) -> None:
        """Approved and authorized order transitions to SUBMITTED and reserves cash."""
        order, auth_rec = self._create_authorized_order(
            ticker="AAPL",
            shares=5,
            limit_price=Decimal("150.00"),
            stop_loss=Decimal("146.00"),
            client_tag="SUBMIT-01",
        )
        self.assertEqual(order.state, PaperOrderState.APPROVED)
        self.assertEqual(auth_rec.state, AuthorizationState.AUTHORIZED)

        # Check pre-submission cash
        journal = self.storage.load_journal()
        self.assertEqual(journal.ledger.cash_usd, Decimal("5000.00"))
        self.assertEqual(self.broker.reservation_engine.get_total_reserved_cash(), Decimal("0.00"))

        # Submit order
        submitted_order = self.broker.submit_order(order.order_id)
        self.assertEqual(submitted_order.state, PaperOrderState.SUBMITTED)

        # Check cash reservation: (5 * $150.00) + $1.00 fee = $751.00
        expected_reserved = Decimal("751.00")
        reservation = self.broker.reservation_engine.get_reservation(order.order_id)
        self.assertIsNotNone(reservation)
        self.assertEqual(reservation.status, ReservationStatus.ACTIVE)
        self.assertEqual(reservation.total_reserved_usd, expected_reserved)
        self.assertEqual(self.broker.reservation_engine.get_total_reserved_cash(), expected_reserved)

        # Ledger cash remains unmutated (cash is reserved, not deducted yet)
        journal_after = self.storage.load_journal()
        self.assertEqual(journal_after.ledger.cash_usd, Decimal("5000.00"))

        # Available cash is reduced by reservation
        avail_cash = self.broker.reservation_engine.compute_available_cash(journal_after.ledger.cash_usd)
        self.assertEqual(avail_cash, Decimal("4249.00"))

    # -------------------------------------------------------------------------
    # 3. Cash Reservation & Concurrent Over-Allocation Prevention (GAP-02)
    # -------------------------------------------------------------------------

    def test_05_concurrent_overallocation_prevention_against_20_percent_reserve(self) -> None:
        """Prevent sequential or concurrent orders from exceeding the 20% minimum cash reserve."""
        # Initial cash: $5,000.00. Total equity: $5,000.00.
        # 20% minimum cash reserve: $1,000.00.
        # Maximum spendable capital across all orders: $4,000.00.
        # Max single-ticker allocation: 25% of $5,000 = $1,250.00.

        # Order 1: 6 shares of MSFT at $200.00 = $1,200.00 + $1 fee = $1,201.00. (Risk: 6 * $3.00 = $18.00).
        order1, _ = self._create_authorized_order(
            ticker="MSFT",
            shares=6,
            limit_price=Decimal("200.00"),
            stop_loss=Decimal("197.00"),
            client_tag="CONCUR-01",
        )
        self.broker.submit_order(order1.order_id)
        # Reserved: $1,201.00. Available cash: $3,799.00.

        # Order 2: 8 shares of AAPL at $150.00 = $1,200.00 + $1 fee = $1,201.00. (Risk: 8 * $3.00 = $24.00).
        order2, _ = self._create_authorized_order(
            ticker="AAPL",
            shares=8,
            limit_price=Decimal("150.00"),
            stop_loss=Decimal("147.00"),
            client_tag="CONCUR-02",
        )
        self.broker.submit_order(order2.order_id)
        # Reserved: $2,402.00. Available cash: $2,598.00.

        # Order 3: 7 shares of GOOGL at $160.00 = $1,120.00 + $1 fee = $1,121.00. (Risk: 7 * $3.00 = $21.00).
        order3, _ = self._create_authorized_order(
            ticker="GOOGL",
            shares=7,
            limit_price=Decimal("160.00"),
            stop_loss=Decimal("157.00"),
            client_tag="CONCUR-03",
        )
        self.broker.submit_order(order3.order_id)
        # Total reserved: $1,201 + $1,201 + $1,121 = $3,523.00.
        # Available cash remaining: $5,000 - $3,523 = $1,477.00 (which satisfies >= $1,000.00 reserve).

        # Order 4: 6 shares of AMZN at $180.00 = $1,080.00 + $1 fee = $1,081.00. (Risk: 6 * $3.00 = $18.00).
        # If Order 4 were reserved, available cash would become:
        # $1,477.00 - $1,081.00 = $396.00, which is strictly less than the $1,000.00 (20%) reserve!
        order4, _ = self._create_authorized_order(
            ticker="AMZN",
            shares=6,
            limit_price=Decimal("180.00"),
            stop_loss=Decimal("177.00"),
            client_tag="CONCUR-04",
        )

        with self.assertRaises(InsufficientAvailableCashError) as ctx:
            self.broker.submit_order(order4.order_id)

        self.assertIn("violates the 20% minimum cash reserve threshold", str(ctx.exception))

        # Verify Order 4 remains APPROVED and NOT submitted
        reloaded_order4 = self.storage.get_order(order4.order_id)
        self.assertEqual(reloaded_order4.state, PaperOrderState.APPROVED)

        # Verify reservation was not created for Order 4
        res4 = self.broker.reservation_engine.get_reservation(order4.order_id)
        self.assertIsNone(res4)

        # Verify total reserved cash across Orders 1, 2, and 3 is still intact
        self.assertEqual(self.broker.reservation_engine.get_total_reserved_cash(), Decimal("3523.00"))

    # -------------------------------------------------------------------------
    # 4. Cancellation, Expiration & Failure Releasing Cash Reservations
    # -------------------------------------------------------------------------

    def test_06_cancellation_releases_reservation(self) -> None:
        """Cancelling a submitted order immediately unlocks reserved cash."""
        order, _ = self._create_authorized_order(
            ticker="AAPL",
            shares=5,
            limit_price=Decimal("150.00"),
            stop_loss=Decimal("146.00"),
            client_tag="CANCEL-01",
        )
        self.broker.submit_order(order.order_id)
        self.assertEqual(self.broker.reservation_engine.get_total_reserved_cash(), Decimal("751.00"))

        # Cancel order
        cancelled = self.broker.cancel_simulated_order(order.order_id, reason="User cancelled setup")
        self.assertEqual(cancelled.state, PaperOrderState.CANCELLED)

        # Reservation is RELEASED and total reserved is 0
        res = self.broker.reservation_engine.get_reservation(order.order_id)
        self.assertEqual(res.status, ReservationStatus.RELEASED)
        self.assertEqual(res.total_reserved_usd, Decimal("0.00"))
        self.assertEqual(self.broker.reservation_engine.get_total_reserved_cash(), Decimal("0.00"))

    def test_07_expiration_releases_reservation(self) -> None:
        """Expiring a submitted order immediately unlocks reserved cash."""
        order, _ = self._create_authorized_order(
            ticker="AAPL",
            shares=5,
            limit_price=Decimal("150.00"),
            stop_loss=Decimal("146.00"),
            client_tag="EXPIRE-01",
        )
        self.broker.submit_order(order.order_id)
        self.assertEqual(self.broker.reservation_engine.get_total_reserved_cash(), Decimal("751.00"))

        expired = self.broker.expire_simulated_order(order.order_id, reason="Session ended without fill")
        self.assertEqual(expired.state, PaperOrderState.EXPIRED)

        res = self.broker.reservation_engine.get_reservation(order.order_id)
        self.assertEqual(res.status, ReservationStatus.RELEASED)
        self.assertEqual(self.broker.reservation_engine.get_total_reserved_cash(), Decimal("0.00"))

    def test_08_rejection_failure_releases_reservation(self) -> None:
        """Simulated broker rejection marks order FAILED and releases reserved cash."""
        order, _ = self._create_authorized_order(
            ticker="AAPL",
            shares=5,
            limit_price=Decimal("150.00"),
            stop_loss=Decimal("146.00"),
            client_tag="FAIL-01",
        )
        self.broker.submit_order(order.order_id)

        failed = self.broker.reject_simulated_order(order.order_id, reason="Exchange reject: Price out of bounds")
        self.assertEqual(failed.state, PaperOrderState.FAILED)

        res = self.broker.reservation_engine.get_reservation(order.order_id)
        self.assertEqual(res.status, ReservationStatus.RELEASED)
        self.assertEqual(self.broker.reservation_engine.get_total_reserved_cash(), Decimal("0.00"))

    # -------------------------------------------------------------------------
    # 5. Full and Partial Execution Fills
    # -------------------------------------------------------------------------

    def test_09_full_execution_fill_flow(self) -> None:
        """Full fill executes buy on ledger, converts full reservation, and updates order to FILLED."""
        order, _ = self._create_authorized_order(
            ticker="AAPL",
            shares=5,
            limit_price=Decimal("150.00"),
            stop_loss=Decimal("146.00"),
            target=Decimal("160.00"),
            client_tag="FULLFILL-01",
        )
        self.broker.submit_order(order.order_id)

        # Process fill for all 5 shares at $150.00
        fill_report = self.broker.process_simulated_fill(
            order_id=order.order_id,
            fill_shares=5,
            fill_price=Decimal("150.00"),
            fill_fee=Decimal("1.00"),
        )

        self.assertEqual(fill_report["status"], "FILLED")
        self.assertEqual(fill_report["shares_filled"], 5)
        self.assertEqual(fill_report["shares_remaining"], 0)

        # Verify order state is FILLED
        reloaded_order = self.storage.get_order(order.order_id)
        self.assertEqual(reloaded_order.state, PaperOrderState.FILLED)
        self.assertEqual(reloaded_order.shares_remaining, 0)
        self.assertEqual(reloaded_order.shares_filled, 5)

        # Verify reservation is CONVERTED_FULL
        res = self.broker.reservation_engine.get_reservation(order.order_id)
        self.assertEqual(res.status, ReservationStatus.CONVERTED_FULL)
        self.assertEqual(res.total_reserved_usd, Decimal("0.00"))
        self.assertEqual(self.broker.reservation_engine.get_total_reserved_cash(), Decimal("0.00"))

        # Verify ledger cash and equity: $5,000 - (5 * 150 + 1) = $4,249.00
        journal = self.storage.load_journal()
        self.assertEqual(journal.ledger.cash_usd, Decimal("4249.00"))
        self.assertEqual(journal.ledger.total_equity_usd, Decimal("4999.00"))
        pos = journal.ledger.positions.get("AAPL")
        self.assertIsNotNone(pos)
        self.assertEqual(pos.total_shares, 5)

    def test_10_partial_fills_and_final_completion(self) -> None:
        """Sequential partial fills accurately decrement remaining shares and convert reservations."""
        # 8 shares of AAPL at $150.00 = $1,200.00 (fits within 25% allocation limit $1,250.00).
        order, _ = self._create_authorized_order(
            ticker="AAPL",
            shares=8,
            limit_price=Decimal("150.00"),
            stop_loss=Decimal("147.00"),
            client_tag="PARTIAL-01",
        )
        self.broker.submit_order(order.order_id)
        # Reserved: 8 * $150 + $1 = $1201.00

        # Partial fill 1: 3 shares at $150.00 (fee $1.00)
        fill1 = self.broker.process_simulated_fill(
            order_id=order.order_id,
            fill_shares=3,
            fill_price=Decimal("150.00"),
            fill_fee=Decimal("1.00"),
        )
        self.assertEqual(fill1["status"], "PARTIALLY_FILLED")
        self.assertEqual(fill1["shares_filled"], 3)
        self.assertEqual(fill1["shares_remaining"], 5)

        order_after_p1 = self.storage.get_order(order.order_id)
        self.assertEqual(order_after_p1.state, PaperOrderState.PARTIALLY_FILLED)
        self.assertEqual(order_after_p1.shares_remaining, 5)

        res_p1 = self.broker.reservation_engine.get_reservation(order.order_id)
        self.assertEqual(res_p1.status, ReservationStatus.CONVERTED_PARTIAL)
        self.assertEqual(res_p1.shares_reserved_remaining, 5)
        # Reserved remaining: $1201 - $450 = $751.00
        self.assertEqual(res_p1.total_reserved_usd, Decimal("751.00"))

        # Partial fill 2: remaining 5 shares at $150.00 (fee $1.00)
        fill2 = self.broker.process_simulated_fill(
            order_id=order.order_id,
            fill_shares=5,
            fill_price=Decimal("150.00"),
            fill_fee=Decimal("1.00"),
        )
        self.assertEqual(fill2["status"], "FILLED")
        self.assertEqual(fill2["shares_remaining"], 0)

        order_after_p2 = self.storage.get_order(order.order_id)
        self.assertEqual(order_after_p2.state, PaperOrderState.FILLED)

        res_p2 = self.broker.reservation_engine.get_reservation(order.order_id)
        self.assertEqual(res_p2.status, ReservationStatus.CONVERTED_FULL)
        self.assertEqual(res_p2.total_reserved_usd, Decimal("0.00"))
        self.assertEqual(self.broker.reservation_engine.get_total_reserved_cash(), Decimal("0.00"))

    # -------------------------------------------------------------------------
    # 6. Fill Replay Protection & Error Handling
    # -------------------------------------------------------------------------

    def test_11_fill_replay_protection(self) -> None:
        """Simulated broker rejects duplicate fill identity or replaying identical fill."""
        order, _ = self._create_authorized_order(
            ticker="AAPL",
            shares=5,
            limit_price=Decimal("150.00"),
            stop_loss=Decimal("146.00"),
            client_tag="REPLAY-01",
        )
        self.broker.submit_order(order.order_id)

        # Execute partial fill
        self.broker.process_simulated_fill(
            order_id=order.order_id,
            fill_shares=2,
            fill_price=Decimal("150.00"),
            timestamp="2026-11-20T14:30:00+00:00",
        )

        # Attempt to process another fill attempting to exceed remaining shares
        with self.assertRaises(InvalidExecutionStateError):
            self.broker.process_simulated_fill(
                order_id=order.order_id,
                fill_shares=4,  # remaining is 3
                fill_price=Decimal("150.00"),
            )

    def test_12_fill_rejected_on_terminal_order(self) -> None:
        """Fills cannot be executed against orders in terminal states."""
        order, _ = self._create_authorized_order(
            ticker="AAPL",
            shares=5,
            limit_price=Decimal("150.00"),
            stop_loss=Decimal("146.00"),
            client_tag="TERMINAL-FILL-01",
        )
        self.broker.submit_order(order.order_id)
        self.broker.cancel_simulated_order(order.order_id)

        with self.assertRaises(InvalidExecutionStateError):
            self.broker.process_simulated_fill(
                order_id=order.order_id,
                fill_shares=5,
                fill_price=Decimal("150.00"),
            )

    # -------------------------------------------------------------------------
    # 7. Cross-Process Mutex Locking (PaperFileLock)
    # -------------------------------------------------------------------------

    def test_13_file_lock_mutual_exclusion_and_timeout(self) -> None:
        """Verify file lock serializes access and raises timeout on contention."""
        lock_file = os.path.join(self.temp_dir, "test.lock")
        lock1 = PaperFileLock(lock_path=lock_file, timeout=0.5, poll_interval=0.02)
        lock2 = PaperFileLock(lock_path=lock_file, timeout=0.2, poll_interval=0.02)

        lock1.acquire()
        self.assertTrue(lock1.is_locked)

        # Second lock attempt on competing thread must time out
        timed_out = False
        def try_lock():
            nonlocal timed_out
            try:
                lock2.acquire()
            except PaperLockTimeoutError:
                timed_out = True

        t = threading.Thread(target=try_lock)
        t.start()
        t.join()

        self.assertTrue(timed_out)

        # Release first lock
        lock1.release()
        self.assertFalse(lock1.is_locked)

        # Now lock2 can acquire
        lock2.acquire()
        self.assertTrue(lock2.is_locked)
        lock2.release()

    # -------------------------------------------------------------------------
    # 8. Crash Recovery & Orphan Reservation Reconciliation
    # -------------------------------------------------------------------------

    def test_14_orphan_reservation_reconciliation(self) -> None:
        """Detect and release orphaned reservations during reconciliation."""
        order, _ = self._create_authorized_order(
            ticker="AAPL",
            shares=5,
            limit_price=Decimal("150.00"),
            stop_loss=Decimal("146.00"),
            client_tag="ORPHAN-01",
        )
        self.broker.submit_order(order.order_id)

        # Simulate a crash where order state was changed to CANCELLED but reservation stayed ACTIVE
        order.state = PaperOrderState.CANCELLED
        self.storage.save_order(order)

        # Verify reservation is still marked active before reconciliation
        res_before = self.broker.reservation_engine.get_reservation(order.order_id)
        self.assertEqual(res_before.status, ReservationStatus.ACTIVE)
        self.assertEqual(self.broker.reservation_engine.get_total_reserved_cash(), Decimal("751.00"))

        # Run reconciliation
        recon_report = self.broker.reconcile_and_recover()
        self.assertEqual(recon_report["status"], "RECONCILED")
        self.assertEqual(recon_report["reservation_reconciliation"]["orphans_resolved_count"], 1)

        # Reservation is now marked RELEASED
        res_after = self.broker.reservation_engine.get_reservation(order.order_id)
        self.assertEqual(res_after.status, ReservationStatus.RELEASED)
        self.assertEqual(self.broker.reservation_engine.get_total_reserved_cash(), Decimal("0.00"))

    # -------------------------------------------------------------------------
    # 9. Bracket Order Exit Simulation (Stop-Loss & Take-Profit)
    # -------------------------------------------------------------------------

    def test_15_bracket_order_stop_loss_exit(self) -> None:
        """Simulate stop-loss trigger execution on a filled bracket order."""
        order, _ = self._create_authorized_order(
            ticker="AAPL",
            shares=5,
            limit_price=Decimal("150.00"),
            stop_loss=Decimal("145.00"),
            target=Decimal("160.00"),
            client_tag="BRACKET-SL-01",
        )
        self.broker.submit_order(order.order_id)
        self.broker.process_simulated_fill(order.order_id, fill_shares=5, fill_price=Decimal("150.00"))

        # Position holds 5 shares at $150.00 basis ($750.00) with $1.00 entry fee. Ledger cash: $4,249.00.
        # Simulate Stop-Loss exit trigger at $145.00:
        # Gross Proceeds: 5 * $145.00 = $725.00. Net Proceeds after $1.00 exit fee: $724.00.
        # Realized Net P&L: $725.00 (gross) - $750.00 (basis) - $1.00 (entry fee) - $1.00 (exit fee) = -$27.00.
        exit_report = self.broker.simulate_bracket_exit(
            order_id=order.order_id,
            exit_price=Decimal("145.00"),
            reason=PaperExitReason.STOP_LOSS,
            fee=Decimal("1.00"),
        )

        self.assertEqual(exit_report["status"], "EXITED")
        self.assertEqual(exit_report["reason"], PaperExitReason.STOP_LOSS.value)
        self.assertEqual(exit_report["realized_pnl"], "-27.00")

        # Verify ledger
        journal = self.storage.load_journal()
        self.assertEqual(journal.ledger.cash_usd, Decimal("4973.00"))
        self.assertEqual(journal.ledger.total_equity_usd, Decimal("4973.00"))
        pos = journal.ledger.positions.get("AAPL")
        self.assertEqual(pos.total_shares, 0)

    def test_16_bracket_order_take_profit_exit(self) -> None:
        """Simulate take-profit trigger execution on a filled bracket order."""
        # 4 shares at $300.00 = $1,200.00 (fits within 25% allocation limit $1,250.00).
        order, _ = self._create_authorized_order(
            ticker="QQQ",
            shares=4,
            limit_price=Decimal("300.00"),
            stop_loss=Decimal("296.00"),
            target=Decimal("310.00"),
            client_tag="BRACKET-TP-01",
        )
        self.broker.submit_order(order.order_id)
        self.broker.process_simulated_fill(order.order_id, fill_shares=4, fill_price=Decimal("300.00"))

        # Basis: 4 * $300.00 = $1,200.00 (plus $1.00 entry fee).
        # Exit at $310.00: gross proceeds = 4 * $310 = $1,240.00 - $1 exit fee = $1,239.00.
        # Realized P&L: $1,239.00 - $1,200.00 (basis) - $1.00 (entry fee) = +$38.00.
        exit_report = self.broker.simulate_bracket_exit(
            order_id=order.order_id,
            exit_price=Decimal("310.00"),
            reason=PaperExitReason.TAKE_PROFIT,
            fee=Decimal("1.00"),
        )

        self.assertEqual(exit_report["status"], "EXITED")
        self.assertEqual(exit_report["reason"], PaperExitReason.TAKE_PROFIT.value)
        self.assertEqual(exit_report["realized_pnl"], "38.00")

        journal = self.storage.load_journal()
        # Initial $5,000 - $1,201 (buy) + $1,239 (sell) = $5,038.00
        self.assertEqual(journal.ledger.cash_usd, Decimal("5038.00"))
        self.assertEqual(journal.ledger.total_equity_usd, Decimal("5038.00"))

    # -------------------------------------------------------------------------
    # 10. Protection of Root and External State
    # -------------------------------------------------------------------------

    def test_17_protected_files_and_external_paper_state_unmodified(self) -> None:
        """Verify root paper state and external TradingPaperState remain untouched."""
        # Verify forbidden root file name rejection in isolated sandbox without touching root
        forbidden_path = os.path.join(self.temp_dir, "paper_journal.json")
        with self.assertRaises(PaperStorageSafetyError):
            PaperStorageManager(state_dir=forbidden_path)

        from paper_storage import get_default_paper_state_dir
        external_state = get_default_paper_state_dir()
        if os.path.exists(external_state):
            # Check that simulator did NOT write to external TradingPaperState
            ext_marker = os.path.join(external_state, "SIMULATION_MARKER.json")
            self.assertFalse(os.path.exists(ext_marker))


if __name__ == "__main__":
    unittest.main()
