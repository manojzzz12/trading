"""Unit and integration tests for Phase 5.9.1 Critical Corporate-Action Corrections in trader-swing.

Enforces:
1. F-01: Split-adjustment consistency - prevents double split adjustment on SPLIT_ADJUSTED data; applies split correctly on RAW_UNADJUSTED data.
2. F-02: Dividend-adjustment consistency - prevents double dividend counting on SPLIT_AND_DIVIDEND_ADJUSTED data; accrues/settles on SPLIT_ADJUSTED and RAW_UNADJUSTED data.
3. F-03: Reverse split accounting - reuses authoritative Phase 1 AccountingLedger.apply_reverse_split() with cash-in-lieu and exact cent invariant reconciliation.
4. F-04: Historical calendar correctness - verified NYSE holiday and early close coverage for 2015-2027; unsupported calendar years marked UNSUPPORTED_CALENDAR.
5. F-05: Corporate-action registry validation and deduplication - duplicate split/dividend rejection, positive ratio/amount checks, conflict rejection.
6. F-06: Dividend payable-date settlement - missing payable dates assigned 14-day estimated settlement lag (never settle on ex-date).
7. F-07: Price adjustment metadata - enforces compatibility between provider output, dataset metadata, and BacktestConfig.
8. F-08: Full event-driven engine integration testing combining entries, corporate actions, exits, mark-to-market, and invariant validation.
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
from backtest_data import (
    HistoricalDataManager,
    HistoricalDataValidationError,
    SURVIVORSHIP_BIAS_DISCLOSURE,
)
from backtest_engine import EventDrivenBacktestEngine
from backtest_models import (
    BacktestConfig,
    DatasetIntegrityReport,
    DividendEvent,
    ExecutionAssumptions,
    PriceAdjustmentType,
    SimulatedOrder,
    SimulatedOrderSide,
    SimulatedOrderStatus,
    SimulatedOrderType,
    SlippageConfig,
    StockSplitEvent,
)
from backtest_portfolio import BacktestPortfolio
from market_data_models import CandleStatus, MarketClock, OHLCVCandle, UTC
from risk import FeeConfig, FeeSchedule, RiskConfig
from universe import SURVIVORSHIP_BIAS_DISCLOSURE as UNIVERSE_SURVIVORSHIP_DISCLOSURE

# Pre-generate confirmed NYSE trading days for synthetic test fixtures
_DATA_READINESS_TEST_TRADING_DAYS = []
_curr_d = date(2026, 1, 5)
while len(_DATA_READINESS_TEST_TRADING_DAYS) < 100:
    if MarketClock.get_session(_curr_d):
        _DATA_READINESS_TEST_TRADING_DAYS.append(_curr_d)
    _curr_d += timedelta(days=1)


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


def _make_bullish_series(ticker: str, base_price: float, count: int = 35) -> list:
    candles = []
    curr = base_price
    for i in range(count):
        step = 0.50
        if i % 2 == 0:
            curr = curr - (step * 0.4)
        else:
            curr = curr + step
        d = _DATA_READINESS_TEST_TRADING_DAYS[i]
        dt = datetime.combine(d, time(20, 0, 0), tzinfo=UTC)
        c_dec = Decimal(f"{curr:.2f}")
        spread = max(Decimal("2.00"), c_dec * Decimal("0.02"))
        candles.append(
            _create_daily_candle(
                ticker=ticker,
                dt=dt,
                open_px=c_dec,
                high_px=c_dec + spread,
                low_px=c_dec - spread,
                close_px=c_dec,
                volume=2000000,
            )
        )
    return candles


class TestBacktestModelsDataReadiness(unittest.TestCase):
    """Test data models introduced or updated in Phase 5.9 and Phase 5.9.1."""

    def test_price_adjustment_type_enum(self):
        self.assertEqual(PriceAdjustmentType.RAW_UNADJUSTED.value, "RAW_UNADJUSTED")
        self.assertEqual(PriceAdjustmentType.SPLIT_ADJUSTED.value, "SPLIT_ADJUSTED")
        self.assertEqual(
            PriceAdjustmentType.SPLIT_AND_DIVIDEND_ADJUSTED.value,
            "SPLIT_AND_DIVIDEND_ADJUSTED",
        )

    def test_stock_split_event_dataclass(self):
        split = StockSplitEvent(
            ticker="AAPL",
            split_date=date(2020, 8, 31),
            ratio=Decimal("4.0"),
            split_numerator=4,
            split_denominator=1,
            description="4-for-1 forward stock split",
        )
        self.assertEqual(split.ticker, "AAPL")
        self.assertEqual(split.split_date, date(2020, 8, 31))
        self.assertEqual(split.ratio, Decimal("4.0"))
        self.assertEqual(split.split_numerator, 4)
        self.assertEqual(split.split_denominator, 1)
        d = split.to_dict()
        self.assertEqual(d["ticker"], "AAPL")
        self.assertEqual(d["split_date"], "2020-08-31")
        self.assertEqual(d["ratio"], "4.0")
        self.assertEqual(d["split_numerator"], 4)
        self.assertEqual(d["split_denominator"], 1)

    def test_dividend_event_dataclass(self):
        div = DividendEvent(
            ticker="MSFT",
            ex_date=date(2024, 2, 14),
            amount_per_share=Decimal("0.75"),
            payable_date=date(2024, 3, 14),
            is_estimated_payable_date=False,
        )
        self.assertEqual(div.ticker, "MSFT")
        self.assertEqual(div.ex_date, date(2024, 2, 14))
        self.assertEqual(div.amount_per_share, Decimal("0.75"))
        self.assertEqual(div.payable_date, date(2024, 3, 14))
        self.assertFalse(div.is_estimated_payable_date)
        d = div.to_dict()
        self.assertEqual(d["ticker"], "MSFT")
        self.assertEqual(d["ex_date"], "2024-02-14")
        self.assertEqual(d["amount_per_share"], "0.75")
        self.assertEqual(d["payable_date"], "2024-03-14")
        self.assertFalse(d["is_estimated_payable_date"])

    def test_backtest_config_with_price_adjustment_type(self):
        cfg = BacktestConfig(
            start_date=date(2024, 1, 1),
            end_date=date(2024, 6, 30),
            universe=["AAPL"],
            price_adjustment_type=PriceAdjustmentType.SPLIT_ADJUSTED,
        )
        self.assertEqual(cfg.price_adjustment_type, PriceAdjustmentType.SPLIT_ADJUSTED)
        d = cfg.to_dict()
        self.assertEqual(d["price_adjustment_type"], "SPLIT_ADJUSTED")

    def test_dataset_integrity_report_dataclass(self):
        rep = DatasetIntegrityReport(
            ticker="NVDA",
            total_candles=100,
            start_date=date(2024, 1, 2),
            end_date=date(2024, 5, 24),
            expected_calendar_sessions=100,
            missing_sessions=[],
            duplicate_dates_count=0,
            detected_unadjusted_splits=[],
            adjustment_type=PriceAdjustmentType.SPLIT_ADJUSTED,
            quality_status="CLEAN",
            warnings=[],
        )
        self.assertEqual(rep.quality_status, "CLEAN")
        d = rep.to_dict()
        self.assertEqual(d["ticker"], "NVDA")
        self.assertEqual(d["quality_status"], "CLEAN")
        self.assertEqual(d["total_candles"], 100)


class TestSplitAdjustmentConsistency(unittest.TestCase):
    """Test F-01: Split-adjustment consistency in both RAW and SPLIT_ADJUSTED modes."""

    def test_raw_split_applies_to_portfolio_without_equity_change(self):
        # 10 shares bought at $100 raw = $1,000 cost basis.
        # 2-for-1 forward split on Day 5.
        # In RAW_UNADJUSTED mode: Day 5 price drops to $50.
        # Portfolio splits: 20 shares @ $50 = $1,000 GMV. Equity unchanged!
        portfolio = BacktestPortfolio(initial_capital_usd=Decimal("5000.00"))
        ts = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)

        order = SimulatedOrder(
            order_id="ORD-RAW-1",
            ticker="XYZ",
            side=SimulatedOrderSide.BUY,
            order_type=SimulatedOrderType.MARKET_NEXT_OPEN,
            quantity=10,
            signal_timestamp=ts,
            submission_timestamp=ts,
            reference_price=Decimal("100.00"),
            fill_timestamp=ts,
            fill_price=Decimal("100.00"),
            status=SimulatedOrderStatus.FILLED,
            stop_loss_price=Decimal("90.00"),
            target_price=Decimal("120.00"),
        )
        lot_id = portfolio.execute_entry(order)
        portfolio.mark_to_market({"XYZ": Decimal("100.00")})

        equity_before = portfolio.ledger.total_equity_usd
        self.assertEqual(equity_before, Decimal("5000.00"))

        # Apply 2-for-1 forward split
        portfolio.apply_stock_split("XYZ", Decimal("2.0"))

        pos = portfolio.ledger.positions["XYZ"]
        self.assertEqual(pos.total_shares, 20)
        self.assertEqual(pos.lots[0].shares_remaining, 20)
        self.assertEqual(pos.lots[0].entry_price, Decimal("50.00"))
        self.assertEqual(pos.lots[0].total_lot_cost_basis_usd, Decimal("1000.00"))
        self.assertEqual(portfolio.lot_stops[lot_id], Decimal("45.00"))
        self.assertEqual(portfolio.lot_targets[lot_id], Decimal("60.00"))

        # Post-split market price drops to $50.00
        portfolio.mark_to_market({"XYZ": Decimal("50.00")})
        equity_after = portfolio.ledger.total_equity_usd
        self.assertEqual(equity_after, Decimal("5000.00"))
        inv = portfolio.ledger.validate_equity_invariant()
        self.assertTrue(inv["holds"])
        self.assertEqual(inv["discrepancy"], Decimal("0.00"))

    def test_engine_skips_split_adjustment_when_dataset_is_split_adjusted(self):
        # When dataset is SPLIT_ADJUSTED, pre-split prices are already in post-split dollars ($50).
        # Order fills at $50 for 10 shares = $500 cost basis.
        # On split date, engine MUST NOT apply split again (prevents double split).
        dm = HistoricalDataManager()
        data_by_ticker = {
            "SPY": _make_bullish_series("SPY", 450.0, 35),
            "QQQ": _make_bullish_series("QQQ", 350.0, 35),
            "IWM": _make_bullish_series("IWM", 180.0, 35),
            "AAPL": _make_bullish_series("AAPL", 50.0, 35),
        }
        for sym, bars in data_by_ticker.items():
            dm.load_candles(sym, bars, adjustment_type=PriceAdjustmentType.SPLIT_ADJUSTED)

        sessions = dm.get_all_session_dates()
        split_date = sessions[5]
        dm.load_splits("AAPL", [StockSplitEvent(ticker="AAPL", split_date=split_date, ratio=Decimal("2.0"))])

        cfg = BacktestConfig(
            start_date=sessions[0],
            end_date=sessions[-1],
            universe=["AAPL"],
            initial_capital_usd=Decimal("5000.00"),
            price_adjustment_type=PriceAdjustmentType.SPLIT_ADJUSTED,
        )

        engine = EventDrivenBacktestEngine(config=cfg, data_manager=dm)

        # Seed pre-split entry order
        ts = datetime(sessions[0].year, sessions[0].month, sessions[0].day, 14, 30, tzinfo=UTC)
        order = SimulatedOrder(
            order_id="SEED-BUY",
            ticker="AAPL",
            side=SimulatedOrderSide.BUY,
            order_type=SimulatedOrderType.MARKET_NEXT_OPEN,
            quantity=10,
            signal_timestamp=ts,
            submission_timestamp=ts,
            reference_price=Decimal("50.00"),
            fill_timestamp=ts,
            fill_price=Decimal("50.00"),
            status=SimulatedOrderStatus.FILLED,
            stop_loss_price=Decimal("20.00"),
            target_price=Decimal("200.00"),
        )
        engine.portfolio.execute_entry(order)

        res = engine.run()

        # Shares must remain 10 (not doubled to 20, preventing phantom gain!)
        pos = engine.portfolio.ledger.positions["AAPL"]
        self.assertEqual(pos.total_shares, 10)
        inv = engine.portfolio.ledger.validate_equity_invariant()
        self.assertTrue(inv["holds"])
        self.assertEqual(inv["discrepancy"], Decimal("0.00"))


class TestDividendAdjustmentConsistency(unittest.TestCase):
    """Test F-02: Dividend-adjustment consistency."""

    def test_incompatible_dividend_registration_rejected_on_split_and_dividend_adjusted_data(self):
        dm = HistoricalDataManager()
        dt1 = datetime(2026, 1, 5, 20, 0, 0, tzinfo=UTC)
        c1 = _create_daily_candle("DIVX", dt1, Decimal("100.00"), Decimal("105.00"), Decimal("99.00"), Decimal("100.00"))
        dm.load_candles("DIVX", [c1], adjustment_type=PriceAdjustmentType.SPLIT_AND_DIVIDEND_ADJUSTED)

        div = DividendEvent(ticker="DIVX", ex_date=date(2026, 1, 6), amount_per_share=Decimal("1.50"))
        with self.assertRaises(HistoricalDataValidationError):
            dm.load_dividends("DIVX", [div])

    def test_engine_rejects_split_and_dividend_adjusted_config_with_cash_dividends(self):
        dm = HistoricalDataManager()
        data_by_ticker = {
            "SPY": _make_bullish_series("SPY", 450.0, 35),
            "QQQ": _make_bullish_series("QQQ", 350.0, 35),
            "IWM": _make_bullish_series("IWM", 180.0, 35),
            "AAPL": _make_bullish_series("AAPL", 150.0, 35),
        }
        for sym, bars in data_by_ticker.items():
            dm.load_candles(sym, bars, adjustment_type=PriceAdjustmentType.SPLIT_ADJUSTED)

        # Force registration of dividend prior to adjustment type change
        dm._adjustment_type_by_ticker["AAPL"] = PriceAdjustmentType.SPLIT_AND_DIVIDEND_ADJUSTED
        dm._dividends_by_ticker["AAPL"].append(DividendEvent(ticker="AAPL", ex_date=date(2026, 1, 10), amount_per_share=Decimal("1.00")))

        cfg = BacktestConfig(
            start_date=date(2026, 1, 5),
            end_date=date(2026, 2, 1),
            universe=["AAPL"],
            price_adjustment_type=PriceAdjustmentType.SPLIT_AND_DIVIDEND_ADJUSTED,
        )

        with self.assertRaises(HistoricalDataValidationError):
            EventDrivenBacktestEngine(config=cfg, data_manager=dm)


class TestReverseSplitAccounting(unittest.TestCase):
    """Test F-03: Reverse split accounting reusing Phase 1 AccountingLedger."""

    def test_15_shares_1_for_10_reverse_split(self):
        # 15 shares bought at $10.00 = $150.00 cost basis.
        # 1-for-10 reverse split at post-split settlement price $100.00.
        # Entitlement: 1 whole share + 0.5 fractional share paid as $50.00 CIL cash.
        # Cash increases from $4,850 to $4,900.
        portfolio = BacktestPortfolio(initial_capital_usd=Decimal("5000.00"))
        ts = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)

        order = SimulatedOrder(
            order_id="ORD-1", ticker="REV", side=SimulatedOrderSide.BUY,
            order_type=SimulatedOrderType.MARKET_NEXT_OPEN, quantity=15,
            signal_timestamp=ts, submission_timestamp=ts, reference_price=Decimal("10.00"),
            fill_timestamp=ts, fill_price=Decimal("10.00"), status=SimulatedOrderStatus.FILLED,
        )
        lot_id = portfolio.execute_entry(order)
        portfolio.mark_to_market({"REV": Decimal("10.00")})
        self.assertEqual(portfolio.ledger.cash_usd, Decimal("4850.00"))

        summary = portfolio.apply_stock_split(
            ticker="REV", ratio=Decimal("0.10"), split_numerator=1, split_denominator=10,
            cil_settlement_price=Decimal("100.00"), effective_date="2026-01-06"
        )
        self.assertIsNotNone(summary)
        self.assertEqual(portfolio.ledger.cash_usd, Decimal("4900.00"))
        self.assertEqual(portfolio.ledger.positions["REV"].total_shares, 1)

        lot = portfolio.ledger.positions["REV"].lots[0]
        self.assertEqual(lot.shares_remaining, 1)
        self.assertEqual(lot.entry_price, Decimal("100.00"))
        self.assertEqual(lot.total_lot_cost_basis_usd, Decimal("100.00"))

        inv = portfolio.ledger.validate_equity_invariant()
        self.assertTrue(inv["holds"])
        self.assertEqual(inv["discrepancy"], Decimal("0.00"))

    def test_10_shares_1_for_3_reverse_split(self):
        # 10 shares bought at $10.00 = $100.00 cost basis.
        # 1-for-3 reverse split at post-split price $30.00.
        # Entitlement: 3 whole shares + 0.333 share paid as $10.00 CIL cash.
        portfolio = BacktestPortfolio(initial_capital_usd=Decimal("5000.00"))
        ts = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)

        order = SimulatedOrder(
            order_id="ORD-2", ticker="REV", side=SimulatedOrderSide.BUY,
            order_type=SimulatedOrderType.MARKET_NEXT_OPEN, quantity=10,
            signal_timestamp=ts, submission_timestamp=ts, reference_price=Decimal("10.00"),
            fill_timestamp=ts, fill_price=Decimal("10.00"), status=SimulatedOrderStatus.FILLED,
        )
        portfolio.execute_entry(order)
        portfolio.mark_to_market({"REV": Decimal("10.00")})

        portfolio.apply_stock_split(
            ticker="REV", ratio=Decimal("1") / Decimal("3"), split_numerator=1, split_denominator=3,
            cil_settlement_price=Decimal("30.00"), effective_date="2026-01-06"
        )
        self.assertEqual(portfolio.ledger.cash_usd, Decimal("4910.00"))
        self.assertEqual(portfolio.ledger.positions["REV"].total_shares, 3)

        inv = portfolio.ledger.validate_equity_invariant()
        self.assertTrue(inv["holds"])
        self.assertEqual(inv["discrepancy"], Decimal("0.00"))

    def test_7_shares_1_for_3_reverse_split(self):
        # 7 shares bought at $10.00 = $70.00 cost basis.
        # 1-for-3 reverse split at post-split price $30.00.
        # Entitlement: 2 whole shares + 0.333 share paid as $10.00 CIL cash.
        portfolio = BacktestPortfolio(initial_capital_usd=Decimal("5000.00"))
        ts = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)

        order = SimulatedOrder(
            order_id="ORD-3", ticker="REV", side=SimulatedOrderSide.BUY,
            order_type=SimulatedOrderType.MARKET_NEXT_OPEN, quantity=7,
            signal_timestamp=ts, submission_timestamp=ts, reference_price=Decimal("10.00"),
            fill_timestamp=ts, fill_price=Decimal("10.00"), status=SimulatedOrderStatus.FILLED,
        )
        portfolio.execute_entry(order)
        portfolio.mark_to_market({"REV": Decimal("10.00")})

        portfolio.apply_stock_split(
            ticker="REV", ratio=Decimal("1") / Decimal("3"), split_numerator=1, split_denominator=3,
            cil_settlement_price=Decimal("30.00"), effective_date="2026-01-06"
        )
        self.assertEqual(portfolio.ledger.cash_usd, Decimal("4940.00"))
        self.assertEqual(portfolio.ledger.positions["REV"].total_shares, 2)

        inv = portfolio.ledger.validate_equity_invariant()
        self.assertTrue(inv["holds"])
        self.assertEqual(inv["discrepancy"], Decimal("0.00"))

    def test_full_fractional_position_closure_5_shares_1_for_10(self):
        # 5 shares bought at $10.00 = $50.00 cost basis.
        # 1-for-10 reverse split at post-split price $100.00.
        # Entitlement: 0 whole shares + 0.5 fractional share paid as $50.00 CIL cash.
        # Position closed cleanly with discrepancy 0.00 (NO CRASH!).
        portfolio = BacktestPortfolio(initial_capital_usd=Decimal("5000.00"))
        ts = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)

        order = SimulatedOrder(
            order_id="ORD-4", ticker="REV", side=SimulatedOrderSide.BUY,
            order_type=SimulatedOrderType.MARKET_NEXT_OPEN, quantity=5,
            signal_timestamp=ts, submission_timestamp=ts, reference_price=Decimal("10.00"),
            fill_timestamp=ts, fill_price=Decimal("10.00"), status=SimulatedOrderStatus.FILLED,
        )
        lot_id = portfolio.execute_entry(order)
        portfolio.mark_to_market({"REV": Decimal("10.00")})

        summary = portfolio.apply_stock_split(
            ticker="REV", ratio=Decimal("0.10"), split_numerator=1, split_denominator=10,
            cil_settlement_price=Decimal("100.00"), effective_date="2026-01-06"
        )
        self.assertEqual(portfolio.ledger.cash_usd, Decimal("5000.00"))
        self.assertEqual(portfolio.ledger.positions["REV"].total_shares, 0)
        self.assertNotIn(lot_id, portfolio.lot_stops)

        inv = portfolio.ledger.validate_equity_invariant()
        self.assertTrue(inv["holds"])
        self.assertEqual(inv["discrepancy"], Decimal("0.00"))

    def test_no_fractional_entitlement_20_shares_1_for_10(self):
        # 20 shares bought at $10.00 = $200.00 cost basis.
        # 1-for-10 reverse split at post-split price $100.00.
        # Entitlement: 2 whole shares, $0.00 CIL cash.
        portfolio = BacktestPortfolio(initial_capital_usd=Decimal("5000.00"))
        ts = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)

        order = SimulatedOrder(
            order_id="ORD-5", ticker="REV", side=SimulatedOrderSide.BUY,
            order_type=SimulatedOrderType.MARKET_NEXT_OPEN, quantity=20,
            signal_timestamp=ts, submission_timestamp=ts, reference_price=Decimal("10.00"),
            fill_timestamp=ts, fill_price=Decimal("10.00"), status=SimulatedOrderStatus.FILLED,
        )
        portfolio.execute_entry(order)
        portfolio.mark_to_market({"REV": Decimal("10.00")})

        portfolio.apply_stock_split(
            ticker="REV", ratio=Decimal("0.10"), split_numerator=1, split_denominator=10,
            cil_settlement_price=Decimal("100.00"), effective_date="2026-01-06"
        )
        self.assertEqual(portfolio.ledger.cash_usd, Decimal("4800.00"))
        self.assertEqual(portfolio.ledger.positions["REV"].total_shares, 2)

        inv = portfolio.ledger.validate_equity_invariant()
        self.assertTrue(inv["holds"])
        self.assertEqual(inv["discrepancy"], Decimal("0.00"))


class TestHistoricalCalendarCorrectness(unittest.TestCase):
    """Test F-04: Historical NYSE calendar coverage across 2015-2027."""

    def test_market_clock_supported_years_range(self):
        for y in range(2015, 2028):
            self.assertTrue(MarketClock.is_historical_year_supported(y))
        self.assertFalse(MarketClock.is_historical_year_supported(2014))
        self.assertFalse(MarketClock.is_historical_year_supported(2028))

    def test_historical_holidays_confirmed(self):
        # Good Friday 2015: April 3, 2015
        is_open_gf, reason = MarketClock.is_historical_trading_day(date(2015, 4, 3))
        self.assertFalse(is_open_gf)
        self.assertIn("Good Friday", reason)

        # George H.W. Bush National Day of Mourning: Dec 5, 2018
        is_open_bush, reason_bush = MarketClock.is_historical_trading_day(date(2018, 12, 5))
        self.assertFalse(is_open_bush)
        self.assertIn("George H.W. Bush", reason_bush)

        # Jimmy Carter National Day of Mourning: Jan 9, 2025
        is_open_carter, reason_carter = MarketClock.is_historical_trading_day(date(2025, 1, 9))
        self.assertFalse(is_open_carter)
        self.assertIn("Jimmy Carter", reason_carter)

        # Juneteenth 2024: June 19, 2024
        is_open_june, reason_june = MarketClock.is_historical_trading_day(date(2024, 6, 19))
        self.assertFalse(is_open_june)
        self.assertIn("Juneteenth", reason_june)

        # MLK Day 2026: Jan 19, 2026
        is_open_mlk, reason_mlk = MarketClock.is_historical_trading_day(date(2026, 1, 19))
        self.assertFalse(is_open_mlk)
        self.assertIn("Martin Luther King", reason_mlk)

    def test_unsupported_calendar_year_flags_unsupported_status(self):
        dm = HistoricalDataManager()
        # 2010 candle (outside 2015-2027 confirmed coverage)
        dt = datetime(2010, 6, 1, 20, 0, 0, tzinfo=UTC)
        c = _create_daily_candle("OLD", dt, Decimal("50.00"), Decimal("52.00"), Decimal("49.00"), Decimal("51.00"))
        dm.load_candles("OLD", [c])

        rep = dm.validate_dataset_integrity("OLD")
        self.assertEqual(rep.quality_status, "UNSUPPORTED_CALENDAR")
        self.assertTrue(any("unsupported calendar years" in w.lower() for w in rep.warnings))


class TestCorporateActionRegistryValidation(unittest.TestCase):
    """Test F-05: Strict validation and deduplication in corporate action registries."""

    def test_duplicate_split_same_ratio_deduplicated(self):
        dm = HistoricalDataManager()
        s1 = StockSplitEvent(ticker="AAPL", split_date=date(2026, 1, 15), ratio=Decimal("2.0"))
        s2 = StockSplitEvent(ticker="AAPL", split_date=date(2026, 1, 15), ratio=Decimal("2.0"))
        dm.load_splits("AAPL", [s1])
        dm.load_splits("AAPL", [s2])
        # Deduplicated to 1 event
        self.assertEqual(len(dm.get_splits_for_session(date(2026, 1, 15))), 1)

    def test_conflicting_split_ratio_rejected(self):
        dm = HistoricalDataManager()
        s1 = StockSplitEvent(ticker="AAPL", split_date=date(2026, 1, 15), ratio=Decimal("2.0"))
        s2 = StockSplitEvent(ticker="AAPL", split_date=date(2026, 1, 15), ratio=Decimal("3.0"))
        dm.load_splits("AAPL", [s1])
        with self.assertRaises(HistoricalDataValidationError):
            dm.load_splits("AAPL", [s2])

    def test_invalid_split_ratios_rejected(self):
        dm = HistoricalDataManager()
        # Zero ratio
        with self.assertRaises(HistoricalDataValidationError):
            dm.load_splits("AAPL", [StockSplitEvent(ticker="AAPL", split_date=date(2026, 1, 15), ratio=Decimal("0.0"))])
        # Negative ratio
        with self.assertRaises(HistoricalDataValidationError):
            dm.load_splits("AAPL", [StockSplitEvent(ticker="AAPL", split_date=date(2026, 1, 15), ratio=Decimal("-2.0"))])
        # 1:1 ratio
        with self.assertRaises(HistoricalDataValidationError):
            dm.load_splits("AAPL", [StockSplitEvent(ticker="AAPL", split_date=date(2026, 1, 15), ratio=Decimal("1.0"))])

    def test_duplicate_dividend_on_same_ex_date_rejected(self):
        dm = HistoricalDataManager()
        d1 = DividendEvent(ticker="MSFT", ex_date=date(2026, 1, 15), amount_per_share=Decimal("0.75"))
        d2 = DividendEvent(ticker="MSFT", ex_date=date(2026, 1, 15), amount_per_share=Decimal("0.80"))
        dm.load_dividends("MSFT", [d1])
        with self.assertRaises(HistoricalDataValidationError):
            dm.load_dividends("MSFT", [d2])

    def test_invalid_dividend_values_rejected(self):
        dm = HistoricalDataManager()
        # Negative amount
        with self.assertRaises(HistoricalDataValidationError):
            dm.load_dividends("MSFT", [DividendEvent(ticker="MSFT", ex_date=date(2026, 1, 15), amount_per_share=Decimal("-0.50"))])
        # Payable date before ex-date
        with self.assertRaises(HistoricalDataValidationError):
            dm.load_dividends("MSFT", [
                DividendEvent(
                    ticker="MSFT",
                    ex_date=date(2026, 1, 15),
                    amount_per_share=Decimal("0.50"),
                    payable_date=date(2026, 1, 10),
                )
            ])


class TestDividendSettlementLag(unittest.TestCase):
    """Test F-06: Missing payable dates receive 14-day estimated lag and do not settle on ex-date."""

    def test_missing_payable_date_gets_14_day_lag_and_does_not_settle_on_ex_date(self):
        dm = HistoricalDataManager()
        ex_d = date(2026, 1, 15)
        d = DividendEvent(ticker="DIVX", ex_date=ex_d, amount_per_share=Decimal("0.50"))
        dm.load_dividends("DIVX", [d])

        registered = dm.get_dividends_for_session(ex_d)[0]
        self.assertTrue(registered.is_estimated_payable_date)
        self.assertEqual(registered.payable_date, ex_d + timedelta(days=14))

        # Test portfolio settlement behavior
        portfolio = BacktestPortfolio(initial_capital_usd=Decimal("5000.00"))
        ts = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
        order = SimulatedOrder(
            order_id="BUY-DIV", ticker="DIVX", side=SimulatedOrderSide.BUY,
            order_type=SimulatedOrderType.MARKET_NEXT_OPEN, quantity=100,
            signal_timestamp=ts, submission_timestamp=ts, reference_price=Decimal("20.00"),
            fill_timestamp=ts, fill_price=Decimal("20.00"), status=SimulatedOrderStatus.FILLED,
        )
        portfolio.execute_entry(order)

        # Accrue on ex-date
        portfolio.accrue_dividend("DIVX", registered.ex_date.isoformat(), registered.amount_per_share, registered.payable_date.isoformat())

        # Attempt settlement on ex-date (must NOT settle!)
        settled_ex = portfolio.settle_pending_dividends(ex_d)
        self.assertEqual(len(settled_ex), 0)
        self.assertEqual(portfolio.ledger.cash_usd, Decimal("3000.00"))

        # Attempt settlement 7 days later (must NOT settle!)
        settled_mid = portfolio.settle_pending_dividends(ex_d + timedelta(days=7))
        self.assertEqual(len(settled_mid), 0)
        self.assertEqual(portfolio.ledger.cash_usd, Decimal("3000.00"))

        # Settle on 14th day (must settle!)
        settled_final = portfolio.settle_pending_dividends(ex_d + timedelta(days=14))
        self.assertEqual(len(settled_final), 1)
        self.assertEqual(portfolio.ledger.cash_usd, Decimal("3050.00"))


class MockYahooService:
    """Mock service presenting native SPLIT_ADJUSTED provider."""
    class Provider:
        name = "yahoo"
        native_adjustment_type = "SPLIT_ADJUSTED"
    provider = Provider()

    def get_historical_bars(self, ticker, interval, start, end, use_cache=True):
        return [
            _create_daily_candle(ticker, start, Decimal("100.00"), Decimal("105.00"), Decimal("99.00"), Decimal("101.00"))
        ]


class TestPriceAdjustmentMetadataEnforcement(unittest.TestCase):
    """Test F-07: Enforces compatibility between provider output and dataset metadata."""

    def test_load_from_service_rejects_incompatible_native_label(self):
        dm = HistoricalDataManager()
        mock_svc = MockYahooService()
        dt1 = datetime(2026, 1, 5, 20, 0, 0, tzinfo=UTC)

        # Yahoo is natively SPLIT_ADJUSTED; attempting to label as RAW_UNADJUSTED must fail
        with self.assertRaises(HistoricalDataValidationError):
            dm.load_from_service(
                service=mock_svc,
                ticker="TEST",
                start=dt1,
                end=dt1,
                adjustment_type=PriceAdjustmentType.RAW_UNADJUSTED,
            )

    def test_engine_rejects_config_vs_dataset_adjustment_type_mismatch(self):
        dm = HistoricalDataManager()
        data_by_ticker = {
            "SPY": _make_bullish_series("SPY", 450.0, 35),
            "QQQ": _make_bullish_series("QQQ", 350.0, 35),
            "IWM": _make_bullish_series("IWM", 180.0, 35),
            "AAPL": _make_bullish_series("AAPL", 150.0, 35),
        }
        for sym, bars in data_by_ticker.items():
            dm.load_candles(sym, bars, adjustment_type=PriceAdjustmentType.SPLIT_ADJUSTED)

        # Config asks for RAW_UNADJUSTED, but dataset is SPLIT_ADJUSTED
        cfg = BacktestConfig(
            start_date=date(2026, 1, 5),
            end_date=date(2026, 2, 1),
            universe=["AAPL"],
            price_adjustment_type=PriceAdjustmentType.RAW_UNADJUSTED,
        )

        with self.assertRaises(HistoricalDataValidationError):
            EventDrivenBacktestEngine(config=cfg, data_manager=dm)


class TestFullEngineCorporateActionIntegration(unittest.TestCase):
    """Test F-08: Full integration verification across engine, accounting, and corporate actions."""

    def test_raw_unadjusted_backtest_with_split_and_dividend(self):
        # Full backtest in RAW_UNADJUSTED mode:
        # Pre-split AAPL price $150.
        # Session 5: 2:1 split. Candle price drops to $75.
        # Portfolio splits 10 shares @ $150 -> 20 shares @ $75. GMV remains constant.
        # Session 10: Ex-date for $0.50 dividend on 20 shares = $10.00 receivable.
        # Session 24 (14-day lag): Payable date settles $10.00 into cash.
        dm = HistoricalDataManager()

        base_dt = datetime(2026, 1, 5, 20, 0, 0, tzinfo=UTC)
        spy_bars = _make_bullish_series("SPY", 450.0, 35)
        qqq_bars = _make_bullish_series("QQQ", 350.0, 35)
        iwm_bars = _make_bullish_series("IWM", 180.0, 35)

        # Raw AAPL bars: sessions 0-4 around $150, sessions 5-34 around $75
        aapl_bars = []
        for i in range(35):
            d = _DATA_READINESS_TEST_TRADING_DAYS[i]
            dt = datetime.combine(d, time(20, 0, 0), tzinfo=UTC)
            px = Decimal("150.00") + Decimal(i) if i < 5 else Decimal("75.00") + Decimal(i - 5)
            aapl_bars.append(_create_daily_candle("AAPL", dt, px, px + Decimal("2.00"), px - Decimal("2.00"), px))

        dm.load_candles("SPY", spy_bars, adjustment_type=PriceAdjustmentType.RAW_UNADJUSTED)
        dm.load_candles("QQQ", qqq_bars, adjustment_type=PriceAdjustmentType.RAW_UNADJUSTED)
        dm.load_candles("IWM", iwm_bars, adjustment_type=PriceAdjustmentType.RAW_UNADJUSTED)
        dm.load_candles("AAPL", aapl_bars, adjustment_type=PriceAdjustmentType.RAW_UNADJUSTED)

        sessions = dm.get_all_session_dates()
        split_date = sessions[5]
        ex_date = sessions[10]
        pay_date = sessions[24]

        dm.load_splits("AAPL", [StockSplitEvent(ticker="AAPL", split_date=split_date, ratio=Decimal("2.0"))])
        dm.load_dividends("AAPL", [DividendEvent(ticker="AAPL", ex_date=ex_date, amount_per_share=Decimal("0.50"), payable_date=pay_date)])

        cfg = BacktestConfig(
            start_date=sessions[0],
            end_date=sessions[-1],
            universe=["AAPL"],
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
            top_candidates_per_session=1,
            price_adjustment_type=PriceAdjustmentType.RAW_UNADJUSTED,
        )

        engine = EventDrivenBacktestEngine(config=cfg, data_manager=dm)

        # Pre-seed open position before session 0
        ts = datetime(sessions[0].year, sessions[0].month, sessions[0].day, 14, 30, tzinfo=UTC)
        order = SimulatedOrder(
            order_id="SEED-BUY-RAW",
            ticker="AAPL",
            side=SimulatedOrderSide.BUY,
            order_type=SimulatedOrderType.MARKET_NEXT_OPEN,
            quantity=10,
            signal_timestamp=ts,
            submission_timestamp=ts,
            reference_price=Decimal("150.00"),
            fill_timestamp=ts,
            fill_price=Decimal("150.00"),
            status=SimulatedOrderStatus.FILLED,
            stop_loss_price=Decimal("30.00"),
            target_price=Decimal("300.00"),
        )
        engine.portfolio.execute_entry(order)

        results = engine.run()

        # Invariant must hold
        inv = engine.portfolio.ledger.validate_equity_invariant()
        self.assertTrue(inv["holds"])
        self.assertEqual(inv["discrepancy"], Decimal("0.00"))

        # Position should have split from 10 to 20 shares on session 5
        pos = engine.portfolio.ledger.positions["AAPL"]
        self.assertEqual(pos.total_shares, 20)

        # Dividend should have settled: 20 shares * $0.50 = $10.00
        self.assertEqual(engine.portfolio.ledger.cumulative_dividend_income_usd, Decimal("10.00"))
        self.assertEqual(engine.portfolio.ledger.dividends_receivable_usd, Decimal("0.00"))

        # Survivorship notice present
        self.assertIn("survivorship_bias_notice", results)


class TestSurvivorshipBiasDisclosure(unittest.TestCase):
    """Test survivorship bias documentation and disclosure requirements."""

    def test_disclosure_constants_exist_and_match(self):
        self.assertTrue(len(SURVIVORSHIP_BIAS_DISCLOSURE) > 50)
        self.assertTrue(len(UNIVERSE_SURVIVORSHIP_DISCLOSURE) > 50)
        self.assertIn("survivorship bias", SURVIVORSHIP_BIAS_DISCLOSURE.lower())
        self.assertIn("survivorship bias", UNIVERSE_SURVIVORSHIP_DISCLOSURE.lower())

    def test_data_manager_survivorship_notice(self):
        dm = HistoricalDataManager()
        notice = dm.get_survivorship_bias_notice()
        self.assertIn("SURVIVORSHIP BIAS NOTICE", notice)


if __name__ == "__main__":
    unittest.main()
