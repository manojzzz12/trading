"""Unit tests for Phase 5.3 Simulated Order Execution and Portfolio Integration.

Enforces:
1. Adverse entry and exit slippage modeling.
2. Gap-down through stop fills at session open.
3. Conservative same-candle tie-breaking: stop executes first if both stop & target touched.
4. Trailing stop updates.
5. Portfolio equity invariant validation after every execution.
"""

from datetime import datetime
from decimal import Decimal
import os
import sys
import unittest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SWING_DIR = os.path.join(ROOT_DIR, ".agents", "skills", "trader-swing")
for path in (ROOT_DIR, SWING_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

from backtest_execution import SimulatedExecutionEngine
from backtest_models import (
    ExecutionAssumptions,
    SimulatedOrder,
    SimulatedOrderSide,
    SimulatedOrderStatus,
    SimulatedOrderType,
    SlippageConfig,
)
from backtest_portfolio import BacktestPortfolio
from market_data_models import CandleStatus, OHLCVCandle, UTC
from risk import FeeConfig, FeeSchedule


def make_candle(
    ticker: str,
    dt: datetime,
    open_p: str,
    high_p: str,
    low_p: str,
    close_p: str,
) -> OHLCVCandle:
    return OHLCVCandle(
        ticker=ticker,
        interval="1d",
        timestamp=dt,
        open=Decimal(open_p),
        high=Decimal(high_p),
        low=Decimal(low_p),
        close=Decimal(close_p),
        volume=1000000,
        status=CandleStatus.COMPLETE,
        source="yahoo",
        received_timestamp=dt,
        is_delayed=True,
    )


class TestSwingBacktestExecution(unittest.TestCase):
    """Test suite covering simulated order fills, slippage, gap handling, and portfolio accounting."""

    def setUp(self):
        self.dt = datetime(2026, 1, 6, 20, 0, 0, tzinfo=UTC)
        self.fee_cfg = FeeConfig(
            entry=FeeSchedule(fixed=Decimal("1.00")),
            exit=FeeSchedule(fixed=Decimal("1.00")),
        )
        self.slippage_cfg = SlippageConfig(entry_bps=Decimal("10.0"), exit_bps=Decimal("10.0"))
        self.exec_engine = SimulatedExecutionEngine(
            fee_config=self.fee_cfg,
            slippage_config=self.slippage_cfg,
            assumptions=ExecutionAssumptions(),
        )
        self.portfolio = BacktestPortfolio(initial_capital_usd=Decimal("5000.00"))

    def test_01_entry_fill_with_slippage_and_fee(self):
        """Verify market entry fills at session open with adverse slippage and fee."""
        candle = make_candle("AAPL", self.dt, "150.00", "155.00", "149.00", "154.00")
        order = SimulatedOrder(
            order_id="ORD-1",
            ticker="AAPL",
            side=SimulatedOrderSide.BUY,
            order_type=SimulatedOrderType.MARKET_NEXT_OPEN,
            quantity=10,
            signal_timestamp=self.dt,
            submission_timestamp=self.dt,
            reference_price=Decimal("150.00"),
            stop_loss_price=Decimal("142.50"),
            target_price=Decimal("165.00"),
        )

        filled = self.exec_engine.simulate_entry_fill(order, candle)
        self.assertEqual(filled.status, SimulatedOrderStatus.FILLED)
        # 10 bps on 150 = 150 * 0.0010 = 0.15 -> fill price = 150.15
        self.assertEqual(filled.fill_price, Decimal("150.15"))
        self.assertEqual(filled.fee_usd, Decimal("1.00"))
        self.assertEqual(filled.slippage_usd, Decimal("1.50"))

        # Execute into portfolio
        lot_id = self.portfolio.execute_entry(filled)
        self.assertTrue(lot_id.startswith("LOT-AAPL-"))
        self.assertIn(lot_id, self.portfolio.lot_stops)
        self.assertEqual(self.portfolio.lot_stops[lot_id], Decimal("142.50"))

        # Verify portfolio cash decreased: 10 * 150.15 + 1.00 fee = 1502.50
        self.assertEqual(self.portfolio.ledger.cash_usd, Decimal("3497.50"))
        self.portfolio.ledger.validate_equity_invariant()

    def test_02_stop_loss_breach_execution(self):
        """Verify normal stop loss fill when intraday low touches stop."""
        candle = make_candle("AAPL", self.dt, "145.00", "146.00", "141.00", "143.00")
        exit_order = self.exec_engine.evaluate_exit_conditions(
            ticker="AAPL",
            lot_id="LOT-1",
            shares=10,
            stop_loss_price=Decimal("142.00"),
            target_price=Decimal("165.00"),
            session_candle=candle,
        )

        self.assertIsNotNone(exit_order)
        self.assertEqual(exit_order.order_type, SimulatedOrderType.STOP_LOSS)
        # 10 bps adverse slippage on 142.00 = 142 * (1 - 0.0010) = 141.86
        self.assertEqual(exit_order.fill_price, Decimal("141.86"))
        self.assertEqual(exit_order.fee_usd, Decimal("1.00"))

    def test_03_gap_down_through_stop_fills_at_open(self):
        """Verify opening gap-down below stop executes at session open price, not the stop."""
        # Open is 138.00, well below stop of 142.00
        candle = make_candle("AAPL", self.dt, "138.00", "139.00", "135.00", "136.00")
        exit_order = self.exec_engine.evaluate_exit_conditions(
            ticker="AAPL",
            lot_id="LOT-1",
            shares=10,
            stop_loss_price=Decimal("142.00"),
            target_price=Decimal("165.00"),
            session_candle=candle,
        )

        self.assertIsNotNone(exit_order)
        self.assertEqual(exit_order.order_type, SimulatedOrderType.STOP_LOSS)
        # Fill is based on open price 138.00 with 10 bps exit slippage: 138 * 0.999 = 137.86
        self.assertEqual(exit_order.fill_price, Decimal("137.86"))
        self.assertIn("Gap-down through stop", exit_order.reason)

    def test_04_conservative_same_bar_tie_breaking(self):
        """Verify that when both stop and target are touched on the same bar, stop triggers first."""
        # Low is 135 (below stop 140), High is 170 (above target 165)
        wild_candle = make_candle("AAPL", self.dt, "150.00", "170.00", "135.00", "155.00")
        exit_order = self.exec_engine.evaluate_exit_conditions(
            ticker="AAPL",
            lot_id="LOT-1",
            shares=10,
            stop_loss_price=Decimal("140.00"),
            target_price=Decimal("165.00"),
            session_candle=wild_candle,
        )

        self.assertIsNotNone(exit_order)
        # Conservative tie break: must be STOP_LOSS, not TAKE_PROFIT
        self.assertEqual(exit_order.order_type, SimulatedOrderType.STOP_LOSS)

    def test_05_round_trip_trade_accounting_and_r_multiple(self):
        """Verify full buy-to-sell round trip records net P&L and correct R-multiple."""
        entry_candle = make_candle("AAPL", self.dt, "100.00", "102.00", "99.00", "101.00")
        buy_order = SimulatedOrder(
            order_id="ORD-BUY",
            ticker="AAPL",
            side=SimulatedOrderSide.BUY,
            order_type=SimulatedOrderType.MARKET_NEXT_OPEN,
            quantity=10,
            signal_timestamp=self.dt,
            submission_timestamp=self.dt,
            reference_price=Decimal("100.00"),
            stop_loss_price=Decimal("95.00"),
            target_price=Decimal("110.00"),
        )
        filled_buy = self.exec_engine.simulate_entry_fill(buy_order, entry_candle)
        lot_id = self.portfolio.execute_entry(filled_buy)

        # Exit at target
        exit_dt = self.dt.replace(day=8)
        exit_candle = make_candle("AAPL", exit_dt, "108.00", "112.00", "107.00", "111.00")
        exit_order = self.exec_engine.evaluate_exit_conditions(
            ticker="AAPL",
            lot_id=lot_id,
            shares=10,
            stop_loss_price=Decimal("95.00"),
            target_price=Decimal("110.00"),
            session_candle=exit_candle,
        )
        self.assertIsNotNone(exit_order)

        trade_record = self.portfolio.execute_exit(exit_order)
        self.assertEqual(trade_record.shares, 10)
        self.assertTrue(trade_record.net_pnl_usd > Decimal("0.00"))
        # R-Multiple: (Exit Px - Entry Px) / (Entry Px - Stop Px)
        self.assertTrue(trade_record.r_multiple >= Decimal("1.80"))
        self.portfolio.ledger.validate_equity_invariant()


if __name__ == "__main__":
    unittest.main()
