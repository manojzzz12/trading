"""Exhaustive offline test suite for Trader-Swing Phase 8.0: Order Lifecycle Models & Idempotency.

Tests:
1. Every legal state transition across all active states.
2. Illegal state transition rejection.
3. Terminal-state protection (FILLED, REJECTED, CANCELLED, EXPIRED, FAILED).
4. Deterministic order ID and idempotency key generation.
5. Identical intent produces same idempotency key; different intent produces different key.
6. Duplicate order submission returns existing order idempotently (no duplicate creation).
7. Conflicting idempotency key reuse raises ConflictingIdempotencyKeyError.
8. Execution fill share tracking: partial fills, remaining decrementing, transition to FILLED.
9. Oversized fill rejection (shares > shares_remaining).
10. Duplicate fill event replay: returns existing record, zero double cash deduction, zero duplicate lots.
11. Conflicting fill identity reuse raises ConflictingFillIdentityError.
12. Persistence round-trip across independent storage instances.
13. Audit trail generation and transition history tracking.
14. Backward compatibility with Phase 6.1 manual fill recording.
"""

from datetime import datetime, timezone
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

from order_models import (
    IllegalStateTransitionError,
    OrderStateTransition,
    PaperOrder,
    PaperOrderState,
    TerminalOrderStateError,
    TERMINAL_ORDER_STATES,
    LEGAL_ORDER_TRANSITIONS,
)
from order_idempotency import (
    CanonicalOrderIntent,
    ConflictingFillIdentityError,
    ConflictingIdempotencyKeyError,
    compute_fill_identity,
    compute_manual_fill_identity,
    compute_order_id,
)
from order_service import PaperOrderEngine
from paper_models import PaperOrderSide
from paper_service import PaperTradingService
from paper_storage import PaperStorageManager


class TestSwingOrderLifecycleAndIdempotency(unittest.TestCase):
    """Targeted tests for Phase 8.0 order lifecycle models, transitions, and idempotency."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.external_state_dir = os.path.join(self.temp_dir.name, "ExternalPaperState")
        os.makedirs(self.external_state_dir, exist_ok=True)
        self.storage = PaperStorageManager(state_dir=self.external_state_dir)
        self.service = PaperTradingService(storage=self.storage)
        self.storage.initialize_state(initial_capital=Decimal("5000.00"), force=True)

        self.valid_trading_ts = "2026-10-19T14:15:00+00:00"
        self.valid_reference_now = datetime.fromisoformat("2026-10-19T14:30:00+00:00")

    def tearDown(self):
        self.temp_dir.cleanup()

    # -------------------------------------------------------------------------
    # 1. Deterministic Order ID & Idempotency Key Generation
    # -------------------------------------------------------------------------

    def test_01_deterministic_idempotency_key_generation(self):
        """1. Verify same order intent produces exact same key; different intent produces different key."""
        intent1 = CanonicalOrderIntent(
            session_date="20261019",
            ticker="AAPL",
            side=PaperOrderSide.BUY,
            shares=10,
            order_type="LIMIT",
            limit_price=Decimal("150.00"),
            stop_loss=Decimal("145.00"),
            target=Decimal("160.00"),
        )
        intent2 = CanonicalOrderIntent(
            session_date="2026-10-19",  # Hyphenated date normalizes to same 20261019
            ticker="aapl",             # Lowercase normalizes to AAPL
            side=PaperOrderSide.BUY,
            shares=10,
            order_type="limit",        # Lowercase normalizes to LIMIT
            limit_price=Decimal("150.000"),
            stop_loss=Decimal("145.00"),
            target=Decimal("160.00"),
        )

        key1 = intent1.compute_idempotency_key()
        key2 = intent2.compute_idempotency_key()
        self.assertEqual(key1, key2)
        self.assertTrue(key1.startswith("IDEMP-20261019-AAPL-"))

        # Materially different: shares = 11
        intent_diff_shares = CanonicalOrderIntent(
            session_date="20261019",
            ticker="AAPL",
            side=PaperOrderSide.BUY,
            shares=11,
            order_type="LIMIT",
            limit_price=Decimal("150.00"),
            stop_loss=Decimal("145.00"),
            target=Decimal("160.00"),
        )
        self.assertNotEqual(key1, intent_diff_shares.compute_idempotency_key())

        # Materially different: stop_loss = 144.50
        intent_diff_stop = CanonicalOrderIntent(
            session_date="20261019",
            ticker="AAPL",
            side=PaperOrderSide.BUY,
            shares=10,
            order_type="LIMIT",
            limit_price=Decimal("150.00"),
            stop_loss=Decimal("144.50"),
            target=Decimal("160.00"),
        )
        self.assertNotEqual(key1, intent_diff_stop.compute_idempotency_key())

        # Materially different: ticker = MSFT
        intent_diff_sym = CanonicalOrderIntent(
            session_date="20261019",
            ticker="MSFT",
            side=PaperOrderSide.BUY,
            shares=10,
            order_type="LIMIT",
            limit_price=Decimal("150.00"),
            stop_loss=Decimal("145.00"),
            target=Decimal("160.00"),
        )
        self.assertNotEqual(key1, intent_diff_sym.compute_idempotency_key())

    def test_02_order_intent_validation_invariants(self):
        """2. Verify intent validation enforces positive quantities and price sanity."""
        # A. Stop loss >= limit price for BUY
        bad_stop_intent = CanonicalOrderIntent(
            session_date="20261019",
            ticker="AAPL",
            side=PaperOrderSide.BUY,
            shares=10,
            order_type="LIMIT",
            limit_price=Decimal("150.00"),
            stop_loss=Decimal("155.00"),  # Invalid: stop >= limit
        )
        with self.assertRaises(ValueError):
            bad_stop_intent.validate()

        # B. Target <= limit price for BUY
        bad_target_intent = CanonicalOrderIntent(
            session_date="20261019",
            ticker="AAPL",
            side=PaperOrderSide.BUY,
            shares=10,
            order_type="LIMIT",
            limit_price=Decimal("150.00"),
            stop_loss=Decimal("145.00"),
            target=Decimal("148.00"),  # Invalid: target <= limit
        )
        with self.assertRaises(ValueError):
            bad_target_intent.validate()

        # C. Zero or negative shares
        bad_shares_intent = CanonicalOrderIntent(
            session_date="20261019",
            ticker="AAPL",
            side=PaperOrderSide.BUY,
            shares=0,
            order_type="LIMIT",
            limit_price=Decimal("150.00"),
            stop_loss=Decimal("145.00"),
        )
        with self.assertRaises(ValueError):
            bad_shares_intent.validate()

    # -------------------------------------------------------------------------
    # 2. Legal State Transitions
    # -------------------------------------------------------------------------

    def test_03_legal_transition_lifecycle_full_happy_path(self):
        """3. Verify complete legal transition flow: PROPOSED -> PENDING_APPROVAL -> APPROVED -> SUBMITTED -> PARTIALLY_FILLED -> FILLED."""
        engine = PaperOrderEngine(storage=self.storage)
        intent = CanonicalOrderIntent(
            session_date="20261019",
            ticker="AAPL",
            side=PaperOrderSide.BUY,
            shares=10,
            order_type="LIMIT",
            limit_price=Decimal("150.00"),
            stop_loss=Decimal("145.00"),
            target=Decimal("160.00"),
        )

        # 1. Create order (starts PROPOSED)
        order = engine.create_order(intent, reference_now=self.valid_reference_now)
        self.assertEqual(order.state, PaperOrderState.PROPOSED)
        self.assertEqual(order.shares_initial, 10)
        self.assertEqual(order.shares_remaining, 10)
        self.assertEqual(order.shares_filled, 0)
        self.assertFalse(order.is_terminal)
        self.assertTrue(order.is_active)

        # 2. PROPOSED -> PENDING_APPROVAL
        order = engine.transition_order(order.order_id, PaperOrderState.PENDING_APPROVAL, reason="Risk check passed")
        self.assertEqual(order.state, PaperOrderState.PENDING_APPROVAL)

        # 3. PENDING_APPROVAL -> APPROVED
        order = engine.transition_order(order.order_id, PaperOrderState.APPROVED, reason="User confirmed approval")
        self.assertEqual(order.state, PaperOrderState.APPROVED)

        # 4. APPROVED -> SUBMITTED
        order = engine.transition_order(order.order_id, PaperOrderState.SUBMITTED, reason="Dispatched to simulated adapter")
        self.assertEqual(order.state, PaperOrderState.SUBMITTED)

        # 5. SUBMITTED -> PARTIALLY_FILLED (fill 4 shares)
        order, fill1_id = engine.apply_fill_to_order(
            order.order_id,
            shares=4,
            price=Decimal("150.00"),
            timestamp=self.valid_trading_ts,
        )
        self.assertEqual(order.state, PaperOrderState.PARTIALLY_FILLED)
        self.assertEqual(order.shares_filled, 4)
        self.assertEqual(order.shares_remaining, 6)
        self.assertIn(fill1_id, order.fill_ids)

        # 6. PARTIALLY_FILLED -> PARTIALLY_FILLED (fill 3 more shares)
        order, fill2_id = engine.apply_fill_to_order(
            order.order_id,
            shares=3,
            price=Decimal("150.00"),
            timestamp=self.valid_trading_ts,
        )
        self.assertEqual(order.state, PaperOrderState.PARTIALLY_FILLED)
        self.assertEqual(order.shares_filled, 7)
        self.assertEqual(order.shares_remaining, 3)

        # 7. PARTIALLY_FILLED -> FILLED (fill final 3 shares)
        order, fill3_id = engine.apply_fill_to_order(
            order.order_id,
            shares=3,
            price=Decimal("150.00"),
            timestamp=self.valid_trading_ts,
        )
        self.assertEqual(order.state, PaperOrderState.FILLED)
        self.assertEqual(order.shares_filled, 10)
        self.assertEqual(order.shares_remaining, 0)
        self.assertTrue(order.is_terminal)
        self.assertFalse(order.is_active)
        self.assertEqual(len(order.fill_ids), 3)

    def test_04_legal_transition_rejections_and_cancellations(self):
        """4. Verify alternative legal exits: REJECTED, CANCELLED, EXPIRED, FAILED."""
        engine = PaperOrderEngine(storage=self.storage)

        def make_intent(sym: str) -> CanonicalOrderIntent:
            return CanonicalOrderIntent(
                session_date="20261019",
                ticker=sym,
                side=PaperOrderSide.BUY,
                shares=5,
                order_type="LIMIT",
                limit_price=Decimal("100.00"),
                stop_loss=Decimal("95.00"),
            )

        # A. PROPOSED -> REJECTED
        o1 = engine.create_order(make_intent("MSFT"))
        o1 = engine.transition_order(o1.order_id, PaperOrderState.REJECTED, reason="Risk allocation exceeded")
        self.assertEqual(o1.state, PaperOrderState.REJECTED)
        self.assertEqual(o1.rejection_reason, "Risk allocation exceeded")
        self.assertTrue(o1.is_terminal)

        # B. PROPOSED -> CANCELLED
        o2 = engine.create_order(make_intent("GOOGL"))
        o2 = engine.transition_order(o2.order_id, PaperOrderState.CANCELLED, reason="Cancelled by user before approval")
        self.assertEqual(o2.state, PaperOrderState.CANCELLED)
        self.assertEqual(o2.cancellation_reason, "Cancelled by user before approval")
        self.assertTrue(o2.is_terminal)

        # C. PENDING_APPROVAL -> EXPIRED
        o3 = engine.create_order(make_intent("AMZN"))
        o3 = engine.transition_order(o3.order_id, PaperOrderState.PENDING_APPROVAL)
        o3 = engine.transition_order(o3.order_id, PaperOrderState.EXPIRED, reason="Approval timeout window elapsed")
        self.assertEqual(o3.state, PaperOrderState.EXPIRED)
        self.assertTrue(o3.is_terminal)

        # D. SUBMITTED -> FAILED
        o4 = engine.create_order(make_intent("META"))
        o4 = engine.transition_order(o4.order_id, PaperOrderState.PENDING_APPROVAL)
        o4 = engine.transition_order(o4.order_id, PaperOrderState.APPROVED)
        o4 = engine.transition_order(o4.order_id, PaperOrderState.SUBMITTED)
        o4 = engine.transition_order(o4.order_id, PaperOrderState.FAILED, reason="Broker connection error")
        self.assertEqual(o4.state, PaperOrderState.FAILED)
        self.assertEqual(o4.failure_reason, "Broker connection error")
        self.assertTrue(o4.is_terminal)

    # -------------------------------------------------------------------------
    # 3. Illegal State Transitions & Terminal Protection
    # -------------------------------------------------------------------------

    def test_05_illegal_state_transitions_rejected(self):
        """5. Verify illegal transitions are blocked according to state table."""
        engine = PaperOrderEngine(storage=self.storage)
        intent = CanonicalOrderIntent(
            session_date="20261019",
            ticker="TSLA",
            side=PaperOrderSide.BUY,
            shares=5,
            order_type="LIMIT",
            limit_price=Decimal("200.00"),
            stop_loss=Decimal("190.00"),
        )
        order = engine.create_order(intent)

        # Cannot jump PROPOSED -> SUBMITTED
        with self.assertRaises(IllegalStateTransitionError):
            engine.transition_order(order.order_id, PaperOrderState.SUBMITTED)

        # Cannot jump PROPOSED -> FILLED
        with self.assertRaises(IllegalStateTransitionError):
            engine.transition_order(order.order_id, PaperOrderState.FILLED)

        # Cannot jump PROPOSED -> PARTIALLY_FILLED
        with self.assertRaises(IllegalStateTransitionError):
            engine.transition_order(order.order_id, PaperOrderState.PARTIALLY_FILLED)

        # Advance to PENDING_APPROVAL
        order = engine.transition_order(order.order_id, PaperOrderState.PENDING_APPROVAL)

        # Cannot jump PENDING_APPROVAL -> SUBMITTED
        with self.assertRaises(IllegalStateTransitionError):
            engine.transition_order(order.order_id, PaperOrderState.SUBMITTED)

    def test_06_terminal_state_protection(self):
        """6. Verify orders in terminal states can NEVER transition to any state."""
        engine = PaperOrderEngine(storage=self.storage)
        intent = CanonicalOrderIntent(
            session_date="20261019",
            ticker="NVDA",
            side=PaperOrderSide.BUY,
            shares=5,
            order_type="LIMIT",
            limit_price=Decimal("120.00"),
            stop_loss=Decimal("110.00"),
        )
        order = engine.create_order(intent)
        order = engine.transition_order(order.order_id, PaperOrderState.REJECTED, reason="Terminated")

        self.assertTrue(order.is_terminal)
        for target_state in PaperOrderState:
            with self.assertRaises(TerminalOrderStateError):
                engine.transition_order(order.order_id, target_state)

    # -------------------------------------------------------------------------
    # 4. Idempotent Order Creation & Conflicting Key Detection
    # -------------------------------------------------------------------------

    def test_07_duplicate_order_creation_is_idempotent(self):
        """7. Verify submitting identical intent multiple times returns existing order."""
        engine = PaperOrderEngine(storage=self.storage)
        intent = CanonicalOrderIntent(
            session_date="20261019",
            ticker="SPY",
            side=PaperOrderSide.BUY,
            shares=10,
            order_type="LIMIT",
            limit_price=Decimal("500.00"),
            stop_loss=Decimal("490.00"),
            target=Decimal("520.00"),
        )

        order1 = engine.create_order(intent)
        order2 = engine.create_order(intent)
        self.assertEqual(order1.order_id, order2.order_id)
        self.assertEqual(order1.idempotency_key, order2.idempotency_key)

        # Verify only one order is in storage
        all_orders = engine.list_orders()
        self.assertEqual(len(all_orders), 1)

    def test_08_conflicting_idempotency_key_reuse_rejected(self):
        """8. Verify reusing an idempotency key with conflicting intent raises ConflictingIdempotencyKeyError."""
        engine = PaperOrderEngine(storage=self.storage)
        intent1 = CanonicalOrderIntent(
            session_date="20261019",
            ticker="QQQ",
            side=PaperOrderSide.BUY,
            shares=10,
            order_type="LIMIT",
            limit_price=Decimal("400.00"),
            stop_loss=Decimal("390.00"),
        )
        order1 = engine.create_order(intent1)

        # Directly attempt creating order with same idempotency key but different intent hash
        class MockConflictingIntent:
            def validate(self): pass
            def compute_idempotency_key(self): return order1.idempotency_key
            def compute_intent_hash(self): return "DIFFERENT_HASH"

        with self.assertRaises(ConflictingIdempotencyKeyError):
            engine.create_order(MockConflictingIntent())

    # -------------------------------------------------------------------------
    # 5. Execution Fill Quantity Integrity
    # -------------------------------------------------------------------------

    def test_09_fill_quantity_decrementing_and_overflow_protection(self):
        """9. Verify fills decrement remaining shares and reject fills exceeding remaining shares."""
        engine = PaperOrderEngine(storage=self.storage)
        intent = CanonicalOrderIntent(
            session_date="20261019",
            ticker="IWM",
            side=PaperOrderSide.BUY,
            shares=8,
            order_type="LIMIT",
            limit_price=Decimal("200.00"),
            stop_loss=Decimal("190.00"),
        )
        order = engine.create_order(intent)
        order = engine.transition_order(order.order_id, PaperOrderState.PENDING_APPROVAL)
        order = engine.transition_order(order.order_id, PaperOrderState.APPROVED)
        order = engine.transition_order(order.order_id, PaperOrderState.SUBMITTED)

        # Fill 5 shares -> 3 remaining
        order, _ = engine.apply_fill_to_order(order.order_id, shares=5, price=Decimal("200.00"))
        self.assertEqual(order.shares_filled, 5)
        self.assertEqual(order.shares_remaining, 3)
        self.assertEqual(order.state, PaperOrderState.PARTIALLY_FILLED)

        # Attempt to fill 4 shares (exceeds remaining 3) -> rejected
        with self.assertRaises(ValueError) as cm:
            engine.apply_fill_to_order(order.order_id, shares=4, price=Decimal("200.00"))
        self.assertIn("exceeds order remaining quantity", str(cm.exception))

        # Fill exact remaining 3 shares -> FILLED
        order, _ = engine.apply_fill_to_order(order.order_id, shares=3, price=Decimal("200.00"))
        self.assertEqual(order.shares_filled, 8)
        self.assertEqual(order.shares_remaining, 0)
        self.assertEqual(order.state, PaperOrderState.FILLED)

    # -------------------------------------------------------------------------
    # 6. Duplicate Fill Protection & Cash Invariant (GAP-01)
    # -------------------------------------------------------------------------

    def test_10_duplicate_fill_replay_protection_prevents_double_deduction(self):
        """10. Verify replaying a fill event with same fill_id/idempotency_key does NOT deduct cash twice or create duplicate lot."""
        initial_status = self.service.get_status()
        self.assertEqual(initial_status["cash_usd"], "5000.00")
        self.assertEqual(initial_status["open_positions_count"], 0)

        idemp_key = "IDEMP-20261019-AAPL-TESTKEY001"
        fill_id = "FILL-IDEMP-20261019-AAPL-TESTKEY001"

        # First entry submission
        res1 = self.service.record_paper_entry(
            ticker="AAPL",
            shares=5,
            price="150.00",
            stop_loss="145.00",
            timestamp=self.valid_trading_ts,
            paper_only=True,
            reference_now=self.valid_reference_now,
            idempotency_key=idemp_key,
            fill_id=fill_id,
        )
        self.assertEqual(res1["status"], "RECORDED")
        self.assertEqual(res1["fill_id"], fill_id)
        self.assertFalse(res1.get("idempotent_replay", False))

        status_after_first = self.service.get_status()
        self.assertEqual(status_after_first["cash_usd"], "4250.00")  # 5000 - (5 * 150) = 4250
        self.assertEqual(status_after_first["open_positions_count"], 1)

        # Second entry submission with exact same fill_id and parameters
        res2 = self.service.record_paper_entry(
            ticker="AAPL",
            shares=5,
            price="150.00",
            stop_loss="145.00",
            timestamp=self.valid_trading_ts,
            paper_only=True,
            reference_now=self.valid_reference_now,
            idempotency_key=idemp_key,
            fill_id=fill_id,
        )
        self.assertEqual(res2["status"], "RECORDED")
        self.assertEqual(res2["fill_id"], fill_id)
        self.assertTrue(res2.get("idempotent_replay", False))

        # Assert cash and positions are UNCHANGED (no double deduction, no second lot)
        status_after_replay = self.service.get_status()
        self.assertEqual(status_after_replay["cash_usd"], "4250.00")
        self.assertEqual(status_after_replay["open_positions_count"], 1)
        self.assertTrue(status_after_replay["equity_invariant_holds"])

        # Check orders registry has only 1 fill
        orders_data = self.storage.load_orders()
        self.assertEqual(len(orders_data["fills"]), 1)

    def test_11_conflicting_fill_identity_rejected(self):
        """11. Verify replaying an existing fill ID with conflicting parameters is rejected."""
        idemp_key = "IDEMP-20261019-MSFT-TESTKEY002"
        fill_id = "FILL-IDEMP-20261019-MSFT-TESTKEY002"

        # First entry: 4 shares @ $200
        self.service.record_paper_entry(
            ticker="MSFT",
            shares=4,
            price="200.00",
            stop_loss="195.00",
            timestamp=self.valid_trading_ts,
            paper_only=True,
            reference_now=self.valid_reference_now,
            idempotency_key=idemp_key,
            fill_id=fill_id,
        )

        # Attempt reuse with 5 shares (conflicting quantity)
        with self.assertRaises(ConflictingFillIdentityError) as cm:
            self.service.record_paper_entry(
                ticker="MSFT",
                shares=5,  # Conflict: was 4
                price="200.00",
                stop_loss="195.00",
                timestamp=self.valid_trading_ts,
                paper_only=True,
                reference_now=self.valid_reference_now,
                idempotency_key=idemp_key,
                fill_id=fill_id,
            )
        self.assertIn("Conflicting fill identity", str(cm.exception))

    # -------------------------------------------------------------------------
    # 7. Persistence Round-Trip & Audit Integrity
    # -------------------------------------------------------------------------

    def test_12_persistence_round_trip_independent_instances(self):
        """12. Verify orders and transitions reload accurately from disk on clean instance."""
        engine = PaperOrderEngine(storage=self.storage)
        intent = CanonicalOrderIntent(
            session_date="20261019",
            ticker="AMZN",
            side=PaperOrderSide.BUY,
            shares=6,
            order_type="LIMIT",
            limit_price=Decimal("180.00"),
            stop_loss=Decimal("170.00"),
            target=Decimal("200.00"),
            client_tag="TAG-12345",
        )
        order = engine.create_order(intent)
        order = engine.transition_order(order.order_id, PaperOrderState.PENDING_APPROVAL)
        order = engine.transition_order(order.order_id, PaperOrderState.APPROVED)
        order = engine.transition_order(order.order_id, PaperOrderState.SUBMITTED)
        order, fill_id = engine.apply_fill_to_order(order.order_id, shares=2, price=Decimal("180.00"))

        # Spawn clean independent storage and engine
        fresh_storage = PaperStorageManager(state_dir=self.external_state_dir)
        fresh_engine = PaperOrderEngine(storage=fresh_storage)

        reloaded = fresh_engine.get_order(order.order_id)
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.order_id, order.order_id)
        self.assertEqual(reloaded.idempotency_key, order.idempotency_key)
        self.assertEqual(reloaded.state, PaperOrderState.PARTIALLY_FILLED)
        self.assertEqual(reloaded.shares_initial, 6)
        self.assertEqual(reloaded.shares_filled, 2)
        self.assertEqual(reloaded.shares_remaining, 4)
        self.assertEqual(reloaded.client_order_id, "TAG-12345")
        self.assertEqual(len(reloaded.transition_history), 5)  # PROPOSED, PENDING_APPROVAL, APPROVED, SUBMITTED, PARTIALLY_FILLED
        self.assertEqual(reloaded.fill_ids, [fill_id])

        # Test lookup by idempotency key
        reloaded_by_key = fresh_engine.get_order_by_idempotency_key(order.idempotency_key)
        self.assertIsNotNone(reloaded_by_key)
        self.assertEqual(reloaded_by_key.order_id, order.order_id)

    # -------------------------------------------------------------------------
    # 8. Backward Compatibility with Phase 6.1 Manual Workflow
    # -------------------------------------------------------------------------

    def test_13_backward_compatibility_with_manual_entry_and_exit(self):
        """13. Verify manual entry without explicit order ID or idempotency key still functions seamlessly."""
        # Risk limit: 0.50% of $5000 = $25.00. For stop $140, risk/sh = $10.00 -> max 2 shares ($20.00 risk)
        res_entry = self.service.record_paper_entry(
            ticker="GOOGL",
            shares=2,
            price="150.00",
            stop_loss="140.00",
            timestamp=self.valid_trading_ts,
            paper_only=True,
            reference_now=self.valid_reference_now,
        )
        self.assertEqual(res_entry["status"], "RECORDED")
        self.assertTrue(res_entry["fill_id"].startswith("FILL-"))
        self.assertIsNone(res_entry.get("order_id"))

        res_exit = self.service.record_paper_exit(
            ticker="GOOGL",
            shares=2,
            price="160.00",
            reason="TAKE_PROFIT",
            timestamp=self.valid_trading_ts,
            paper_only=True,
            reference_now=self.valid_reference_now,
        )
        self.assertEqual(res_exit["status"], "RECORDED")
        self.assertTrue(res_exit["exit_id"].startswith("EXIT-"))

        status = self.service.get_status()
        self.assertEqual(status["cash_usd"], "5020.00")
        self.assertEqual(status["open_positions_count"], 0)
        self.assertTrue(status["equity_invariant_holds"])

    # -------------------------------------------------------------------------
    # 9. Exhaustive State Matrix & Invariant Verification
    # -------------------------------------------------------------------------

    def test_14_exhaustive_legal_transition_matrix(self):
        """14. Systematically verify every permitted transition defined in LEGAL_ORDER_TRANSITIONS."""
        engine = PaperOrderEngine(storage=self.storage)

        def make_base_order(ticker: str) -> PaperOrder:
            intent = CanonicalOrderIntent(
                session_date="20261019",
                ticker=ticker,
                side=PaperOrderSide.BUY,
                shares=10,
                order_type="LIMIT",
                limit_price=Decimal("100.00"),
                stop_loss=Decimal("95.00"),
            )
            return engine.create_order(intent)

        # Iterate through all legal transition paths
        # 1. PROPOSED -> PENDING_APPROVAL, REJECTED, CANCELLED
        for target in (PaperOrderState.PENDING_APPROVAL, PaperOrderState.REJECTED, PaperOrderState.CANCELLED):
            o = make_base_order(f"P{target.value[:3]}")
            engine.transition_order(o.order_id, target, reason="Legal test")
            self.assertEqual(engine.get_order(o.order_id).state, target)

        # 2. PENDING_APPROVAL -> APPROVED, REJECTED, CANCELLED, EXPIRED
        for target in (PaperOrderState.APPROVED, PaperOrderState.REJECTED, PaperOrderState.CANCELLED, PaperOrderState.EXPIRED):
            o = make_base_order(f"PA{target.value[:3]}")
            engine.transition_order(o.order_id, PaperOrderState.PENDING_APPROVAL)
            engine.transition_order(o.order_id, target, reason="Legal test")
            self.assertEqual(engine.get_order(o.order_id).state, target)

        # 3. APPROVED -> SUBMITTED, CANCELLED, EXPIRED
        for target in (PaperOrderState.SUBMITTED, PaperOrderState.CANCELLED, PaperOrderState.EXPIRED):
            o = make_base_order(f"AP{target.value[:3]}")
            engine.transition_order(o.order_id, PaperOrderState.PENDING_APPROVAL)
            engine.transition_order(o.order_id, PaperOrderState.APPROVED)
            engine.transition_order(o.order_id, target, reason="Legal test")
            self.assertEqual(engine.get_order(o.order_id).state, target)

        # 4. SUBMITTED -> CANCELLED, EXPIRED, FAILED
        for target in (PaperOrderState.CANCELLED, PaperOrderState.EXPIRED, PaperOrderState.FAILED):
            o = make_base_order(f"SB{target.value[:3]}")
            engine.transition_order(o.order_id, PaperOrderState.PENDING_APPROVAL)
            engine.transition_order(o.order_id, PaperOrderState.APPROVED)
            engine.transition_order(o.order_id, PaperOrderState.SUBMITTED)
            engine.transition_order(o.order_id, target, reason="Legal test")
            self.assertEqual(engine.get_order(o.order_id).state, target)

    def test_15_exhaustive_illegal_transition_matrix(self):
        """15. Systematically verify that every disallowed transition is blocked."""
        engine = PaperOrderEngine(storage=self.storage)

        def make_base_order(ticker: str) -> PaperOrder:
            intent = CanonicalOrderIntent(
                session_date="20261019",
                ticker=ticker,
                side=PaperOrderSide.BUY,
                shares=10,
                order_type="LIMIT",
                limit_price=Decimal("100.00"),
                stop_loss=Decimal("95.00"),
            )
            return engine.create_order(intent)

        # Check all active states and their disallowed targets
        for current_state in (PaperOrderState.PROPOSED, PaperOrderState.PENDING_APPROVAL, PaperOrderState.APPROVED, PaperOrderState.SUBMITTED):
            allowed = LEGAL_ORDER_TRANSITIONS[current_state]
            disallowed = [s for s in PaperOrderState if s not in allowed]

            for bad_target in disallowed:
                sym = f"X{current_state.value[:2]}{bad_target.value[:2]}"
                o = make_base_order(sym)

                # Advance to current_state
                if current_state == PaperOrderState.PENDING_APPROVAL:
                    engine.transition_order(o.order_id, PaperOrderState.PENDING_APPROVAL)
                elif current_state == PaperOrderState.APPROVED:
                    engine.transition_order(o.order_id, PaperOrderState.PENDING_APPROVAL)
                    engine.transition_order(o.order_id, PaperOrderState.APPROVED)
                elif current_state == PaperOrderState.SUBMITTED:
                    engine.transition_order(o.order_id, PaperOrderState.PENDING_APPROVAL)
                    engine.transition_order(o.order_id, PaperOrderState.APPROVED)
                    engine.transition_order(o.order_id, PaperOrderState.SUBMITTED)

                with self.assertRaises((IllegalStateTransitionError, TerminalOrderStateError)):
                    engine.transition_order(o.order_id, bad_target)

    def test_16_exhaustive_terminal_state_lockout(self):
        """16. Verify each of the 5 terminal states blocks 100% of transitions to all 10 states."""
        engine = PaperOrderEngine(storage=self.storage)

        def setup_terminal_order(terminal_state: PaperOrderState, sym: str) -> PaperOrder:
            intent = CanonicalOrderIntent(
                session_date="20261019",
                ticker=sym,
                side=PaperOrderSide.BUY,
                shares=5,
                order_type="LIMIT",
                limit_price=Decimal("100.00"),
                stop_loss=Decimal("95.00"),
            )
            o = engine.create_order(intent)
            if terminal_state == PaperOrderState.REJECTED:
                return engine.transition_order(o.order_id, PaperOrderState.REJECTED)
            elif terminal_state == PaperOrderState.CANCELLED:
                return engine.transition_order(o.order_id, PaperOrderState.CANCELLED)
            elif terminal_state == PaperOrderState.EXPIRED:
                engine.transition_order(o.order_id, PaperOrderState.PENDING_APPROVAL)
                return engine.transition_order(o.order_id, PaperOrderState.EXPIRED)
            elif terminal_state == PaperOrderState.FAILED:
                engine.transition_order(o.order_id, PaperOrderState.PENDING_APPROVAL)
                engine.transition_order(o.order_id, PaperOrderState.APPROVED)
                engine.transition_order(o.order_id, PaperOrderState.SUBMITTED)
                return engine.transition_order(o.order_id, PaperOrderState.FAILED)
            elif terminal_state == PaperOrderState.FILLED:
                engine.transition_order(o.order_id, PaperOrderState.PENDING_APPROVAL)
                engine.transition_order(o.order_id, PaperOrderState.APPROVED)
                engine.transition_order(o.order_id, PaperOrderState.SUBMITTED)
                ord_filled, _ = engine.apply_fill_to_order(o.order_id, shares=5, price=Decimal("100.00"))
                return ord_filled
            raise ValueError(f"Unknown terminal: {terminal_state}")

        for term_state in TERMINAL_ORDER_STATES:
            sym = f"T{term_state.value[:3]}"
            ord_obj = setup_terminal_order(term_state, sym)
            self.assertTrue(ord_obj.is_terminal)

            for target in PaperOrderState:
                with self.assertRaises(TerminalOrderStateError):
                    engine.transition_order(ord_obj.order_id, target)

    def test_17_deterministic_fill_identity_helpers(self):
        """17. Verify deterministic fill identity calculations are reproducible and unique."""
        # 1. Order fill identity
        f1 = compute_fill_identity("ORD-001", 1, 5, Decimal("150.00"), "2026-10-19T14:15:00+00:00")
        f2 = compute_fill_identity("ORD-001", 1, 5, Decimal("150.00"), "2026-10-19T14:15:00+00:00")
        self.assertEqual(f1, f2)
        self.assertTrue(f1.startswith("FILL-ORD-001-01-"))

        # Different index
        f_diff_idx = compute_fill_identity("ORD-001", 2, 5, Decimal("150.00"), "2026-10-19T14:15:00+00:00")
        self.assertNotEqual(f1, f_diff_idx)

        # 2. Manual fill identity
        mf1 = compute_manual_fill_identity("20261019", "AAPL", 10, Decimal("150.00"), Decimal("145.00"), "2026-10-19T14:15:00+00:00")
        mf2 = compute_manual_fill_identity("2026-10-19", "aapl", 10, Decimal("150.00"), Decimal("145.00"), "2026-10-19T14:15:00+00:00")
        self.assertEqual(mf1, mf2)
        self.assertTrue(mf1.startswith("FILL-MANUAL-20261019-AAPL-"))

    def test_18_atomic_order_persistence_and_concurrent_simulation(self):
        """18. Verify rapid sequential / simulated duplicate submissions maintain 100% order integrity."""
        engine = PaperOrderEngine(storage=self.storage)
        intent = CanonicalOrderIntent(
            session_date="20261019",
            ticker="IBM",
            side=PaperOrderSide.BUY,
            shares=5,
            order_type="LIMIT",
            limit_price=Decimal("140.00"),
            stop_loss=Decimal("135.00"),
        )

        orders = [engine.create_order(intent) for _ in range(10)]
        self.assertEqual(len({o.order_id for o in orders}), 1)
        self.assertEqual(len({o.idempotency_key for o in orders}), 1)

        # Assert only 1 order exists in storage
        all_orders = engine.list_orders(ticker="IBM")
        self.assertEqual(len(all_orders), 1)


if __name__ == "__main__":
    unittest.main()
