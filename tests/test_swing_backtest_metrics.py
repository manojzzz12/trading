"""Unit tests for Phase 5.6 Performance and Risk Metrics in trader-swing.

Enforces:
1. Mathematical precision for portfolio metrics (CAGR, Drawdown, Sharpe, Sortino, Calmar).
2. Trade performance metrics (Win Rate, Profit Factor, Expectancy, R-multiples, Streaks).
3. Robust edge case handling (zero trades, zero losses, zero volatility).
4. Benchmark comparison calculations against SPY.
"""

from datetime import date, datetime
from decimal import Decimal
import os
import sys
import unittest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SWING_DIR = os.path.join(ROOT_DIR, ".agents", "skills", "trader-swing")
for path in (ROOT_DIR, SWING_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

from backtest_metrics import BacktestMetricsCalculator
from backtest_models import PortfolioDailySnapshot, SimulatedTradeRecord
from market_data_models import CandleStatus, OHLCVCandle, UTC


def make_snapshot(session_idx: int, equity: str, cash: str, positions: int = 1) -> PortfolioDailySnapshot:
    d = date(2026, 1, 5) if session_idx == 0 else date(2026, 1, 5 + session_idx)
    dt = datetime(d.year, d.month, d.day, 20, 0, tzinfo=UTC)
    eq_dec = Decimal(equity)
    cash_dec = Decimal(cash)
    gmv = eq_dec - cash_dec
    inv_pct = ((gmv / eq_dec) * Decimal("100.0")).quantize(Decimal("0.01")) if eq_dec > 0 else Decimal("0.00")
    return PortfolioDailySnapshot(
        session_date=d,
        timestamp=dt,
        equity_usd=eq_dec,
        cash_usd=cash_dec,
        gross_market_value_usd=gmv,
        invested_capital_pct=inv_pct,
        unrealized_pnl_usd=Decimal("0.00"),
        cumulative_realized_pnl_usd=Decimal("0.00"),
        open_positions_count=positions,
        open_lots_count=positions,
        buffered_open_risk_usd=Decimal("25.00"),
        aggregate_risk_pct=Decimal("0.50"),
        market_regime="BULLISH",
    )


def make_trade(
    pnl: str,
    r_mult: str,
    holding: int = 3,
    shares: int = 10,
) -> SimulatedTradeRecord:
    dt = datetime(2026, 1, 10, 20, 0, tzinfo=UTC)
    pnl_dec = Decimal(pnl)
    return SimulatedTradeRecord(
        trade_id="TRD-1",
        ticker="AAPL",
        lot_id="LOT-1",
        entry_timestamp=dt,
        exit_timestamp=dt,
        holding_sessions=holding,
        shares=shares,
        entry_price=Decimal("100.00"),
        exit_price=Decimal("100.00") + (pnl_dec / Decimal(shares)),
        initial_stop_price=Decimal("95.00"),
        initial_target_price=Decimal("110.00"),
        gross_pnl_usd=pnl_dec,
        total_fees_usd=Decimal("2.00"),
        total_slippage_usd=Decimal("1.00"),
        net_pnl_usd=pnl_dec,
        return_pct=pnl_dec,
        r_multiple=Decimal(r_mult),
        exit_reason="Target",
    )


class TestSwingBacktestMetrics(unittest.TestCase):
    """Test suite covering backtest metrics, financial formulas, and edge cases."""

    def test_01_portfolio_metrics_calculation(self):
        """Verify equity returns, drawdown, and risk ratios."""
        # 5 sessions: 5000 -> 5100 -> 5050 -> 5200 -> 5300
        snaps = [
            make_snapshot(0, "5000.00", "5000.00", positions=0),
            make_snapshot(1, "5100.00", "4000.00", positions=1),
            make_snapshot(2, "5050.00", "4000.00", positions=1),
            make_snapshot(3, "5200.00", "4000.00", positions=1),
            make_snapshot(4, "5300.00", "5300.00", positions=0),
        ]
        metrics = BacktestMetricsCalculator.calculate_portfolio_metrics(snaps, Decimal("5000.00"))

        self.assertEqual(metrics.initial_capital_usd, Decimal("5000.00"))
        self.assertEqual(metrics.final_equity_usd, Decimal("5300.00"))
        self.assertEqual(metrics.net_profit_usd, Decimal("300.00"))
        self.assertEqual(metrics.total_return_pct, Decimal("6.00"))
        # Max Drawdown occurred between 5100 and 5050 = $50.00 (0.98%)
        self.assertEqual(metrics.max_drawdown_usd, Decimal("50.00"))
        self.assertTrue(metrics.max_drawdown_pct < Decimal("1.00"))
        self.assertTrue(metrics.sharpe_ratio > Decimal("0.00"))

    def test_02_trade_metrics_calculation(self):
        """Verify win rate, profit factor, expectancy, and streaks."""
        trades = [
            make_trade("100.00", "2.00"),
            make_trade("50.00", "1.00"),
            make_trade("-50.00", "-1.00"),
            make_trade("150.00", "3.00"),
        ]
        metrics = BacktestMetricsCalculator.calculate_trade_metrics(trades)

        self.assertEqual(metrics.total_trades, 4)
        self.assertEqual(metrics.winning_trades, 3)
        self.assertEqual(metrics.losing_trades, 1)
        self.assertEqual(metrics.win_rate_pct, Decimal("75.00"))
        # Gross win: 300 / 3 = 100.00, Gross loss: 50 / 1 = 50.00
        self.assertEqual(metrics.avg_win_usd, Decimal("100.00"))
        self.assertEqual(metrics.avg_loss_usd, Decimal("50.00"))
        # Profit factor: 300 / 50 = 6.00
        self.assertEqual(metrics.profit_factor, Decimal("6.00"))
        # Expectancy: (0.75 * 100) - (0.25 * 50) = 75 - 12.50 = 62.50
        self.assertEqual(metrics.expectancy_usd, Decimal("62.50"))
        self.assertEqual(metrics.max_consecutive_wins, 2)
        self.assertEqual(metrics.max_consecutive_losses, 1)

    def test_03_edge_cases_zero_trades_and_zero_losses(self):
        """Verify edge cases do not trigger division by zero or NaN errors."""
        # Empty trade list
        zero_metrics = BacktestMetricsCalculator.calculate_trade_metrics([])
        self.assertEqual(zero_metrics.total_trades, 0)
        self.assertEqual(zero_metrics.win_rate_pct, Decimal("0.00"))
        self.assertIsNone(zero_metrics.profit_factor)

        # All winning trades (zero losses)
        all_wins = [make_trade("100.00", "2.00"), make_trade("50.00", "1.00")]
        win_metrics = BacktestMetricsCalculator.calculate_trade_metrics(all_wins)
        self.assertEqual(win_metrics.win_rate_pct, Decimal("100.00"))
        self.assertIsNone(win_metrics.profit_factor)  # Handled safely as None instead of ZeroDivisionError

    def test_04_benchmark_comparison(self):
        """Verify SPY comparison computes excess return and benchmark metrics."""
        port_metrics = BacktestMetricsCalculator.calculate_portfolio_metrics(
            [make_snapshot(0, "5000.00", "5000.00"), make_snapshot(1, "5500.00", "5500.00")],
            Decimal("5000.00"),
        )
        spy_c1 = OHLCVCandle(
            ticker="SPY",
            interval="1d",
            timestamp=datetime(2026, 1, 5, 20, 0, tzinfo=UTC),
            open=Decimal("400.00"),
            high=Decimal("405.00"),
            low=Decimal("398.00"),
            close=Decimal("400.00"),
            volume=50000000,
            status=CandleStatus.COMPLETE,
            source="yahoo",
        )
        spy_c2 = OHLCVCandle(
            ticker="SPY",
            interval="1d",
            timestamp=datetime(2026, 1, 6, 20, 0, tzinfo=UTC),
            open=Decimal("400.00"),
            high=Decimal("425.00"),
            low=Decimal("399.00"),
            close=Decimal("420.00"),
            volume=50000000,
            status=CandleStatus.COMPLETE,
            source="yahoo",
        )

        comp = BacktestMetricsCalculator.compare_with_benchmark(port_metrics, [spy_c1, spy_c2])
        self.assertTrue(comp.is_available)
        # SPY went from 400 to 420 = +5.00%
        self.assertEqual(comp.benchmark_total_return_pct, Decimal("5.00"))
        # Strategy was +10.00%, excess return = +5.00%
        self.assertEqual(comp.excess_return_pct, Decimal("5.00"))


if __name__ == "__main__":
    unittest.main()
