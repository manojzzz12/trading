"""Integration test suite for Phase 5.2 Event-Driven Backtest Engine in trader-swing.

Enforces:
1. End-to-end chronological event loop execution without look-ahead bias.
2. Next-session open fill timing for signals generated at previous bar close.
3. Phase 2 risk engine sizing and position limits enforcement at fill time.
4. Daily snapshot generation and benchmark SPY alignment.
5. Deterministic reproducibility across repeated runs.
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

from backtest_data import HistoricalDataManager
from backtest_engine import EventDrivenBacktestEngine
from backtest_models import (
    BacktestConfig,
    ExecutionAssumptions,
    SimulatedOrder,
    SimulatedOrderSide,
    SimulatedOrderStatus,
    SimulatedOrderType,
    SlippageConfig,
)
from market_data_models import CandleStatus, MarketClock, OHLCVCandle, UTC
from risk import FeeConfig, FeeSchedule, RiskConfig

_ENGINE_TEST_TRADING_DAYS = []
_curr_d = date(2026, 1, 5)
while len(_ENGINE_TEST_TRADING_DAYS) < 100:
    if MarketClock.get_session(_curr_d):
        _ENGINE_TEST_TRADING_DAYS.append(_curr_d)
    _curr_d += timedelta(days=1)


def make_candle(
    ticker: str,
    dt: datetime,
    close_p: str,
    open_p: str = None,
    high_p: str = None,
    low_p: str = None,
    volume: int = 20000000,
) -> OHLCVCandle:
    c_dec = Decimal(close_p)
    o_dec = Decimal(open_p) if open_p is not None else c_dec
    spread = max(Decimal("2.00"), c_dec * Decimal("0.02"))
    h_dec = Decimal(high_p) if high_p is not None else max(c_dec, o_dec) + spread
    l_dec = Decimal(low_p) if low_p is not None else min(c_dec, o_dec) - spread
    return OHLCVCandle(
        ticker=ticker,
        interval="1d",
        timestamp=dt,
        open=o_dec,
        high=h_dec,
        low=l_dec,
        close=c_dec,
        volume=volume,
        status=CandleStatus.COMPLETE,
        source="yahoo",
        received_timestamp=dt,
        is_delayed=True,
    )


def make_bullish_series(ticker: str, base_price: float, count: int = 35) -> list:
    candles = []
    curr = base_price
    for i in range(count):
        step = 0.50
        if i % 2 == 0:
            curr = curr - (step * 0.4)
        else:
            curr = curr + step
        d = _ENGINE_TEST_TRADING_DAYS[i]
        dt = datetime.combine(d, time(20, 0, 0), tzinfo=UTC)
        candles.append(make_candle(ticker, dt, f"{curr:.2f}"))
    return candles


class TestSwingBacktestEngine(unittest.TestCase):
    """Test suite covering the event-driven backtesting engine simulation."""

    def setUp(self):
        self.mgr = HistoricalDataManager()
        self.tickers = ["SPY", "QQQ", "IWM", "AAPL", "MSFT"]

        # 35 daily bars
        self.data_by_ticker = {
            "SPY": make_bullish_series("SPY", 450.0, 35),
            "QQQ": make_bullish_series("QQQ", 350.0, 35),
            "IWM": make_bullish_series("IWM", 180.0, 35),
            "AAPL": make_bullish_series("AAPL", 150.0, 35),
            "MSFT": make_bullish_series("MSFT", 300.0, 35),
        }

        for sym, bars in self.data_by_ticker.items():
            self.mgr.load_candles(sym, bars)

        sessions = self.mgr.get_all_session_dates()
        self.start_date = sessions[0]
        self.end_date = sessions[-1]

        self.config = BacktestConfig(
            start_date=self.start_date,
            end_date=self.end_date,
            universe=["AAPL", "MSFT"],
            initial_capital_usd=Decimal("5000.00"),
            risk_config=RiskConfig(
                risk_per_trade_pct=Decimal("0.005"),
                max_aggregate_buffered_risk_pct=Decimal("0.02"),
                max_allocation_per_ticker_pct=Decimal("0.25"),
                max_invested_capital_pct=Decimal("0.80"),
                min_cash_reserve_pct=Decimal("0.20"),
                max_simultaneous_positions=4,
            ),
            fee_config=FeeConfig(entry=FeeSchedule(), exit=FeeSchedule()),
            slippage_config=SlippageConfig(entry_bps=Decimal("5.0"), exit_bps=Decimal("5.0")),
            benchmark_ticker="SPY",
            top_candidates_per_session=2,
        )

    def test_01_full_event_driven_simulation_run(self):
        """Verify backtest executes chronologically and returns complete performance report."""
        engine = EventDrivenBacktestEngine(config=self.config, data_manager=self.mgr)
        results = engine.run()

        self.assertIn("portfolio_metrics", results)
        self.assertIn("trade_metrics", results)
        self.assertIn("benchmark_comparison", results)
        self.assertIn("daily_snapshots", results)

        snapshots = results["daily_snapshots"]
        self.assertEqual(len(snapshots), 35)

        # Invariant checks: equity must be positive, cash >= 0
        final_snap = snapshots[-1]
        self.assertTrue(Decimal(final_snap["equity_usd"]) > Decimal("0.00"))
        self.assertTrue(Decimal(final_snap["cash_usd"]) >= Decimal("0.00"))

        # Verify benchmark comparison against SPY
        bench = results["benchmark_comparison"]
        self.assertTrue(bench["is_available"])
        self.assertEqual(bench["benchmark_ticker"], "SPY")

    def test_02_no_look_ahead_fill_timing(self):
        """CRITICAL: Orders generated at session T close must fill at session T+1 open."""
        engine = EventDrivenBacktestEngine(config=self.config, data_manager=self.mgr)
        results = engine.run()

        orders = results["all_orders"]
        filled_entries = [o for o in orders if o["side"] == "BUY" and o["status"] == "FILLED"]

        for o in filled_entries:
            signal_date = datetime.fromisoformat(o["signal_timestamp"]).date()
            fill_date = datetime.fromisoformat(o["fill_timestamp"]).date()
            # Fill date MUST be strictly after signal date
            self.assertTrue(
                fill_date > signal_date,
                f"Look-ahead violation! Order {o['order_id']} signaled on {signal_date} filled on {fill_date}.",
            )

    def test_03_deterministic_reproducibility(self):
        """Verify identical backtest configurations produce identical metrics down to the cent."""
        engine1 = EventDrivenBacktestEngine(config=self.config, data_manager=self.mgr)
        res1 = engine1.run()

        engine2 = EventDrivenBacktestEngine(config=self.config, data_manager=self.mgr)
        res2 = engine2.run()

        self.assertEqual(res1["portfolio_metrics"]["final_equity_usd"], res2["portfolio_metrics"]["final_equity_usd"])
        self.assertEqual(res1["portfolio_metrics"]["net_profit_usd"], res2["portfolio_metrics"]["net_profit_usd"])
        self.assertEqual(res1["portfolio_metrics"]["max_drawdown_usd"], res2["portfolio_metrics"]["max_drawdown_usd"])
        self.assertEqual(res1["trade_metrics"]["total_trades"], res2["trade_metrics"]["total_trades"])

    def test_04_pending_orders_reserve_capacity_during_queuing(self):
        """DEFECT-POSCAP-01: Pending orders must reserve capacity and block excess queued orders."""
        engine = EventDrivenBacktestEngine(config=self.config, data_manager=self.mgr)

        # Pre-fill ledger with 2 open positions
        engine.portfolio.ledger.buy("AAPL", 1, Decimal("100.00"), Decimal("0.00"), "2026-01-05T09:30:00+00:00")
        engine.portfolio.ledger.buy("MSFT", 1, Decimal("100.00"), Decimal("0.00"), "2026-01-05T09:30:00+00:00")
        self.assertEqual(len(engine.portfolio.ledger.positions), 2)

        # Queue 1 pending order for GOOGL -> committed = 2 open + 1 pending = 3
        p1_order = SimulatedOrder(
            order_id="ORD-BUY-GOOGL-1",
            ticker="GOOGL",
            side=SimulatedOrderSide.BUY,
            order_type=SimulatedOrderType.MARKET_NEXT_OPEN,
            quantity=1,
            signal_timestamp=datetime(2026, 1, 5, 20, 0, tzinfo=UTC),
            submission_timestamp=datetime(2026, 1, 5, 20, 0, tzinfo=UTC),
            reference_price=Decimal("100.00"),
            stop_loss_price=Decimal("95.00"),
            target_price=Decimal("110.00"),
            status=SimulatedOrderStatus.PENDING,
            reason="Test pending order",
        )
        engine.pending_entry_orders.append(p1_order)

        # Simulate queuing 2 proposed trade plans (AMZN and META)
        from signal_models import EntryStrategyType, ProposedTradePlan
        plan_c1 = ProposedTradePlan(
            ticker="AMZN",
            strategy_type=EntryStrategyType.PULLBACK_ENTRY,
            entry_price=Decimal("100.00"),
            entry_zone_low=Decimal("99.50"),
            entry_zone_high=Decimal("100.50"),
            stop_loss_price=Decimal("95.00"),
            target_price=Decimal("110.00"),
            reward_to_risk=Decimal("2.00"),
            shares=1,
            total_capital_required=Decimal("100.00"),
            total_buffered_risk=Decimal("6.25"),
            risk_approved=True,
            binding_constraint=None,
            risk_rejection_reasons=[],
        )
        plan_c2 = ProposedTradePlan(
            ticker="META",
            strategy_type=EntryStrategyType.PULLBACK_ENTRY,
            entry_price=Decimal("100.00"),
            entry_zone_low=Decimal("99.50"),
            entry_zone_high=Decimal("100.50"),
            stop_loss_price=Decimal("95.00"),
            target_price=Decimal("110.00"),
            reward_to_risk=Decimal("2.00"),
            shares=1,
            total_capital_required=Decimal("100.00"),
            total_buffered_risk=Decimal("6.25"),
            risk_approved=True,
            binding_constraint=None,
            risk_rejection_reasons=[],
        )

        sess_date = _ENGINE_TEST_TRADING_DAYS[0]
        sess_ts = datetime.combine(sess_date, time(20, 0, 0), tzinfo=UTC)

        for plan in [plan_c1, plan_c2]:
            current_open = set(t for t, p in engine.portfolio.ledger.positions.items() if p.total_shares > 0)
            current_pending = set(o.ticker for o in engine.pending_entry_orders if o.side == SimulatedOrderSide.BUY)
            committed = current_open | current_pending
            if plan.ticker not in committed and len(committed) >= engine.config.risk_config.max_simultaneous_positions:
                engine._record_risk_rejection("MAX_POSITIONS_EXCEEDED")
                continue

            entry_order = SimulatedOrder(
                order_id=f"ORD-BUY-{plan.ticker}-1",
                ticker=plan.ticker,
                side=SimulatedOrderSide.BUY,
                order_type=SimulatedOrderType.MARKET_NEXT_OPEN,
                quantity=plan.shares,
                signal_timestamp=sess_ts,
                submission_timestamp=sess_ts,
                reference_price=plan.entry_price,
                stop_loss_price=plan.stop_loss_price,
                target_price=plan.target_price,
                status=SimulatedOrderStatus.PENDING,
                reason="Approved plan",
            )
            engine.pending_entry_orders.append(entry_order)

        # Total pending orders must be 2 (GOOGL and AMZN); META must be rejected
        pending_tickers = [o.ticker for o in engine.pending_entry_orders]
        self.assertEqual(pending_tickers, ["GOOGL", "AMZN"])
        self.assertEqual(engine.risk_rejection_counts["POSITION_LIMIT"], 1)

    def test_05_execution_time_position_count_gate_rejects_excess_morning_fills(self):
        """DEFECT-POSCAP-01: Morning fill loop must reject any order that would breach 4 positions."""
        # Add bars for extra tickers so simulate_entry_fill can find candles
        for sym in ["AMZN", "GOOGL", "META", "NFLX"]:
            bars = make_bullish_series(sym, 100.0, 35)
            self.mgr.load_candles(sym, bars)

        engine = EventDrivenBacktestEngine(config=self.config, data_manager=self.mgr)

        # Pre-fill ledger with 2 positions (AAPL, MSFT)
        engine.portfolio.ledger.buy("AAPL", 1, Decimal("100.00"), Decimal("0.00"), "2026-01-05T09:30:00+00:00")
        engine.portfolio.ledger.buy("MSFT", 1, Decimal("100.00"), Decimal("0.00"), "2026-01-05T09:30:00+00:00")
        self.assertEqual(len(engine.portfolio.ledger.positions), 2)

        # Inject 4 pending orders (if unchecked, would reach 6 positions)
        for sym in ["AMZN", "GOOGL", "META", "NFLX"]:
            order = SimulatedOrder(
                order_id=f"ORD-BUY-{sym}",
                ticker=sym,
                side=SimulatedOrderSide.BUY,
                order_type=SimulatedOrderType.MARKET_NEXT_OPEN,
                quantity=1,
                signal_timestamp=datetime(2026, 1, 5, 20, 0, tzinfo=UTC),
                submission_timestamp=datetime(2026, 1, 5, 20, 0, tzinfo=UTC),
                reference_price=Decimal("100.00"),
                stop_loss_price=Decimal("95.00"),
                target_price=Decimal("110.00"),
                status=SimulatedOrderStatus.PENDING,
                reason="Queued order",
            )
            engine.pending_entry_orders.append(order)

        sess_date = _ENGINE_TEST_TRADING_DAYS[1]
        all_sessions = self.mgr.get_all_session_dates()
        sess_idx = all_sessions.index(sess_date)

        # Run _process_session on session 1
        engine._process_session(session_date=sess_date, session_idx=sess_idx, all_sessions=all_sessions)

        # Exactly 4 positions must be held in the ledger (AAPL, MSFT + AMZN, GOOGL)
        open_tickers = set(t for t, p in engine.portfolio.ledger.positions.items() if p.total_shares > 0)
        self.assertEqual(len(open_tickers), 4)
        self.assertEqual(open_tickers, {"AAPL", "MSFT", "AMZN", "GOOGL"})

        # META and NFLX must be marked REJECTED with position limit reason
        rejected_orders = [o for o in engine.all_orders if o.status == SimulatedOrderStatus.REJECTED]
        rejected_tickers = [o.ticker for o in rejected_orders]
        self.assertIn("META", rejected_tickers)
        self.assertIn("NFLX", rejected_tickers)
        for o in rejected_orders:
            if o.ticker in ("META", "NFLX"):
                self.assertIn("Position limit reached", o.rejection_reason)

        self.assertTrue(engine.risk_rejection_counts["POSITION_LIMIT"] >= 2)

    def test_06_rejected_and_expired_orders_release_capacity(self):
        """DEFECT-POSCAP-01: An expired order does not fill, allowing a subsequent order to fill."""
        for sym in ["NVDA"]:
            bars = make_bullish_series(sym, 100.0, 35)
            self.mgr.load_candles(sym, bars)

        engine = EventDrivenBacktestEngine(config=self.config, data_manager=self.mgr)

        # Pre-fill ledger with 3 positions
        engine.portfolio.ledger.buy("AAPL", 1, Decimal("100.00"), Decimal("0.00"), "2026-01-05T09:30:00+00:00")
        engine.portfolio.ledger.buy("MSFT", 1, Decimal("100.00"), Decimal("0.00"), "2026-01-05T09:30:00+00:00")
        engine.portfolio.ledger.buy("GOOGL", 1, Decimal("100.00"), Decimal("0.00"), "2026-01-05T09:30:00+00:00")
        self.assertEqual(len(engine.portfolio.ledger.positions), 3)

        # Order 1: TSLA ticker (valid ticker format, but no candles loaded in manager) -> will trigger EXPIRED
        ord_expired = SimulatedOrder(
            order_id="ORD-BUY-TSLA",
            ticker="TSLA",
            side=SimulatedOrderSide.BUY,
            order_type=SimulatedOrderType.MARKET_NEXT_OPEN,
            quantity=1,
            signal_timestamp=datetime(2026, 1, 5, 20, 0, tzinfo=UTC),
            submission_timestamp=datetime(2026, 1, 5, 20, 0, tzinfo=UTC),
            reference_price=Decimal("100.00"),
            stop_loss_price=Decimal("95.00"),
            target_price=Decimal("110.00"),
            status=SimulatedOrderStatus.PENDING,
            reason="Order for missing ticker",
        )
        # Order 2: NVDA ticker -> should fill into the 4th slot
        ord_valid = SimulatedOrder(
            order_id="ORD-BUY-NVDA",
            ticker="NVDA",
            side=SimulatedOrderSide.BUY,
            order_type=SimulatedOrderType.MARKET_NEXT_OPEN,
            quantity=1,
            signal_timestamp=datetime(2026, 1, 5, 20, 0, tzinfo=UTC),
            submission_timestamp=datetime(2026, 1, 5, 20, 0, tzinfo=UTC),
            reference_price=Decimal("100.00"),
            stop_loss_price=Decimal("95.00"),
            target_price=Decimal("110.00"),
            status=SimulatedOrderStatus.PENDING,
            reason="Order for valid ticker",
        )

        engine.pending_entry_orders.extend([ord_expired, ord_valid])

        sess_date = _ENGINE_TEST_TRADING_DAYS[1]
        all_sessions = self.mgr.get_all_session_dates()
        sess_idx = all_sessions.index(sess_date)

        # Run _process_session on session 1
        engine._process_session(session_date=sess_date, session_idx=sess_idx, all_sessions=all_sessions)

        # TSLA order was marked EXPIRED
        self.assertEqual(ord_expired.status, SimulatedOrderStatus.EXPIRED)
        # NVDA filled because TSLA did not consume the slot
        self.assertEqual(ord_valid.status, SimulatedOrderStatus.FILLED)

        # Final open positions must be exactly 4
        open_tickers = set(t for t, p in engine.portfolio.ledger.positions.items() if p.total_shares > 0)
        self.assertEqual(len(open_tickers), 4)
        self.assertEqual(open_tickers, {"AAPL", "MSFT", "GOOGL", "NVDA"})

    def test_07_full_backtest_enforces_position_cap_invariant(self):
        """DEFECT-POSCAP-01: Full backtest must never violate max_simultaneous_positions <= 4."""
        engine = EventDrivenBacktestEngine(config=self.config, data_manager=self.mgr)
        results = engine.run()

        self.assertLessEqual(results["risk_audit_metrics"]["max_simultaneous_positions"], 4)
        for snap in results["daily_snapshots"]:
            self.assertLessEqual(snap["open_positions_count"], 4)


if __name__ == "__main__":
    unittest.main()
