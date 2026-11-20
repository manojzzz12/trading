"""Unit and invariant test suite for trader-swing core precision accounting.

Verifies:
1. Initial $5,000 portfolio state.
2. Buy transaction and entry fee deduction.
3. Full position exit.
4. Partial position exit.
5. Multiple lots with FIFO default execution.
6. Entry fee residual allocation without penny loss.
7. Realized trading P&L calculation.
8. Mark-to-market unrealized trading P&L.
9. Dividend ex-date accrual (receivable and income).
10. Dividend payable-date settlement into cash.
11. Dividend payment after position closure.
12. Reverse split with no fractional entitlement.
13. 1-for-10 reverse split with 15 shares.
14. 1-for-3 reverse split with 10 shares.
15. Reverse split closing an entire position.
16. Recurring fractional entitlement with 7 shares in a 1-for-3 split.
17. Continuous accounting invariant assertion across all transactions.
18. Rejection of invalid Decimal, float, NaN, and negative financial inputs.
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
    AccountingInvariantViolation,
    InvalidFinancialValue,
    InsufficientCashError,
    PositionNotFoundError,
    LotNotFoundError,
    DividendError,
    to_decimal,
    quantize_cent,
)


class TestSwingAccounting(unittest.TestCase):
    """Comprehensive test cases for AccountingLedger and mathematical invariants."""

    def test_01_initial_portfolio_state(self):
        """1. Initial $5,000 portfolio."""
        ledger = AccountingLedger(initial_capital="5000.00")
        self.assertEqual(ledger.initial_capital_usd, Decimal("5000.00"))
        self.assertEqual(ledger.cash_usd, Decimal("5000.00"))
        self.assertEqual(ledger.gross_market_value_usd, Decimal("0.00"))
        self.assertEqual(ledger.dividends_receivable_usd, Decimal("0.00"))
        self.assertEqual(ledger.cumulative_dividend_income_usd, Decimal("0.00"))
        self.assertEqual(ledger.cumulative_realized_trading_pnl_usd, Decimal("0.00"))
        self.assertEqual(ledger.unrealized_trading_pnl_usd, Decimal("0.00"))
        self.assertEqual(ledger.total_equity_usd, Decimal("5000.00"))
        inv = ledger.validate_equity_invariant()
        self.assertTrue(inv["holds"])

    def test_02_buy_transaction_and_entry_fees(self):
        """2. Buy transaction and entry fees."""
        ledger = AccountingLedger("5000.00")
        # Buy 10 shares of AAPL at $150.00, fee $1.50
        lot = ledger.buy(
            ticker="AAPL",
            shares=10,
            price="150.00",
            fee="1.50",
            timestamp="2026-11-03T09:30:00-05:00",
        )
        self.assertEqual(lot.shares_remaining, 10)
        self.assertEqual(lot.total_lot_cost_basis_usd, Decimal("1500.00"))
        self.assertEqual(lot.entry_fee_remaining, Decimal("1.50"))

        # Cash reduced by principal (1500) + fee (1.50) = 1501.50
        self.assertEqual(ledger.cash_usd, Decimal("3498.50"))
        self.assertEqual(ledger.gross_market_value_usd, Decimal("1500.00"))
        # Unrealized P&L is 0 - entry fee = -1.50
        self.assertEqual(ledger.unrealized_trading_pnl_usd, Decimal("-1.50"))
        self.assertEqual(ledger.total_equity_usd, Decimal("4998.50"))
        ledger.validate_equity_invariant()

    def test_03_full_position_exit(self):
        """3. Full position exit."""
        ledger = AccountingLedger("5000.00")
        ledger.buy("AAPL", 10, "150.00", fee="1.50")
        # Sell 10 shares at $160.00, fee $1.50
        trade = ledger.sell("AAPL", 10, "160.00", fee="1.50")

        self.assertEqual(trade["shares_sold"], 10)
        self.assertEqual(trade["gross_proceeds"], "1600.00")
        self.assertEqual(trade["allocated_basis"], "1500.00")
        self.assertEqual(trade["allocated_entry_fee"], "1.50")
        self.assertEqual(trade["exit_fee"], "1.50")
        # Realized net P&L = 1600 - 1500 - 1.50 - 1.50 = +97.00
        self.assertEqual(trade["realized_net_pnl"], "97.00")
        self.assertEqual(ledger.cumulative_realized_trading_pnl_usd, Decimal("97.00"))

        # Cash: 3498.50 + 1600 - 1.50 = 5097.00
        self.assertEqual(ledger.cash_usd, Decimal("5097.00"))
        self.assertEqual(ledger.gross_market_value_usd, Decimal("0.00"))
        self.assertEqual(ledger.unrealized_trading_pnl_usd, Decimal("0.00"))
        self.assertEqual(ledger.total_equity_usd, Decimal("5097.00"))
        ledger.validate_equity_invariant()

    def test_04_partial_position_exit(self):
        """4. Partial position exit."""
        ledger = AccountingLedger("5000.00")
        ledger.buy("NVDA", 20, "100.00", fee="2.00")
        # Sell 5 shares (25%) at $110.00, exit fee $1.00
        trade = ledger.sell("NVDA", 5, "110.00", fee="1.00")

        # 5 / 20 = 25% of basis (500.00) and fee (0.50)
        self.assertEqual(trade["allocated_basis"], "500.00")
        self.assertEqual(trade["allocated_entry_fee"], "0.50")
        # Realized: 550 - 500 - 0.50 - 1.00 = +48.50
        self.assertEqual(trade["realized_net_pnl"], "48.50")
        self.assertEqual(ledger.cumulative_realized_trading_pnl_usd, Decimal("48.50"))

        # Remaining 15 shares: basis 1500.00, entry fee 1.50
        pos = ledger.positions["NVDA"]
        self.assertEqual(pos.total_shares, 15)
        self.assertEqual(pos.total_cost_basis_usd, Decimal("1500.00"))
        self.assertEqual(pos.total_entry_fee_remaining_usd, Decimal("1.50"))
        ledger.validate_equity_invariant()

    def test_05_multiple_lots_with_fifo(self):
        """5. Multiple lots with FIFO."""
        ledger = AccountingLedger("5000.00")
        # Lot 1: 10 shares @ $100.00, fee $1.00
        l1 = ledger.buy("MSFT", 10, "100.00", fee="1.00")
        # Lot 2: 10 shares @ $110.00, fee $1.00
        l2 = ledger.buy("MSFT", 10, "110.00", fee="1.00")

        # Sell 15 shares (should consume all 10 of Lot 1, and 5 of Lot 2)
        trade = ledger.sell("MSFT", 15, "120.00", fee="1.50")
        self.assertEqual(l1.status, "CLOSED")
        self.assertEqual(l1.shares_remaining, 0)
        self.assertEqual(l2.status, "OPEN")
        self.assertEqual(l2.shares_remaining, 5)

        # Lot 1 basis: 1000.00, fee: 1.00
        # Lot 2 basis: 5/10 * 1100 = 550.00, fee: 5/10 * 1.00 = 0.50
        # Total basis = 1550.00, entry fees = 1.50
        self.assertEqual(trade["allocated_basis"], "1550.00")
        self.assertEqual(trade["allocated_entry_fee"], "1.50")
        # Gross = 15 * 120 = 1800.00. Realized = 1800 - 1550 - 1.50 - 1.50 = +247.00
        self.assertEqual(trade["realized_net_pnl"], "247.00")
        ledger.validate_equity_invariant()

    def test_06_entry_fee_residual_allocation(self):
        """6. Entry fee residual allocation without penny loss."""
        ledger = AccountingLedger("5000.00")
        # 3 shares with an entry fee of $1.00 (not divisible by 3)
        ledger.buy("TSLA", 3, "200.00", fee="1.00")

        # Partial exit 1: 1 share
        t1 = ledger.sell("TSLA", 1, "210.00", fee="0.50")
        # 1/3 of 1.00 = round(0.3333) = 0.33
        self.assertEqual(t1["allocated_entry_fee"], "0.33")

        # Partial exit 2: 1 share
        # Remaining fee is 0.67, 1/2 of 0.67 = round(0.335) = 0.34
        t2 = ledger.sell("TSLA", 1, "210.00", fee="0.50")
        self.assertEqual(t2["allocated_entry_fee"], "0.34")

        # Final exit: 1 share (must absorb remaining 0.33 exactly)
        t3 = ledger.sell("TSLA", 1, "210.00", fee="0.50")
        self.assertEqual(t3["allocated_entry_fee"], "0.33")

        # Sum of allocated fees: 0.33 + 0.34 + 0.33 = 1.00
        fee_sum = Decimal(t1["allocated_entry_fee"]) + Decimal(t2["allocated_entry_fee"]) + Decimal(t3["allocated_entry_fee"])
        self.assertEqual(fee_sum, Decimal("1.00"))
        ledger.validate_equity_invariant()

    def test_07_realized_pnl(self):
        """7. Realized P&L accounting."""
        ledger = AccountingLedger("5000.00")
        ledger.buy("AMZN", 10, "100.00", fee="1.00")
        # Sell at loss: $90.00, fee $1.00
        trade = ledger.sell("AMZN", 10, "90.00", fee="1.00")
        # Gross = 900. Cost = 1000. Entry Fee = 1.00. Exit Fee = 1.00.
        # Realized = 900 - 1000 - 1.00 - 1.00 = -102.00
        self.assertEqual(trade["realized_net_pnl"], "-102.00")
        self.assertEqual(ledger.cumulative_realized_trading_pnl_usd, Decimal("-102.00"))
        self.assertEqual(ledger.cash_usd, Decimal("4898.00"))
        self.assertEqual(ledger.total_equity_usd, Decimal("4898.00"))
        ledger.validate_equity_invariant()

    def test_08_unrealized_pnl(self):
        """8. Mark-to-market unrealized P&L."""
        ledger = AccountingLedger("5000.00")
        ledger.buy("META", 10, "300.00", fee="2.00")
        # Mark price up to $320.00
        ledger.update_market_price("META", "320.00")

        # Gross MV = 3200. Cost Basis = 3000. Entry fee rem = 2.00.
        # Unrealized Net P&L = 3200 - 3000 - 2.00 = +198.00
        self.assertEqual(ledger.gross_market_value_usd, Decimal("3200.00"))
        self.assertEqual(ledger.unrealized_trading_pnl_usd, Decimal("198.00"))
        self.assertEqual(ledger.total_equity_usd, Decimal("5198.00"))
        ledger.validate_equity_invariant()

    def test_09_dividend_ex_date_accrual(self):
        """9. Dividend ex-date accrual."""
        ledger = AccountingLedger("5000.00")
        ledger.buy("KO", 50, "50.00", fee="1.00")
        # Accrue dividend: $0.50/share
        div = ledger.accrue_dividend("KO", "2026-11-04", "0.50")

        self.assertEqual(div.amount_usd, Decimal("25.00"))
        self.assertEqual(ledger.dividends_receivable_usd, Decimal("25.00"))
        self.assertEqual(ledger.cumulative_dividend_income_usd, Decimal("25.00"))
        ledger.validate_equity_invariant()

    def test_10_dividend_payable_date_settlement(self):
        """10. Dividend payable-date settlement into cash."""
        ledger = AccountingLedger("5000.00")
        ledger.buy("KO", 50, "50.00", fee="1.00")
        div = ledger.accrue_dividend("KO", "2026-11-04", "0.50")
        cash_before = ledger.cash_usd

        # Settle on payable date
        ledger.settle_dividend(div.dividend_id, "2026-11-20")
        self.assertEqual(ledger.dividends_receivable_usd, Decimal("0.00"))
        self.assertEqual(ledger.cash_usd, cash_before + Decimal("25.00"))
        # Income is NOT recognized again
        self.assertEqual(ledger.cumulative_dividend_income_usd, Decimal("25.00"))
        ledger.validate_equity_invariant()

    def test_11_dividend_payment_after_position_closure(self):
        """11. Dividend payment after position closure."""
        ledger = AccountingLedger("5000.00")
        ledger.buy("PG", 20, "150.00", fee="1.00")
        div = ledger.accrue_dividend("PG", "2026-11-04", "1.00")  # $20.00 dividend

        # Sell entire position on ex-date or after ex-date
        ledger.sell("PG", 20, "152.00", fee="1.00")
        self.assertEqual(ledger.positions["PG"].total_shares, 0)
        self.assertEqual(ledger.dividends_receivable_usd, Decimal("20.00"))

        # Settle dividend after position is closed
        ledger.settle_dividend(div.dividend_id, "2026-11-25")
        self.assertEqual(ledger.dividends_receivable_usd, Decimal("0.00"))
        ledger.validate_equity_invariant()

    def test_12_reverse_split_no_fractional_entitlement(self):
        """12. Reverse split with no fractional entitlement (20 shares in 1-for-10)."""
        ledger = AccountingLedger("5000.00")
        ledger.buy("XYZ", 20, "10.00", fee="1.00")
        # 20 * 1 / 10 = 2 whole shares, 0 remainder
        res = ledger.apply_reverse_split("XYZ", 1, 10, "100.00", "2026-11-05")

        pos = ledger.positions["XYZ"]
        self.assertEqual(pos.total_shares, 2)
        self.assertEqual(pos.total_cost_basis_usd, Decimal("200.00"))
        self.assertEqual(pos.total_entry_fee_remaining_usd, Decimal("1.00"))
        self.assertEqual(res["total_cil_proceeds"], "0.00")
        ledger.validate_equity_invariant()

    def test_13_reverse_split_1_for_10_with_15_shares(self):
        """13. 1-for-10 reverse split with 15 shares."""
        ledger = AccountingLedger("5000.00")
        # 15 shares @ $10.00 = $150.00, fee $1.50
        ledger.buy("ABC", 15, "10.00", fee="1.50")
        # CIL settlement at $120.00
        res = ledger.apply_reverse_split("ABC", 1, 10, "120.00", "2026-11-05")

        # 15 * 1 / 10 = 1 whole share, 5 rem (0.5 fractional)
        # alpha_cil = 5 / 15 = 1/3
        # CIL basis = 150.00 * 1/3 = 50.00
        # CIL fee = 1.50 * 1/3 = 0.50
        # CIL proceeds = 0.5 * 120.00 = 60.00
        # CIL net PnL = 60 - 50 - 0.50 = +9.50
        self.assertEqual(res["total_cil_proceeds"], "60.00")
        self.assertEqual(res["total_cil_cost_basis"], "50.00")
        self.assertEqual(res["total_cil_entry_fee"], "0.50")
        self.assertEqual(res["total_cil_net_pnl"], "9.50")

        pos = ledger.positions["ABC"]
        self.assertEqual(pos.total_shares, 1)
        self.assertEqual(pos.total_cost_basis_usd, Decimal("100.00"))
        self.assertEqual(pos.total_entry_fee_remaining_usd, Decimal("1.00"))
        ledger.validate_equity_invariant()

    def test_14_reverse_split_1_for_3_with_10_shares(self):
        """14. 1-for-3 reverse split with 10 shares."""
        ledger = AccountingLedger("5000.00")
        # 10 shares @ $30.00 = $300.00, fee $2.00
        ledger.buy("DEF", 10, "30.00", fee="2.00")
        # CIL settlement at $95.00
        res = ledger.apply_reverse_split("DEF", 1, 3, "95.00", "2026-11-05")

        # 10 * 1 / 3 = 3 whole shares, 1 rem (1/3 fractional)
        # alpha_cil = 1 / 10 = 0.10
        # CIL basis = 300.00 * 0.10 = 30.00
        # CIL fee = 2.00 * 0.10 = 0.20
        # CIL proceeds = round(1/3 * 95.00) = 31.67
        # CIL net PnL = 31.67 - 30.00 - 0.20 = +1.47
        self.assertEqual(res["total_cil_cost_basis"], "30.00")
        self.assertEqual(res["total_cil_entry_fee"], "0.20")
        self.assertEqual(res["total_cil_proceeds"], "31.67")
        self.assertEqual(res["total_cil_net_pnl"], "1.47")

        pos = ledger.positions["DEF"]
        self.assertEqual(pos.total_shares, 3)
        self.assertEqual(pos.total_cost_basis_usd, Decimal("270.00"))
        self.assertEqual(pos.total_entry_fee_remaining_usd, Decimal("1.80"))
        ledger.validate_equity_invariant()

    def test_15_reverse_split_closing_entire_position(self):
        """15. Reverse split closing an entire position (5 shares in 1-for-10)."""
        ledger = AccountingLedger("5000.00")
        ledger.buy("TINY", 5, "10.00", fee="1.00")  # Cost $50.00
        res = ledger.apply_reverse_split("TINY", 1, 10, "110.00", "2026-11-05")

        # 5 * 1 / 10 = 0 whole shares, 5 rem (0.5 fractional)
        # alpha_cil = 5 / 5 = 1.0
        # CIL proceeds = 0.5 * 110.00 = 55.00
        # CIL basis = 50.00, fee = 1.00
        # CIL net PnL = 55.00 - 50.00 - 1.00 = +4.00
        self.assertEqual(res["total_cil_proceeds"], "55.00")
        self.assertEqual(res["total_cil_cost_basis"], "50.00")
        self.assertEqual(res["total_cil_entry_fee"], "1.00")
        self.assertEqual(res["total_cil_net_pnl"], "4.00")

        pos = ledger.positions["TINY"]
        self.assertEqual(pos.total_shares, 0)
        self.assertEqual(pos.total_cost_basis_usd, Decimal("0.00"))
        self.assertEqual(pos.total_entry_fee_remaining_usd, Decimal("0.00"))
        ledger.validate_equity_invariant()

    def test_16_recurring_fractional_entitlement(self):
        """16. Recurring fractional entitlement with 7 shares in a 1-for-3 split."""
        ledger = AccountingLedger("5000.00")
        # 7 shares @ $20.00 = $140.00, fee $1.40
        ledger.buy("RECUR", 7, "20.00", fee="1.40")
        res = ledger.apply_reverse_split("RECUR", 1, 3, "60.00", "2026-11-05")

        # 7 * 1 / 3 = 2 whole shares, 1 rem (1/3 fractional)
        # alpha_cil = 1 / (7 * 1) = 1/7
        # CIL basis = round(140.00 * 1/7) = 20.00
        # Remaining basis = 140.00 - 20.00 = 120.00
        # CIL fee = round(1.40 * 1/7) = 0.20
        # Remaining fee = 1.40 - 0.20 = 1.20
        self.assertEqual(res["total_cil_cost_basis"], "20.00")
        self.assertEqual(res["total_cil_entry_fee"], "0.20")
        # Proceeds: 1/3 * 60.00 = 20.00
        self.assertEqual(res["total_cil_proceeds"], "20.00")
        # Net PnL = 20.00 - 20.00 - 0.20 = -0.20
        self.assertEqual(res["total_cil_net_pnl"], "-0.20")

        pos = ledger.positions["RECUR"]
        self.assertEqual(pos.total_shares, 2)
        self.assertEqual(pos.total_cost_basis_usd, Decimal("120.00"))
        self.assertEqual(pos.total_entry_fee_remaining_usd, Decimal("1.20"))
        ledger.validate_equity_invariant()

    def test_17_accounting_invariant_randomized_transitions(self):
        """17. Accounting invariant after every transaction in a multi-step sequence."""
        ledger = AccountingLedger("5000.00")
        ledger.validate_equity_invariant()

        # Step 1: Buy Stock A
        ledger.buy("STKA", 25, "40.00", fee="1.25")
        ledger.validate_equity_invariant()

        # Step 2: Mark up
        ledger.update_market_price("STKA", "45.00")
        ledger.validate_equity_invariant()

        # Step 3: Buy Stock B
        ledger.buy("STKB", 10, "120.00", fee="1.50")
        ledger.validate_equity_invariant()

        # Step 4: Accrue dividend on Stock A
        div_a = ledger.accrue_dividend("STKA", "2026-11-04", "0.75")
        ledger.validate_equity_invariant()

        # Step 5: Partial sell Stock A
        ledger.sell("STKA", 10, "46.00", fee="1.00")
        ledger.validate_equity_invariant()

        # Step 6: Reverse split Stock B (1-for-2)
        ledger.apply_reverse_split("STKB", 1, 2, "240.00", "2026-11-05")
        ledger.validate_equity_invariant()

        # Step 7: Settle dividend on Stock A
        ledger.settle_dividend(div_a.dividend_id, "2026-11-20")
        ledger.validate_equity_invariant()

        # Step 8: Final sell of Stock A
        ledger.sell("STKA", 15, "48.00", fee="1.00")
        ledger.validate_equity_invariant()

        # Step 9: Final sell of Stock B
        ledger.sell("STKB", 5, "250.00", fee="1.50")
        ledger.validate_equity_invariant()

    def test_18_invalid_decimal_and_financial_inputs(self):
        """18. Rejection of invalid Decimal, float, NaN, and negative inputs."""
        ledger = AccountingLedger("5000.00")

        # Float rejected
        with self.assertRaises(InvalidFinancialValue):
            ledger.buy("AAPL", 10, 150.00)  # float price

        with self.assertRaises(InvalidFinancialValue):
            ledger.buy("AAPL", 10, "150.00", fee=1.50)  # float fee

        # Negative shares rejected
        with self.assertRaises(InvalidFinancialValue):
            ledger.buy("AAPL", -5, "150.00")

        # Zero shares rejected
        with self.assertRaises(InvalidFinancialValue):
            ledger.buy("AAPL", 0, "150.00")

        # Negative price rejected
        with self.assertRaises(InvalidFinancialValue):
            ledger.buy("AAPL", 10, "-150.00")

        # Insufficient cash
        with self.assertRaises(InsufficientCashError):
            ledger.buy("AAPL", 100, "100.00")  # requires $10,000, has $5,000

        # NaN / Inf string rejected
        with self.assertRaises(InvalidFinancialValue):
            to_decimal("NaN")

        with self.assertRaises(InvalidFinancialValue):
            to_decimal("Infinity")


if __name__ == "__main__":
    unittest.main()
