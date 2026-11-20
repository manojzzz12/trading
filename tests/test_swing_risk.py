"""Unit and invariant test suite for trader-swing deterministic risk management and position sizing.

Verifies:
1. Initial portfolio risk metrics on empty $5,000 ledger.
2. Basic single-trade risk calculation breakdown.
3. Correct 1.25x gap-risk buffer applied strictly to price downside.
4. Transaction fees are added strictly once and not multiplied by 1.25x buffer.
5. Trailing stop above entry produces zero downside price risk.
6. Per-trade risk limit enforcement (0.5% max risk).
7. Aggregate portfolio risk limit enforcement (2.0% max risk).
8. Maximum allocation per ticker cap enforcement (25.0% max allocation).
9. Existing lots included in cumulative ticker allocation check.
10. Scale-in to existing ticker allowed within position and allocation limits.
11. Four-position limit rejecting 5th unique ticker.
12. Unique ticker counting permitting scale-in on 4th position.
13. Minimum cash reserve enforcement (20.0% minimum reserve).
14. Maximum invested capital enforcement (80.0% maximum GMV).
15. Entry fee impact reducing position sizing.
16. Exit fee impact reducing position sizing.
17. Per-share fee schedule calculation and sizing impact.
18. Basis-point fee schedule calculation and sizing impact.
19. Minimum fee schedule enforcement.
20. Fixed fee schedule enforcement.
21. Nonlinear fee combinations (min + per-share + fixed).
22. Insufficient cash rejection.
23. Invalid stop-loss and entry prices rejection.
24. Zero-share outcome when risk or capital constraints cannot fit 1 share.
25. Largest-valid-integer-quantity verification (Q passes, Q+1 fails).
26. Custom duplicate lot_id rejection across open lots.
27. Historical closed lot_id reuse rejection.
"""

from decimal import Decimal
import os
import sys
import unittest

SWING_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".agents",
    "skills",
    "trader-swing",
)
if SWING_DIR not in sys.path:
    sys.path.insert(0, SWING_DIR)

from accounting import (
    AccountingLedger,
    InvalidFinancialValue,
    InsufficientCashError,
    quantize_cent,
)
from risk import (
    FeeSchedule,
    FeeConfig,
    RiskConfig,
    SingleTradeRisk,
    PortfolioRiskMetrics,
    RiskVerdict,
    SizingResult,
    RiskEngine,
    RiskError,
    ConfigurationError,
)


class TestSwingRisk(unittest.TestCase):
    """Test suite covering deterministic risk management, fee schedules, and position sizing."""

    def setUp(self):
        self.ledger = AccountingLedger("5000.00")
        self.engine = RiskEngine()

    # 1. Initial portfolio risk
    def test_01_initial_portfolio_risk(self):
        metrics = self.engine.calculate_portfolio_risk(self.ledger)
        self.assertEqual(metrics.current_equity_usd, Decimal("5000.00"))
        self.assertEqual(metrics.current_cash_usd, Decimal("5000.00"))
        self.assertEqual(metrics.gross_market_value_usd, Decimal("0.00"))
        self.assertEqual(metrics.invested_capital_pct, Decimal("0.00"))
        self.assertEqual(metrics.open_positions_count, 0)
        self.assertEqual(metrics.total_open_lots_count, 0)
        self.assertEqual(metrics.total_remaining_entry_fees_usd, Decimal("0.00"))
        self.assertEqual(metrics.total_estimated_exit_fees_usd, Decimal("0.00"))
        self.assertEqual(metrics.total_unbuffered_open_risk_usd, Decimal("0.00"))
        self.assertEqual(metrics.total_buffered_open_risk_usd, Decimal("0.00"))
        self.assertEqual(metrics.aggregate_risk_pct, Decimal("0.00"))
        # 2% of $5,000 = $100.00
        self.assertEqual(metrics.available_risk_capacity_usd, Decimal("100.00"))

    # 2. Basic single-trade risk
    def test_02_basic_single_trade_risk(self):
        trade_risk = self.engine.calculate_single_trade_risk(
            entry_price=Decimal("100.00"),
            stop_loss_price=Decimal("98.00"),
            shares=10,
            estimated_entry_fee=Decimal("1.00"),
            estimated_exit_fee=Decimal("1.00"),
        )
        self.assertEqual(trade_risk.shares, 10)
        self.assertEqual(trade_risk.entry_price, Decimal("100.00"))
        self.assertEqual(trade_risk.stop_loss_price, Decimal("98.00"))
        # Downside per share = $2.00 * 10 sh = $20.00
        self.assertEqual(trade_risk.unbuffered_price_risk_usd, Decimal("20.00"))
        # 1.25x buffer = $20.00 * 1.25 = $25.00
        self.assertEqual(trade_risk.buffered_price_risk_usd, Decimal("25.00"))
        self.assertEqual(trade_risk.estimated_entry_fee_usd, Decimal("1.00"))
        self.assertEqual(trade_risk.estimated_exit_fee_usd, Decimal("1.00"))
        # Total = $25.00 + $1.00 + $1.00 = $27.00
        self.assertEqual(trade_risk.total_buffered_trade_risk_usd, Decimal("27.00"))

    # 3. Correct 1.25x price-risk buffer
    def test_03_correct_gap_price_risk_buffer(self):
        trade_risk = self.engine.calculate_single_trade_risk(
            entry_price=Decimal("150.00"),
            stop_loss_price=Decimal("140.00"),
            shares=5,
        )
        # Unbuffered = 5 * ($150 - $140) = $50.00
        self.assertEqual(trade_risk.unbuffered_price_risk_usd, Decimal("50.00"))
        # Buffered = $50.00 * 1.25 = $62.50
        self.assertEqual(trade_risk.buffered_price_risk_usd, Decimal("62.50"))
        self.assertEqual(trade_risk.total_buffered_trade_risk_usd, Decimal("62.50"))

    # 4. Fees not multiplied by gap buffer
    def test_04_fees_not_multiplied_by_gap_buffer(self):
        trade_risk = self.engine.calculate_single_trade_risk(
            entry_price=Decimal("50.00"),
            stop_loss_price=Decimal("48.00"),
            shares=10,
            estimated_entry_fee=Decimal("3.00"),
            estimated_exit_fee=Decimal("2.00"),
        )
        # Unbuffered downside = 10 * $2.00 = $20.00
        # Buffered downside = $20.00 * 1.25 = $25.00
        # Fees = $3.00 + $2.00 = $5.00
        # Total = $25.00 + $5.00 = $30.00
        self.assertEqual(trade_risk.total_buffered_trade_risk_usd, Decimal("30.00"))
        # If fees were erroneously multiplied by 1.25x: ($20 + $5) * 1.25 = $31.25
        self.assertNotEqual(trade_risk.total_buffered_trade_risk_usd, Decimal("31.25"))

    # 5. Stop above entry
    def test_05_stop_above_entry(self):
        # Single-trade calculation with trailing stop >= entry
        trade_risk = self.engine.calculate_single_trade_risk(
            entry_price=Decimal("100.00"),
            stop_loss_price=Decimal("105.00"),
            shares=10,
            estimated_entry_fee=Decimal("1.50"),
            estimated_exit_fee=Decimal("1.50"),
        )
        self.assertEqual(trade_risk.unbuffered_price_risk_usd, Decimal("0.00"))
        self.assertEqual(trade_risk.buffered_price_risk_usd, Decimal("0.00"))
        self.assertEqual(trade_risk.total_buffered_trade_risk_usd, Decimal("3.00"))

        # In portfolio metrics with trailing stop above entry price
        self.ledger.buy("AAPL", 10, Decimal("100.00"), fee=Decimal("1.00"))
        stops = {"AAPL": Decimal("105.00")}
        metrics = self.engine.calculate_portfolio_risk(self.ledger, current_stops=stops)
        # Price downside is 0.00; risk consists only of unamortized entry fee ($1.00) + exit fee ($0.00 default)
        self.assertEqual(metrics.total_unbuffered_open_risk_usd, Decimal("1.00"))
        self.assertEqual(metrics.total_buffered_open_risk_usd, Decimal("1.00"))

    # 6. Per-trade risk limit
    def test_06_per_trade_risk_limit(self):
        # 0.5% of $5,000 = $25.00 max risk per trade
        # Propose trade with buffered risk = 3 * ($100 - $90) * 1.25 = $37.50 > $25.00
        verdict = self.engine.evaluate_proposed_trade(
            self.ledger, "AAPL", 3, Decimal("100.00"), Decimal("90.00")
        )
        self.assertFalse(verdict.approved)
        self.assertEqual(verdict.binding_constraint, "TRADE_RISK_LIMIT_EXCEEDED")

        # Propose trade with buffered risk = 10 * ($100 - $98) * 1.25 = $25.00 <= $25.00
        verdict_ok = self.engine.evaluate_proposed_trade(
            self.ledger, "AAPL", 10, Decimal("100.00"), Decimal("98.00")
        )
        self.assertTrue(verdict_ok.approved)

    # 7. Aggregate portfolio risk limit
    def test_07_aggregate_portfolio_risk_limit(self):
        # Setup existing positions with buffered risk totaling $85.00
        # T1: 10 sh @ $30, stop $28 -> risk = 10 * $2 * 1.25 = $25.00, principal = $300
        # T2: 10 sh @ $30, stop $28 -> risk = $25.00, principal = $300
        # T3: 10 sh @ $30, stop $28 -> risk = $25.00, principal = $300
        # T1 lot 2: 4 sh @ $30, stop $28 -> risk = 4 * $2 * 1.25 = $10.00, principal = $120
        self.ledger.buy("T1", 10, Decimal("30.00"))
        self.ledger.buy("T2", 10, Decimal("30.00"))
        self.ledger.buy("T3", 10, Decimal("30.00"))
        self.ledger.buy("T1", 4, Decimal("30.00"))

        stops = {"T1": Decimal("28.00"), "T2": Decimal("28.00"), "T3": Decimal("28.00")}
        metrics = self.engine.calculate_portfolio_risk(self.ledger, current_stops=stops)
        self.assertEqual(metrics.total_buffered_open_risk_usd, Decimal("85.00"))
        self.assertEqual(metrics.available_risk_capacity_usd, Decimal("15.00"))

        # Propose trade with $25.00 risk on T4: 10 sh @ $30, stop $28
        # Post-trade aggregate risk = $85.00 + $25.00 = $110.00 > $100.00 (2.0% limit)
        verdict = self.engine.evaluate_proposed_trade(
            self.ledger, "T4", 10, Decimal("30.00"), Decimal("28.00"), current_stops=stops
        )
        self.assertFalse(verdict.approved)
        self.assertEqual(verdict.binding_constraint, "AGGREGATE_RISK_LIMIT_EXCEEDED")

    # 8. Ticker allocation cap
    def test_08_ticker_allocation_cap(self):
        # 25% of $5,000 = $1,250.00 max ticker allocation
        # Propose trade of 13 shares @ $100.00 = $1,300.00 > $1,250.00
        verdict = self.engine.evaluate_proposed_trade(
            self.ledger, "MSFT", 13, Decimal("100.00"), Decimal("99.00")
        )
        self.assertFalse(verdict.approved)
        self.assertEqual(verdict.binding_constraint, "TICKER_ALLOCATION_LIMIT_EXCEEDED")

        # Propose trade of 12 shares @ $100.00 = $1,200.00 <= $1,250.00
        verdict_ok = self.engine.evaluate_proposed_trade(
            self.ledger, "MSFT", 12, Decimal("100.00"), Decimal("99.00")
        )
        self.assertTrue(verdict_ok.approved)

    # 9. Existing lots included in ticker allocation
    def test_09_existing_lots_included_in_ticker_allocation(self):
        # Buy 8 shares of MSFT @ $100.00 = $800.00
        self.ledger.buy("MSFT", 8, Decimal("100.00"))

        # Propose buying 5 more shares @ $100.00 = $500.00 (total $1,300.00 > $1,250.00)
        verdict = self.engine.evaluate_proposed_trade(
            self.ledger, "MSFT", 5, Decimal("100.00"), Decimal("99.00")
        )
        self.assertFalse(verdict.approved)
        self.assertEqual(verdict.binding_constraint, "TICKER_ALLOCATION_LIMIT_EXCEEDED")

        # Propose buying 4 more shares @ $100.00 = $400.00 (total $1,200.00 <= $1,250.00)
        verdict_ok = self.engine.evaluate_proposed_trade(
            self.ledger, "MSFT", 4, Decimal("100.00"), Decimal("99.00")
        )
        self.assertTrue(verdict_ok.approved)

    # 10. Scale-in to existing ticker
    def test_10_scale_in_to_existing_ticker(self):
        # Initial lot of 5 shares of AAPL @ $100.00 = $500.00
        self.ledger.buy("AAPL", 5, Decimal("100.00"))

        # Scale in with another 5 shares @ $100.00, stop $96.00 (risk = 5 * $4 * 1.25 = $25.00)
        # Total exposure = $1,000.00 <= $1,250.00; cash = $4,000.00 >= $1,000.00
        verdict = self.engine.evaluate_proposed_trade(
            self.ledger, "AAPL", 5, Decimal("100.00"), Decimal("96.00")
        )
        self.assertTrue(verdict.approved)
        self.assertIsNone(verdict.binding_constraint)

    # 11. Four-position limit
    def test_11_four_position_limit(self):
        # Open 4 distinct tickers
        self.ledger.buy("T1", 5, Decimal("50.00"))
        self.ledger.buy("T2", 5, Decimal("50.00"))
        self.ledger.buy("T3", 5, Decimal("50.00"))
        self.ledger.buy("T4", 5, Decimal("50.00"))

        # Attempt to open a 5th ticker
        verdict = self.engine.evaluate_proposed_trade(
            self.ledger, "T5", 5, Decimal("50.00"), Decimal("48.00")
        )
        self.assertFalse(verdict.approved)
        self.assertEqual(verdict.binding_constraint, "MAX_POSITIONS_EXCEEDED")

    # 12. Unique ticker counting
    def test_12_unique_ticker_counting(self):
        # Open 4 distinct tickers
        self.ledger.buy("T1", 5, Decimal("50.00"))
        self.ledger.buy("T2", 5, Decimal("50.00"))
        self.ledger.buy("T3", 5, Decimal("50.00"))
        self.ledger.buy("T4", 5, Decimal("50.00"))

        # Scaling in to T1 (already open) must NOT consume another slot
        verdict = self.engine.evaluate_proposed_trade(
            self.ledger, "T1", 5, Decimal("50.00"), Decimal("48.00")
        )
        self.assertTrue(verdict.approved)

    # 13. Minimum cash reserve
    def test_13_minimum_cash_reserve(self):
        # Equity = $5,000. Minimum cash reserve = 20% = $1,000.00.
        # Deploy $3,800 into positions: cash becomes $1,200.00.
        self.ledger.buy("T1", 19, Decimal("100.00"))
        self.ledger.buy("T2", 19, Decimal("100.00"))
        self.assertEqual(self.ledger.cash_usd, Decimal("1200.00"))

        # Propose trade requiring $300.00. Post-trade cash = $900.00 < $1,000.00
        verdict = self.engine.evaluate_proposed_trade(
            self.ledger, "T3", 3, Decimal("100.00"), Decimal("98.00")
        )
        self.assertFalse(verdict.approved)
        self.assertEqual(verdict.binding_constraint, "CASH_RESERVE_VIOLATED")

    # 14. Maximum invested capital
    def test_14_maximum_invested_capital(self):
        # Equity = $5,000. Max invested capital = 80% = $4,000.00.
        # Deploy $3,800.00 into GMV.
        self.ledger.buy("T1", 19, Decimal("100.00"))
        self.ledger.buy("T2", 19, Decimal("100.00"))

        # Propose trade with principal $300.00. Post-trade GMV = $4,100.00 > $4,000.00
        verdict = self.engine.evaluate_proposed_trade(
            self.ledger, "T3", 3, Decimal("100.00"), Decimal("98.00")
        )
        self.assertFalse(verdict.approved)
        self.assertIn(verdict.binding_constraint, ["CASH_RESERVE_VIOLATED", "MAX_INVESTED_CAPITAL_EXCEEDED"])

    # 15. Entry fee impact on position sizing
    def test_15_entry_fee_impact_on_position_sizing(self):
        # Zero fees: entry $100, stop $98 -> downside $2.50/sh.
        # Max trade risk = $25.00 -> 10 shares.
        zero_res = self.engine.size_position(
            self.ledger, "AAPL", Decimal("100.00"), Decimal("98.00")
        )
        self.assertEqual(zero_res.shares, 10)

        # Config with $5.00 fixed entry fee
        fee_cfg = FeeConfig(entry=FeeSchedule(fixed=Decimal("5.00")), exit=FeeSchedule())
        fee_engine = RiskEngine(fee_config=fee_cfg)
        fee_res = fee_engine.size_position(
            self.ledger, "AAPL", Decimal("100.00"), Decimal("98.00")
        )
        # Remaining risk budget = $25.00 - $5.00 = $20.00 -> 20 / 2.50 = 8 shares
        self.assertEqual(fee_res.shares, 8)
        self.assertEqual(fee_res.estimated_entry_fee, Decimal("5.00"))

    # 16. Exit fee impact on position sizing
    def test_16_exit_fee_impact_on_position_sizing(self):
        # Config with $5.00 fixed exit fee
        fee_cfg = FeeConfig(entry=FeeSchedule(), exit=FeeSchedule(fixed=Decimal("5.00")))
        fee_engine = RiskEngine(fee_config=fee_cfg)
        fee_res = fee_engine.size_position(
            self.ledger, "AAPL", Decimal("100.00"), Decimal("98.00")
        )
        # Remaining risk budget = $25.00 - $5.00 = $20.00 -> 8 shares
        self.assertEqual(fee_res.shares, 8)
        self.assertEqual(fee_res.estimated_exit_fee, Decimal("5.00"))

    # 17. Per-share fee schedule
    def test_17_per_share_fee_schedule(self):
        sched = FeeSchedule(per_share=Decimal("0.05"))
        fee_10 = sched.calculate_fee(10, Decimal("100.00"))
        self.assertEqual(fee_10, Decimal("0.50"))

        fee_100 = sched.calculate_fee(100, Decimal("100.00"))
        self.assertEqual(fee_100, Decimal("5.00"))

    # 18. Basis-point fee schedule
    def test_18_basis_point_fee_schedule(self):
        sched = FeeSchedule(basis_points=Decimal("10.00"))  # 10 bps = 0.10%
        # 100 shares @ $50.00 = $5,000.00 -> 0.10% = $5.00
        fee = sched.calculate_fee(100, Decimal("50.00"))
        self.assertEqual(fee, Decimal("5.00"))

    # 19. Minimum fee schedule
    def test_19_minimum_fee_schedule(self):
        sched = FeeSchedule(per_share=Decimal("0.005"), minimum=Decimal("1.00"))
        # 10 shares: variable fee = $0.05 -> minimum $1.00 applies
        self.assertEqual(sched.calculate_fee(10, Decimal("100.00")), Decimal("1.00"))
        # 500 shares: variable fee = $2.50 -> exceeds minimum, $2.50 applies
        self.assertEqual(sched.calculate_fee(500, Decimal("100.00")), Decimal("2.50"))

    # 20. Fixed fee schedule
    def test_20_fixed_fee_schedule(self):
        sched = FeeSchedule(fixed=Decimal("2.50"))
        self.assertEqual(sched.calculate_fee(1, Decimal("100.00")), Decimal("2.50"))
        self.assertEqual(sched.calculate_fee(100, Decimal("100.00")), Decimal("2.50"))

    # 21. Nonlinear fee combinations
    def test_21_nonlinear_fee_combinations(self):
        # per_share $0.01, min $1.50, fixed $1.00
        sched = FeeSchedule(
            per_share=Decimal("0.01"),
            minimum=Decimal("1.50"),
            fixed=Decimal("1.00"),
        )
        # 10 shares: var = $0.10 -> max(1.50, 0.10) + 1.00 = $2.50
        self.assertEqual(sched.calculate_fee(10, Decimal("50.00")), Decimal("2.50"))
        # 200 shares: var = $2.00 -> max(1.50, 2.00) + 1.00 = $3.00
        self.assertEqual(sched.calculate_fee(200, Decimal("50.00")), Decimal("3.00"))

    # 22. Insufficient cash
    def test_22_insufficient_cash(self):
        # Ledger initialized with only $50.00 cash
        small_ledger = AccountingLedger("50.00")
        verdict = self.engine.evaluate_proposed_trade(
            small_ledger, "AAPL", 1, Decimal("100.00"), Decimal("95.00")
        )
        self.assertFalse(verdict.approved)
        self.assertEqual(verdict.binding_constraint, "INSUFFICIENT_CASH")

    # 23. Invalid stop-loss and entry prices
    def test_23_invalid_stop_loss_and_entry_prices(self):
        # Stop loss >= entry price
        v1 = self.engine.evaluate_proposed_trade(
            self.ledger, "AAPL", 10, Decimal("100.00"), Decimal("100.00")
        )
        self.assertFalse(v1.approved)
        self.assertEqual(v1.binding_constraint, "INVALID_STOP_DISTANCE")

        v2 = self.engine.evaluate_proposed_trade(
            self.ledger, "AAPL", 10, Decimal("100.00"), Decimal("105.00")
        )
        self.assertFalse(v2.approved)
        self.assertEqual(v2.binding_constraint, "INVALID_STOP_DISTANCE")

        # Zero or negative prices
        v3 = self.engine.evaluate_proposed_trade(
            self.ledger, "AAPL", 10, Decimal("0.00"), Decimal("90.00")
        )
        self.assertFalse(v3.approved)
        self.assertEqual(v3.binding_constraint, "INVALID_PRICE")

        v4 = self.engine.evaluate_proposed_trade(
            self.ledger, "AAPL", 10, Decimal("100.00"), Decimal("-5.00")
        )
        self.assertFalse(v4.approved)
        self.assertEqual(v4.binding_constraint, "INVALID_STOP")

        # Invalid float / NaN input
        with self.assertRaises(InvalidFinancialValue):
            self.engine.evaluate_proposed_trade(
                self.ledger, "AAPL", 10, float("nan"), Decimal("90.00")
            )

    # 24. Zero-share outcome
    def test_24_zero_share_outcome(self):
        # Stop is so wide that even 1 share exceeds $25.00 trade risk budget
        # Entry $100.00, stop $70.00 -> 1 share price risk = $30.00 * 1.25 = $37.50 > $25.00
        res = self.engine.size_position(
            self.ledger, "AAPL", Decimal("100.00"), Decimal("70.00")
        )
        self.assertEqual(res.shares, 0)
        self.assertFalse(res.approved)
        self.assertEqual(res.binding_constraint, "TRADE_RISK_LIMIT_EXCEEDED")

    # 25. Largest-valid-integer-quantity verification
    def test_25_largest_valid_integer_quantity(self):
        # Sizing result for entry $100.00, stop $98.00
        res = self.engine.size_position(
            self.ledger, "AAPL", Decimal("100.00"), Decimal("98.00")
        )
        self.assertTrue(res.approved)
        q = res.shares
        self.assertGreater(q, 0)

        # Assert Q passes all constraints
        v_q = self.engine.evaluate_proposed_trade(
            self.ledger, "AAPL", q, Decimal("100.00"), Decimal("98.00")
        )
        self.assertTrue(v_q.approved)

        # Assert Q + 1 fails at least one constraint
        v_q_plus_1 = self.engine.evaluate_proposed_trade(
            self.ledger, "AAPL", q + 1, Decimal("100.00"), Decimal("98.00")
        )
        self.assertFalse(v_q_plus_1.approved)
        self.assertEqual(res.binding_constraint, v_q_plus_1.binding_constraint)

    # 26. Custom duplicate lot_id rejection
    def test_26_custom_duplicate_lot_id_rejection(self):
        # Create lot with custom lot ID
        self.ledger.buy("AAPL", 10, Decimal("100.00"), lot_id="MY-LOT-001")

        # Second buy with same custom lot ID must be rejected across open lots
        with self.assertRaises(InvalidFinancialValue) as ctx:
            self.ledger.buy("AAPL", 5, Decimal("102.00"), lot_id="MY-LOT-001")
        self.assertIn("Duplicate lot ID 'MY-LOT-001' is not permitted", str(ctx.exception))

        # Rejection must also occur across different tickers
        with self.assertRaises(InvalidFinancialValue) as ctx2:
            self.ledger.buy("MSFT", 5, Decimal("200.00"), lot_id="MY-LOT-001")
        self.assertIn("Duplicate lot ID 'MY-LOT-001' is not permitted", str(ctx2.exception))

    # 27. Historical closed lot_id reuse rejection
    def test_27_historical_closed_lot_id_reuse_rejection(self):
        # Create lot with custom lot ID
        self.ledger.buy("AAPL", 10, Decimal("100.00"), lot_id="HIST-LOT-999")

        # Fully exit the lot so it becomes CLOSED
        self.ledger.sell("AAPL", 10, Decimal("110.00"), lot_id="HIST-LOT-999")
        pos = self.ledger.positions["AAPL"]
        self.assertEqual(pos.total_shares, 0)
        self.assertEqual(pos.lots[0].status, "CLOSED")

        # Attempt to reuse the closed lot ID in a new buy
        with self.assertRaises(InvalidFinancialValue) as ctx:
            self.ledger.buy("AAPL", 5, Decimal("115.00"), lot_id="HIST-LOT-999")
        self.assertIn("Duplicate lot ID 'HIST-LOT-999' is not permitted", str(ctx.exception))
        self.assertIn("CLOSED", str(ctx.exception))

    # 28. Ticker allocation uses market value (principal) and excludes entry fees
    def test_28_ticker_allocation_uses_principal_not_fees(self):
        # Equity = $5,000. 25% ticker cap = $1,250.00.
        # Existing MSFT position: 12 shares @ $100.00 = $1,200.00 market value.
        self.ledger.buy("MSFT", 12, Decimal("100.00"))

        # Configure $10.00 fixed entry fee and $2.00 exit fee
        fee_cfg = FeeConfig(
            entry=FeeSchedule(fixed=Decimal("10.00")),
            exit=FeeSchedule(fixed=Decimal("2.00")),
        )
        engine = RiskEngine(fee_config=fee_cfg)

        # Propose buying 5 shares of MSFT @ $10.00, stop $9.00
        # Proposed principal = 5 * $10.00 = $50.00.
        # Total capital required = $50.00 + $10.00 fee = $60.00.
        # Post-trade ticker exposure = existing $1,200.00 + proposed principal $50.00 = $1,250.00 (EXACTLY matches 25% cap).
        verdict = engine.evaluate_proposed_trade(
            self.ledger, "MSFT", 5, Decimal("10.00"), Decimal("9.00")
        )
        self.assertTrue(verdict.approved)
        # Verify entry fees do NOT increase post-trade ticker exposure:
        self.assertEqual(verdict.details["post_trade_ticker_exposure"], "1250.00")
        # Verify entry fees DO reduce post-trade cash:
        # Cash was $5000 - $1200 = $3800. Post-trade cash = $3800 - $60 = $3740.00.
        self.assertEqual(verdict.details["post_trade_cash"], "3740.00")
        # Verify entry and exit fees DO count toward buffered trade risk:
        # Downside price risk = 5 * ($10 - $9) * 1.25 = $6.25.
        # Fees = $10.00 entry + $2.00 exit = $12.00. Total buffered risk = $6.25 + $12.00 = $18.25.
        self.assertEqual(verdict.details["buffered_trade_risk"], "18.25")

        # Now propose 6 shares @ $10.00: principal = $60.00.
        # Post-trade exposure = $1,200 + $60 = $1,260.00 > $1,250.00 -> rejected by TICKER_ALLOCATION_LIMIT_EXCEEDED
        verdict_exceeded = engine.evaluate_proposed_trade(
            self.ledger, "MSFT", 6, Decimal("10.00"), Decimal("9.00")
        )
        self.assertFalse(verdict_exceeded.approved)
        self.assertEqual(verdict_exceeded.binding_constraint, "TICKER_ALLOCATION_LIMIT_EXCEEDED")

    # 29. Sizer and trade evaluator consistency under fees
    def test_29_sizer_and_evaluator_consistency_under_fees(self):
        # Configure non-linear fee schedule
        fee_cfg = FeeConfig(
            entry=FeeSchedule(per_share=Decimal("0.02"), minimum=Decimal("1.50"), fixed=Decimal("1.00")),
            exit=FeeSchedule(per_share=Decimal("0.01"), minimum=Decimal("1.00")),
        )
        engine = RiskEngine(fee_config=fee_cfg)

        # Sizing on AAPL: entry $50.00, stop $48.00
        res = engine.size_position(self.ledger, "AAPL", Decimal("50.00"), Decimal("48.00"))
        self.assertTrue(res.approved)
        q = res.shares
        self.assertGreater(q, 0)

        # 1. Sizer Q is approved by evaluator
        v_q = engine.evaluate_proposed_trade(self.ledger, "AAPL", q, Decimal("50.00"), Decimal("48.00"))
        self.assertTrue(v_q.approved)

        # 2. Sizer Q + 1 is rejected by evaluator
        v_next = engine.evaluate_proposed_trade(self.ledger, "AAPL", q + 1, Decimal("50.00"), Decimal("48.00"))
        self.assertFalse(v_next.approved)

        # 3. Sizer and trade evaluator agree on the binding constraint at Q + 1
        self.assertEqual(res.binding_constraint, v_next.binding_constraint)


if __name__ == "__main__":
    unittest.main()
