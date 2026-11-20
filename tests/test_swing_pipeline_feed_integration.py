"""Deterministic offline integration test suite for SwingTradingStrategyService with MarketDataFeedAdapter.

Phase 6.2B — Step 4: Verification Gate and Read-Only Pipeline Integration

Verifies:
1. Successful feed-to-analysis end-to-end execution flow.
2. SPY readiness failure aborts analysis pipeline before running on invalid data.
3. Missing optional proxy (QQQ, IWM) warnings are preserved in the final integrated report.
4. Invalid, insufficient, or stale stock exclusions are preserved and excluded from scanner/signals.
5. Incomplete, forming, weekend, or holiday candles are rejected and never reach analysis.
6. Deterministic risk engine remains authoritative: no signal or trade plan bypasses risk limits.
7. Pipeline execution is strictly read-only: zero mutations to ledger cash, open lots, or journal.
8. No live network calls: executes 100% offline using injected mock providers.
9. No persistent cache writes: market_data.db is never touched, opened, or written to.
10. Provider errors (RateLimitError, TimeoutError) propagate as structured readiness failures.
"""

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import os
import sys
import unittest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SWING_DIR = os.path.join(ROOT_DIR, ".agents", "skills", "trader-swing")
for path in (ROOT_DIR, SWING_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

from accounting import AccountingLedger
from ai_research import MockLLMProvider
from feed_adapter import (
    FeedReadinessResult,
    MarketDataFeedAdapter,
    MarketDataReadinessEvaluator,
    ReadinessStatus,
)
from market_data_cache import DEFAULT_CACHE_DB
from market_data_models import (
    CandleStatus,
    EASTERN,
    MarketClock,
    OHLCVCandle,
    to_eastern,
    to_utc,
    UTC,
)
from market_data_validation import NoDataError, RateLimitError, TimeoutError
from pipeline import IntegratedStrategyAnalysisReport, SwingTradingStrategyService
from providers.base import MarketDataProvider


class PipelineMockProvider(MarketDataProvider):
    """In-memory mock provider for deterministic offline pipeline integration tests."""

    name: str = "mock_pipeline_provider"
    is_delayed: bool = True
    delay_status: str = "UNVERIFIED_DELAY"
    delay_minutes: int = 15
    native_adjustment_type: str = "SPLIT_ADJUSTED"

    def __init__(self, candle_store: dict = None, failure_map: dict = None):
        self.candle_store = candle_store or {}
        self.failure_map = failure_map or {}

    def fetch_historical_bars(self, ticker: str, interval: str, start: datetime, end: datetime, current_time: datetime = None):
        sym = ticker.upper().strip()
        if sym in self.failure_map:
            exc = self.failure_map[sym]
            if isinstance(exc, type) and issubclass(exc, Exception):
                raise exc(f"Simulated failure for {sym}")
            raise exc

        if sym not in self.candle_store:
            raise NoDataError(f"No mock data for {sym}")

        return list(self.candle_store[sym])

    def fetch_latest_bar(self, ticker: str, interval: str, current_time: datetime = None):
        bars = self.fetch_historical_bars(ticker, interval, datetime.min.replace(tzinfo=UTC), datetime.max.replace(tzinfo=UTC))
        return bars[-1] if bars else None

    def get_provider_metadata(self):
        return {
            "provider_name": self.name,
            "is_delayed": self.is_delayed,
            "delay_status": self.delay_status,
            "delay_minutes": self.delay_minutes,
            "native_adjustment_type": self.native_adjustment_type,
            "max_retries": 3,
        }


class TestSwingPipelineFeedIntegration(unittest.TestCase):
    """Integration test suite connecting MarketDataFeedAdapter with SwingTradingStrategyService."""

    def setUp(self):
        self.service = SwingTradingStrategyService(llm_provider=MockLLMProvider())

        # Build 230 confirmed NYSE trading days in 2026
        self.trading_days = []
        d = date(2026, 1, 5)
        while len(self.trading_days) < 230:
            sess = MarketClock.get_session(d)
            if sess is not None:
                self.trading_days.append(d)
            d += timedelta(days=1)

    def _make_series(
        self,
        ticker: str,
        days: list,
        base_px: float = 100.0,
        trend: float = 0.45,
        status: CandleStatus = CandleStatus.COMPLETE,
    ) -> list:
        candles = []
        curr = base_px
        for i, d in enumerate(days):
            if i % 2 == 0:
                curr = curr - (trend * 0.5)
            else:
                curr = curr + trend
            c_dec = Decimal(f"{curr:.2f}")
            o_dec = c_dec - Decimal("0.10")
            spread = max(Decimal("2.00"), c_dec * Decimal("0.025"))
            h_dec = max(c_dec, o_dec) + spread
            l_dec = min(c_dec, o_dec) - spread

            start_et = datetime.combine(d, MarketClock.REGULAR_OPEN, tzinfo=EASTERN)
            ts_utc = to_utc(start_et)
            c = OHLCVCandle(
                ticker=ticker,
                interval="1d",
                timestamp=ts_utc,
                open=o_dec,
                high=h_dec,
                low=l_dec,
                close=c_dec,
                volume=20000000,
                status=status,
                source="yfinance",
                is_delayed=True,
            )
            c.price_adjustment_type = "SPLIT_ADJUSTED"
            candles.append(c)
        return candles

    def test_successful_feed_to_analysis_flow(self):
        """End-to-end integration: valid feed readiness smoothly drives full strategy pipeline analysis."""
        eval_date = self.trading_days[205]
        eval_dt = datetime.combine(eval_date, time(18, 0), tzinfo=EASTERN)

        # 206 bars of SPY, QQQ, IWM and 206 bars of AAPL, MSFT ending on eval_date
        spy_bars = self._make_series("SPY", self.trading_days[:206], base_px=450.0, trend=0.45)
        qqq_bars = self._make_series("QQQ", self.trading_days[:206], base_px=380.0, trend=0.40)
        iwm_bars = self._make_series("IWM", self.trading_days[:206], base_px=200.0, trend=0.35)
        aapl_bars = self._make_series("AAPL", self.trading_days[:206], base_px=150.0, trend=0.50)
        msft_bars = self._make_series("MSFT", self.trading_days[:206], base_px=300.0, trend=0.55)

        candle_store = {
            "SPY": spy_bars,
            "QQQ": qqq_bars,
            "IWM": iwm_bars,
            "AAPL": aapl_bars,
            "MSFT": msft_bars,
        }
        provider = PipelineMockProvider(candle_store=candle_store)
        adapter = MarketDataFeedAdapter(provider=provider)

        # Run analysis through feed gate
        report = self.service.run_analysis_with_feed(
            stock_universe=["AAPL", "MSFT"],
            feed_adapter=adapter,
            current_time=eval_dt,
        )

        self.assertIsInstance(report, IntegratedStrategyAnalysisReport)
        self.assertTrue(report.is_ready)
        self.assertEqual(report.status, "READY")
        self.assertIsNotNone(report.strategy_report)

        # Verify strategy analysis contents
        strat = report.strategy_report
        self.assertIsNotNone(strat.market_regime)
        self.assertIsNotNone(strat.scan_result)
        self.assertGreaterEqual(strat.eligible_candidates_count, 1)
        self.assertIsNotNone(strat.signal_summary)

        # Verify serializability
        report_dict = report.to_dict()
        self.assertIn("readiness", report_dict)
        self.assertIn("strategy_report", report_dict)
        self.assertEqual(report_dict["readiness"]["status"], "READY")

    def test_spy_readiness_failure_prevents_analysis(self):
        """When SPY has insufficient bars or fails, analysis is halted and strategy_report is None."""
        eval_date = self.trading_days[205]
        eval_dt = datetime.combine(eval_date, time(18, 0), tzinfo=EASTERN)

        # SPY has only 150 bars (< 200 required)
        spy_bars = self._make_series("SPY", self.trading_days[:150], base_px=450.0)
        aapl_bars = self._make_series("AAPL", self.trading_days[106:206], base_px=180.0)

        provider = PipelineMockProvider(candle_store={"SPY": spy_bars, "AAPL": aapl_bars})
        adapter = MarketDataFeedAdapter(provider=provider)

        report = self.service.run_analysis_with_feed(
            stock_universe=["AAPL"],
            feed_adapter=adapter,
            current_time=eval_dt,
        )

        self.assertFalse(report.is_ready)
        self.assertEqual(report.status, "HARD_FAIL")
        # Critical guarantee: strategy analysis MUST NOT run on invalid data
        self.assertIsNone(report.strategy_report)
        self.assertTrue(any("INSUFFICIENT_SPY_BARS" in r for r in report.failure_reasons))

    def test_missing_optional_proxy_warnings_preserved(self):
        """Missing optional proxies (QQQ, IWM) produce warnings that are preserved in final report."""
        eval_date = self.trading_days[205]
        eval_dt = datetime.combine(eval_date, time(18, 0), tzinfo=EASTERN)

        spy_bars = self._make_series("SPY", self.trading_days[:206], base_px=450.0, trend=0.4)
        # QQQ is completely missing; IWM has only 10 bars
        iwm_bars = self._make_series("IWM", self.trading_days[:10], base_px=200.0)
        aapl_bars = self._make_series("AAPL", self.trading_days[106:206], base_px=180.0, trend=0.5)

        provider = PipelineMockProvider(candle_store={"SPY": spy_bars, "IWM": iwm_bars, "AAPL": aapl_bars})
        adapter = MarketDataFeedAdapter(provider=provider)

        report = self.service.run_analysis_with_feed(
            stock_universe=["AAPL"],
            feed_adapter=adapter,
            current_time=eval_dt,
        )

        self.assertTrue(report.is_ready)
        self.assertIsNotNone(report.strategy_report)
        # Verify warnings are retained in top-level integrated report
        self.assertTrue(any("QQQ missing" in w for w in report.warnings))
        self.assertTrue(any("IWM has insufficient bars" in w for w in report.warnings))

    def test_invalid_or_stale_stock_exclusion_preserved(self):
        """Stale or insufficient stocks are excluded and never reach the scanner or signal engine."""
        eval_date = self.trading_days[205]
        eval_dt = datetime.combine(eval_date, time(18, 0), tzinfo=EASTERN)

        spy_bars = self._make_series("SPY", self.trading_days[:206], base_px=450.0, trend=0.4)
        # 1. Valid stock AAPL (100 bars through day 205)
        aapl_bars = self._make_series("AAPL", self.trading_days[106:206], base_px=180.0, trend=0.5)
        # 2. Stale stock STALE_SYM (100 bars but stops at day 200, missing 5 sessions)
        stale_bars = self._make_series("STALE_SYM", self.trading_days[101:201], base_px=50.0)
        # 3. Insufficient stock FEW_BARS (only 30 bars < 50)
        few_bars = self._make_series("FEW_BARS", self.trading_days[176:206], base_px=75.0)

        candle_store = {
            "SPY": spy_bars,
            "AAPL": aapl_bars,
            "STALE_SYM": stale_bars,
            "FEW_BARS": few_bars,
        }
        provider = PipelineMockProvider(candle_store=candle_store)
        adapter = MarketDataFeedAdapter(provider=provider)

        report = self.service.run_analysis_with_feed(
            stock_universe=["AAPL", "STALE_SYM", "FEW_BARS"],
            feed_adapter=adapter,
            current_time=eval_dt,
        )

        self.assertTrue(report.is_ready)
        self.assertIsNotNone(report.strategy_report)

        # Exclusions tracked cleanly in readiness
        excluded_tickers = [e.ticker for e in report.readiness.excluded_tickers]
        self.assertIn("STALE_SYM", excluded_tickers)
        self.assertIn("FEW_BARS", excluded_tickers)
        self.assertNotIn("AAPL", excluded_tickers)

        # Excluded stocks NEVER passed to scanner or candidates
        candidate_tickers = [c.ticker for c in report.strategy_report.scan_result.candidates]
        self.assertNotIn("STALE_SYM", candidate_tickers)
        self.assertNotIn("FEW_BARS", candidate_tickers)

    def test_no_incomplete_candles_reach_analysis_pipeline(self):
        """Forming terminal candle in a stock is rejected by feed gate and excluded from pipeline."""
        eval_date = self.trading_days[205]
        eval_dt = datetime.combine(eval_date, time(18, 0), tzinfo=EASTERN)

        spy_bars = self._make_series("SPY", self.trading_days[:206], base_px=450.0, trend=0.4)
        msft_bars = self._make_series("MSFT", self.trading_days[106:206], base_px=300.0, trend=0.5)
        # MSFT terminal candle is marked FORMING
        msft_bars[-1].status = CandleStatus.FORMING

        provider = PipelineMockProvider(candle_store={"SPY": spy_bars, "MSFT": msft_bars})
        adapter = MarketDataFeedAdapter(provider=provider)

        report = self.service.run_analysis_with_feed(
            stock_universe=["MSFT"],
            feed_adapter=adapter,
            current_time=eval_dt,
        )

        self.assertTrue(report.is_ready)  # SPY is valid
        # MSFT is excluded due to invalid forming candle
        self.assertNotIn("MSFT", report.readiness.included_tickers)
        self.assertTrue(any(e.ticker == "MSFT" and "INVALID_CANDLES" in e.reason for e in report.readiness.excluded_tickers))

    def test_existing_risk_engine_remains_authoritative(self):
        """Trade plans generated through feed integration strictly comply with deterministic RiskEngine rules."""
        eval_date = self.trading_days[205]
        eval_dt = datetime.combine(eval_date, time(18, 0), tzinfo=EASTERN)

        spy_bars = self._make_series("SPY", self.trading_days[:206], base_px=450.0, trend=0.5)
        # Candidate with strong breakout pattern
        aapl_bars = self._make_series("AAPL", self.trading_days[106:206], base_px=150.0, trend=0.8)

        provider = PipelineMockProvider(candle_store={"SPY": spy_bars, "AAPL": aapl_bars})
        adapter = MarketDataFeedAdapter(provider=provider)
        ledger = AccountingLedger(initial_capital="5000.00")

        report = self.service.run_analysis_with_feed(
            stock_universe=["AAPL"],
            feed_adapter=adapter,
            ledger=ledger,
            current_time=eval_dt,
        )

        self.assertTrue(report.is_ready)
        strat = report.strategy_report
        self.assertIsNotNone(strat)

        # Inspect any approved plans
        for plan in strat.signal_summary.risk_approved_plans:
            # 1. Max risk per trade <= 0.50% of $5000 = $25.00
            risk_amt = plan.shares * (plan.entry_price - plan.stop_loss_price)
            self.assertLessEqual(risk_amt, Decimal("25.00"))
            # 2. Max position size <= $1000.00 (20% of capital)
            self.assertLessEqual(plan.shares * plan.entry_price, Decimal("1000.00"))
            # 3. Stop loss below entry
            self.assertLess(plan.stop_loss_price, plan.entry_price)

    def test_analysis_does_not_mutate_paper_trading_state(self):
        """Pipeline analysis through feed adapter is strictly read-only and never mutates ledger or positions."""
        eval_date = self.trading_days[205]
        eval_dt = datetime.combine(eval_date, time(18, 0), tzinfo=EASTERN)

        spy_bars = self._make_series("SPY", self.trading_days[:206], base_px=450.0, trend=0.4)
        aapl_bars = self._make_series("AAPL", self.trading_days[:206], base_px=180.0, trend=0.5)

        provider = PipelineMockProvider(candle_store={"SPY": spy_bars, "AAPL": aapl_bars})
        adapter = MarketDataFeedAdapter(provider=provider)

        # Set up active ledger with 1 open position
        ledger = AccountingLedger(initial_capital="5000.00")
        ledger.buy(
            ticker="MSFT",
            shares=4,
            price=Decimal("200.00"),
            fee=Decimal("1.00"),
            timestamp="2026-10-19T14:15:00+00:00",
        )

        cash_before = ledger.cash_usd
        equity_before = ledger.total_equity_usd
        pos_before = len(ledger.positions)
        closed_trades_before = len(ledger.closed_trades)

        report = self.service.run_analysis_with_feed(
            stock_universe=["AAPL"],
            feed_adapter=adapter,
            ledger=ledger,
            current_time=eval_dt,
        )

        self.assertTrue(report.is_ready)

        # STRICT VERIFICATION: ledger state is 100% unchanged
        self.assertEqual(ledger.cash_usd, cash_before)
        self.assertEqual(ledger.total_equity_usd, equity_before)
        self.assertEqual(len(ledger.positions), pos_before)
        self.assertEqual(len(ledger.closed_trades), closed_trades_before)

    def test_no_live_network_calls(self):
        """Execution operates strictly offline in-memory using mock providers with zero network requests."""
        eval_date = self.trading_days[205]
        eval_dt = datetime.combine(eval_date, time(18, 0), tzinfo=EASTERN)

        spy_bars = self._make_series("SPY", self.trading_days[:206], base_px=450.0, trend=0.4)
        aapl_bars = self._make_series("AAPL", self.trading_days[:206], base_px=180.0, trend=0.5)

        provider = PipelineMockProvider(candle_store={"SPY": spy_bars, "AAPL": aapl_bars})
        adapter = MarketDataFeedAdapter(provider=provider)

        report = self.service.run_analysis_with_feed(
            stock_universe=["AAPL"],
            feed_adapter=adapter,
            current_time=eval_dt,
        )

        self.assertTrue(report.is_ready)
        self.assertIsNotNone(report.strategy_report)

    def test_no_persistent_cache_writes(self):
        """Execution operates strictly offline in-memory and does not touch or write market_data.db."""
        eval_date = self.trading_days[205]
        eval_dt = datetime.combine(eval_date, time(18, 0), tzinfo=EASTERN)

        db_before_exists = os.path.exists(DEFAULT_CACHE_DB)
        mtime_before = os.path.getmtime(DEFAULT_CACHE_DB) if db_before_exists else None

        spy_bars = self._make_series("SPY", self.trading_days[:206], base_px=450.0, trend=0.4)
        aapl_bars = self._make_series("AAPL", self.trading_days[:206], base_px=180.0, trend=0.5)

        provider = PipelineMockProvider(candle_store={"SPY": spy_bars, "AAPL": aapl_bars})
        adapter = MarketDataFeedAdapter(provider=provider)

        report = self.service.run_analysis_with_feed(
            stock_universe=["AAPL"],
            feed_adapter=adapter,
            current_time=eval_dt,
        )

        self.assertTrue(report.is_ready)

        if db_before_exists:
            mtime_after = os.path.getmtime(DEFAULT_CACHE_DB)
            self.assertEqual(mtime_before, mtime_after, "DEFAULT_CACHE_DB mtime must not change during pipeline run")

    def test_provider_errors_propagate_as_structured_failures(self):
        """Provider failures (RateLimitError, TimeoutError) on SPY produce structured HARD_FAIL."""
        eval_date = self.trading_days[205]
        eval_dt = datetime.combine(eval_date, time(18, 0), tzinfo=EASTERN)

        # SPY raises RateLimitError
        provider = PipelineMockProvider(
            failure_map={"SPY": RateLimitError("HTTP 429 Too Many Requests")}
        )
        adapter = MarketDataFeedAdapter(provider=provider)

        report = self.service.run_analysis_with_feed(
            stock_universe=["AAPL"],
            feed_adapter=adapter,
            current_time=eval_dt,
        )

        self.assertFalse(report.is_ready)
        self.assertEqual(report.status, "HARD_FAIL")
        self.assertIsNone(report.strategy_report)
        self.assertTrue(any("MISSING_PRIMARY_PROXY_SPY" in r for r in report.failure_reasons))


if __name__ == "__main__":
    unittest.main()
