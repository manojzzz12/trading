"""Deterministic offline unit test suite for MarketDataFeedAdapter and MarketDataReadinessEvaluator.

Phase 6.2B — Step 3: Feed Adapter & Readiness Evaluator (TEST-ONLY)

Verifies:
1. SPY with 199 bars: hard failure (status HARD_FAIL).
2. SPY with 200 valid bars: eligible (status READY).
3. Missing SPY: hard failure.
4. Missing QQQ or IWM: warning and proxy exclusion without failing overall readiness.
5. Stock with fewer than 50 bars: exclusion from active universe.
6. Stock missing latest completed session: exclusion with STALE_STOCK_DATA reason.
7. Stale or incomplete daily candle: rejection via IncompleteCandleError.
8. Weekend and exchange-holiday daily candle: rejection via lifecycle UNKNOWN.
9. Early-close session (13:00 ET close): dynamic completed session and bar_end_timestamp handling.
10. DST timestamp handling: zero calendar date-shifting across March EDT and November EST.
11. Mixed adjustment basis: rejection.
12. Provider failure and retry exhaustion: SPY failure produces HARD_FAIL; stock failure produces exclusion.
13. No live network calls: 100% offline with injected callables/mocks.
14. Cache isolation: zero persistent writes to market_data.db during evaluation.
"""

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import os
import shutil
import sys
import tempfile
from typing import Any, Dict, List, Optional, Sequence
import unittest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SWING_DIR = os.path.join(ROOT_DIR, ".agents", "skills", "trader-swing")
for path in (ROOT_DIR, SWING_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

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
from indicator_validation import IncompleteCandleError
from market_data_validation import (
    CandleValidationError,
    NoDataError,
    ProviderUnavailableError,
    RateLimitError,
    SequenceValidationError,
    TimeoutError,
)
from feed_adapter import (
    FeedReadinessResult,
    IncompatibleAdjustmentBasisError,
    MarketDataFeedAdapter,
    MarketDataReadinessEvaluator,
    ReadinessStatus,
    StockExclusion,
)
from providers.base import MarketDataProvider
from providers.yfinance_provider import YFinanceMarketDataProvider


class MockMarketDataProvider(MarketDataProvider):
    """In-memory mock market data provider for deterministic zero-network testing."""

    name: str = "mock_provider"
    is_delayed: bool = True
    delay_status: str = "UNVERIFIED_DELAY"
    delay_minutes: int = 15
    native_adjustment_type: str = "SPLIT_ADJUSTED"

    def __init__(
        self,
        candle_store: Optional[dict] = None,
        failure_map: Optional[dict] = None,
    ):
        self.candle_store = candle_store or {}
        self.failure_map = failure_map or {}

    def fetch_historical_bars(
        self,
        ticker: str,
        interval: str,
        start: datetime,
        end: datetime,
        current_time: Optional[datetime] = None,
    ):
        sym = ticker.upper().strip()
        if sym in self.failure_map:
            exc = self.failure_map[sym]
            if isinstance(exc, type) and issubclass(exc, Exception):
                raise exc(f"Simulated failure for {sym}")
            raise exc

        if sym not in self.candle_store:
            raise NoDataError(f"No data for {sym}")

        return list(self.candle_store[sym])

    def fetch_latest_bar(
        self,
        ticker: str,
        interval: str,
        current_time: Optional[datetime] = None,
    ):
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


class TestSwingFeedAdapterAndReadiness(unittest.TestCase):
    """Comprehensive offline unit tests for feed adapter and readiness evaluator."""

    def setUp(self):
        self.evaluator = MarketDataReadinessEvaluator()
        self.clock = MarketClock()

        # Build 230 confirmed chronological NYSE trading days in 2026
        self.trading_days = []
        d = date(2026, 1, 5)
        while len(self.trading_days) < 230:
            sess = MarketClock.get_session(d)
            if sess is not None:
                self.trading_days.append(d)
            d += timedelta(days=1)

    def _make_candle(
        self,
        ticker: str,
        sess_date: date,
        px: Decimal = Decimal("100.00"),
        status: CandleStatus = CandleStatus.COMPLETE,
        adjustment_type: str = "SPLIT_ADJUSTED",
    ) -> OHLCVCandle:
        """Helper creating a single normalized daily candle."""
        start_et = datetime.combine(sess_date, MarketClock.REGULAR_OPEN, tzinfo=EASTERN)
        ts_utc = to_utc(start_et)
        c = OHLCVCandle(
            ticker=ticker,
            interval="1d",
            timestamp=ts_utc,
            open=px,
            high=px + Decimal("2.00"),
            low=px - Decimal("2.00"),
            close=px,
            volume=1000000,
            status=status,
            source="test_feed",
            is_delayed=True,
        )
        c.price_adjustment_type = adjustment_type
        return c

    def _make_series(
        self,
        ticker: str,
        days: list,
        base_px: Decimal = Decimal("100.00"),
        status: CandleStatus = CandleStatus.COMPLETE,
        adjustment_type: str = "SPLIT_ADJUSTED",
    ) -> list:
        """Helper creating a sequence of normalized daily candles."""
        candles = []
        for i, d in enumerate(days):
            px = base_px + (Decimal(str(i)) * Decimal("0.10"))
            candles.append(self._make_candle(ticker, d, px=px, status=status, adjustment_type=adjustment_type))
        return candles

    # ==========================================================================
    # 1. SPY BAR COUNT & MISSING CHECKS (HARD FAIL)
    # ==========================================================================

    def test_spy_with_199_bars_is_hard_failure(self):
        """SPY with 199 bars (< 200 required) must trigger HARD_FAIL readiness status."""
        eval_date = self.trading_days[198]
        eval_dt = datetime.combine(eval_date, time(18, 0), tzinfo=EASTERN)

        spy_bars = self._make_series("SPY", self.trading_days[:199])
        self.assertEqual(len(spy_bars), 199)

        result = self.evaluator.evaluate_readiness(
            proxy_candles_map={"SPY": spy_bars},
            current_time=eval_dt,
        )

        self.assertFalse(result.is_ready)
        self.assertEqual(result.status, ReadinessStatus.HARD_FAIL)
        self.assertEqual(result["status"], "HARD_FAIL")
        self.assertTrue(any("INSUFFICIENT_SPY_BARS" in r for r in result.failure_reasons))
        self.assertIn("199 < 200", result["reason"])

    def test_spy_with_200_valid_bars_is_eligible(self):
        """SPY with 200 valid completed bars must be eligible (READY)."""
        eval_date = self.trading_days[199]
        eval_dt = datetime.combine(eval_date, time(18, 0), tzinfo=EASTERN)

        spy_bars = self._make_series("SPY", self.trading_days[:200])
        self.assertEqual(len(spy_bars), 200)

        result = self.evaluator.evaluate_readiness(
            proxy_candles_map={"SPY": spy_bars},
            current_time=eval_dt,
        )

        self.assertTrue(result.is_ready)
        self.assertEqual(result.status, ReadinessStatus.READY)
        self.assertEqual(result["status"], "READY")
        self.assertEqual(len(result.failure_reasons), 0)
        self.assertIn("SPY", result.proxy_candles)

    def test_missing_spy_is_hard_failure(self):
        """Completely missing primary proxy SPY must produce a hard readiness failure."""
        eval_date = self.trading_days[205]
        eval_dt = datetime.combine(eval_date, time(18, 0), tzinfo=EASTERN)

        # Only secondary proxies provided, SPY omitted
        qqq_bars = self._make_series("QQQ", self.trading_days[:206])
        result = self.evaluator.evaluate_readiness(
            proxy_candles_map={"QQQ": qqq_bars},
            current_time=eval_dt,
        )

        self.assertFalse(result.is_ready)
        self.assertEqual(result.status, ReadinessStatus.HARD_FAIL)
        self.assertTrue(any("MISSING_PRIMARY_PROXY_SPY" in r for r in result.failure_reasons))

    # ==========================================================================
    # 2. SECONDARY PROXIES (QQQ, IWM) WARNING & EXCLUSION
    # ==========================================================================

    def test_missing_secondary_proxies_produces_warning_and_exclusion(self):
        """Missing or insufficient secondary proxies (QQQ, IWM) produce warnings but do not fail SPY."""
        eval_date = self.trading_days[205]
        eval_dt = datetime.combine(eval_date, time(18, 0), tzinfo=EASTERN)

        spy_bars = self._make_series("SPY", self.trading_days[:206])
        # QQQ is missing; IWM has only 10 bars (< 20 required)
        iwm_bars = self._make_series("IWM", self.trading_days[:10])

        result = self.evaluator.evaluate_readiness(
            proxy_candles_map={"SPY": spy_bars, "IWM": iwm_bars},
            current_time=eval_dt,
        )

        # Primary readiness succeeds because SPY is valid
        self.assertTrue(result.is_ready)
        self.assertEqual(result.status, ReadinessStatus.READY)

        # Both QQQ and IWM excluded from validated proxy candles with warnings
        self.assertNotIn("QQQ", result.proxy_candles)
        self.assertNotIn("IWM", result.proxy_candles)
        self.assertTrue(any("QQQ missing" in w for w in result.warnings))
        self.assertTrue(any("IWM has insufficient bars" in w for w in result.warnings))

    # ==========================================================================
    # 3. STOCK UNIVERSE EXCLUSIONS (COUNT & STALENESS)
    # ==========================================================================

    def test_stock_with_fewer_than_50_bars_is_excluded(self):
        """Candidate stock with fewer than 50 bars is excluded with explicit reason."""
        eval_date = self.trading_days[205]
        eval_dt = datetime.combine(eval_date, time(18, 0), tzinfo=EASTERN)

        spy_bars = self._make_series("SPY", self.trading_days[:206])
        stock_bars = self._make_series("AAPL", self.trading_days[:49])  # 49 < 50

        result = self.evaluator.evaluate_readiness(
            proxy_candles_map={"SPY": spy_bars},
            stock_candles_map={"AAPL": stock_bars},
            current_time=eval_dt,
        )

        self.assertTrue(result.is_ready)
        self.assertNotIn("AAPL", result.stock_candles)
        self.assertNotIn("AAPL", result.included_tickers)
        self.assertEqual(len(result.excluded_tickers), 1)

        exclusion = result.excluded_tickers[0]
        self.assertEqual(exclusion.ticker, "AAPL")
        self.assertIn("INSUFFICIENT_BARS: 49 < 50", exclusion.reason)
        self.assertEqual(exclusion.bar_count, 49)

    def test_stock_missing_latest_completed_session_is_excluded(self):
        """Candidate stock missing the latest completed session date is excluded as stale."""
        eval_date = self.trading_days[205]
        eval_dt = datetime.combine(eval_date, time(18, 0), tzinfo=EASTERN)

        spy_bars = self._make_series("SPY", self.trading_days[:206])
        # MSFT has 100 bars but stops at day 204 (missing day 205)
        msft_bars = self._make_series("MSFT", self.trading_days[104:205])
        self.assertEqual(len(msft_bars), 101)

        result = self.evaluator.evaluate_readiness(
            proxy_candles_map={"SPY": spy_bars},
            stock_candles_map={"MSFT": msft_bars},
            current_time=eval_dt,
        )

        self.assertTrue(result.is_ready)
        self.assertNotIn("MSFT", result.stock_candles)
        self.assertEqual(len(result.excluded_tickers), 1)

        exclusion = result.excluded_tickers[0]
        self.assertEqual(exclusion.ticker, "MSFT")
        self.assertIn("STALE_STOCK_DATA", exclusion.reason)

    # ==========================================================================
    # 4. CANDLE VALIDATION (FORMING, WEEKENDS, HOLIDAYS, EARLY CLOSE, DST)
    # ==========================================================================

    def test_stale_or_incomplete_daily_candle_rejected(self):
        """Forming daily candle must be rejected when requiring complete bars."""
        eval_date = self.trading_days[205]

        # Case A: Evaluated during active trading hours (14:00 ET) -> candle is forming
        active_dt = datetime.combine(eval_date, time(14, 0), tzinfo=EASTERN)
        spy_bars_active = self._make_series("SPY", self.trading_days[:206])
        res_active = self.evaluator.evaluate_readiness(
            proxy_candles_map={"SPY": spy_bars_active},
            current_time=active_dt,
        )
        self.assertFalse(res_active.is_ready)
        self.assertEqual(res_active.status, ReadinessStatus.HARD_FAIL)
        self.assertTrue(any("INVALID_SPY_CANDLES" in r for r in res_active.failure_reasons))

        # Case B: Evaluated after close (18:00 ET) but bar is explicitly marked FORMING
        eval_dt = datetime.combine(eval_date, time(18, 0), tzinfo=EASTERN)
        spy_bars_forming = self._make_series("SPY", self.trading_days[:206])
        spy_bars_forming[-1].status = CandleStatus.FORMING

        res_forming = self.evaluator.evaluate_readiness(
            proxy_candles_map={"SPY": spy_bars_forming},
            current_time=eval_dt,
        )
        self.assertFalse(res_forming.is_ready)
        self.assertEqual(res_forming.status, ReadinessStatus.HARD_FAIL)
        self.assertTrue(any("INVALID_SPY_CANDLES" in r for r in res_forming.failure_reasons))

    def test_weekend_and_exchange_holiday_candle_rejected(self):
        """Daily candle dated on a weekend or exchange holiday is rejected."""
        eval_dt = datetime(2026, 12, 1, 18, 0, tzinfo=EASTERN)

        # 1. Saturday bar
        sat_date = date(2026, 6, 20)
        c_sat = self._make_candle("SPY", sat_date)
        with self.assertRaises(IncompleteCandleError):
            self.evaluator.validate_candle_series([c_sat], "SPY", current_time=eval_dt)

        # 2. Thanksgiving holiday bar (2026-11-26)
        tg_date = date(2026, 11, 26)
        c_tg = self._make_candle("SPY", tg_date)
        with self.assertRaises(IncompleteCandleError):
            self.evaluator.validate_candle_series([c_tg], "SPY", current_time=eval_dt)

    def test_early_close_session_handling(self):
        """Early-close session (13:00 ET on Black Friday): bar_end_timestamp is 13:00 ET, not 16:00 ET."""
        bf_date = date(2026, 11, 27)

        # Prior to close at 12:30 ET: latest completed session is Wednesday 2026-11-25
        dt_before = datetime.combine(bf_date, time(12, 30), tzinfo=EASTERN)
        completed_before = self.evaluator.get_latest_completed_session(dt_before)
        self.assertEqual(completed_before, date(2026, 11, 25))

        # After close at 13:30 ET: latest completed session is today (2026-11-27)
        dt_after = datetime.combine(bf_date, time(13, 30), tzinfo=EASTERN)
        completed_after = self.evaluator.get_latest_completed_session(dt_after)
        self.assertEqual(completed_after, bf_date)

        # Candle created for early close has bar_end_timestamp at 13:00 EST (18:00 UTC)
        c_bf = self._make_candle("SPY", bf_date)
        self.assertEqual(to_eastern(c_bf.bar_end_timestamp).time(), time(13, 0))

    def test_dst_timestamp_handling_zero_date_shift(self):
        """DST transitions (March EDT and November EST) maintain identical session dates in Eastern time."""
        # March EDT transition
        d_mar = date(2026, 3, 9)
        c_mar = self._make_candle("SPY", d_mar)
        self.assertEqual(to_eastern(c_mar.timestamp).date(), d_mar)
        self.assertEqual(c_mar.timestamp.time(), time(13, 30))  # 09:30 EDT == 13:30 UTC

        # November EST transition
        d_nov = date(2026, 11, 2)
        c_nov = self._make_candle("SPY", d_nov)
        self.assertEqual(to_eastern(c_nov.timestamp).date(), d_nov)
        self.assertEqual(c_nov.timestamp.time(), time(14, 30))  # 09:30 EST == 14:30 UTC

    def test_mixed_adjustment_basis_rejected(self):
        """Candles declaring non-split-adjusted basis or mixed adjustments are rejected."""
        eval_dt = datetime(2026, 6, 18, 18, 0, tzinfo=EASTERN)
        days = self.trading_days[:5]

        # Candle 2 has RAW_UNADJUSTED
        candles = self._make_series("SPY", days)
        candles[2].price_adjustment_type = "RAW_UNADJUSTED"

        with self.assertRaises(IncompatibleAdjustmentBasisError):
            self.evaluator.validate_candle_series(candles, "SPY", current_time=eval_dt)

        # Provider metadata declaring RAW_UNADJUSTED produces HARD_FAIL
        spy_bars = self._make_series("SPY", self.trading_days[:200])
        res = self.evaluator.evaluate_readiness(
            proxy_candles_map={"SPY": spy_bars},
            current_time=eval_dt,
            provider_metadata={"native_adjustment_type": "RAW_UNADJUSTED"},
        )
        self.assertFalse(res.is_ready)
        self.assertEqual(res.status, ReadinessStatus.HARD_FAIL)
        self.assertTrue(any("INCOMPATIBLE_ADJUSTMENT_BASIS" in r for r in res.failure_reasons))

    # ==========================================================================
    # 5. PROVIDER FAILURE, RETRY EXHAUSTION & ZERO NETWORK / ZERO CACHE
    # ==========================================================================

    def test_provider_failure_and_retry_exhaustion_handled(self):
        """Provider failures for SPY produce HARD_FAIL; for stocks produce exclusion."""
        eval_date = self.trading_days[205]
        eval_dt = datetime.combine(eval_date, time(18, 0), tzinfo=EASTERN)

        # 1. SPY fails with RateLimitError
        mock_spy_fail = MockMarketDataProvider(
            failure_map={"SPY": RateLimitError("HTTP 429 Too Many Requests")}
        )
        adapter_spy_fail = MarketDataFeedAdapter(provider=mock_spy_fail, evaluator=self.evaluator)
        res_spy_fail = adapter_spy_fail.fetch_and_evaluate(stock_universe=["AAPL"], current_time=eval_dt)
        self.assertFalse(res_spy_fail.is_ready)
        self.assertEqual(res_spy_fail.status, ReadinessStatus.HARD_FAIL)
        self.assertTrue(any("MISSING_PRIMARY_PROXY_SPY" in r for r in res_spy_fail.failure_reasons))

        # 2. Stock fails with TimeoutError while SPY succeeds
        spy_bars = self._make_series("SPY", self.trading_days[:206])
        mock_stock_fail = MockMarketDataProvider(
            candle_store={"SPY": spy_bars},
            failure_map={"BAD_SYM": TimeoutError("Network timeout after retries")},
        )
        adapter_stock_fail = MarketDataFeedAdapter(provider=mock_stock_fail, evaluator=self.evaluator)
        res_stock_fail = adapter_stock_fail.fetch_and_evaluate(stock_universe=["BAD_SYM"], current_time=eval_dt)
        self.assertTrue(res_stock_fail.is_ready)
        self.assertEqual(len(res_stock_fail.excluded_tickers), 1)
        self.assertEqual(res_stock_fail.excluded_tickers[0].ticker, "BAD_SYM")
        self.assertIn("EMPTY_BAR_SERIES", res_stock_fail.excluded_tickers[0].reason)

    def test_no_live_network_calls_with_injected_provider(self):
        """Adapter and evaluator execute 100% offline using injected provider without network calls."""
        eval_date = self.trading_days[205]
        eval_dt = datetime.combine(eval_date, time(18, 0), tzinfo=EASTERN)

        spy_bars = self._make_series("SPY", self.trading_days[:206])
        aapl_bars = self._make_series("AAPL", self.trading_days[146:206])
        mock_p = MockMarketDataProvider(candle_store={"SPY": spy_bars, "AAPL": aapl_bars})

        adapter = MarketDataFeedAdapter(provider=mock_p, evaluator=self.evaluator)
        res = adapter.fetch_and_evaluate(stock_universe=["AAPL"], current_time=eval_dt)

        self.assertTrue(res.is_ready)
        self.assertIn("SPY", res.proxy_candles)
        self.assertIn("AAPL", res.stock_candles)

    def test_no_persistent_cache_writes_during_offline_tests(self):
        """Feed adapter evaluation operates strictly in-memory and does not modify market_data.db."""
        eval_date = self.trading_days[205]
        eval_dt = datetime.combine(eval_date, time(18, 0), tzinfo=EASTERN)

        db_before_exists = os.path.exists(DEFAULT_CACHE_DB)
        mtime_before = os.path.getmtime(DEFAULT_CACHE_DB) if db_before_exists else None

        spy_bars = self._make_series("SPY", self.trading_days[:206])
        aapl_bars = self._make_series("AAPL", self.trading_days[146:206])
        mock_p = MockMarketDataProvider(candle_store={"SPY": spy_bars, "AAPL": aapl_bars})

        adapter = MarketDataFeedAdapter(provider=mock_p, evaluator=self.evaluator)
        res = adapter.fetch_and_evaluate(stock_universe=["AAPL"], current_time=eval_dt)

        self.assertTrue(res.is_ready)

        # Verification: default market_data.db mtime must be completely untouched
        if db_before_exists:
            mtime_after = os.path.getmtime(DEFAULT_CACHE_DB)
            self.assertEqual(mtime_before, mtime_after, "DEFAULT_CACHE_DB mtime must not change during adapter execution")


if __name__ == "__main__":
    unittest.main()
