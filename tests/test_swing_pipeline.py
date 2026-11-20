"""Unit tests for Phase 5 Unified Pipeline Service in trader-swing.

Enforces:
1. Complete end-to-end integration across regime, scanner, signals, risk, and AI research.
2. Read-only execution: zero mutations to ledger, positions, or cash.
3. Graceful handling of bearish regimes, empty universes, and missing candles.
4. Complete serializability of StrategyAnalysisReport.
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal
import os
import sys
import unittest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL_DIR = os.path.join(ROOT_DIR, ".agents", "skills", "trader-swing")
for path in (ROOT_DIR, SKILL_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

from accounting import AccountingLedger
from ai_research import MockLLMProvider
from market_data_models import CandleStatus, MarketClock, OHLCVCandle, UTC
from pipeline import StrategyAnalysisReport, SwingTradingStrategyService
from regime_models import MarketRegime
from research_models import FinalCandidateAssessment, ResearchItem, SourceReliability
from scanner_models import ScreeningCriteria

# Pre-generate confirmed NYSE trading days for synthetic test fixtures
_PIPELINE_TEST_TRADING_DAYS = []
_curr_d = date(2026, 1, 5)
while len(_PIPELINE_TEST_TRADING_DAYS) < 300:
    if MarketClock.get_session(_curr_d):
        _PIPELINE_TEST_TRADING_DAYS.append(_curr_d)
    _curr_d += timedelta(days=1)


def make_candle(
    ticker: str,
    index: int,
    close_p: str,
    open_p: str = None,
    high_p: str = None,
    low_p: str = None,
    volume: int = 20000000,
    status: CandleStatus = CandleStatus.COMPLETE,
    base_date: datetime = None,
) -> OHLCVCandle:
    if base_date is None:
        base_date = datetime(2026, 1, 5, 20, 0, 0, tzinfo=UTC)
    if index < len(_PIPELINE_TEST_TRADING_DAYS):
        d = _PIPELINE_TEST_TRADING_DAYS[index]
        ts = datetime.combine(d, time(20, 0, 0), tzinfo=UTC)
    else:
        ts = base_date + timedelta(days=index)
    c_dec = Decimal(close_p)
    o_dec = Decimal(open_p) if open_p is not None else c_dec
    spread = max(Decimal("2.00"), c_dec * Decimal("0.02"))
    h_dec = Decimal(high_p) if high_p is not None else max(c_dec, o_dec) + spread
    l_dec = Decimal(low_p) if low_p is not None else min(c_dec, o_dec) - spread
    return OHLCVCandle(
        ticker=ticker,
        interval="1d",
        timestamp=ts,
        open=o_dec,
        high=h_dec,
        low=l_dec,
        close=c_dec,
        volume=volume,
        status=status,
        source="yahoo",
    )


def make_bullish_series(ticker: str, base_price: float, count: int = 205) -> list:
    candles = []
    curr = base_price
    for i in range(count):
        step = 0.45
        if i % 2 == 0:
            curr = curr - (step * 0.5)
        else:
            curr = curr + step
        candles.append(make_candle(ticker, i, f"{curr:.2f}"))
    return candles


def make_bearish_series(ticker: str, base_price: float, count: int = 205) -> list:
    candles = []
    curr = base_price
    for i in range(count):
        step = 0.45
        if i % 2 == 0:
            curr = curr + (step * 0.5)
        else:
            curr = curr - step
        candles.append(make_candle(ticker, i, f"{curr:.2f}"))
    return candles


class TestSwingTradingPipeline(unittest.TestCase):
    """Test suite for SwingTradingStrategyService pipeline integration."""

    def setUp(self):
        self.service = SwingTradingStrategyService(llm_provider=MockLLMProvider())

        # Build proxy candles (bullish SPY, QQQ, IWM)
        self.proxy_candles = {
            "SPY": make_bullish_series("SPY", 450.0),
            "QQQ": make_bullish_series("QQQ", 350.0),
            "IWM": make_bullish_series("IWM", 180.0),
        }

        # Build stock candles
        self.stock_candles = {
            "AAPL": make_bullish_series("AAPL", 150.0),
            "MSFT": make_bullish_series("MSFT", 300.0),
            "NVDA": make_bullish_series("NVDA", 400.0),
        }

    def test_01_full_end_to_end_pipeline_analysis(self):
        """Verify full pipeline runs smoothly and returns structured report."""
        universe = ["AAPL", "MSFT", "NVDA"]
        ledger = AccountingLedger(initial_capital="10000.00")
        initial_cash = ledger.cash_usd
        as_of = self.proxy_candles["SPY"][-1].timestamp + timedelta(hours=2)

        report = self.service.run_analysis(
            proxy_candles_map=self.proxy_candles,
            stock_candles_map=self.stock_candles,
            universe=universe,
            ledger=ledger,
            top_n=5,
            current_time=as_of,
        )

        self.assertIsInstance(report, StrategyAnalysisReport)
        self.assertIn(report.market_regime.regime, [MarketRegime.BULLISH, MarketRegime.STRONG_BULLISH])
        self.assertEqual(report.total_universe_count, 3)
        self.assertTrue(report.eligible_candidates_count >= 1)
        self.assertTrue(len(report.enriched_candidates) >= 1)

        # Check candidate enrichment structure
        top_enriched = report.enriched_candidates[0]
        self.assertIn(top_enriched.final_assessment, [
            FinalCandidateAssessment.STRONG_CANDIDATE,
            FinalCandidateAssessment.CANDIDATE,
            FinalCandidateAssessment.WEAK_CANDIDATE,
            FinalCandidateAssessment.REJECTED,
        ])
        self.assertIsNotNone(top_enriched.research_summary)

        # STRICT CHECK: Verify zero ledger mutations occurred during analysis
        self.assertEqual(ledger.cash_usd, initial_cash)
        self.assertEqual(len(ledger.positions), 0)
        self.assertEqual(len(ledger.closed_trades), 0)

        # Check report serialization to dictionary
        as_dict = report.to_dict()
        self.assertEqual(as_dict["total_universe_count"], 3)
        self.assertIn("market_regime", as_dict)
        self.assertIn("enriched_candidates", as_dict)

    def test_02_bearish_regime_disallows_entries_with_warning(self):
        """Verify bearish market proxies produce warnings and suppress entries."""
        bearish_proxies = {
            "SPY": make_bearish_series("SPY", 550.0),
            "QQQ": make_bearish_series("QQQ", 450.0),
            "IWM": make_bearish_series("IWM", 250.0),
        }
        as_of = bearish_proxies["SPY"][-1].timestamp + timedelta(hours=2)

        report = self.service.run_analysis(
            proxy_candles_map=bearish_proxies,
            stock_candles_map=self.stock_candles,
            universe=["AAPL", "MSFT"],
            current_time=as_of,
        )

        self.assertIn(report.market_regime.regime, [MarketRegime.BEARISH, MarketRegime.STRONG_BEARISH])
        self.assertFalse(report.market_regime.policy.allow_new_longs)
        self.assertTrue(any("disallows new long entries" in w for w in report.warnings))
        self.assertEqual(report.eligible_candidates_count, 0)
        self.assertEqual(len(report.enriched_candidates), 0)

    def test_03_exit_monitoring_for_existing_open_positions(self):
        """Verify pipeline monitors and evaluates exits for ledger open positions."""
        ledger = AccountingLedger(initial_capital="10000.00")
        # Buy 10 shares of AAPL at 180.00
        lot = ledger.buy("AAPL", 10, Decimal("180.00"))
        lot_id = lot.lot_id
        as_of = self.proxy_candles["SPY"][-1].timestamp + timedelta(hours=2)

        # Set stop price higher than current price (~185.00) to trigger stop loss breach
        lot_stops = {lot_id: Decimal("190.00")}
        lot_targets = {lot_id: Decimal("220.00")}

        report = self.service.run_analysis(
            proxy_candles_map=self.proxy_candles,
            stock_candles_map=self.stock_candles,
            universe=["AAPL"],
            ledger=ledger,
            lot_stops=lot_stops,
            lot_targets=lot_targets,
            current_time=as_of,
        )

        self.assertEqual(len(ledger.positions), 1)
        self.assertTrue(len(report.exit_signals) >= 1)
        self.assertEqual(report.exit_signals[0].ticker, "AAPL")

    def test_04_graceful_handling_of_empty_and_missing_data(self):
        """Verify empty stock candles or empty universe do not crash the service."""
        empty_candles = {}
        report = self.service.run_analysis(
            proxy_candles_map=self.proxy_candles,
            stock_candles_map=empty_candles,
            universe=["XYZ"],
        )

        self.assertEqual(report.total_universe_count, 1)
        self.assertEqual(report.eligible_candidates_count, 0)
        self.assertEqual(len(report.enriched_candidates), 0)
        self.assertEqual(len(report.exit_signals), 0)


if __name__ == "__main__":
    unittest.main()
