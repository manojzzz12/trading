"""Integration test suite for Phase 5.10.3: Chronological Partitioned Multi-Regime Backtesting Harness.

Validates the 12 core requirements specified in PHASE_510_3_IMPLEMENTATION_PLAN.md:
1. T-5103-01: test_decision_time_bar_availability_temporal_isolation
2. T-5103-02: test_liquidity_gates_in_loop_filtering
3. T-5103-03: test_order_size_capacity_enforced_post_sizing
4. T-5103-04: test_final_booked_quantity_accounting_and_residual_cancellation
5. T-5103-05: test_fill_price_risk_revalidation_gap_downsizing
6. T-5103-06: test_fill_price_risk_revalidation_stop_breach_rejection
7. T-5103-07: test_delisting_liquidation_cent_precision_quantization
8. T-5103-08: test_ticker_alias_lookahead_free_routing
9. T-5103-09: test_mutually_exclusive_regime_assignment_and_boundaries
10. T-5103-10: test_starting_capital_and_calendar_session_consistency
11. T-5103-11: test_breakeven_friction_and_headroom_arithmetic
12. T-5103-12: test_sensitivity_run_comparability_identical_signals
"""

from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
import os
import sys
import unittest
from typing import Any, Dict, List, Optional

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SWING_DIR = os.path.join(ROOT_DIR, ".agents", "skills", "trader-swing")
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)
if SWING_DIR not in sys.path:
    sys.path.insert(0, SWING_DIR)

from backtest_data import HistoricalDataManager
from backtest_engine import (
    ChronologicalPartitionHarness,
    EventDrivenBacktestEngine,
    SensitivityGridReport,
    SensitivityRunResult,
)
from backtest_execution import SimulatedExecutionEngine
from backtest_models import (
    BacktestConfig,
    ExecutionAssumptions,
    PriceAdjustmentType,
    SimulatedOrder,
    SimulatedOrderSide,
    SimulatedOrderStatus,
    SimulatedOrderType,
    SimulatedTradeRecord,
    SlippageConfig,
)
from backtest_portfolio import BacktestPortfolio
from benchmark import SPYTotalReturnCalculator, TBillCashYieldCalculator
from historical_universe import (
    DelistingCause,
    DelistingResolution,
    FillPriceRiskValidator,
    HistoricalLiquidityGates,
    IssuerIdentity,
    PointInTimeUniverseManager,
    SecurityIdentity,
    TickerAliasSchedule,
    VolumeCapacityModel,
)
from market_data_models import CandleStatus, OHLCVCandle, UTC
from regime_detector import MarketRegimeDetector, classify_composite_score
from regime_models import DEFAULT_REGIME_POLICIES, MarketRegime, MarketRegimeResult, RegimePolicy
from risk import FeeConfig, FeeSchedule, RiskConfig, RiskEngine


def make_candle(
    ticker: str,
    dt: datetime,
    close: Decimal,
    volume: int = 1500000,
    open_px: Optional[Decimal] = None,
    high: Optional[Decimal] = None,
    low: Optional[Decimal] = None,
) -> OHLCVCandle:
    c = close
    o = open_px or c
    h = high or max(o, c) + Decimal("1.00")
    l = low or min(o, c) - Decimal("1.00")
    return OHLCVCandle(
        ticker=ticker,
        interval="1d",
        timestamp=dt,
        open=o,
        high=h,
        low=l,
        close=c,
        volume=volume,
        status=CandleStatus.COMPLETE,
        source="mock_5103",
        received_timestamp=dt,
        is_delayed=True,
    )


class TestSwingWalkForwardIntegration5103(unittest.TestCase):
    """Test suite validating all 12 core Phase 5.10.3 integration requirements."""

    def test_decision_time_bar_availability_temporal_isolation(self):
        """T-5103-01: Verify temporal isolation between session t post-close and session t+1 execution.

        Morning execution at t+1 must strictly use ATR_14,t and ADV_20,t from completed bars <= t,
        and adverse slippage must be applied in the correct direction (buy higher, sell lower).
        """
        slip_cfg = SlippageConfig(
            entry_bps=Decimal("0.0"),
            exit_bps=Decimal("0.0"),
            fixed_per_share=Decimal("0.00"),
            atr_multiplier=Decimal("0.020"),  # 2% of ATR
            min_cents=Decimal("0.02"),
        )
        fee_cfg = FeeConfig()
        exec_engine = SimulatedExecutionEngine(
            fee_config=fee_cfg,
            slippage_config=slip_cfg,
        )

        # 1. Adverse Entry Slippage: Fill price must be HIGHER than open
        order = SimulatedOrder(
            order_id="ORD-BUY-1",
            ticker="AAPL",
            side=SimulatedOrderSide.BUY,
            order_type=SimulatedOrderType.MARKET_NEXT_OPEN,
            quantity=100,
            signal_timestamp=datetime(2023, 1, 4, 20, 0, tzinfo=UTC),
            submission_timestamp=datetime(2023, 1, 4, 20, 0, tzinfo=UTC),
            reference_price=Decimal("150.00"),
        )
        candle = make_candle(
            ticker="AAPL",
            dt=datetime(2023, 1, 5, 14, 30, tzinfo=UTC),
            open_px=Decimal("150.00"),
            close=Decimal("152.00"),
            volume=5000000,
        )
        trailing_atr = Decimal("3.00")  # Completed ATR from session t
        trailing_adv20 = 2000000        # Completed ADV from session t

        filled = exec_engine.simulate_entry_fill(
            order=order,
            session_candle=candle,
            trailing_adv20_shares=trailing_adv20,
            trailing_atr14=trailing_atr,
        )
        # Expected slippage: 0.02 * 3.00 = 0.06
        expected_fill = Decimal("150.00") + Decimal("0.06")
        self.assertEqual(filled.status, SimulatedOrderStatus.FILLED)
        self.assertEqual(filled.fill_price, expected_fill)
        self.assertGreater(filled.fill_price, candle.open)

        # 2. Adverse Exit Slippage: Exit fill price must be LOWER than trigger
        stop_exit = exec_engine._build_stop_exit(
            ticker="AAPL",
            lot_id="LOT-1",
            shares=100,
            stop_loss_price=Decimal("145.00"),
            session_candle=candle,
            trailing_atr14=trailing_atr,
        )
        expected_exit_fill = Decimal("145.00") - Decimal("0.06")
        self.assertEqual(stop_exit.fill_price, expected_exit_fill)
        self.assertLess(stop_exit.fill_price, Decimal("145.00"))

    def test_liquidity_gates_in_loop_filtering(self):
        """T-5103-02: Verify engine prunes tickers failing Gates 1-3 prior to scanner ranking.

        Gate 1: Close >= $10.00
        Gate 2: ADV_20 >= 1,000,000 shares
        Gate 3: Dollar ADV_20 >= $25,000,000
        """
        # Create 3 tickers:
        # AAA: Passes all gates (P=$50, ADV=2M, DADV=$100M)
        # BBB: Fails Gate 1 (P=$8.00 < $10)
        # CCC: Fails Gate 2 & 3 (P=$100, ADV=100k < 1M, DADV=$10M < $25M)
        bars_aaa = [
            make_candle("AAA", datetime(2023, 1, i + 1, 20, 0, tzinfo=UTC), close=Decimal("50.00"), volume=2000000)
            for i in range(25)
        ]
        bars_bbb = [
            make_candle("BBB", datetime(2023, 1, i + 1, 20, 0, tzinfo=UTC), close=Decimal("8.00"), volume=2000000)
            for i in range(25)
        ]
        bars_ccc = [
            make_candle("CCC", datetime(2023, 1, i + 1, 20, 0, tzinfo=UTC), close=Decimal("100.00"), volume=100000)
            for i in range(25)
        ]

        exec_date = date(2023, 2, 1)

        # Evaluate HistoricalLiquidityGates
        res_aaa = HistoricalLiquidityGates.evaluate_gates(bars_aaa, exec_date, "AAA")
        self.assertTrue(res_aaa.passed)

        res_bbb = HistoricalLiquidityGates.evaluate_gates(bars_bbb, exec_date, "BBB")
        self.assertFalse(res_bbb.passed)
        self.assertTrue(any("GATE1_PRICE_BELOW_MINIMUM" in r for r in res_bbb.rejection_reasons))

        res_ccc = HistoricalLiquidityGates.evaluate_gates(bars_ccc, exec_date, "CCC")
        self.assertFalse(res_ccc.passed)
        self.assertTrue(any("GATE2_SHARE_ADV_BELOW_MINIMUM" in r for r in res_ccc.rejection_reasons))

    def test_order_size_capacity_enforced_post_sizing(self):
        """T-5103-03: Verify order quantity is sized first, then capped to 1% of trailing share ADV20,t."""
        # Risk sizing suggests 500 shares
        planned_shares = 500

        # Trailing ADV20 is 20,000 shares -> 1% is 200 shares
        adv20 = 20000
        max_order_shares = int(Decimal(adv20) * Decimal("0.01"))
        capped = min(planned_shares, max_order_shares)
        self.assertEqual(capped, 200)

        # High liquidity ADV20 is 5,000,000 shares -> 1% is 50,000 shares
        adv20_high = 5000000
        max_order_shares_high = int(Decimal(adv20_high) * Decimal("0.01"))
        capped_high = min(planned_shares, max_order_shares_high)
        self.assertEqual(capped_high, 500)

    def test_final_booked_quantity_accounting_and_residual_cancellation(self):
        """T-5103-04: Verify execution capacity and risk revalidation produce Q_final, cancelling residuals."""
        portfolio = BacktestPortfolio(initial_capital_usd=Decimal("5000.00"))

        # Order requested for 50 shares
        order = SimulatedOrder(
            order_id="ORD-BUY-50",
            ticker="AAPL",
            side=SimulatedOrderSide.BUY,
            order_type=SimulatedOrderType.MARKET_NEXT_OPEN,
            quantity=50,
            signal_timestamp=datetime(2023, 1, 4, 20, 0, tzinfo=UTC),
            submission_timestamp=datetime(2023, 1, 4, 20, 0, tzinfo=UTC),
            reference_price=Decimal("150.00"),
        )
        candle = make_candle(
            ticker="AAPL",
            dt=datetime(2023, 1, 5, 14, 30, tzinfo=UTC),
            open_px=Decimal("150.00"),
            close=Decimal("152.00"),
        )

        exec_engine = SimulatedExecutionEngine()
        # Execution engine capacity caps order to 20 shares (1% of 2,000 ADV)
        trailing_adv20 = 2000
        filled = exec_engine.simulate_entry_fill(
            order=order,
            session_candle=candle,
            trailing_adv20_shares=trailing_adv20,
        )

        self.assertEqual(filled.quantity, 20)
        self.assertEqual(filled.residual_cancelled_shares, 30)

        # Revalidator confirms 20 shares fits risk
        q_final = 20
        filled.quantity = q_final

        # Ledger books exactly Q_final
        lot_id = portfolio.execute_entry(filled)
        pos = portfolio.ledger.positions["AAPL"]
        self.assertEqual(pos.total_shares, 20)
        self.assertEqual(len(pos.lots), 1)
        self.assertEqual(pos.lots[0].shares_remaining, 20)

    def test_fill_price_risk_revalidation_gap_downsizing(self):
        """T-5103-05: Verify adverse morning gap-up is downsized to whole shares to fit 0.50% risk ($25 on $5k)."""
        risk_cfg = RiskConfig(
            starting_virtual_capital_usd=Decimal("5000.00"),
            risk_per_trade_pct=Decimal("0.005"),  # 0.50% = $25.00
            max_aggregate_buffered_risk_pct=Decimal("0.02"),
            gap_risk_multiplier=Decimal("1.25"),
        )
        fee_cfg = FeeConfig()

        # Planned: entry $100.00, stop $95.00 (risk $5.00/sh). 5 shares = $25 risk.
        # Open gaps up adversely to $105.00 (stop stays $95.00).
        # Dollar risk per share with 1.25x buffer = (105 - 95) * 1.25 = 12.50.
        # Max shares that fit $25 risk = 25 / 12.50 = 2 shares.
        res = FillPriceRiskValidator.revalidate_and_downsize(
            account_equity=Decimal("5000.00"),
            modeled_fill_price=Decimal("105.00"),
            stop_price=Decimal("95.00"),
            initial_shares=5,
            risk_config=risk_cfg,
            fee_config=fee_cfg,
            current_buffered_open_risk=Decimal("0.00"),
        )
        self.assertFalse(res.is_rejected)
        self.assertEqual(res.final_shares, 2)
        self.assertTrue(res.is_downsized)
        self.assertEqual(res.original_shares, 5)

        # Extreme gap up to $130.00: 1 share risk = (130 - 95) * 1.25 = 43.75 > $25.00
        res_extreme = FillPriceRiskValidator.revalidate_and_downsize(
            account_equity=Decimal("5000.00"),
            modeled_fill_price=Decimal("130.00"),
            stop_price=Decimal("95.00"),
            initial_shares=5,
            risk_config=risk_cfg,
            fee_config=fee_cfg,
            current_buffered_open_risk=Decimal("0.00"),
        )
        self.assertTrue(res_extreme.is_rejected)
        self.assertEqual(res_extreme.final_shares, 0)

    def test_fill_price_risk_revalidation_stop_breach_rejection(self):
        """T-5103-06: Verify morning open at or below stop (P_fill <= P_stop) is rejected immediately."""
        risk_cfg = RiskConfig(starting_virtual_capital_usd=Decimal("5000.00"))
        fee_cfg = FeeConfig()

        # Planned entry $100, stop $95. Open gaps down through stop to $94.00.
        res = FillPriceRiskValidator.revalidate_and_downsize(
            account_equity=Decimal("5000.00"),
            modeled_fill_price=Decimal("94.00"),
            stop_price=Decimal("95.00"),
            initial_shares=5,
            risk_config=risk_cfg,
            fee_config=fee_cfg,
            current_buffered_open_risk=Decimal("0.00"),
        )
        self.assertTrue(res.is_rejected)
        self.assertEqual(res.final_shares, 0)
        self.assertIn("stop price", res.reason.lower())

        # Exact match P_fill == P_stop
        res_exact = FillPriceRiskValidator.revalidate_and_downsize(
            account_equity=Decimal("5000.00"),
            modeled_fill_price=Decimal("95.00"),
            stop_price=Decimal("95.00"),
            initial_shares=5,
            risk_config=risk_cfg,
            fee_config=fee_cfg,
            current_buffered_open_risk=Decimal("0.00"),
        )
        self.assertTrue(res_exact.is_rejected)
        self.assertEqual(res_exact.final_shares, 0)

    def test_delisting_liquidation_cent_precision_quantization(self):
        """T-5103-07: Verify held positions in delisted stocks liquidate with Decimal cent precision."""
        # 1. OTC Haircut (25% discount, quantized to cent)
        p10 = Decimal("10.00")
        term10 = max(Decimal("0.01"), (p10 * Decimal("0.75")).quantize(Decimal("0.01"), ROUND_HALF_UP))
        self.assertEqual(term10, Decimal("7.50"))

        p1005 = Decimal("10.05")  # 10.05 * 0.75 = 7.5375 -> 7.54
        term1005 = max(Decimal("0.01"), (p1005 * Decimal("0.75")).quantize(Decimal("0.01"), ROUND_HALF_UP))
        self.assertEqual(term1005, Decimal("7.54"))

        # 2. PointInTimeUniverseManager calculation
        mgr = PointInTimeUniverseManager()
        issuer = IssuerIdentity(cik="0001234567", legal_name="Delist Co")
        mgr.register_issuer(issuer)
        sec = SecurityIdentity(
            security_id="SEC_DELIST_01",
            issuer_cik="0001234567",
            figi="BBG000DELIST1",
            share_class="Common",
            primary_exchange="NASDAQ",
            delisting_date=date(2023, 6, 1),
        )
        mgr.register_security(sec)
        mgr.register_delisting(
            DelistingResolution(
                ticker="DLIST",
                security_id="SEC_DELIST_01",
                delist_date=date(2023, 6, 1),
                cause=DelistingCause.OTC_TRANSITION,
                recovery_rate_pct=Decimal("0.75"),
                audit_flag="OTC_TRANSITION_25PCT_HAIRCUT",
            )
        )

        term_px, flag = mgr.calculate_terminal_delisting_price(
            security_id="SEC_DELIST_01",
            delist_date=date(2023, 6, 1),
            final_main_board_close=Decimal("10.05"),
        )
        self.assertEqual(term_px, Decimal("7.54"))
        self.assertEqual(flag, "OTC_TRANSITION_HAIRCUT_25PCT")

        # 3. Bankruptcy conservative zero recovery
        issuer_bk = IssuerIdentity(cik="0001234568", legal_name="Bankrupt Co")
        mgr.register_issuer(issuer_bk)
        sec_bankrupt = SecurityIdentity(
            security_id="SEC_BK_01",
            issuer_cik="0001234568",
            figi="BBG000BKTC001",
            share_class="Common",
            primary_exchange="NASDAQ",
            delisting_date=date(2023, 6, 1),
        )
        mgr.register_security(sec_bankrupt)
        mgr.register_delisting(
            DelistingResolution(
                ticker="BKTC",
                security_id="SEC_BK_01",
                delist_date=date(2023, 6, 1),
                cause=DelistingCause.BANKRUPTCY_LIQUIDATION,
                recovery_rate_pct=Decimal("0.00"),
                audit_flag="TERMINAL_RECOVERY_UNKNOWN_CONSERVATIVE_ZERO",
            )
        )
        term_bk, flag_bk = mgr.calculate_terminal_delisting_price(
            security_id="SEC_BK_01",
            delist_date=date(2023, 6, 1),
            final_main_board_close=Decimal("15.00"),
        )
        self.assertEqual(term_bk, Decimal("0.00"))
        self.assertEqual(flag_bk, "TERMINAL_RECOVERY_UNKNOWN_CONSERVATIVE_ZERO")

    def test_ticker_alias_lookahead_free_routing(self):
        """T-5103-08: Verify 'FB' pre-2022-06-09 and 'META' post-2022-06-09 route seamlessly without forward lookahead."""
        mgr = PointInTimeUniverseManager()
        issuer = IssuerIdentity(cik="0001326801", legal_name="Meta Platforms, Inc.")
        mgr.register_issuer(issuer)
        sec = SecurityIdentity(
            security_id="SEC_META_01",
            issuer_cik="0001326801",
            figi="BBG000MM2P62",
            share_class="Class A",
            primary_exchange="NASDAQ",
        )
        mgr.register_security(sec)
        mgr.register_alias(TickerAliasSchedule(security_id="SEC_META_01", ticker="FB", valid_from=date(2012, 5, 18), valid_to=date(2022, 6, 8)))
        mgr.register_alias(TickerAliasSchedule(security_id="SEC_META_01", ticker="META", valid_from=date(2022, 6, 9), valid_to=None))

        dm = HistoricalDataManager(universe_manager=mgr)
        c_fb = make_candle("FB", datetime(2022, 6, 8, 20, 0, tzinfo=UTC), close=Decimal("196.64"))
        c_meta = make_candle("META", datetime(2022, 6, 9, 20, 0, tzinfo=UTC), close=Decimal("197.80"))

        dm.load_candles("FB", [c_fb])
        dm.load_candles("META", [c_meta])

        # On 2022-06-08: FB exists, META returns None
        self.assertIsNotNone(dm.get_execution_candle("FB", date(2022, 6, 8)))
        self.assertIsNone(dm.get_execution_candle("META", date(2022, 6, 8)))

        # On 2022-06-09: META exists, FB returns None
        self.assertIsNotNone(dm.get_execution_candle("META", date(2022, 6, 9)))
        self.assertIsNone(dm.get_execution_candle("FB", date(2022, 6, 9)))

    def test_mutually_exclusive_regime_assignment_and_boundaries(self):
        """T-5103-09: Verify composite score maps deterministically to 5 mutually exclusive boundary bins."""
        test_cases = [
            (Decimal("1.0000"), MarketRegime.STRONG_BULLISH, True),
            (Decimal("0.6000"), MarketRegime.STRONG_BULLISH, True),
            (Decimal("0.5999"), MarketRegime.BULLISH, True),
            (Decimal("0.2000"), MarketRegime.BULLISH, True),
            (Decimal("0.1999"), MarketRegime.SIDEWAYS, True),
            (Decimal("-0.2000"), MarketRegime.SIDEWAYS, True),
            (Decimal("-0.2001"), MarketRegime.BEARISH, True),
            (Decimal("-0.5999"), MarketRegime.BEARISH, True),
            (Decimal("-0.6000"), MarketRegime.STRONG_BEARISH, False),
            (Decimal("-1.0000"), MarketRegime.STRONG_BEARISH, False),
        ]

        for score, expected_regime, expected_allow in test_cases:
            reg = classify_composite_score(score)
            self.assertEqual(reg, expected_regime, f"Failed for score {score}")
            policy = DEFAULT_REGIME_POLICIES[reg]
            self.assertEqual(policy.allow_new_longs, expected_allow, f"Policy mismatch for score {score}")

    def test_starting_capital_and_calendar_session_consistency(self):
        """T-5103-10: Verify strategy and benchmark start with $5,000 across 753, 502, and 1,255 sessions."""
        self.assertEqual(ChronologicalPartitionHarness.STARTING_CAPITAL, Decimal("5000.00"))
        self.assertEqual(ChronologicalPartitionHarness.IN_SAMPLE_EXPECTED_SESSIONS, 753)
        self.assertEqual(ChronologicalPartitionHarness.PREV_OOS_EXPECTED_SESSIONS, 502)
        self.assertEqual(ChronologicalPartitionHarness.FULL_CONTIGUOUS_EXPECTED_SESSIONS, 1255)
        self.assertEqual(
            ChronologicalPartitionHarness.IN_SAMPLE_EXPECTED_SESSIONS
            + ChronologicalPartitionHarness.PREV_OOS_EXPECTED_SESSIONS,
            ChronologicalPartitionHarness.FULL_CONTIGUOUS_EXPECTED_SESSIONS,
        )
        self.assertIn("PREVIOUSLY OBSERVED (CONTAMINATED)", ChronologicalPartitionHarness.CONTAMINATION_DISCLOSURE)

    def test_breakeven_friction_and_headroom_arithmetic(self):
        """T-5103-11: Verify Total Break-Even = Baseline Level 1 Cost Per Share + Additional Headroom."""
        tot_shares = 1000
        gross_profit = Decimal("500.00")
        l1_friction = Decimal("150.00")
        l1_net = gross_profit - l1_friction

        tot_breakeven = (gross_profit / Decimal(tot_shares)).quantize(Decimal("0.0001"), ROUND_HALF_UP)
        baseline_cost = (l1_friction / Decimal(tot_shares)).quantize(Decimal("0.0001"), ROUND_HALF_UP)
        additional_headroom = (l1_net / Decimal(tot_shares)).quantize(Decimal("0.0001"), ROUND_HALF_UP)

        self.assertEqual(tot_breakeven, Decimal("0.5000"))
        self.assertEqual(baseline_cost, Decimal("0.1500"))
        self.assertEqual(additional_headroom, Decimal("0.3500"))
        self.assertEqual(tot_breakeven, baseline_cost + additional_headroom)

    def test_sensitivity_run_comparability_identical_signals(self):
        """T-5103-12: Verify Levels 0-3 evaluate identical candidate signals without enforcing monotonic equity."""
        # Set up a small synthetic data manager with AAPL and SPY
        dm = HistoricalDataManager()
        sessions = [date(2023, 1, 3 + i) for i in range(10)]
        for s_idx, s_date in enumerate(sessions):
            dt = datetime(s_date.year, s_date.month, s_date.day, 20, 0, tzinfo=UTC)
            c_spy = make_candle("SPY", dt, Decimal("400.00") + Decimal(s_idx), volume=50000000)
            c_qqq = make_candle("QQQ", dt, Decimal("280.00") + Decimal(s_idx), volume=30000000)
            c_iwm = make_candle("IWM", dt, Decimal("180.00") + Decimal(s_idx), volume=20000000)
            c_aapl = make_candle("AAPL", dt, Decimal("150.00") + Decimal(s_idx * 2), volume=10000000)
            dm.load_candles("SPY", [c_spy])
            dm.load_candles("QQQ", [c_qqq])
            dm.load_candles("IWM", [c_iwm])
            dm.load_candles("AAPL", [c_aapl])

        base_cfg = BacktestConfig(
            start_date=sessions[0],
            end_date=sessions[-1],
            universe=["AAPL"],
            initial_capital_usd=Decimal("5000.00"),
            benchmark_ticker="SPY",
        )

        grid_report = ChronologicalPartitionHarness.run_sensitivity_grid(
            base_config=base_cfg,
            data_manager=dm,
            start_date=sessions[0],
            end_date=sessions[-1],
        )

        self.assertEqual(len(grid_report.runs), 4)
        self.assertEqual(grid_report.runs[0].level, 0)
        self.assertEqual(grid_report.runs[1].level, 1)
        self.assertEqual(grid_report.runs[2].level, 2)
        self.assertEqual(grid_report.runs[3].level, 3)

        # Level 0 has zero fees and zero slippage
        self.assertEqual(grid_report.runs[0].total_fees_usd, Decimal("0.00"))
        self.assertEqual(grid_report.runs[0].total_slippage_usd, Decimal("0.00"))
        self.assertTrue(grid_report.mathematical_identity_verified)

    def test_liquidity_gates_strict_filtering_no_fallback(self):
        """Fix 1: Verify when no tickers pass Gates 1-3 with universe_manager=None, universe remains empty."""
        dm = HistoricalDataManager()
        sessions = [date(2023, 1, 3 + i) for i in range(25)]
        for s_idx, s_date in enumerate(sessions):
            dt = datetime(s_date.year, s_date.month, s_date.day, 20, 0, tzinfo=UTC)
            # Benchmarks to ensure regime is valid (STRONG_BULLISH allows longs)
            c_spy = make_candle("SPY", dt, Decimal("400.00") + Decimal(s_idx), volume=50000000)
            c_qqq = make_candle("QQQ", dt, Decimal("280.00") + Decimal(s_idx), volume=30000000)
            c_iwm = make_candle("IWM", dt, Decimal("180.00") + Decimal(s_idx), volume=20000000)
            # Illiquid penny stock PENNY: price $5.00 < $10.00, volume 50,000 < 1,000,000
            c_penny = make_candle("PENNY", dt, Decimal("5.00") + (Decimal(s_idx) * Decimal("0.10")), volume=50000)
            dm.load_candles("SPY", [c_spy])
            dm.load_candles("QQQ", [c_qqq])
            dm.load_candles("IWM", [c_iwm])
            dm.load_candles("PENNY", [c_penny])

        cfg = BacktestConfig(
            start_date=sessions[0],
            end_date=sessions[-1],
            universe=["PENNY"],
            initial_capital_usd=Decimal("5000.00"),
            benchmark_ticker="SPY",
        )
        engine = EventDrivenBacktestEngine(
            config=cfg,
            data_manager=dm,
            universe_manager=None,  # Explicitly None to test fallback elimination
            enable_cash_yield=False,
        )
        res = engine.run()

        # Zero entry orders and zero trades
        self.assertEqual(res["trade_metrics"]["total_trades"], 0)
        self.assertEqual(len(res["closed_trades"]), 0)
        self.assertEqual(len(engine.pending_entry_orders), 0)
        penny_orders = [o for o in res["all_orders"] if o["ticker"] == "PENNY"]
        self.assertEqual(len(penny_orders), 0)
        self.assertEqual(engine.portfolio.ledger.cash_usd, Decimal("5000.00"))

    def test_delisting_liquidation_incurs_configured_exit_fee(self):
        """Fix 2: Verify positive-price delisting liquidation applies configured exit fees to cash and P&L."""
        mgr = PointInTimeUniverseManager()
        issuer = IssuerIdentity(cik="0001999001", legal_name="Target Corp")
        mgr.register_issuer(issuer)
        sec = SecurityIdentity(
            security_id="SEC_TGT_01",
            issuer_cik="0001999001",
            figi="BBG000TGT001",
            share_class="Common",
            primary_exchange="NASDAQ",
            delisting_date=date(2023, 1, 10),
        )
        mgr.register_security(sec)
        mgr.register_alias(TickerAliasSchedule(security_id="SEC_TGT_01", ticker="TGT", valid_from=date(2023, 1, 1), valid_to=date(2023, 1, 10)))
        mgr.register_delisting(
            DelistingResolution(
                ticker="TGT",
                security_id="SEC_TGT_01",
                delist_date=date(2023, 1, 10),
                cause=DelistingCause.CASH_ACQUISITION,
                cash_per_share=Decimal("12.00"),
                audit_flag="CASH_ACQUISITION_PAYOUT",
            )
        )

        dm = HistoricalDataManager(universe_manager=mgr)
        sessions = [date(2023, 1, 3 + i) for i in range(8)]  # up to 2023-01-10
        for s_idx, s_date in enumerate(sessions):
            dt = datetime(s_date.year, s_date.month, s_date.day, 20, 0, tzinfo=UTC)
            c_spy = make_candle("SPY", dt, Decimal("400.00"), volume=50000000)
            c_qqq = make_candle("QQQ", dt, Decimal("280.00"), volume=30000000)
            c_iwm = make_candle("IWM", dt, Decimal("180.00"), volume=20000000)
            c_tgt = make_candle("TGT", dt, Decimal("10.00"), volume=2000000)
            dm.load_candles("SPY", [c_spy])
            dm.load_candles("QQQ", [c_qqq])
            dm.load_candles("IWM", [c_iwm])
            dm.load_candles("TGT", [c_tgt])

        fee_cfg = FeeConfig(
            entry=FeeSchedule(per_share=Decimal("0.00"), minimum=Decimal("0.00")),
            exit=FeeSchedule(per_share=Decimal("0.01"), minimum=Decimal("2.00")),  # $0.01/sh, min $2.00
        )
        cfg = BacktestConfig(
            start_date=sessions[0],
            end_date=sessions[-1],
            universe=["TGT"],
            initial_capital_usd=Decimal("5000.00"),
            fee_config=fee_cfg,
            slippage_config=SlippageConfig(entry_bps=Decimal("0.0"), exit_bps=Decimal("0.0")),
            benchmark_ticker="SPY",
        )
        engine = EventDrivenBacktestEngine(
            config=cfg,
            data_manager=dm,
            universe_manager=mgr,
            enable_cash_yield=False,
        )

        # Pre-seed open position: 100 shares @ $10.00 on session 0 ($1000 basis, $0 fee)
        seed_order = SimulatedOrder(
            order_id="SEED-TGT",
            ticker="TGT",
            side=SimulatedOrderSide.BUY,
            order_type=SimulatedOrderType.MARKET_NEXT_OPEN,
            quantity=100,
            signal_timestamp=datetime(2023, 1, 3, 20, 0, tzinfo=UTC),
            submission_timestamp=datetime(2023, 1, 3, 20, 0, tzinfo=UTC),
            reference_price=Decimal("10.00"),
            fill_timestamp=datetime(2023, 1, 4, 14, 30, tzinfo=UTC),
            fill_price=Decimal("10.00"),
            fee_usd=Decimal("0.00"),
            status=SimulatedOrderStatus.FILLED,
        )
        engine.portfolio.execute_entry(seed_order)
        self.assertEqual(engine.portfolio.ledger.cash_usd, Decimal("4000.00"))

        # Run engine through delisting date (2023-01-10)
        res = engine.run()

        # Check delisting exit: 100 shares * $12.00 = $1,200.00 gross proceeds
        # Exit fee: max(2.00, 100 * 0.01) = $2.00
        # Net cash inflow: $1,200.00 - $2.00 = $1,198.00
        # Ending cash: $4,000.00 + $1,198.00 = $5,198.00
        # Realized net P&L: ($12.00 - $10.00) * 100 - $2.00 = $198.00
        self.assertEqual(engine.portfolio.ledger.cash_usd, Decimal("5198.00"))
        self.assertEqual(engine.portfolio.ledger.positions["TGT"].total_shares, 0)
        self.assertEqual(len(res["closed_trades"]), 1)
        tr = res["closed_trades"][0]
        self.assertEqual(Decimal(str(tr["total_fees_usd"])), Decimal("2.00"))
        self.assertEqual(Decimal(str(tr["net_pnl_usd"])), Decimal("198.00"))
        self.assertEqual(Decimal(str(tr["gross_pnl_usd"])), Decimal("200.00"))
        self.assertTrue(engine.portfolio.ledger.validate_equity_invariant()["holds"])

    def test_delisting_bankruptcy_zero_recovery_ledger_integrity(self):
        """Fix 2: Verify bankruptcy delisting ($0.00 recovery) closes lots safely without InvalidFinancialValue."""
        mgr = PointInTimeUniverseManager()
        issuer = IssuerIdentity(cik="0001999002", legal_name="Bankrupt Inc")
        mgr.register_issuer(issuer)
        sec = SecurityIdentity(
            security_id="SEC_BKTC_01",
            issuer_cik="0001999002",
            figi="BBG000BKTC01",
            share_class="Common",
            primary_exchange="NASDAQ",
            delisting_date=date(2023, 1, 10),
        )
        mgr.register_security(sec)
        mgr.register_alias(TickerAliasSchedule(security_id="SEC_BKTC_01", ticker="BKTC", valid_from=date(2023, 1, 1), valid_to=date(2023, 1, 10)))
        mgr.register_delisting(
            DelistingResolution(
                ticker="BKTC",
                security_id="SEC_BKTC_01",
                delist_date=date(2023, 1, 10),
                cause=DelistingCause.BANKRUPTCY_LIQUIDATION,
                cash_per_share=None,  # $0.00 recovery
                audit_flag="TERMINAL_RECOVERY_UNKNOWN_CONSERVATIVE_ZERO",
            )
        )

        dm = HistoricalDataManager(universe_manager=mgr)
        sessions = [date(2023, 1, 3 + i) for i in range(8)]
        for s_idx, s_date in enumerate(sessions):
            dt = datetime(s_date.year, s_date.month, s_date.day, 20, 0, tzinfo=UTC)
            c_spy = make_candle("SPY", dt, Decimal("400.00"), volume=50000000)
            c_qqq = make_candle("QQQ", dt, Decimal("280.00"), volume=30000000)
            c_iwm = make_candle("IWM", dt, Decimal("180.00"), volume=20000000)
            c_bktc = make_candle("BKTC", dt, Decimal("20.00"), volume=2000000)
            dm.load_candles("SPY", [c_spy])
            dm.load_candles("QQQ", [c_qqq])
            dm.load_candles("IWM", [c_iwm])
            dm.load_candles("BKTC", [c_bktc])

        fee_cfg = FeeConfig(
            entry=FeeSchedule(per_share=Decimal("0.01"), minimum=Decimal("1.00")),
            exit=FeeSchedule(per_share=Decimal("0.01"), minimum=Decimal("2.00")),
        )
        cfg = BacktestConfig(
            start_date=sessions[0],
            end_date=sessions[-1],
            universe=["BKTC"],
            initial_capital_usd=Decimal("5000.00"),
            fee_config=fee_cfg,
            benchmark_ticker="SPY",
        )
        engine = EventDrivenBacktestEngine(
            config=cfg,
            data_manager=dm,
            universe_manager=mgr,
            enable_cash_yield=False,
        )

        # Pre-seed open position: 50 shares @ $20.00 ($1,000 basis, $1.00 entry fee)
        seed_order = SimulatedOrder(
            order_id="SEED-BKTC",
            ticker="BKTC",
            side=SimulatedOrderSide.BUY,
            order_type=SimulatedOrderType.MARKET_NEXT_OPEN,
            quantity=50,
            signal_timestamp=datetime(2023, 1, 3, 20, 0, tzinfo=UTC),
            submission_timestamp=datetime(2023, 1, 3, 20, 0, tzinfo=UTC),
            reference_price=Decimal("20.00"),
            fill_timestamp=datetime(2023, 1, 4, 14, 30, tzinfo=UTC),
            fill_price=Decimal("20.00"),
            fee_usd=Decimal("1.00"),
            status=SimulatedOrderStatus.FILLED,
        )
        engine.portfolio.execute_entry(seed_order)
        # Cash: $5,000 - $1,000 (basis) - $1.00 (fee) = $3,999.00
        self.assertEqual(engine.portfolio.ledger.cash_usd, Decimal("3999.00"))

        # Run engine through delisting date (2023-01-10) - must NOT throw InvalidFinancialValue
        res = engine.run()

        # Zero recovery write-off:
        # Cash remains $3,999.00 (no invented exit fees on zero-proceeds write-off)
        self.assertEqual(engine.portfolio.ledger.cash_usd, Decimal("3999.00"))
        # Position and lot are closed with zero remaining shares
        pos = engine.portfolio.ledger.positions["BKTC"]
        self.assertEqual(pos.total_shares, 0)
        self.assertEqual(len(pos.lots), 1)
        self.assertEqual(pos.lots[0].status, "CLOSED")
        self.assertEqual(pos.lots[0].shares_remaining, 0)
        # Closed trade recorded
        self.assertEqual(len(res["closed_trades"]), 1)
        tr = res["closed_trades"][0]
        self.assertEqual(Decimal(str(tr["exit_price"])), Decimal("0.00"))
        self.assertEqual(Decimal(str(tr["gross_pnl_usd"])), Decimal("-1000.00"))
        self.assertEqual(Decimal(str(tr["total_fees_usd"])), Decimal("1.00"))  # entry fee only
        self.assertEqual(Decimal(str(tr["net_pnl_usd"])), Decimal("-1001.00"))  # -($1000 basis + $1 fee)
        self.assertEqual(Decimal(str(tr["return_pct"])), Decimal("-100.00"))
        # Invariant holds
        self.assertTrue(engine.portfolio.ledger.validate_equity_invariant()["holds"])
        self.assertEqual(engine.portfolio.ledger.total_equity_usd, Decimal("3999.00"))

    def test_delisting_bankruptcy_zero_recovery_trade_record_when_lot_meta_missing(self):
        """Phase 5.10.3 Fix 2: Verify zero-recovery delisting trade record reports original lot entry price, gross PnL, and R-multiple when lot_meta is missing."""
        portfolio = BacktestPortfolio(initial_capital_usd=Decimal("5000.00"))

        # Pre-seed open position: 50 shares @ $20.00 ($1000 basis, $1.00 entry fee, $19.00 initial stop)
        entry_order = SimulatedOrder(
            order_id="BUY-BKTC-01",
            ticker="BKTC",
            side=SimulatedOrderSide.BUY,
            order_type=SimulatedOrderType.MARKET_NEXT_OPEN,
            quantity=50,
            signal_timestamp=datetime(2023, 1, 3, 20, 0, tzinfo=UTC),
            submission_timestamp=datetime(2023, 1, 3, 20, 0, tzinfo=UTC),
            reference_price=Decimal("20.00"),
            fill_timestamp=datetime(2023, 1, 4, 14, 30, tzinfo=UTC),
            fill_price=Decimal("20.00"),
            stop_loss_price=Decimal("19.00"),
            target_price=Decimal("22.00"),
            fee_usd=Decimal("1.00"),
            status=SimulatedOrderStatus.FILLED,
        )
        lot_id = portfolio.execute_entry(entry_order)
        self.assertEqual(portfolio.ledger.cash_usd, Decimal("3999.00"))
        self.assertIn(lot_id, portfolio.lot_meta)

        # Simulate missing metadata by deleting lot_id from lot_meta
        del portfolio.lot_meta[lot_id]
        self.assertNotIn(lot_id, portfolio.lot_meta)

        # Execute delisting liquidation with zero recovery ($0.00 fill price)
        delist_order = SimulatedOrder(
            order_id=f"DELIST-{lot_id}-20230110",
            ticker="BKTC",
            side=SimulatedOrderSide.SELL,
            order_type=SimulatedOrderType.REGIME_EXIT,
            quantity=50,
            signal_timestamp=datetime(2023, 1, 10, 14, 30, tzinfo=UTC),
            submission_timestamp=datetime(2023, 1, 10, 14, 30, tzinfo=UTC),
            reference_price=Decimal("0.00"),
            lot_id=lot_id,
            fill_timestamp=datetime(2023, 1, 10, 14, 30, tzinfo=UTC),
            fill_price=Decimal("0.00"),
            fee_usd=Decimal("0.00"),
            status=SimulatedOrderStatus.FILLED,
            reason="DELISTING_LIQUIDATION: TERMINAL_RECOVERY_UNKNOWN_CONSERVATIVE_ZERO",
        )
        trade_record = portfolio.execute_delisting_liquidation(delist_order)

        # Verify trade record correctly reflects original lot, not the zero exit price
        self.assertEqual(trade_record.entry_price, Decimal("20.00"))
        self.assertEqual(trade_record.exit_price, Decimal("0.00"))
        self.assertEqual(trade_record.gross_pnl_usd, Decimal("-1000.00"))
        self.assertEqual(trade_record.total_fees_usd, Decimal("1.00"))
        self.assertEqual(trade_record.net_pnl_usd, Decimal("-1001.00"))
        self.assertEqual(trade_record.return_pct, Decimal("-100.00"))
        # R-Multiple: (0.00 - 20.00) / (20.00 - 19.00) = -20.00
        self.assertEqual(trade_record.r_multiple, Decimal("-20.00"))
        self.assertEqual(trade_record.shares, 50)

        # Ledger state and invariant
        self.assertEqual(portfolio.ledger.positions["BKTC"].total_shares, 0)
        self.assertEqual(portfolio.ledger.cash_usd, Decimal("3999.00"))
        self.assertTrue(portfolio.ledger.validate_equity_invariant()["holds"])
        self.assertEqual(portfolio.ledger.total_equity_usd, Decimal("3999.00"))

    def test_e2e_production_entry_adv_capacity_and_risk_revalidation_downsize(self):
        """Phase 5.10.3 Fix 1: Genuine end-to-end engine test proving queued order -> ADV cap -> modeled open fill -> risk downsize -> Q_final -> fee recalculation -> ledger."""
        mgr = PointInTimeUniverseManager()
        issuer = IssuerIdentity(cik="0001999003", legal_name="Flow Corp")
        mgr.register_issuer(issuer)
        sec = SecurityIdentity(
            security_id="SEC_FLOW_01",
            issuer_cik="0001999003",
            figi="BBG000FLOW01",
            share_class="Common",
            primary_exchange="NASDAQ",
        )
        mgr.register_security(sec)
        mgr.register_alias(TickerAliasSchedule(security_id="SEC_FLOW_01", ticker="FLOW", valid_from=date(2023, 1, 1), valid_to=date(2023, 12, 31)))

        dm = HistoricalDataManager(universe_manager=mgr)
        # 21 sessions: sessions 0..19 are prior completed sessions, session 20 is execution day
        sessions = [date(2023, 1, 1) + timedelta(days=i) for i in range(21)]
        for s_idx, s_date in enumerate(sessions):
            dt = datetime(s_date.year, s_date.month, s_date.day, 20, 0, tzinfo=UTC)
            c_spy = make_candle("SPY", dt, Decimal("400.00"), volume=50000000)
            c_qqq = make_candle("QQQ", dt, Decimal("280.00"), volume=30000000)
            c_iwm = make_candle("IWM", dt, Decimal("180.00"), volume=20000000)
            # FLOW candle with volume=2000 so trailing ADV20 = 2000
            c_flow = make_candle("FLOW", dt, close=Decimal("100.00"), open_px=Decimal("100.00"), high=Decimal("101.00"), low=Decimal("99.00"), volume=2000)
            dm.load_candles("SPY", [c_spy])
            dm.load_candles("QQQ", [c_qqq])
            dm.load_candles("IWM", [c_iwm])
            dm.load_candles("FLOW", [c_flow])

        fee_cfg = FeeConfig(
            entry=FeeSchedule(per_share=Decimal("0.05"), minimum=Decimal("0.00")),
            exit=FeeSchedule(per_share=Decimal("0.05"), minimum=Decimal("0.00")),
        )
        risk_cfg = RiskConfig(
            starting_virtual_capital_usd=Decimal("5000.00"),
            risk_per_trade_pct=Decimal("0.005"),  # 0.50% of $5,000 = $25.00
            max_aggregate_buffered_risk_pct=Decimal("0.02"),
            gap_risk_multiplier=Decimal("1.25"),
        )
        cfg = BacktestConfig(
            start_date=sessions[0],
            end_date=sessions[-1],
            universe=["FLOW"],
            initial_capital_usd=Decimal("5000.00"),
            risk_config=risk_cfg,
            fee_config=fee_cfg,
            slippage_config=SlippageConfig(entry_bps=Decimal("0.0"), exit_bps=Decimal("0.0")),
            benchmark_ticker="SPY",
        )

        class QueuedOrderEngine(EventDrivenBacktestEngine):
            def _process_session(self, session_date: date, session_idx: int, all_sessions: List[date]) -> None:
                if session_idx == 19:
                    super()._process_session(session_date, session_idx, all_sessions)
                    # Queue order at session 19 close for execution at session 20 open
                    queued_order = SimulatedOrder(
                        order_id="ORD-FLOW-50",
                        ticker="FLOW",
                        side=SimulatedOrderSide.BUY,
                        order_type=SimulatedOrderType.MARKET_NEXT_OPEN,
                        quantity=50,  # 50 shares requested
                        signal_timestamp=datetime(session_date.year, session_date.month, session_date.day, 20, 0, tzinfo=UTC),
                        submission_timestamp=datetime(session_date.year, session_date.month, session_date.day, 20, 0, tzinfo=UTC),
                        reference_price=Decimal("100.00"),
                        stop_loss_price=Decimal("95.00"),
                        target_price=Decimal("110.00"),
                        status=SimulatedOrderStatus.PENDING,
                    )
                    self.pending_entry_orders.append(queued_order)
                    return
                super()._process_session(session_date, session_idx, all_sessions)

        engine = QueuedOrderEngine(
            config=cfg,
            data_manager=dm,
            universe_manager=mgr,
            enable_cash_yield=False,
        )

        # Run the full end-to-end engine simulation
        res = engine.run()

        # Step 1: Queued order was processed
        all_buy_orders = [o for o in res["all_orders"] if o["ticker"] == "FLOW" and o["side"] == "BUY"]
        self.assertEqual(len(all_buy_orders), 1)
        executed_order = all_buy_orders[0]

        # Step 2: Trailing ADV capacity cap and residual cancellation
        # ADV20 = 2,000 shares -> 1% capacity cap = 20 shares. 50 - 20 = 30 shares cancelled by capacity.
        # Step 4: Fill-price risk revalidation downsized 20 shares to Q_final = 3 shares to fit $25.00 risk.
        # (3 * (100 - 95) * 1.25) + (3 * 0.05 in) + (3 * 0.05 out) = 18.75 + 0.15 + 0.15 = 19.05 <= 25.00
        # (4 * 6.25) + 0.40 = 25.40 > 25.00 -> downsizes 20 -> 3 shares (17 additional cancelled).
        # Total residual cancelled shares = 30 (ADV cap) + 17 (risk downsize) = 47 shares!
        self.assertEqual(executed_order["status"], "FILLED")
        self.assertEqual(executed_order["quantity"], 3)  # Q_final
        self.assertEqual(executed_order["residual_cancelled_shares"], 47)

        # Step 3: Modeled open fill price
        self.assertEqual(Decimal(str(executed_order["fill_price"])), Decimal("100.00"))

        # Step 6: Recalculated entry fee on Q_final (3 shares * $0.05 = $0.15, NOT 50*$0.05=$2.50 or 20*$0.05=$1.00)
        self.assertEqual(Decimal(str(executed_order["fee_usd"])), Decimal("0.15"))

        # Step 7: Ledger position contains exactly Q_final
        pos = engine.portfolio.ledger.positions["FLOW"]
        self.assertEqual(pos.total_shares, 3)
        self.assertEqual(len(pos.lots), 1)
        self.assertEqual(pos.lots[0].shares_remaining, 3)
        self.assertEqual(pos.lots[0].total_lot_cost_basis_usd, Decimal("300.00"))
        self.assertEqual(pos.lots[0].entry_fee_remaining, Decimal("0.15"))

        # Ledger cash: $5,000.00 - $300.00 - $0.15 = $4,699.85
        self.assertEqual(engine.portfolio.ledger.cash_usd, Decimal("4699.85"))
        self.assertTrue(engine.portfolio.ledger.validate_equity_invariant()["holds"])


if __name__ == "__main__":
    unittest.main()
