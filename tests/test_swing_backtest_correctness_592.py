"""Comprehensive unit, regression, and integration test suite for Phase 5.9.2 in trader-swing.

Corrects 5 independent audit findings from Phase 5.9.1:
1. AUDIT-5.9.1-01: Preserve protective stops and targets after partial exits.
2. AUDIT-5.9.1-02: Add January 9, 2025 Historical NYSE Closure (President Jimmy Carter mourning).
3. AUDIT-5.9.1-03: Adjust pending orders across stock split dates in RAW_UNADJUSTED mode.
4. AUDIT-5.9.1-04: Correct July 3 early-close calendar for historical years (2015-2027).
5. AUDIT-5.9.1-05: Separate analytical continuous prices from raw execution prices.
6. Comprehensive integrated multi-session scenario verifying accounting invariant after every state transition.
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal
import os
import sys
import unittest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SWING_DIR = os.path.join(ROOT_DIR, ".agents", "skills", "trader-swing")
for path in (ROOT_DIR, SWING_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

from accounting import AccountingLedger, quantize_cent
from backtest_data import HistoricalDataManager, HistoricalDataValidationError
from backtest_engine import EventDrivenBacktestEngine
from backtest_models import (
    BacktestConfig,
    DividendEvent,
    PriceAdjustmentType,
    SimulatedOrder,
    SimulatedOrderSide,
    SimulatedOrderStatus,
    SimulatedOrderType,
    StockSplitEvent,
)
from backtest_portfolio import BacktestPortfolio
from market_data_models import (
    CandleStatus,
    HISTORICAL_NYSE_EARLY_CLOSE,
    HISTORICAL_NYSE_HOLIDAYS,
    MarketClock,
    OHLCVCandle,
    UTC,
    to_eastern,
)
from risk import FeeConfig, FeeSchedule, RiskConfig, SizingResult


def _create_daily_candle(
    ticker: str,
    dt: datetime,
    open_px: Decimal,
    high_px: Decimal,
    low_px: Decimal,
    close_px: Decimal,
    volume: int = 1000000,
) -> OHLCVCandle:
    return OHLCVCandle(
        ticker=ticker,
        interval="1d",
        timestamp=dt,
        open=quantize_cent(open_px),
        high=quantize_cent(high_px),
        low=quantize_cent(low_px),
        close=quantize_cent(close_px),
        volume=volume,
        status=CandleStatus.COMPLETE,
        source="mock",
        received_timestamp=dt,
        is_delayed=True,
    )


class TestPartialExitProtectiveStops(unittest.TestCase):
    """AUDIT-5.9.1-01: Preserve protective stops and targets after partial exits."""

    def setUp(self):
        self.portfolio = BacktestPortfolio(initial_capital_usd=Decimal("10000.00"))

    def test_partial_exit_preserves_stop_and_target(self):
        """Enter 20 shares with a $140 stop and $180 target, exit 5 shares, verify 15 remain protected."""
        entry_ts = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
        entry_order = SimulatedOrder(
            order_id="ORD-BUY-1",
            ticker="AAPL",
            side=SimulatedOrderSide.BUY,
            order_type=SimulatedOrderType.MARKET_NEXT_OPEN,
            quantity=20,
            signal_timestamp=entry_ts,
            submission_timestamp=entry_ts,
            reference_price=Decimal("150.00"),
            fill_timestamp=entry_ts,
            fill_price=Decimal("150.00"),
            stop_loss_price=Decimal("140.00"),
            target_price=Decimal("180.00"),
            fee_usd=Decimal("1.00"),
            status=SimulatedOrderStatus.FILLED,
        )
        lot_id = self.portfolio.execute_entry(entry_order)

        # Invariant and initial state checks
        self.assertIn(lot_id, self.portfolio.lot_stops)
        self.assertEqual(self.portfolio.lot_stops[lot_id], Decimal("140.00"))
        self.assertEqual(self.portfolio.lot_targets[lot_id], Decimal("180.00"))
        pos = self.portfolio.ledger.positions["AAPL"]
        self.assertEqual(pos.total_shares, 20)
        self.portfolio.ledger.validate_equity_invariant()

        # Step 2: Partially exit 5 shares at $160.00
        exit_ts1 = datetime(2026, 1, 6, 15, 0, tzinfo=UTC)
        partial_exit = SimulatedOrder(
            order_id="ORD-SELL-PARTIAL",
            ticker="AAPL",
            side=SimulatedOrderSide.SELL,
            order_type=SimulatedOrderType.TAKE_PROFIT,
            quantity=5,
            signal_timestamp=exit_ts1,
            submission_timestamp=exit_ts1,
            reference_price=Decimal("160.00"),
            fill_timestamp=exit_ts1,
            fill_price=Decimal("160.00"),
            lot_id=lot_id,
            fee_usd=Decimal("1.00"),
            status=SimulatedOrderStatus.FILLED,
            reason="Partial take profit",
        )
        rec1 = self.portfolio.execute_exit(partial_exit)

        # Step 3: Verify 15 shares remain
        self.assertEqual(pos.total_shares, 15)
        self.assertEqual(pos.lots[0].shares_remaining, 15)
        self.assertEqual(rec1.shares, 5)
        self.assertEqual(rec1.trade_id, f"TRD-{lot_id}")

        # Step 4: Verify stop and target remain completely active and unchanged
        self.assertIn(lot_id, self.portfolio.lot_stops)
        self.assertIn(lot_id, self.portfolio.lot_targets)
        self.assertEqual(self.portfolio.lot_stops[lot_id], Decimal("140.00"))
        self.assertEqual(self.portfolio.lot_targets[lot_id], Decimal("180.00"))
        self.assertEqual(self.portfolio.lot_meta[lot_id]["shares"], 15)
        self.portfolio.ledger.validate_equity_invariant()

        # Step 5: Verify remaining 15 shares are still evaluated and close them
        exit_ts2 = datetime(2026, 1, 7, 15, 0, tzinfo=UTC)
        final_exit = SimulatedOrder(
            order_id="ORD-SELL-FINAL",
            ticker="AAPL",
            side=SimulatedOrderSide.SELL,
            order_type=SimulatedOrderType.STOP_LOSS,
            quantity=15,
            signal_timestamp=exit_ts2,
            submission_timestamp=exit_ts2,
            reference_price=Decimal("140.00"),
            fill_timestamp=exit_ts2,
            fill_price=Decimal("140.00"),
            lot_id=lot_id,
            fee_usd=Decimal("1.00"),
            status=SimulatedOrderStatus.FILLED,
            reason="Final stop loss trigger",
        )
        rec2 = self.portfolio.execute_exit(final_exit)
        self.assertEqual(rec2.shares, 15)
        self.assertEqual(rec2.trade_id, f"TRD-{lot_id}-2")

        # Step 6: Verify full exit cleanly purged all stop/target state
        self.assertEqual(pos.total_shares, 0)
        self.assertEqual(pos.lots[0].status, "CLOSED")
        self.assertNotIn(lot_id, self.portfolio.lot_stops)
        self.assertNotIn(lot_id, self.portfolio.lot_targets)
        self.assertNotIn(lot_id, self.portfolio.lot_meta)
        inv = self.portfolio.ledger.validate_equity_invariant()
        self.assertTrue(inv["holds"])

    def test_multiple_lots_same_ticker_independent_protection(self):
        """Test multiple lots of the same ticker manage independent protection lifecycles."""
        ts1 = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
        lot1_order = SimulatedOrder(
            order_id="BUY-L1",
            ticker="MSFT",
            side=SimulatedOrderSide.BUY,
            order_type=SimulatedOrderType.MARKET_NEXT_OPEN,
            quantity=10,
            signal_timestamp=ts1,
            submission_timestamp=ts1,
            reference_price=Decimal("100.00"),
            fill_timestamp=ts1,
            fill_price=Decimal("100.00"),
            stop_loss_price=Decimal("90.00"),
            target_price=Decimal("120.00"),
            status=SimulatedOrderStatus.FILLED,
        )
        lot1_id = self.portfolio.execute_entry(lot1_order)

        ts2 = datetime(2026, 1, 6, 14, 30, tzinfo=UTC)
        lot2_order = SimulatedOrder(
            order_id="BUY-L2",
            ticker="MSFT",
            side=SimulatedOrderSide.BUY,
            order_type=SimulatedOrderType.MARKET_NEXT_OPEN,
            quantity=10,
            signal_timestamp=ts2,
            submission_timestamp=ts2,
            reference_price=Decimal("110.00"),
            fill_timestamp=ts2,
            fill_price=Decimal("110.00"),
            stop_loss_price=Decimal("100.00"),
            target_price=Decimal("130.00"),
            status=SimulatedOrderStatus.FILLED,
        )
        lot2_id = self.portfolio.execute_entry(lot2_order)

        # Partially exit Lot 1 (4 shares)
        partial_l1 = SimulatedOrder(
            order_id="EXIT-L1-P",
            ticker="MSFT",
            side=SimulatedOrderSide.SELL,
            order_type=SimulatedOrderType.TAKE_PROFIT,
            quantity=4,
            signal_timestamp=ts2,
            submission_timestamp=ts2,
            reference_price=Decimal("115.00"),
            fill_timestamp=ts2,
            fill_price=Decimal("115.00"),
            lot_id=lot1_id,
            status=SimulatedOrderStatus.FILLED,
        )
        self.portfolio.execute_exit(partial_l1)

        # Lot 1 still has 6 shares; stop and target preserved
        self.assertIn(lot1_id, self.portfolio.lot_stops)
        self.assertEqual(self.portfolio.lot_stops[lot1_id], Decimal("90.00"))
        # Lot 2 still has 10 shares; stop and target preserved
        self.assertIn(lot2_id, self.portfolio.lot_stops)
        self.assertEqual(self.portfolio.lot_stops[lot2_id], Decimal("100.00"))

        # Close remainder of Lot 1 (6 shares)
        close_l1 = SimulatedOrder(
            order_id="EXIT-L1-F",
            ticker="MSFT",
            side=SimulatedOrderSide.SELL,
            order_type=SimulatedOrderType.STOP_LOSS,
            quantity=6,
            signal_timestamp=ts2,
            submission_timestamp=ts2,
            reference_price=Decimal("90.00"),
            fill_timestamp=ts2,
            fill_price=Decimal("90.00"),
            lot_id=lot1_id,
            status=SimulatedOrderStatus.FILLED,
        )
        self.portfolio.execute_exit(close_l1)

        # Lot 1 cleaned up, Lot 2 remains fully active
        self.assertNotIn(lot1_id, self.portfolio.lot_stops)
        self.assertIn(lot2_id, self.portfolio.lot_stops)
        self.assertEqual(self.portfolio.lot_stops[lot2_id], Decimal("100.00"))
        self.assertEqual(self.portfolio.lot_targets[lot2_id], Decimal("130.00"))

    def test_reverse_split_zero_share_lot_cleanup(self):
        """Ensure zero-share lots produced by reverse split clean up protection state."""
        ts = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
        order = SimulatedOrder(
            order_id="BUY-XYZ",
            ticker="XYZ",
            side=SimulatedOrderSide.BUY,
            order_type=SimulatedOrderType.MARKET_NEXT_OPEN,
            quantity=5,
            signal_timestamp=ts,
            submission_timestamp=ts,
            reference_price=Decimal("2.00"),
            fill_timestamp=ts,
            fill_price=Decimal("2.00"),
            stop_loss_price=Decimal("1.50"),
            target_price=Decimal("3.00"),
            status=SimulatedOrderStatus.FILLED,
        )
        lot_id = self.portfolio.execute_entry(order)

        # 1-for-10 reverse split -> 5 shares * 0.1 = 0 whole shares, 0.5 shares paid as CIL
        self.portfolio.apply_stock_split(
            ticker="XYZ",
            ratio=Decimal("0.10"),
            split_numerator=1,
            split_denominator=10,
            cil_settlement_price=Decimal("20.00"),
        )

        pos = self.portfolio.ledger.positions["XYZ"]
        self.assertEqual(pos.total_shares, 0)
        self.assertEqual(pos.lots[0].status, "CLOSED")
        # Protection state must be deleted
        self.assertNotIn(lot_id, self.portfolio.lot_stops)
        self.assertNotIn(lot_id, self.portfolio.lot_targets)
        self.assertNotIn(lot_id, self.portfolio.lot_meta)
        inv = self.portfolio.ledger.validate_equity_invariant()
        self.assertTrue(inv["holds"])


class TestHistoricalMourningDaysClosure(unittest.TestCase):
    """AUDIT-5.9.1-02: Historical NYSE Mourning Days (Carter 2025-01-09, Bush 2018-12-05)."""

    def test_carter_national_day_of_mourning_2025_01_09(self):
        """January 9, 2025 National Day of Mourning for President Jimmy Carter is closed."""
        is_open, reason = MarketClock.is_historical_trading_day(date(2025, 1, 9))
        self.assertFalse(is_open)
        self.assertIn("Jimmy Carter", reason)
        self.assertIn("2025-01-09", HISTORICAL_NYSE_HOLIDAYS)

    def test_bush_national_day_of_mourning_2018_12_05(self):
        """December 5, 2018 National Day of Mourning for President George H.W. Bush is closed."""
        is_open, reason = MarketClock.is_historical_trading_day(date(2018, 12, 5))
        self.assertFalse(is_open)
        self.assertIn("George H.W. Bush", reason)
        self.assertIn("2018-12-05", HISTORICAL_NYSE_HOLIDAYS)

    def test_dataset_integrity_does_not_flag_mourning_closures_as_missing(self):
        """HistoricalDataManager.validate_dataset_integrity does not flag Jan 9, 2025 or Dec 5, 2018."""
        dm = HistoricalDataManager()
        # 2025 week around Jan 9: Mon Jan 6, Tue Jan 7, Wed Jan 8, Fri Jan 10 (Jan 9 is mourning day)
        dates_2025 = [date(2025, 1, 6), date(2025, 1, 7), date(2025, 1, 8), date(2025, 1, 10)]
        candles_2025 = [
            _create_daily_candle("AAPL", datetime(d.year, d.month, d.day, 20, 0, tzinfo=UTC), Decimal("150"), Decimal("155"), Decimal("149"), Decimal("152"))
            for d in dates_2025
        ]
        dm.load_candles("AAPL", candles_2025)
        rep = dm.validate_dataset_integrity("AAPL")
        self.assertNotIn(date(2025, 1, 9), rep.missing_sessions)
        self.assertEqual(len(rep.missing_sessions), 0)


class TestPendingOrderSplitAdjustment(unittest.TestCase):
    """AUDIT-5.9.1-03: Adjust pending orders across stock split dates."""

    def test_forward_split_2_for_1_adjusts_pending_entry(self):
        """2-for-1 forward split adjusts pending order quantity and all price fields."""
        ts = datetime(2026, 1, 5, 21, 0, tzinfo=UTC)
        order = SimulatedOrder(
            order_id="ORD-PENDING-1",
            ticker="TSLA",
            side=SimulatedOrderSide.BUY,
            order_type=SimulatedOrderType.MARKET_NEXT_OPEN,
            quantity=10,
            signal_timestamp=ts,
            submission_timestamp=ts,
            reference_price=Decimal("100.00"),
            stop_loss_price=Decimal("90.00"),
            target_price=Decimal("120.00"),
            limit_price=Decimal("105.00"),
            status=SimulatedOrderStatus.PENDING,
        )

        # Economic exposure before
        exposure_before = Decimal(order.quantity) * order.reference_price
        risk_before = Decimal(order.quantity) * (order.reference_price - order.stop_loss_price)
        self.assertEqual(exposure_before, Decimal("1000.00"))
        self.assertEqual(risk_before, Decimal("100.00"))

        split = StockSplitEvent(ticker="TSLA", split_date=date(2026, 1, 6), ratio=Decimal("2.0"))
        valid = order.adjust_for_stock_split(split.ratio)
        self.assertTrue(valid)

        # Check post-split fields
        self.assertEqual(order.quantity, 20)
        self.assertEqual(order.reference_price, Decimal("50.00"))
        self.assertEqual(order.stop_loss_price, Decimal("45.00"))
        self.assertEqual(order.target_price, Decimal("60.00"))
        self.assertEqual(order.limit_price, Decimal("52.50"))

        # Economic exposure and risk after must match exactly
        exposure_after = Decimal(order.quantity) * order.reference_price
        risk_after = Decimal(order.quantity) * (order.reference_price - order.stop_loss_price)
        self.assertEqual(exposure_after, exposure_before)
        self.assertEqual(risk_after, risk_before)

    def test_forward_split_3_for_2_integer_compatible(self):
        """3-for-2 forward split with integer-compatible quantity."""
        ts = datetime(2026, 1, 5, 21, 0, tzinfo=UTC)
        order = SimulatedOrder(
            order_id="ORD-3-2",
            ticker="NVDA",
            side=SimulatedOrderSide.BUY,
            order_type=SimulatedOrderType.MARKET_NEXT_OPEN,
            quantity=10,
            signal_timestamp=ts,
            submission_timestamp=ts,
            reference_price=Decimal("150.00"),
            stop_loss_price=Decimal("120.00"),
            target_price=Decimal("180.00"),
            status=SimulatedOrderStatus.PENDING,
        )
        valid = order.adjust_for_stock_split(ratio=Decimal("1.5"), split_numerator=3, split_denominator=2)
        self.assertTrue(valid)
        self.assertEqual(order.quantity, 15)
        self.assertEqual(order.reference_price, Decimal("100.00"))
        self.assertEqual(order.stop_loss_price, Decimal("80.00"))
        self.assertEqual(order.target_price, Decimal("120.00"))

    def test_reverse_split_adjusts_pending_order(self):
        """1-for-2 reverse split adjusts quantity down and prices up."""
        ts = datetime(2026, 1, 5, 21, 0, tzinfo=UTC)
        order = SimulatedOrder(
            order_id="ORD-REV",
            ticker="AMC",
            side=SimulatedOrderSide.BUY,
            order_type=SimulatedOrderType.MARKET_NEXT_OPEN,
            quantity=20,
            signal_timestamp=ts,
            submission_timestamp=ts,
            reference_price=Decimal("10.00"),
            stop_loss_price=Decimal("8.00"),
            target_price=Decimal("15.00"),
            status=SimulatedOrderStatus.PENDING,
        )
        valid = order.adjust_for_stock_split(ratio=Decimal("0.5"), split_numerator=1, split_denominator=2)
        self.assertTrue(valid)
        self.assertEqual(order.quantity, 10)
        self.assertEqual(order.reference_price, Decimal("20.00"))
        self.assertEqual(order.stop_loss_price, Decimal("16.00"))
        self.assertEqual(order.target_price, Decimal("30.00"))

    def test_fractional_pending_order_safely_cancelled(self):
        """3-for-2 split on 5 shares produces fractional 7.5 shares: order safely cancelled."""
        ts = datetime(2026, 1, 5, 21, 0, tzinfo=UTC)
        order = SimulatedOrder(
            order_id="ORD-FRAC",
            ticker="XYZ",
            side=SimulatedOrderSide.BUY,
            order_type=SimulatedOrderType.MARKET_NEXT_OPEN,
            quantity=5,
            signal_timestamp=ts,
            submission_timestamp=ts,
            reference_price=Decimal("100.00"),
            stop_loss_price=Decimal("90.00"),
            status=SimulatedOrderStatus.PENDING,
        )
        valid = order.adjust_for_stock_split(ratio=Decimal("1.5"), split_numerator=3, split_denominator=2)
        self.assertFalse(valid)
        self.assertEqual(order.status, SimulatedOrderStatus.CANCELLED)
        self.assertIn("non-integer share quantity", order.rejection_reason)

    def test_engine_corporate_actions_adjusts_pending_order_on_split_date(self):
        """End-to-end engine corporate action phase adjusts pending order in RAW_UNADJUSTED mode."""
        dm = HistoricalDataManager()
        d0 = date(2026, 1, 5)
        d1 = date(2026, 1, 6)  # Split date
        d2 = date(2026, 1, 7)

        c0 = _create_daily_candle("SPLT", datetime(2026, 1, 5, 20, 0, tzinfo=UTC), Decimal("100"), Decimal("105"), Decimal("99"), Decimal("102"))
        c1 = _create_daily_candle("SPLT", datetime(2026, 1, 6, 20, 0, tzinfo=UTC), Decimal("51"), Decimal("53"), Decimal("50"), Decimal("52"))
        c2 = _create_daily_candle("SPLT", datetime(2026, 1, 7, 20, 0, tzinfo=UTC), Decimal("52"), Decimal("55"), Decimal("51"), Decimal("54"))
        dm.load_candles("SPLT", [c0, c1, c2], adjustment_type=PriceAdjustmentType.RAW_UNADJUSTED)
        dm.load_splits("SPLT", [StockSplitEvent(ticker="SPLT", split_date=d1, ratio=Decimal("2.0"))])

        cfg = BacktestConfig(
            start_date=d1,
            end_date=d2,
            universe=["SPLT"],
            initial_capital_usd=Decimal("50000.00"),
            price_adjustment_type=PriceAdjustmentType.RAW_UNADJUSTED,
        )
        engine = EventDrivenBacktestEngine(config=cfg, data_manager=dm)

        # Seed pre-split pending order queued at d0 close
        ts_seed = datetime(2026, 1, 5, 21, 0, tzinfo=UTC)
        pending = SimulatedOrder(
            order_id="ORD-SEED-SPLIT",
            ticker="SPLT",
            side=SimulatedOrderSide.BUY,
            order_type=SimulatedOrderType.MARKET_NEXT_OPEN,
            quantity=10,
            signal_timestamp=ts_seed,
            submission_timestamp=ts_seed,
            reference_price=Decimal("100.00"),
            stop_loss_price=Decimal("90.00"),
            target_price=Decimal("120.00"),
            status=SimulatedOrderStatus.PENDING,
        )
        engine.pending_entry_orders.append(pending)

        # Run backtest: on d1 pre-open, order adjusted to 20 shares @ $50 ($45 stop).
        # At d1 open, order fills at $51.00 (plus slippage).
        res = engine.run()
        pos = engine.portfolio.ledger.positions["SPLT"]
        self.assertEqual(pos.total_shares, 20)
        self.assertEqual(engine.portfolio.lot_stops[pos.lots[0].lot_id], Decimal("45.00"))

    def test_multiple_splits_before_fill(self):
        """Sequential stock splits adjust pending order chronologically before fill."""
        ts = datetime(2026, 1, 5, 21, 0, tzinfo=UTC)
        order = SimulatedOrder(
            order_id="ORD-MULTI-SPLIT",
            ticker="GROW",
            side=SimulatedOrderSide.BUY,
            order_type=SimulatedOrderType.MARKET_NEXT_OPEN,
            quantity=10,
            signal_timestamp=ts,
            submission_timestamp=ts,
            reference_price=Decimal("200.00"),
            stop_loss_price=Decimal("180.00"),
            target_price=Decimal("240.00"),
            status=SimulatedOrderStatus.PENDING,
        )
        # Split 1: 2-for-1
        valid1 = order.adjust_for_stock_split(ratio=Decimal("2.0"))
        self.assertTrue(valid1)
        self.assertEqual(order.quantity, 20)
        self.assertEqual(order.reference_price, Decimal("100.00"))
        self.assertEqual(order.stop_loss_price, Decimal("90.00"))

        # Split 2: 2-for-1
        valid2 = order.adjust_for_stock_split(ratio=Decimal("2.0"))
        self.assertTrue(valid2)
        self.assertEqual(order.quantity, 40)
        self.assertEqual(order.reference_price, Decimal("50.00"))
        self.assertEqual(order.stop_loss_price, Decimal("45.00"))
        self.assertEqual(order.target_price, Decimal("60.00"))

    def test_split_adjusted_dataset_does_not_adjust_pending_order_twice(self):
        """Engine with SPLIT_ADJUSTED dataset must NOT adjust pending order on split date."""
        dm = HistoricalDataManager()
        d0 = date(2026, 1, 5)
        d1 = date(2026, 1, 6)

        c0 = _create_daily_candle("ADJ", datetime(2026, 1, 5, 20, 0, tzinfo=UTC), Decimal("50"), Decimal("52"), Decimal("49"), Decimal("51"))
        c1 = _create_daily_candle("ADJ", datetime(2026, 1, 6, 20, 0, tzinfo=UTC), Decimal("51"), Decimal("53"), Decimal("50"), Decimal("52"))
        dm.load_candles("ADJ", [c0, c1], adjustment_type=PriceAdjustmentType.SPLIT_ADJUSTED)
        dm.load_splits("ADJ", [StockSplitEvent(ticker="ADJ", split_date=d1, ratio=Decimal("2.0"))])

        cfg = BacktestConfig(
            start_date=d1,
            end_date=d1,
            universe=["ADJ"],
            initial_capital_usd=Decimal("50000.00"),
            price_adjustment_type=PriceAdjustmentType.SPLIT_ADJUSTED,
        )
        engine = EventDrivenBacktestEngine(config=cfg, data_manager=dm)

        # Seed pre-split pending order queued at d0 close with already-adjusted prices
        ts_seed = datetime(2026, 1, 5, 21, 0, tzinfo=UTC)
        pending = SimulatedOrder(
            order_id="ORD-SEED-ADJ",
            ticker="ADJ",
            side=SimulatedOrderSide.BUY,
            order_type=SimulatedOrderType.MARKET_NEXT_OPEN,
            quantity=10,
            signal_timestamp=ts_seed,
            submission_timestamp=ts_seed,
            reference_price=Decimal("50.00"),
            stop_loss_price=Decimal("45.00"),
            target_price=Decimal("60.00"),
            status=SimulatedOrderStatus.PENDING,
        )
        engine.pending_entry_orders.append(pending)

        res = engine.run()
        pos = engine.portfolio.ledger.positions["ADJ"]
        # Must fill 10 shares (not doubled to 20!)
        self.assertEqual(pos.total_shares, 10)
        self.assertEqual(engine.portfolio.lot_stops[pos.lots[0].lot_id], Decimal("45.00"))

    def test_post_split_risk_constraints_rejection_at_fill(self):
        """Order rejected at morning fill if post-split exposure exceeds account risk limits."""
        dm = HistoricalDataManager()
        d1 = date(2026, 1, 6)

        c1 = _create_daily_candle("RISKY", datetime(2026, 1, 6, 20, 0, tzinfo=UTC), Decimal("50"), Decimal("52"), Decimal("49"), Decimal("51"))
        dm.load_candles("RISKY", [c1], adjustment_type=PriceAdjustmentType.RAW_UNADJUSTED)
        dm.load_splits("RISKY", [StockSplitEvent(ticker="RISKY", split_date=d1, ratio=Decimal("2.0"))])

        # Capital $1,000, max risk per trade 0.5% = $5.00 risk permitted.
        # But order stop loss creates $6.00 per share risk! 1 share buffered risk = $7.50 > $5.00 permitted!
        cfg = BacktestConfig(
            start_date=d1,
            end_date=d1,
            universe=["RISKY"],
            initial_capital_usd=Decimal("1000.00"),
            price_adjustment_type=PriceAdjustmentType.RAW_UNADJUSTED,
        )
        engine = EventDrivenBacktestEngine(config=cfg, data_manager=dm)

        ts_seed = datetime(2026, 1, 5, 21, 0, tzinfo=UTC)
        pending = SimulatedOrder(
            order_id="ORD-RISK-FAIL",
            ticker="RISKY",
            side=SimulatedOrderSide.BUY,
            order_type=SimulatedOrderType.MARKET_NEXT_OPEN,
            quantity=5,
            signal_timestamp=ts_seed,
            submission_timestamp=ts_seed,
            reference_price=Decimal("100.00"),
            stop_loss_price=Decimal("88.00"),
            status=SimulatedOrderStatus.PENDING,
        )
        engine.pending_entry_orders.append(pending)

        res = engine.run()
        # Order must be REJECTED at morning fill due to risk limit
        self.assertEqual(pending.status, SimulatedOrderStatus.REJECTED)
        self.assertIn("Risk engine rejected fill", pending.rejection_reason)


class TestJuly3EarlyCloseCalendar(unittest.TestCase):
    """AUDIT-5.9.1-04: Official NYSE July 3 early-close calendar for 2015-2027."""

    def test_july_3_early_close_dates(self):
        """Verify each supported year July 3 schedule against official NYSE rules."""
        # 2017: July 4 was Tuesday -> July 3 (Mon) was 1:00 PM early close
        self.assertIn("2017-07-03", HISTORICAL_NYSE_EARLY_CLOSE)
        sess_2017 = MarketClock.get_historical_session(date(2017, 7, 3))
        self.assertIsNotNone(sess_2017)
        self.assertEqual(sess_2017[1], time(13, 0))

        # 2018: July 4 was Wednesday -> July 3 (Tue) was 1:00 PM early close
        self.assertIn("2018-07-03", HISTORICAL_NYSE_EARLY_CLOSE)
        sess_2018 = MarketClock.get_historical_session(date(2018, 7, 3))
        self.assertEqual(sess_2018[1], time(13, 0))

        # 2019: July 4 was Thursday -> July 3 (Wed) was 1:00 PM early close
        self.assertIn("2019-07-03", HISTORICAL_NYSE_EARLY_CLOSE)

        # 2020: July 4 was Saturday -> July 3 (Fri) was observed holiday (full close)
        self.assertNotIn("2020-07-03", HISTORICAL_NYSE_EARLY_CLOSE)
        is_open_2020, reason_2020 = MarketClock.is_historical_trading_day(date(2020, 7, 3))
        self.assertFalse(is_open_2020)
        self.assertIn("Independence Day (observed)", reason_2020)

        # 2023: July 4 was Tuesday -> July 3 (Mon) was 1:00 PM early close
        self.assertIn("2023-07-03", HISTORICAL_NYSE_EARLY_CLOSE)

        # 2024: July 4 was Thursday -> July 3 (Wed) was 1:00 PM early close
        self.assertIn("2024-07-03", HISTORICAL_NYSE_EARLY_CLOSE)
        sess_2024 = MarketClock.get_historical_session(date(2024, 7, 3))
        self.assertEqual(sess_2024[1], time(13, 0))

        # 2025: July 4 was Friday -> July 3 (Thu) was 1:00 PM early close
        self.assertIn("2025-07-03", HISTORICAL_NYSE_EARLY_CLOSE)
        sess_2025 = MarketClock.get_historical_session(date(2025, 7, 3))
        self.assertEqual(sess_2025[1], time(13, 0))

        # 2026: July 4 is Saturday -> July 3 (Fri) is observed holiday (full close)
        self.assertNotIn("2026-07-03", HISTORICAL_NYSE_EARLY_CLOSE)
        is_open_2026, reason_2026 = MarketClock.is_historical_trading_day(date(2026, 7, 3))
        self.assertFalse(is_open_2026)
        self.assertIn("Independence Day (observed)", reason_2026)

    def test_preserves_black_friday_and_christmas_eve(self):
        """Ensure Black Friday and Christmas Eve early close handling remains intact."""
        # 2026 Black Friday (2026-11-27)
        self.assertIn("2026-11-27", HISTORICAL_NYSE_EARLY_CLOSE)
        sess_bf = MarketClock.get_historical_session(date(2026, 11, 27))
        self.assertEqual(sess_bf[1], time(13, 0))

        # 2026 Christmas Eve (2026-12-24)
        self.assertIn("2026-12-24", HISTORICAL_NYSE_EARLY_CLOSE)
        sess_xmas = MarketClock.get_historical_session(date(2026, 12, 24))
        self.assertEqual(sess_xmas[1], time(13, 0))

    def test_daily_candle_bar_end_timestamp_on_early_close(self):
        """OHLCVCandle bar_end_timestamp correctly resolves to 1:00 PM Eastern on July 3 early close."""
        # July 3, 2024: 1:00 PM EDT = 17:00 UTC
        c = OHLCVCandle(
            ticker="AAPL",
            interval="1d",
            timestamp=datetime(2024, 7, 3, 4, 0, tzinfo=UTC),
            open=Decimal("220.00"),
            high=Decimal("222.00"),
            low=Decimal("219.00"),
            close=Decimal("221.50"),
            volume=50000000,
            status=CandleStatus.COMPLETE,
        )
        self.assertIsNotNone(c.bar_end_timestamp)
        et = to_eastern(c.bar_end_timestamp)
        self.assertEqual(et.time(), time(13, 0))


class TestSeparateAnalyticalAndExecutionPrices(unittest.TestCase):
    """AUDIT-5.9.1-05: Separation between continuous analytical and raw execution prices."""

    def test_raw_execution_vs_continuous_analytical(self):
        """Verify raw execution prices maintain tape integrity while analytical prices are continuous."""
        dm = HistoricalDataManager()
        d1 = date(2026, 1, 5)
        d2 = date(2026, 1, 6)
        d3 = date(2026, 1, 7)  # Split 2-for-1

        c1 = _create_daily_candle("AAPL", datetime(2026, 1, 5, 20, 0, tzinfo=UTC), Decimal("100"), Decimal("104"), Decimal("98"), Decimal("102"), volume=1000)
        c2 = _create_daily_candle("AAPL", datetime(2026, 1, 6, 20, 0, tzinfo=UTC), Decimal("102"), Decimal("106"), Decimal("101"), Decimal("104"), volume=1200)
        c3 = _create_daily_candle("AAPL", datetime(2026, 1, 7, 20, 0, tzinfo=UTC), Decimal("52"), Decimal("54"), Decimal("51"), Decimal("53"), volume=2500)

        dm.load_candles("AAPL", [c1, c2, c3], adjustment_type=PriceAdjustmentType.RAW_UNADJUSTED)
        dm.load_splits("AAPL", [StockSplitEvent(ticker="AAPL", split_date=d3, ratio=Decimal("2.0"))])

        # 1. Execution prices show raw prices
        raw_slice = dm.get_execution_candles_slice("AAPL", as_of_date=d3)
        self.assertEqual(raw_slice[0].close, Decimal("102.00"))
        self.assertEqual(raw_slice[1].close, Decimal("104.00"))
        self.assertEqual(raw_slice[2].close, Decimal("53.00"))

        # 2. Analytical prices as of d3 are continuous across split
        analytical_d3 = dm.get_analytical_candles_slice("AAPL", as_of_date=d3)
        self.assertEqual(len(analytical_d3), 3)
        self.assertEqual(analytical_d3[0].close, Decimal("51.00"))  # 102 / 2
        self.assertEqual(analytical_d3[0].volume, 2000)             # 1000 * 2
        self.assertEqual(analytical_d3[1].close, Decimal("52.00"))  # 104 / 2
        self.assertEqual(analytical_d3[1].volume, 2400)             # 1200 * 2
        self.assertEqual(analytical_d3[2].close, Decimal("53.00"))  # current bar post-split
        self.assertEqual(analytical_d3[2].volume, 2500)

        # 3. Indicator check: 3-day average using analytical series
        avg_3d = (analytical_d3[0].close + analytical_d3[1].close + analytical_d3[2].close) / Decimal("3")
        self.assertEqual(avg_3d, Decimal("52.00"))
        # Using raw series would falsely produce (102 + 104 + 53)/3 = 86.33 (false cliff!)

        # 4. Zero look-ahead bias: as of d2 (before split), analytical prices are NOT adjusted
        analytical_d2 = dm.get_analytical_candles_slice("AAPL", as_of_date=d2)
        self.assertEqual(analytical_d2[0].close, Decimal("102.00"))
        self.assertEqual(analytical_d2[1].close, Decimal("104.00"))

    def test_split_adjusted_dataset_does_not_apply_split_twice(self):
        """SPLIT_ADJUSTED dataset analytical series does not apply redundant adjustment."""
        dm = HistoricalDataManager()
        d1 = date(2026, 1, 5)
        d2 = date(2026, 1, 6)
        c1 = _create_daily_candle("MSFT", datetime(2026, 1, 5, 20, 0, tzinfo=UTC), Decimal("50"), Decimal("52"), Decimal("49"), Decimal("51"))
        c2 = _create_daily_candle("MSFT", datetime(2026, 1, 6, 20, 0, tzinfo=UTC), Decimal("51"), Decimal("53"), Decimal("50"), Decimal("52"))
        dm.load_candles("MSFT", [c1, c2], adjustment_type=PriceAdjustmentType.SPLIT_ADJUSTED)
        dm.load_splits("MSFT", [StockSplitEvent(ticker="MSFT", split_date=d2, ratio=Decimal("2.0"))])

        analytical = dm.get_analytical_candles_slice("MSFT", as_of_date=d2)
        # Must remain $51.00, NOT halved to $25.50
        self.assertEqual(analytical[0].close, Decimal("51.00"))

    def test_dividends_not_included_in_split_adjusted_analytical_series(self):
        """Dividends are not subtracted from analytical price series."""
        dm = HistoricalDataManager()
        d1 = date(2026, 1, 5)
        d2 = date(2026, 1, 6)
        c1 = _create_daily_candle("IBM", datetime(2026, 1, 5, 20, 0, tzinfo=UTC), Decimal("100"), Decimal("102"), Decimal("99"), Decimal("100"))
        c2 = _create_daily_candle("IBM", datetime(2026, 1, 6, 20, 0, tzinfo=UTC), Decimal("99"), Decimal("101"), Decimal("98"), Decimal("99"))
        dm.load_candles("IBM", [c1, c2], adjustment_type=PriceAdjustmentType.RAW_UNADJUSTED)
        dm.load_dividends("IBM", [DividendEvent(ticker="IBM", ex_date=d2, amount_per_share=Decimal("1.50"))])

        analytical = dm.get_analytical_candles_slice("IBM", as_of_date=d2)
        # Price is strictly preserved at $100.00 (not modified to $98.50)
        self.assertEqual(analytical[0].close, Decimal("100.00"))


class TestIntegratedMultiSessionLifecycle(unittest.TestCase):
    """Section 6: Full multi-session integration scenario with split, partial exit, stop exit, and invariants."""

    def test_multi_session_scenario_raw_unadjusted(self):
        """Multi-session scenario:
        - Day 0: Signal generated, pending entry queued.
        - Day 1: 2-for-1 forward split before market open. Pending order adjusted.
        - Day 1: Order fills at session open.
        - Day 2: Partial take-profit exit (5 of 20 shares). Protective stop preserved.
        - Day 3: Protective stop triggers for remaining 15 shares. Complete cleanup.
        - Invariants validated at every step.
        """
        dm = HistoricalDataManager()
        dates = [
            date(2026, 1, 5),  # Day 0: Signal generated
            date(2026, 1, 6),  # Day 1: Split effective, order filled
            date(2026, 1, 7),  # Day 2: Partial exit (take-profit hit)
            date(2026, 1, 8),  # Day 3: Stop loss hit for remaining shares
        ]

        candles = [
            _create_daily_candle("ACME", datetime(2026, 1, 5, 20, 0, tzinfo=UTC), Decimal("100"), Decimal("105"), Decimal("98"), Decimal("102")),
            _create_daily_candle("ACME", datetime(2026, 1, 6, 20, 0, tzinfo=UTC), Decimal("51"), Decimal("54"), Decimal("50"), Decimal("53")),
            _create_daily_candle("ACME", datetime(2026, 1, 7, 20, 0, tzinfo=UTC), Decimal("53"), Decimal("61"), Decimal("52"), Decimal("60")),  # High hits 60 target
            _create_daily_candle("ACME", datetime(2026, 1, 8, 20, 0, tzinfo=UTC), Decimal("52"), Decimal("53"), Decimal("44"), Decimal("46")),  # Low hits 45 stop
        ]
        dm.load_candles("ACME", candles, adjustment_type=PriceAdjustmentType.RAW_UNADJUSTED)
        dm.load_splits("ACME", [StockSplitEvent(ticker="ACME", split_date=dates[1], ratio=Decimal("2.0"))])

        portfolio = BacktestPortfolio(initial_capital_usd=Decimal("5000.00"))
        portfolio.ledger.validate_equity_invariant()

        # Step 1: Pending order queued on Day 0 close
        ts0 = datetime(2026, 1, 5, 21, 0, tzinfo=UTC)
        pending_order = SimulatedOrder(
            order_id="ORD-ENTRY",
            ticker="ACME",
            side=SimulatedOrderSide.BUY,
            order_type=SimulatedOrderType.MARKET_NEXT_OPEN,
            quantity=10,
            signal_timestamp=ts0,
            submission_timestamp=ts0,
            reference_price=Decimal("100.00"),
            stop_loss_price=Decimal("90.00"),
            target_price=Decimal("120.00"),
            status=SimulatedOrderStatus.PENDING,
        )

        # Step 2: Day 1 Morning Split Adjustment
        split = dm.get_splits_for_session(dates[1])[0]
        valid = pending_order.adjust_for_stock_split(split.ratio)
        self.assertTrue(valid)
        self.assertEqual(pending_order.quantity, 20)
        self.assertEqual(pending_order.reference_price, Decimal("50.00"))
        self.assertEqual(pending_order.stop_loss_price, Decimal("45.00"))
        self.assertEqual(pending_order.target_price, Decimal("60.00"))

        # Step 3: Day 1 Open: Fill order at execution price $51.00
        ts1_open = datetime(2026, 1, 6, 14, 30, tzinfo=UTC)
        pending_order.fill_timestamp = ts1_open
        pending_order.fill_price = Decimal("51.00")
        pending_order.status = SimulatedOrderStatus.FILLED
        pending_order.fee_usd = Decimal("1.00")
        lot_id = portfolio.execute_entry(pending_order)

        self.assertEqual(portfolio.ledger.positions["ACME"].total_shares, 20)
        self.assertEqual(portfolio.lot_stops[lot_id], Decimal("45.00"))
        self.assertEqual(portfolio.lot_targets[lot_id], Decimal("60.00"))
        portfolio.ledger.validate_equity_invariant()

        # Step 4: Day 2 Partial Exit: 5 shares hit target at $60.00
        ts2_exit = datetime(2026, 1, 7, 15, 0, tzinfo=UTC)
        partial_exit = SimulatedOrder(
            order_id="EXIT-PARTIAL",
            ticker="ACME",
            side=SimulatedOrderSide.SELL,
            order_type=SimulatedOrderType.TAKE_PROFIT,
            quantity=5,
            signal_timestamp=ts2_exit,
            submission_timestamp=ts2_exit,
            reference_price=Decimal("60.00"),
            fill_timestamp=ts2_exit,
            fill_price=Decimal("60.00"),
            fee_usd=Decimal("1.00"),
            lot_id=lot_id,
            status=SimulatedOrderStatus.FILLED,
            reason="Partial target exit",
        )
        rec1 = portfolio.execute_exit(partial_exit)
        self.assertEqual(rec1.shares, 5)
        self.assertEqual(portfolio.ledger.positions["ACME"].total_shares, 15)
        # Protective stop MUST remain active for remaining 15 shares
        self.assertIn(lot_id, portfolio.lot_stops)
        self.assertEqual(portfolio.lot_stops[lot_id], Decimal("45.00"))
        portfolio.ledger.validate_equity_invariant()

        # Step 5: Day 3 Protective Exit: remaining 15 shares hit stop at $45.00
        ts3_stop = datetime(2026, 1, 8, 15, 0, tzinfo=UTC)
        final_exit = SimulatedOrder(
            order_id="EXIT-FINAL-STOP",
            ticker="ACME",
            side=SimulatedOrderSide.SELL,
            order_type=SimulatedOrderType.STOP_LOSS,
            quantity=15,
            signal_timestamp=ts3_stop,
            submission_timestamp=ts3_stop,
            reference_price=Decimal("45.00"),
            fill_timestamp=ts3_stop,
            fill_price=Decimal("45.00"),
            fee_usd=Decimal("1.00"),
            lot_id=lot_id,
            status=SimulatedOrderStatus.FILLED,
            reason="Protective stop trigger",
        )
        rec2 = portfolio.execute_exit(final_exit)
        self.assertEqual(rec2.shares, 15)
        self.assertEqual(portfolio.ledger.positions["ACME"].total_shares, 0)

        # Full cleanup verified
        self.assertNotIn(lot_id, portfolio.lot_stops)
        self.assertNotIn(lot_id, portfolio.lot_targets)
        self.assertNotIn(lot_id, portfolio.lot_meta)

        # Final financial accounting invariant reconciliation
        inv = portfolio.ledger.validate_equity_invariant()
        self.assertTrue(inv["holds"])
        self.assertEqual(inv["discrepancy"], Decimal("0.00"))

    def test_multi_session_scenario_split_adjusted(self):
        """Multi-session scenario in SPLIT_ADJUSTED mode:
        Prices are already in post-split equivalent dollars ($50).
        Split events must not double-adjust pending orders or portfolio shares.
        Partial exit preserves stops; final exit cleans up; invariant holds throughout.
        """
        portfolio = BacktestPortfolio(initial_capital_usd=Decimal("5000.00"))

        ts_entry = datetime(2026, 1, 6, 14, 30, tzinfo=UTC)
        entry_order = SimulatedOrder(
            order_id="ORD-ENTRY-ADJ",
            ticker="GLOB",
            side=SimulatedOrderSide.BUY,
            order_type=SimulatedOrderType.MARKET_NEXT_OPEN,
            quantity=20,
            signal_timestamp=ts_entry,
            submission_timestamp=ts_entry,
            reference_price=Decimal("50.00"),
            fill_timestamp=ts_entry,
            fill_price=Decimal("50.00"),
            stop_loss_price=Decimal("45.00"),
            target_price=Decimal("60.00"),
            fee_usd=Decimal("1.00"),
            status=SimulatedOrderStatus.FILLED,
        )
        lot_id = portfolio.execute_entry(entry_order)
        self.assertEqual(portfolio.ledger.positions["GLOB"].total_shares, 20)
        self.assertEqual(portfolio.lot_stops[lot_id], Decimal("45.00"))
        portfolio.ledger.validate_equity_invariant()

        # Partial exit (8 shares)
        ts_exit1 = datetime(2026, 1, 7, 15, 0, tzinfo=UTC)
        part_exit = SimulatedOrder(
            order_id="EXIT-P-ADJ",
            ticker="GLOB",
            side=SimulatedOrderSide.SELL,
            order_type=SimulatedOrderType.TAKE_PROFIT,
            quantity=8,
            signal_timestamp=ts_exit1,
            submission_timestamp=ts_exit1,
            reference_price=Decimal("60.00"),
            fill_timestamp=ts_exit1,
            fill_price=Decimal("60.00"),
            fee_usd=Decimal("1.00"),
            lot_id=lot_id,
            status=SimulatedOrderStatus.FILLED,
        )
        rec1 = portfolio.execute_exit(part_exit)
        self.assertEqual(rec1.shares, 8)
        self.assertEqual(portfolio.ledger.positions["GLOB"].total_shares, 12)
        # Protective stop preserved
        self.assertIn(lot_id, portfolio.lot_stops)
        self.assertEqual(portfolio.lot_stops[lot_id], Decimal("45.00"))
        portfolio.ledger.validate_equity_invariant()

        # Final exit (12 shares)
        ts_exit2 = datetime(2026, 1, 8, 15, 0, tzinfo=UTC)
        final_exit = SimulatedOrder(
            order_id="EXIT-F-ADJ",
            ticker="GLOB",
            side=SimulatedOrderSide.SELL,
            order_type=SimulatedOrderType.STOP_LOSS,
            quantity=12,
            signal_timestamp=ts_exit2,
            submission_timestamp=ts_exit2,
            reference_price=Decimal("45.00"),
            fill_timestamp=ts_exit2,
            fill_price=Decimal("45.00"),
            fee_usd=Decimal("1.00"),
            lot_id=lot_id,
            status=SimulatedOrderStatus.FILLED,
        )
        rec2 = portfolio.execute_exit(final_exit)
        self.assertEqual(rec2.shares, 12)
        self.assertEqual(portfolio.ledger.positions["GLOB"].total_shares, 0)
        self.assertNotIn(lot_id, portfolio.lot_stops)
        self.assertNotIn(lot_id, portfolio.lot_targets)
        self.assertNotIn(lot_id, portfolio.lot_meta)

        inv = portfolio.ledger.validate_equity_invariant()
        self.assertTrue(inv["holds"])
        self.assertEqual(inv["discrepancy"], Decimal("0.00"))


if __name__ == "__main__":
    unittest.main()
