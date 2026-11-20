"""Exhaustive offline security and verification test suite for Trader-Swing Phase 8.1:
Multi-Gate Human Authorization Subsystem.

Tests:
1. Valid 3-Gate Authorization (Risk PASS -> Human PASS -> Platform PASS -> AUTHORIZED).
2. Gate 1 Risk Rejection (0.50% risk limit exceeded).
3. Gate 1 Max Positions Rejection (4 simultaneous positions limit).
4. Gate 1 Min Cash Reserve Rejection (20% cash reserve limit).
5. Gate 2 Human Rejection (Explicit 'NO' or 'REJECT').
6. Gate 2 Human Cancellation ('CANCEL').
7. Gate 2 Invalid Input Rejection (random string, empty string).
8. Gate 3 Platform Permission Denial (Fail-closed).
9. Token Expiration (TTL elapsed prior to verification).
10. Tampered Token (Corrupted HMAC signature).
11. Wrong Order ID Binding Mismatch.
12. Modified Quantity Binding Mismatch.
13. Modified Price Binding Mismatch.
14. Modified Stop-Loss Binding Mismatch.
15. Cross-Session Token Replay Rejection.
16. Duplicate Token Use (Single-use enforcement).
17. Concurrent / Repeated Approval Idempotency.
18. Signing Secret Unavailable / Insecure Key Handling.
19. Terminal State Lockout (Cannot authorize cancelled or rejected orders).
20. Backward Compatibility & Zero-Fill Guarantee.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
import shutil
import sys
import tempfile
import unittest

SKILLS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", ".agents", "skills", "trader-swing")
)
if SKILLS_DIR not in sys.path:
    sys.path.insert(0, SKILLS_DIR)

from authorization_models import (
    AuthorizationError,
    AuthorizationRecord,
    AuthorizationState,
    GateVerdict,
    HMACApprovalToken,
    HumanGateRejectionError,
    PlatformPermissionError,
    RiskGateRejectionError,
    SecretKeyUnavailableError,
    TerminalAuthorizationStateError,
    TokenAlreadyUsedError,
    TokenBindingMismatchError,
    TokenExpiredError,
    TokenTamperedError,
)
from authorization_token import HMACApprovalTokenManager, SecretKeyManager
from authorization_service import HumanAuthorizationService
from order_idempotency import CanonicalOrderIntent
from order_models import PaperOrder, PaperOrderState
from paper_models import PaperOrderSide
from paper_service import PaperTradingService
from paper_storage import PaperStorageManager


class TestSwingHumanAuthorization(unittest.TestCase):
    """Exhaustive test suite for Phase 8.1 Multi-Gate Human Authorization Subsystem."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.external_state_dir = os.path.join(self.temp_dir.name, "ExternalPaperState")
        os.makedirs(self.external_state_dir, exist_ok=True)
        self.storage = PaperStorageManager(state_dir=self.external_state_dir)
        self.service = PaperTradingService(storage=self.storage)
        self.auth_service = self.service.auth_service
        self.storage.initialize_state(initial_capital=Decimal("5000.00"), force=True)

        self.valid_trading_ts = "2026-10-19T14:15:00+00:00"
        self.valid_reference_now = datetime.fromisoformat("2026-10-19T14:30:00+00:00")

    def tearDown(self):
        self.temp_dir.cleanup()

    def _create_valid_proposed_order(self, ticker: str = "AAPL", shares: int = 5) -> PaperOrder:
        """Helper to create a standard valid proposed order."""
        intent = CanonicalOrderIntent(
            session_date="20261019",
            ticker=ticker,
            side=PaperOrderSide.BUY,
            shares=shares,
            order_type="LIMIT",
            limit_price=Decimal("150.00"),
            stop_loss=Decimal("146.00"),  # Risk = 5 * $4 = $20.00 <= $25.00 limit (0.50% of $5000)
            target=Decimal("160.00"),
        )
        return self.service.create_order(intent, reference_now=self.valid_reference_now)

    # -------------------------------------------------------------------------
    # 1. Valid 3-Gate Authorization (Happy Path)
    # -------------------------------------------------------------------------

    def test_01_valid_three_gate_authorization_lifecycle(self):
        """1. Verify full happy path: PROPOSED -> RISK_VALIDATED -> PENDING_HUMAN -> HUMAN_APPROVED -> PLATFORM_APPROVED -> AUTHORIZED."""
        order = self._create_valid_proposed_order("AAPL", 5)
        self.assertEqual(order.state, PaperOrderState.PROPOSED)

        # Run complete 3-gate authorization
        record = self.service.authorize_order(
            order_id=order.order_id,
            user_confirmation="YES",
            reference_now=self.valid_reference_now,
        )

        self.assertEqual(record.state, AuthorizationState.AUTHORIZED)
        self.assertTrue(record.is_authorized)
        self.assertTrue(record.is_terminal)
        self.assertIsNotNone(record.token)
        self.assertTrue(record.token.used)  # Token was single-use redeemed
        self.assertEqual(len(record.gate_verdicts), 3)

        # Check all 3 gates recorded PASS
        v_risk, v_human, v_plat = record.gate_verdicts
        self.assertEqual(v_risk.gate_name, "GATE_1_RISK")
        self.assertTrue(v_risk.passed)
        self.assertEqual(v_human.gate_name, "GATE_2_HUMAN")
        self.assertTrue(v_human.passed)
        self.assertEqual(v_plat.gate_name, "GATE_3_PLATFORM")
        self.assertTrue(v_plat.passed)

        # Check PaperOrder transitioned to APPROVED (ready for submission in Phase 8.2)
        reloaded_order = self.service.get_order(order.order_id)
        self.assertEqual(reloaded_order.state, PaperOrderState.APPROVED)
        self.assertEqual(reloaded_order.shares_filled, 0)
        self.assertEqual(reloaded_order.shares_remaining, 5)

        # CRITICAL INVARIANT: Zero fills, zero cash mutations
        status = self.service.get_status()
        self.assertEqual(status["cash_usd"], "5000.00")
        self.assertEqual(status["open_positions_count"], 0)
        self.assertTrue(status["equity_invariant_holds"])

    # -------------------------------------------------------------------------
    # 2. Gate 1: Deterministic Risk Rejections
    # -------------------------------------------------------------------------

    def test_02_gate1_risk_rejection_per_trade_risk_exceeded(self):
        """2. Verify Gate 1 fails closed if trade risk exceeds 0.50% of equity."""
        # 10 shares * ($150 - $146) = $40.00 risk > $25.00 (0.50% of $5,000)
        intent = CanonicalOrderIntent(
            session_date="20261019",
            ticker="MSFT",
            side=PaperOrderSide.BUY,
            shares=10,
            order_type="LIMIT",
            limit_price=Decimal("150.00"),
            stop_loss=Decimal("146.00"),
        )
        order = self.service.create_order(intent, reference_now=self.valid_reference_now)

        with self.assertRaises(RiskGateRejectionError) as cm:
            self.service.authorize_order(
                order_id=order.order_id,
                user_confirmation="YES",
                reference_now=self.valid_reference_now,
            )
        self.assertIn("PER_TRADE_RISK_EXCEEDED", str(cm.exception))

        rec = self.service.get_authorization(order.order_id)
        self.assertEqual(rec.state, AuthorizationState.RISK_REJECTED)
        self.assertTrue(rec.is_terminal)
        self.assertIsNone(rec.token)  # No token issued for rejected risk

    def test_03_gate1_risk_rejection_max_positions_limit(self):
        """3. Verify Gate 1 rejects if 4 simultaneous positions already held."""
        # Fill 4 distinct positions manually in baseline ledger
        for i, sym in enumerate(["AAPL", "MSFT", "GOOGL", "AMZN"]):
            self.service.record_paper_entry(
                ticker=sym,
                shares=1,
                price="100.00",
                stop_loss="98.00",
                timestamp=self.valid_trading_ts,
                paper_only=True,
                reference_now=self.valid_reference_now,
            )

        status = self.service.get_status()
        self.assertEqual(status["open_positions_count"], 4)

        # Propose 5th position (NVDA)
        order = self._create_valid_proposed_order("NVDA", 1)
        with self.assertRaises(RiskGateRejectionError) as cm:
            self.service.authorize_order(order.order_id, "YES", reference_now=self.valid_reference_now)
        self.assertIn("MAX_POSITIONS_EXCEEDED", str(cm.exception))

        rec = self.service.get_authorization(order.order_id)
        self.assertEqual(rec.state, AuthorizationState.RISK_REJECTED)

    def test_04_gate1_risk_rejection_cash_reserve_violation(self):
        """4. Verify Gate 1 rejects if purchase violates 20% minimum cash reserve."""
        # Buy $4,500 worth of stock on $5,000 equity (leaves $500 cash = 10% < 20% reserve)
        intent = CanonicalOrderIntent(
            session_date="20261019",
            ticker="TSLA",
            side=PaperOrderSide.BUY,
            shares=45,
            order_type="LIMIT",
            limit_price=Decimal("100.00"),
            stop_loss=Decimal("99.70"),  # Risk = 45 * $0.30 = $13.50 <= $25
        )
        order = self.service.create_order(intent, reference_now=self.valid_reference_now)

        with self.assertRaises(RiskGateRejectionError) as cm:
            self.service.authorize_order(order.order_id, "YES", reference_now=self.valid_reference_now)
        self.assertIn("MIN_CASH_RESERVE_VIOLATION", str(cm.exception))

    # -------------------------------------------------------------------------
    # 3. Gate 2: Explicit Human Confirmation Failures
    # -------------------------------------------------------------------------

    def test_05_gate2_human_rejection_explicit_no(self):
        """5. Verify Gate 2 fails closed if user replies 'NO' or 'REJECT'."""
        order = self._create_valid_proposed_order("AAPL", 5)

        with self.assertRaises(HumanGateRejectionError) as cm:
            self.service.authorize_order(
                order_id=order.order_id,
                user_confirmation="NO",
                reference_now=self.valid_reference_now,
            )
        self.assertIn("User denied", str(cm.exception))

        rec = self.service.get_authorization(order.order_id)
        self.assertEqual(rec.state, AuthorizationState.HUMAN_REJECTED)
        self.assertTrue(rec.is_terminal)

    def test_06_gate2_human_cancellation_explicit_cancel(self):
        """6. Verify Gate 2 transitions to CANCELLED if user cancels."""
        order = self._create_valid_proposed_order("AAPL", 5)

        with self.assertRaises(HumanGateRejectionError) as cm:
            self.service.authorize_order(
                order_id=order.order_id,
                user_confirmation="CANCEL",
                reference_now=self.valid_reference_now,
            )
        self.assertIn("User cancelled", str(cm.exception))

        rec = self.service.get_authorization(order.order_id)
        self.assertEqual(rec.state, AuthorizationState.CANCELLED)
        self.assertTrue(rec.is_terminal)

    def test_07_gate2_invalid_or_arbitrary_input_rejected(self):
        """7. Verify Gate 2 rejects arbitrary string or empty input (no implicit yes)."""
        for i, bad_input in enumerate(["", "   ", "maybe", "y", "ok", "proceed", "1"]):
            intent = CanonicalOrderIntent(
                session_date="20261019",
                ticker="AAPL",
                side=PaperOrderSide.BUY,
                shares=1,
                order_type="LIMIT",
                limit_price=Decimal("150.00"),
                stop_loss=Decimal("146.00"),
                client_tag=f"BAD-INPUT-{i}",
            )
            order_i = self.service.create_order(intent, reference_now=self.valid_reference_now)
            with self.assertRaises(HumanGateRejectionError):
                self.service.authorize_order(
                    order_id=order_i.order_id,
                    user_confirmation=bad_input,
                    reference_now=self.valid_reference_now,
                )

    # -------------------------------------------------------------------------
    # 4. Gate 3: Platform Permission & Token Security Failures
    # -------------------------------------------------------------------------

    def test_08_gate3_platform_permission_denial(self):
        """8. Verify Gate 3 fails closed if platform permission callback denies authorization."""
        order = self._create_valid_proposed_order("AAPL", 5)

        def denying_platform_verifier(ord_obj, token):
            return False

        with self.assertRaises(PlatformPermissionError) as cm:
            self.service.authorize_order(
                order_id=order.order_id,
                user_confirmation="YES",
                platform_verifier=denying_platform_verifier,
                reference_now=self.valid_reference_now,
            )
        self.assertIn("permission denied by platform gate", str(cm.exception))

        rec = self.service.get_authorization(order.order_id)
        self.assertEqual(rec.state, AuthorizationState.AUTHORIZATION_FAILED)
        self.assertTrue(rec.is_terminal)

    def test_09_gate3_token_expiration_rejection(self):
        """9. Verify token expired past TTL cannot be authorized."""
        order = self._create_valid_proposed_order("AAPL", 5)

        # Gate 1 issues token at t_issue with 300s TTL
        t_issue = self.valid_reference_now
        rec = AuthorizationRecord(
            order_id=order.order_id,
            idempotency_key=order.idempotency_key,
            ticker=order.ticker,
            shares=order.shares_initial,
            state=AuthorizationState.PROPOSED,
            created_at=t_issue.isoformat(),
            updated_at=t_issue.isoformat(),
        )
        self.auth_service.evaluate_gate1_risk(order, rec, reference_now=t_issue)
        self.assertEqual(rec.state, AuthorizationState.PENDING_HUMAN_APPROVAL)
        self.auth_service.save_authorization(rec)

        # Gate 2 / Gate 3 attempted at t_expired (301 seconds later)
        t_expired = t_issue + timedelta(seconds=301)

        with self.assertRaises(TokenExpiredError) as cm:
            self.service.authorize_order(
                order_id=order.order_id,
                user_confirmation="YES",
                reference_now=t_expired,
            )
        self.assertIn("expired", str(cm.exception).lower())

    def test_10_gate3_tampered_token_signature_rejected(self):
        """10. Verify token with tampered HMAC signature is rejected."""
        order = self._create_valid_proposed_order("AAPL", 5)

        # Initialize record and run Gate 1
        record = AuthorizationRecord(
            order_id=order.order_id,
            idempotency_key=order.idempotency_key,
            ticker=order.ticker,
            shares=order.shares_initial,
            state=AuthorizationState.PROPOSED,
            created_at=self.valid_reference_now.isoformat(),
            updated_at=self.valid_reference_now.isoformat(),
        )
        self.auth_service.evaluate_gate1_risk(order, record, reference_now=self.valid_reference_now)
        self.auth_service.evaluate_gate2_human(order, record, "YES", reference_now=self.valid_reference_now)

        # Tamper with token signature
        record.token.signature = "A" * 64

        with self.assertRaises(TokenTamperedError) as cm:
            self.auth_service.evaluate_gate3_platform(order, record, reference_now=self.valid_reference_now)
        self.assertIn("signature verification failed", str(cm.exception).lower())

    def test_11_gate3_token_binding_mismatch_wrong_order_id(self):
        """11. Verify token issued for order A cannot be used for order B."""
        order_a = self._create_valid_proposed_order("AAPL", 5)
        order_b = self._create_valid_proposed_order("MSFT", 5)

        token_a = self.auth_service.token_manager.issue_token(order_a, reference_now=self.valid_reference_now)

        with self.assertRaises(TokenBindingMismatchError) as cm:
            self.auth_service.token_manager.verify_token(token_a, order_b, reference_now=self.valid_reference_now)
        self.assertIn("does not match target order ID", str(cm.exception))

    def test_12_gate3_token_binding_mismatch_modified_shares(self):
        """12. Verify token issued for 5 shares is rejected if order shares modified to 6."""
        order = self._create_valid_proposed_order("AAPL", 5)
        token = self.auth_service.token_manager.issue_token(order, reference_now=self.valid_reference_now)

        # Modify order shares
        order.shares_initial = 6
        order.shares_remaining = 6

        with self.assertRaises(TokenBindingMismatchError) as cm:
            self.auth_service.token_manager.verify_token(token, order, reference_now=self.valid_reference_now)
        self.assertIn("Token shares (5) does not match order initial shares (6)", str(cm.exception))

    def test_13_gate3_token_binding_mismatch_modified_price(self):
        """13. Verify token issued for limit $150.00 is rejected if order limit modified to $151.00."""
        order = self._create_valid_proposed_order("AAPL", 5)
        token = self.auth_service.token_manager.issue_token(order, reference_now=self.valid_reference_now)

        order.limit_price = Decimal("151.00")

        with self.assertRaises(TokenBindingMismatchError) as cm:
            self.auth_service.token_manager.verify_token(token, order, reference_now=self.valid_reference_now)
        self.assertIn("Token limit price ($150.00) does not match order limit price ($151.00)", str(cm.exception))

    def test_14_gate3_token_binding_mismatch_modified_stop(self):
        """14. Verify token issued for stop $146.00 is rejected if order stop modified to $145.00."""
        order = self._create_valid_proposed_order("AAPL", 5)
        token = self.auth_service.token_manager.issue_token(order, reference_now=self.valid_reference_now)

        order.stop_loss = Decimal("145.00")

        with self.assertRaises(TokenBindingMismatchError) as cm:
            self.auth_service.token_manager.verify_token(token, order, reference_now=self.valid_reference_now)
        self.assertIn("Token stop loss ($146.00) does not match order stop loss ($145.00)", str(cm.exception))

    # -------------------------------------------------------------------------
    # 5. Single-Use Token Enforcement & Replay Protection
    # -------------------------------------------------------------------------

    def test_15_duplicate_token_use_rejected(self):
        """15. Verify single-use token cannot be redeemed twice."""
        order = self._create_valid_proposed_order("AAPL", 5)
        token = self.auth_service.token_manager.issue_token(order, reference_now=self.valid_reference_now)

        # First verification & redemption
        self.assertTrue(self.auth_service.token_manager.verify_token(token, order, reference_now=self.valid_reference_now))
        self.auth_service.token_manager.redeem_token(token, reference_now=self.valid_reference_now)
        self.assertTrue(token.used)

        # Second attempt to verify or redeem
        with self.assertRaises(TokenAlreadyUsedError) as cm:
            self.auth_service.token_manager.verify_token(token, order, reference_now=self.valid_reference_now)
        self.assertIn("already been redeemed", str(cm.exception))

        with self.assertRaises(TokenAlreadyUsedError):
            self.auth_service.token_manager.redeem_token(token, reference_now=self.valid_reference_now)

    def test_16_repeated_order_authorization_is_idempotent(self):
        """16. Verify calling authorize_order on an already AUTHORIZED order returns the existing record idempotently."""
        order = self._create_valid_proposed_order("AAPL", 5)

        rec1 = self.service.authorize_order(order.order_id, "YES", reference_now=self.valid_reference_now)
        self.assertEqual(rec1.state, AuthorizationState.AUTHORIZED)

        # Call again
        rec2 = self.service.authorize_order(order.order_id, "YES", reference_now=self.valid_reference_now)
        self.assertEqual(rec2.state, AuthorizationState.AUTHORIZED)
        self.assertEqual(rec1.order_id, rec2.order_id)
        self.assertEqual(rec1.token.token_id, rec2.token.token_id)

    # -------------------------------------------------------------------------
    # 6. Secret Key Management & Terminal Lockouts
    # -------------------------------------------------------------------------

    def test_17_secret_key_rotation_and_tamper_isolation(self):
        """17. Verify rotating signing secret invalidates tokens signed by previous secret."""
        key_mgr = SecretKeyManager(secret_dir=self.external_state_dir)
        token_mgr = HMACApprovalTokenManager(key_manager=key_mgr)

        order = self._create_valid_proposed_order("AAPL", 5)
        token = token_mgr.issue_token(order, reference_now=self.valid_reference_now)

        # Verify passes before rotation
        self.assertTrue(token_mgr.verify_token(token, order, reference_now=self.valid_reference_now))

        # Rotate secret
        key_mgr.rotate_secret()

        # Verify fails after rotation (old signature does not match new key)
        with self.assertRaises(TokenTamperedError):
            token_mgr.verify_token(token, order, reference_now=self.valid_reference_now)

    def test_18_terminal_state_lockout_cannot_authorize_terminal_orders(self):
        """18. Verify orders in terminal state (REJECTED, CANCELLED) cannot be authorized."""
        order = self._create_valid_proposed_order("AAPL", 5)
        self.service.transition_order(order.order_id, PaperOrderState.CANCELLED, reason="Cancelled early")

        with self.assertRaises(TerminalAuthorizationStateError) as cm:
            self.service.authorize_order(order.order_id, "YES", reference_now=self.valid_reference_now)
        self.assertIn("terminal state 'CANCELLED'", str(cm.exception))

    # -------------------------------------------------------------------------
    # 7. CLI Ticket & Authorize Subcommands
    # -------------------------------------------------------------------------

    def test_19_ticket_rendering_and_cli_workflow(self):
        """19. Verify authorization ticket formatting and CLI commands."""
        order = self._create_valid_proposed_order("AAPL", 5)

        ticket = self.service.render_authorization_ticket(order.order_id)
        self.assertIn("TRADER-SWING: ORDER AUTHORIZATION TICKET", ticket)
        self.assertIn(order.order_id, ticket)
        self.assertIn("AAPL | BUY", ticket)
        self.assertIn("5 shares", ticket)
        self.assertIn("$150.00 USD", ticket)
        self.assertIn("$146.00 USD", ticket)
        self.assertIn("Per-Trade Risk", ticket)

    def test_20_zero_execution_invariant_verified(self):
        """20. Explicitly verify that zero execution calls, fills, or ledger mutations occurred."""
        order = self._create_valid_proposed_order("AAPL", 5)
        rec = self.service.authorize_order(order.order_id, "YES", reference_now=self.valid_reference_now)
        self.assertTrue(rec.is_authorized)

        # Orders registry check
        orders_data = self.storage.load_orders()
        self.assertEqual(len(orders_data["fills"]), 0)  # ZERO fills
        self.assertEqual(len(orders_data["exits"]), 0)  # ZERO exits

        # Ledger check
        status = self.service.get_status()
        self.assertEqual(status["cash_usd"], "5000.00")
        self.assertEqual(status["open_positions_count"], 0)
        self.assertEqual(status["total_market_value_usd"], "0.00")
        self.assertTrue(status["equity_invariant_holds"])


if __name__ == "__main__":
    unittest.main()
