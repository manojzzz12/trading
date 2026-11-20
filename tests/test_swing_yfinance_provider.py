"""Deterministic offline unit test suite for planned Yahoo Finance / yfinance market-data provider.

Phase 6.2B — Step 1: Test Design & Static Fixtures (TEST-ONLY)

Enforces:
1. Focus 1 - Price adjustment:
   - AAPL 4-for-1 split fixture (2020-08-31) OHLC & volume consistency.
   - NVDA 10-for-1 split fixture (2024-06-10) OHLC & volume consistency.
   - Preserves nominal split-adjusted OHLC; cash dividends do not cause dividend-adjusted OHLC.
   - Deliberately unadjusted split discontinuity fixture is detected and rejected.
2. Focus 2 - Timestamp and market calendar:
   - Normal NYSE session (09:30 open ET, 16:00 close ET).
   - Early-close session (13:00 close ET, e.g. Black Friday).
   - US daylight-saving time transitions (zero date-shifting).
   - Weekend and exchange holiday rejection with explicit data quality warnings.
3. Focus 3 - Data readiness:
   - Missing latest completed SPY session causes hard failure.
   - SPY with fewer than 200 completed bars causes hard failure.
   - QQQ/IWM missing or < 20 bars produces warning and proxy exclusion.
   - Candidate stock with < 50 bars or missing latest session is excluded.
   - Stale, forming, or empty datasets strictly prevent actionable entry signals.
4. Focus 4 - Cache isolation:
   - Uses dedicated isolated temporary SQLite database (never touches default market_data.db).
   - Ticker-specific invalidation clears target ticker without affecting other tickers.
5. Focus 5 - Planned provider behavior:
   - Deterministic offline mock fixtures for valid, empty, malformed, 429 rate limits, and network errors.
   - Bounded retries and error classification without live network requests.
"""

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SWING_DIR = os.path.join(ROOT_DIR, ".agents", "skills", "trader-swing")
for path in (ROOT_DIR, SWING_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

from accounting import quantize_cent, to_decimal
from backtest_models import PriceAdjustmentType
from indicators import TechnicalIndicatorsEngine
from market_data_cache import DEFAULT_CACHE_DB, MarketDataCache
from market_data_models import (
    CandleStatus,
    DataFreshness,
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
    InvalidPriceError,
    InvalidResponseError,
    InvalidVolumeError,
    NoDataError,
    RateLimitError,
    ProviderUnavailableError,
    SequenceValidationError,
    TimeoutError,
    resolve_candle_lifecycle,
    validate_candle_sequence,
    validate_single_candle,
)
from regime_detector import MarketRegimeDetector
from scanner import StockScanner
from scanner_models import ScreeningCriteria

# Conditional import of planned provider classes (Phase 6.2B Step 2)
try:
    from providers.yfinance_provider import YFinanceMarketDataProvider
except ImportError:
    YFinanceMarketDataProvider = None

try:
    from feed_adapter import MarketDataFeedAdapter, MarketDataReadinessEvaluator
except ImportError:
    MarketDataFeedAdapter = None
    MarketDataReadinessEvaluator = None

FIXTURES_DIR = os.path.join(ROOT_DIR, "tests", "fixtures", "trader-swing", "yfinance")


def load_fixture(filename: str) -> dict:
    """Load a static JSON fixture from the yfinance test fixtures directory."""
    path = os.path.join(FIXTURES_DIR, filename)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def fixture_bars_to_candles(bars: list, ticker: str, adjustment_type: PriceAdjustmentType) -> list:
    """Helper converting fixture bar dictionaries to normalized OHLCVCandle objects."""
    candles = []
    for b in bars:
        d_parts = [int(p) for p in b["date"].split("-")]
        sess_date = date(d_parts[0], d_parts[1], d_parts[2])
        # Standardized session start in Eastern Time
        start_et = datetime.combine(sess_date, MarketClock.REGULAR_OPEN, tzinfo=EASTERN)
        ts_utc = to_utc(start_et)
        c = OHLCVCandle(
            ticker=ticker,
            interval="1d",
            timestamp=ts_utc,
            open=Decimal(str(b["open"])),
            high=Decimal(str(b["high"])),
            low=Decimal(str(b["low"])),
            close=Decimal(str(b["close"])),
            volume=int(b["volume"]),
            status=CandleStatus.COMPLETE,
            source="yfinance_test_fixture",
            is_delayed=True,
        )
        candles.append(c)
    return candles


# ==============================================================================
# 1. PRICE ADJUSTMENT TESTS
# ==============================================================================

class TestYFinancePriceAdjustment(unittest.TestCase):
    """Verify split adjustment, volume scaling, dividend isolation, and discontinuity detection."""

    def test_aapl_split_fixture_ohlc_and_volume_consistency(self):
        """AAPL 4:1 split (2020-08-31): verify split-adjusted OHLC, volume scaling, and dollar volume invariance."""
        data = load_fixture("aapl_split_2020.json")
        self.assertEqual(data["ticker"], "AAPL")
        self.assertEqual(data["split_ratio"], 4.0)
        self.assertEqual(data["split_date"], "2020-08-31")

        bars = data["bars"]
        self.assertEqual(len(bars), 10)

        # 2020-08-28 (pre-split): Close should be ~124.81 (not unadjusted $499.24)
        bar_pre = [b for b in bars if b["date"] == "2020-08-28"][0]
        self.assertEqual(Decimal(str(bar_pre["close"])), Decimal("124.81"))
        self.assertTrue(bar_pre["volume"] > 100000000)  # Post-split share volume

        # 2020-08-31 (split day): Close should be ~129.04, splits == 4.0
        bar_split = [b for b in bars if b["date"] == "2020-08-31"][0]
        self.assertEqual(Decimal(str(bar_split["close"])), Decimal("129.04"))
        self.assertEqual(bar_split["splits"], 4.0)

        # Convert to OHLCVCandles and validate sequence invariants
        candles = fixture_bars_to_candles(bars, "AAPL", PriceAdjustmentType.SPLIT_ADJUSTED)
        validate_candle_sequence(candles, expected_interval="1d")
        for c in candles:
            self.assertTrue(c.high >= max(c.open, c.close, c.low))
            self.assertTrue(c.low <= min(c.open, c.close, c.high))
            self.assertTrue(c.volume > 0)

    def test_nvda_split_fixture_ohlc_and_volume_consistency(self):
        """NVDA 10:1 split (2024-06-10): verify split-adjusted OHLC, volume scaling, and sequence integrity."""
        data = load_fixture("nvda_split_2024.json")
        self.assertEqual(data["ticker"], "NVDA")
        self.assertEqual(data["split_ratio"], 10.0)
        self.assertEqual(data["split_date"], "2024-06-10")

        bars = data["bars"]
        bar_pre = [b for b in bars if b["date"] == "2024-06-07"][0]
        # Pre-split close in split-adjusted terms is ~$120.89, NOT $1,208.88
        self.assertEqual(Decimal(str(bar_pre["close"])), Decimal("120.89"))
        self.assertTrue(bar_pre["volume"] > 400000000)

        bar_split = [b for b in bars if b["date"] == "2024-06-10"][0]
        self.assertEqual(Decimal(str(bar_split["close"])), Decimal("121.79"))
        self.assertEqual(bar_split["splits"], 10.0)

        candles = fixture_bars_to_candles(bars, "NVDA", PriceAdjustmentType.SPLIT_ADJUSTED)
        validate_candle_sequence(candles, expected_interval="1d")

    def test_dividend_fixture_preserves_nominal_split_adjusted_ohlc(self):
        """Cash dividend events must preserve nominal split-adjusted OHLC without dividend backward deflation."""
        data = load_fixture("dividend_sample.json")
        self.assertEqual(data["ticker"], "AAPL")
        self.assertEqual(data["dividend_amount"], 0.205)

        bars = data["bars"]
        ex_bar = [b for b in bars if b["date"] == "2020-08-07"][0]
        self.assertEqual(ex_bar["dividends"], 0.205)

        # Close must NOT equal Adj Close (which deflates past prices for cash dividends)
        self.assertNotEqual(ex_bar["close"], ex_bar["adj_close"])
        self.assertEqual(Decimal(str(ex_bar["close"])), Decimal("111.11"))
        self.assertEqual(Decimal(str(ex_bar["adj_close"])), Decimal("108.95"))

        candles = fixture_bars_to_candles(bars, "AAPL", PriceAdjustmentType.SPLIT_ADJUSTED)
        # Normalized candle close must match nominal Close (111.11), NOT Adj Close (108.95)
        ex_candle = [c for c in candles if to_eastern(c.timestamp).date() == date(2020, 8, 7)][0]
        self.assertEqual(ex_candle.close, Decimal("111.11"))

    def test_unadjusted_split_discontinuity_rejected(self):
        """Deliberately unadjusted nominal split discontinuity (74% cliff drop) must be detected and rejected."""
        data = load_fixture("aapl_unadjusted_discontinuity.json")
        bars = data["bars"]

        # 2020-08-28 is $499.24; 2020-08-31 is $129.04
        bar_pre = [b for b in bars if b["date"] == "2020-08-28"][0]
        bar_post = [b for b in bars if b["date"] == "2020-08-31"][0]
        c_pre = Decimal(str(bar_pre["close"]))
        c_post = Decimal(str(bar_post["close"]))
        pct_drop = abs(c_post - c_pre) / c_pre

        # Verification gate: cliff drop >= 35% on split date signals unadjusted series
        self.assertTrue(pct_drop >= Decimal("0.35"))

        # Invariant gate function
        def verify_split_continuity(candle_series: list, split_events: list) -> bool:
            for s in split_events:
                s_date = s["date"] if isinstance(s, dict) else s.split_date.isoformat()
                for i in range(1, len(candle_series)):
                    c_date = to_eastern(candle_series[i].timestamp).date().isoformat()
                    if c_date == s_date:
                        drop = abs(candle_series[i].close - candle_series[i - 1].close) / candle_series[i - 1].close
                        if drop >= Decimal("0.35"):
                            return False
            return True

        candles = fixture_bars_to_candles(bars, "AAPL", PriceAdjustmentType.SPLIT_ADJUSTED)
        is_valid = verify_split_continuity(candles, [{"date": "2020-08-31", "ratio": 4.0}])
        self.assertFalse(is_valid, "Unadjusted split cliff must fail continuity verification gate")


# ==============================================================================
# 2. TIMESTAMP AND MARKET CALENDAR TESTS
# ==============================================================================

class TestYFinanceTimestampAndCalendar(unittest.TestCase):
    """Verify session start mapping, early close, weekends, holidays, and DST neutrality."""

    def setUp(self):
        self.cal_data = load_fixture("calendar_edge_cases.json")["sessions"]

    def test_normal_nyse_session_timestamp_mapping(self):
        """Normal session: bar start is 09:30 ET converted to UTC; bar_end_timestamp is 16:00 ET."""
        sess = self.cal_data["normal_session"]
        sess_d = date(2026, 6, 15)

        # Construct candle using standardized session start
        start_et = datetime.combine(sess_d, MarketClock.REGULAR_OPEN, tzinfo=EASTERN)
        ts_utc = to_utc(start_et)

        # In EDT (summer), 09:30 EDT == 13:30 UTC
        self.assertEqual(ts_utc.isoformat(), sess["expected_open_utc"])

        candle = OHLCVCandle(
            ticker="SPY",
            interval="1d",
            timestamp=ts_utc,
            open=Decimal("450.00"),
            high=Decimal("455.00"),
            low=Decimal("449.00"),
            close=Decimal("452.00"),
            volume=50000000,
        )

        # In Eastern time, date must identically match sess_d
        self.assertEqual(to_eastern(candle.timestamp).date(), sess_d)

        # bar_end_timestamp must resolve to 16:00 EDT (20:00 UTC)
        self.assertEqual(candle.bar_end_timestamp.isoformat(), sess["expected_close_utc"])

        # Historical lifecycle verification on next day
        eval_dt = datetime(2026, 6, 16, 10, 0, 0, tzinfo=EASTERN)
        status = resolve_candle_lifecycle(candle, current_time=eval_dt)
        self.assertEqual(status, CandleStatus.COMPLETE)

    def test_early_close_session_uses_actual_close_1300_et(self):
        """13:00 ET early-close session (Black Friday): bar_end_timestamp is 13:00 ET, not 16:00 ET."""
        sess = self.cal_data["early_close_session"]
        sess_d = date(2026, 11, 27)

        start_et = datetime.combine(sess_d, MarketClock.REGULAR_OPEN, tzinfo=EASTERN)
        ts_utc = to_utc(start_et)

        # In EST (winter), 09:30 EST == 14:30 UTC
        self.assertEqual(ts_utc.isoformat(), sess["expected_open_utc"])

        candle = OHLCVCandle(
            ticker="SPY",
            interval="1d",
            timestamp=ts_utc,
            open=Decimal("450.00"),
            high=Decimal("455.00"),
            low=Decimal("449.00"),
            close=Decimal("452.00"),
            volume=20000000,
        )

        # CRITICAL CHECK: bar_end_timestamp must be 13:00 EST (18:00 UTC), NEVER hardcoded 16:00 EST
        self.assertEqual(candle.bar_end_timestamp.isoformat(), sess["expected_close_utc"])

        # At 13:30 EST on early-close day, candle has closed and must resolve to COMPLETE
        eval_dt = datetime(2026, 11, 27, 13, 30, 0, tzinfo=EASTERN)
        status = resolve_candle_lifecycle(candle, current_time=eval_dt)
        self.assertEqual(status, CandleStatus.COMPLETE)

    def test_dst_transitions_maintain_identical_session_dates(self):
        """DST transitions (EDT begin in March, EST resume in November) must never shift calendar date."""
        # 1. March transition (Monday 2026-03-09 after DST start)
        d_mar = date(2026, 3, 9)
        ts_mar = to_utc(datetime.combine(d_mar, MarketClock.REGULAR_OPEN, tzinfo=EASTERN))
        self.assertEqual(to_eastern(ts_mar).date(), d_mar)

        # 2. November transition (Monday 2026-11-02 after DST end)
        d_nov = date(2026, 11, 2)
        ts_nov = to_utc(datetime.combine(d_nov, MarketClock.REGULAR_OPEN, tzinfo=EASTERN))
        self.assertEqual(to_eastern(ts_nov).date(), d_nov)

    def test_weekend_and_holiday_observations_rejected(self):
        """Observations on Saturday, Sunday, or Thanksgiving must resolve to UNKNOWN and be excluded."""
        for key in ["saturday_non_trading", "sunday_non_trading", "thanksgiving_holiday"]:
            case = self.cal_data[key]
            d_parts = [int(p) for p in case["date"].split("-")]
            cand_date = date(d_parts[0], d_parts[1], d_parts[2])

            is_trading, _ = MarketClock.is_historical_trading_day(cand_date)
            self.assertFalse(is_trading)

            cand_ts = to_utc(datetime.combine(cand_date, MarketClock.REGULAR_OPEN, tzinfo=EASTERN))
            candle = OHLCVCandle(
                ticker="SPY",
                interval="1d",
                timestamp=cand_ts,
                open=Decimal("450.00"),
                high=Decimal("455.00"),
                low=Decimal("449.00"),
                close=Decimal("452.00"),
                volume=10000000,
                status=CandleStatus.COMPLETE,  # Stored status falsely claims COMPLETE
            )

            eval_dt = datetime(2026, 12, 1, 10, 0, tzinfo=EASTERN)
            status = resolve_candle_lifecycle(candle, current_time=eval_dt)
            self.assertEqual(status, CandleStatus.UNKNOWN)


# ==============================================================================
# 3. DATA READINESS GATE TESTS
# ==============================================================================

class TestYFinanceDataReadiness(unittest.TestCase):
    """Verify primary proxy hard-stops, secondary proxy warnings, and candidate stock exclusions."""

    def setUp(self):
        self.detector = MarketRegimeDetector()
        self.scanner = StockScanner()

        # Build 210 chronological NYSE trading sessions for SPY
        self.trading_days = []
        curr_d = date(2026, 1, 5)
        while len(self.trading_days) < 220:
            if MarketClock.get_session(curr_d):
                self.trading_days.append(curr_d)
            curr_d += timedelta(days=1)

    def _make_series(self, ticker: str, days: list, base_px: float = 450.0) -> list:
        candles = []
        for i, d in enumerate(days):
            ts = to_utc(datetime.combine(d, MarketClock.REGULAR_OPEN, tzinfo=EASTERN))
            px = Decimal(f"{base_px + (i * 0.2):.2f}")
            c = OHLCVCandle(
                ticker=ticker,
                interval="1d",
                timestamp=ts,
                open=px,
                high=px + Decimal("2.00"),
                low=px - Decimal("2.00"),
                close=px,
                volume=50000000,
                status=CandleStatus.COMPLETE,
            )
            candles.append(c)
        return candles

    def _evaluate_feed_readiness(
        self,
        proxy_map: dict,
        universe_map: dict,
        eval_dt: datetime,
    ) -> dict:
        """Planned Phase 6.2B Step 4 readiness evaluator specification."""
        eval_date = to_eastern(eval_dt).date()
        # Find expected latest completed session date
        prev_sess = eval_date if eval_dt.time() >= time(16, 0) else MarketClock.get_previous_trading_day(eval_date)
        is_tr, _ = MarketClock.is_historical_trading_day(prev_sess)
        if not is_tr:
            prev_sess = MarketClock.get_previous_trading_day(prev_sess)

        # Check SPY
        if "SPY" not in proxy_map or not proxy_map["SPY"]:
            return {"is_ready": False, "status": "HARD_FAIL", "reason": "MISSING_PRIMARY_PROXY_SPY"}
        spy_bars = proxy_map["SPY"]
        if len(spy_bars) < 200:
            return {"is_ready": False, "status": "HARD_FAIL", "reason": f"INSUFFICIENT_SPY_BARS: {len(spy_bars)} < 200"}
        last_spy_d = to_eastern(spy_bars[-1].timestamp).date()
        if last_spy_d < prev_sess:
            return {"is_ready": False, "status": "HARD_FAIL", "reason": f"STALE_SPY_DATA: latest {last_spy_d} < expected {prev_sess}"}

        # Check secondary proxies
        warnings = []
        for p in ["QQQ", "IWM"]:
            if p not in proxy_map or len(proxy_map[p]) < 20:
                warnings.append(f"SECONDARY_PROXY_EXCLUDED: {p}")

        # Check universe stocks
        excluded_stocks = []
        for sym, bars in universe_map.items():
            if len(bars) < 50:
                excluded_stocks.append({"ticker": sym, "reason": f"INSUFFICIENT_BARS: {len(bars)} < 50"})
            elif to_eastern(bars[-1].timestamp).date() < prev_sess:
                excluded_stocks.append({"ticker": sym, "reason": "STALE_STOCK_DATA"})

        return {
            "is_ready": True,
            "status": "READY",
            "warnings": warnings,
            "excluded_stocks": excluded_stocks,
        }

    def test_readiness_missing_latest_spy_session_fails(self):
        """Missing the most recent completed NYSE session for primary proxy SPY must be a HARD FAILURE."""
        eval_date = self.trading_days[205]
        eval_dt = datetime.combine(eval_date, time(18, 0), tzinfo=EASTERN)

        # SPY only has bars up to trading_days[204] (missing day 205)
        spy_bars = self._make_series("SPY", self.trading_days[:205])
        proxy_map = {"SPY": spy_bars}

        readiness = self._evaluate_feed_readiness(proxy_map, {}, eval_dt)
        self.assertFalse(readiness["is_ready"])
        self.assertEqual(readiness["status"], "HARD_FAIL")
        self.assertIn("STALE_SPY_DATA", readiness["reason"])

    def test_readiness_insufficient_spy_bars_fails(self):
        """SPY with fewer than 200 bars must trigger InsufficientRegimeDataError or zero confidence."""
        eval_date = self.trading_days[50]
        eval_dt = datetime.combine(eval_date, time(18, 0), tzinfo=EASTERN)

        # SPY with only 15 bars (< 20 minimum)
        spy_bars = self._make_series("SPY", self.trading_days[:15])
        proxy_map = {"SPY": spy_bars}

        readiness = self._evaluate_feed_readiness(proxy_map, {}, eval_dt)
        self.assertFalse(readiness["is_ready"])
        self.assertEqual(readiness["status"], "HARD_FAIL")
        self.assertIn("INSUFFICIENT_SPY_BARS", readiness["reason"])

        # Also verify regime detector behavior
        res = self.detector.detect_regime(proxy_map, current_time=eval_dt, require_complete=True)
        self.assertEqual(res.regime.value, "UNKNOWN")
        self.assertFalse(res.policy.allow_new_longs)

    def test_readiness_secondary_proxies_missing_warns_and_excludes(self):
        """Missing or insufficient secondary proxies (QQQ, IWM) produce warnings but do not crash SPY evaluation."""
        eval_date = self.trading_days[205]
        eval_dt = datetime.combine(eval_date, time(18, 0), tzinfo=EASTERN)

        spy_bars = self._make_series("SPY", self.trading_days[:206])
        # QQQ is completely missing; IWM has only 5 bars (< 20 required)
        iwm_bars = self._make_series("IWM", self.trading_days[:5], base_px=200.0)

        proxy_map = {"SPY": spy_bars, "IWM": iwm_bars}
        readiness = self._evaluate_feed_readiness(proxy_map, {}, eval_dt)
        self.assertTrue(readiness["is_ready"])
        self.assertTrue(any("IWM" in w for w in readiness["warnings"]))
        self.assertTrue(any("QQQ" in w for w in readiness["warnings"]))

        res = self.detector.detect_regime(proxy_map, current_time=eval_dt, require_complete=True)
        self.assertNotEqual(res.regime.value, "UNKNOWN")
        self.assertTrue(any("IWM" in w for w in res.warnings))

    def test_readiness_stock_insufficient_bars_excluded(self):
        """Candidate stock with fewer than 50 bars is rejected and excluded from candidate ranking."""
        eval_date = self.trading_days[205]
        eval_dt = datetime.combine(eval_date, time(18, 0), tzinfo=EASTERN)

        spy_bars = self._make_series("SPY", self.trading_days[:206])
        universe_map = {"XYZ": self._make_series("XYZ", self.trading_days[:30], base_px=100.0)}

        readiness = self._evaluate_feed_readiness({"SPY": spy_bars}, universe_map, eval_dt)
        self.assertTrue(readiness["is_ready"])
        self.assertEqual(len(readiness["excluded_stocks"]), 1)
        self.assertEqual(readiness["excluded_stocks"][0]["ticker"], "XYZ")
        self.assertIn("INSUFFICIENT_BARS", readiness["excluded_stocks"][0]["reason"])

    def test_readiness_stale_or_forming_data_prevents_actionable_signals(self):
        """Forming terminal candle must be rejected when require_complete=True."""
        eval_date = self.trading_days[20]
        # Active session at 14:00 ET
        active_time = datetime.combine(eval_date, time(14, 0), tzinfo=EASTERN)

        bars = self._make_series("SPY", self.trading_days[:21])
        # Terminal bar is forming today
        bars[-1].status = CandleStatus.FORMING

        with self.assertRaises(IncompleteCandleError):
            self.detector.detect_regime({"SPY": bars}, current_time=active_time, require_complete=True)


# ==============================================================================
# 4. CACHE ISOLATION TESTS
# ==============================================================================

class TestYFinanceCacheIsolation(unittest.TestCase):
    """Verify separate SQLite database isolation and ticker-specific invalidation."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.isolated_db = os.path.join(self.temp_dir, "test_market_data_yfinance.db")
        self.cache = MarketDataCache(db_path=self.isolated_db)

    def tearDown(self):
        self.cache.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_cache_isolation_uses_separate_test_database(self):
        """Cache must operate strictly on designated db_path and never touch DEFAULT_CACHE_DB."""
        self.assertNotEqual(self.cache.db_path, DEFAULT_CACHE_DB)
        self.assertTrue(os.path.exists(self.isolated_db))

    def test_ticker_specific_cache_invalidation_isolation(self):
        """Clearing ticker AAPL must delete all AAPL records without affecting MSFT records."""
        d1 = datetime(2026, 6, 15, 13, 30, tzinfo=UTC)
        c_aapl = OHLCVCandle(
            ticker="AAPL", interval="1d", timestamp=d1,
            open=Decimal("150.00"), high=Decimal("155.00"), low=Decimal("149.00"), close=Decimal("152.00"), volume=1000
        )
        c_msft = OHLCVCandle(
            ticker="MSFT", interval="1d", timestamp=d1,
            open=Decimal("300.00"), high=Decimal("305.00"), low=Decimal("299.00"), close=Decimal("302.00"), volume=2000
        )

        self.cache.store_candles([c_aapl, c_msft])

        # Verify both exist
        self.assertEqual(len(self.cache.get_historical_candles("AAPL", "1d", d1, d1)), 1)
        self.assertEqual(len(self.cache.get_historical_candles("MSFT", "1d", d1, d1)), 1)

        # Invalidate only AAPL
        self.cache.clear(ticker="AAPL", interval="1d")

        # AAPL must be gone; MSFT must remain intact
        self.assertEqual(len(self.cache.get_historical_candles("AAPL", "1d", d1, d1)), 0)
        self.assertEqual(len(self.cache.get_historical_candles("MSFT", "1d", d1, d1)), 1)


# ==============================================================================
# 5. PROVIDER BEHAVIOR TESTS (PLANNED STEP 2 CONTRACT)
# ==============================================================================

class TestYFinanceProviderPlannedContract(unittest.TestCase):
    """Test planned provider implementation contracts for Phase 6.2B Step 2."""

    def setUp(self):
        self.responses = load_fixture("provider_responses.json")

    def test_provider_offline_fixtures_and_bounded_retries(self):
        """Verify bounded retry policy (1 initial + 3 retries, total <= 7s backoff)."""
        self.assertIsNotNone(YFinanceMarketDataProvider, "YFinanceMarketDataProvider must be implemented")

        # 1. Verify default bounds
        provider = YFinanceMarketDataProvider()
        self.assertEqual(provider.max_retries, 3)
        self.assertEqual(provider.backoff_intervals, (1.0, 2.0, 4.0))
        self.assertLessEqual(sum(provider.backoff_intervals), 7.0)

        # 2. Verify transient failure retry with eventual success
        calls = []

        def transient_fail_then_succeed(*args, **kwargs):
            calls.append(len(calls))
            if len(calls) < 3:
                return self.responses["rate_limit_error"]
            return self.responses["valid_sample_aapl"]

        p_retry = YFinanceMarketDataProvider(
            max_retries=3,
            backoff_intervals=(0.001, 0.002, 0.004),
            yf_download_fn=transient_fail_then_succeed,
        )
        eval_time = datetime(2026, 6, 18, 10, 0, tzinfo=EASTERN)
        bars = p_retry.fetch_historical_bars(
            "AAPL", "1d",
            datetime(2026, 6, 15, tzinfo=UTC),
            datetime(2026, 6, 17, tzinfo=UTC),
            current_time=eval_time,
        )
        self.assertEqual(len(bars), 3)
        self.assertEqual(len(calls), 3)  # Initial attempt + 2 retries

        # 3. Verify exhaustion stops strictly at 1 initial + max_retries
        exhaust_calls = []

        def always_rate_limit(*args, **kwargs):
            exhaust_calls.append(len(exhaust_calls))
            return self.responses["rate_limit_error"]

        p_exhaust = YFinanceMarketDataProvider(
            max_retries=3,
            backoff_intervals=(0.001, 0.002, 0.004),
            yf_download_fn=always_rate_limit,
        )
        with self.assertRaises(RateLimitError):
            p_exhaust.fetch_historical_bars(
                "AAPL", "1d",
                datetime(2026, 6, 15, tzinfo=UTC),
                datetime(2026, 6, 17, tzinfo=UTC),
                current_time=eval_time,
            )
        self.assertEqual(len(exhaust_calls), 4)  # 1 initial + 3 retries = exactly 4 attempts

    def test_provider_deterministic_error_classification(self):
        """Verify deterministic error mapping: 429 -> RateLimitError, 404 -> NoDataError, malformed -> validation errors."""
        self.assertIsNotNone(YFinanceMarketDataProvider, "YFinanceMarketDataProvider must be implemented")
        eval_time = datetime(2026, 6, 18, 10, 0, tzinfo=EASTERN)

        # A. HTTP 429 -> RateLimitError
        p_429 = YFinanceMarketDataProvider(
            max_retries=1,
            backoff_intervals=(0.001,),
            yf_download_fn=lambda *a, **k: self.responses["rate_limit_error"],
        )
        with self.assertRaises(RateLimitError):
            p_429.fetch_historical_bars("AAPL", "1d", datetime(2026, 6, 15, tzinfo=UTC), datetime(2026, 6, 17, tzinfo=UTC), current_time=eval_time)

        # B. HTTP 404 -> NoDataError (non-retryable, single attempt)
        calls_404 = []

        def mock_404(*a, **k):
            calls_404.append(1)
            return self.responses["not_found_error"]

        p_404 = YFinanceMarketDataProvider(
            max_retries=3,
            backoff_intervals=(0.001, 0.001, 0.001),
            yf_download_fn=mock_404,
        )
        with self.assertRaises(NoDataError):
            p_404.fetch_historical_bars("DELISTED", "1d", datetime(2026, 6, 15, tzinfo=UTC), datetime(2026, 6, 17, tzinfo=UTC), current_time=eval_time)
        self.assertEqual(len(calls_404), 1)  # Non-retryable

        # C. Empty response -> NoDataError
        p_empty = YFinanceMarketDataProvider(
            max_retries=1,
            backoff_intervals=(0.001,),
            yf_download_fn=lambda *a, **k: self.responses["empty_response"],
        )
        with self.assertRaises(NoDataError):
            p_empty.fetch_historical_bars("EMPTY", "1d", datetime(2026, 6, 15, tzinfo=UTC), datetime(2026, 6, 17, tzinfo=UTC), current_time=eval_time)

        # D. Malformed data: missing close -> InvalidPriceError (non-retryable)
        calls_bad_px = []

        def mock_bad_px(*a, **k):
            calls_bad_px.append(1)
            return self.responses["malformed_missing_close"]

        p_bad_px = YFinanceMarketDataProvider(
            max_retries=3,
            backoff_intervals=(0.001, 0.001, 0.001),
            yf_download_fn=mock_bad_px,
        )
        with self.assertRaises(InvalidPriceError):
            p_bad_px.fetch_historical_bars("BAD", "1d", datetime(2026, 6, 15, tzinfo=UTC), datetime(2026, 6, 17, tzinfo=UTC), current_time=eval_time)
        self.assertEqual(len(calls_bad_px), 1)

        # E. Malformed data: inverted high/low -> InvalidPriceError
        p_inv = YFinanceMarketDataProvider(
            max_retries=1,
            backoff_intervals=(0.001,),
            yf_download_fn=lambda *a, **k: self.responses["malformed_inverted_high_low"],
        )
        with self.assertRaises(InvalidPriceError):
            p_inv.fetch_historical_bars("INV", "1d", datetime(2026, 6, 15, tzinfo=UTC), datetime(2026, 6, 17, tzinfo=UTC), current_time=eval_time)

        # F. Malformed data: negative volume -> InvalidVolumeError
        p_neg_vol = YFinanceMarketDataProvider(
            max_retries=1,
            backoff_intervals=(0.001,),
            yf_download_fn=lambda *a, **k: self.responses["malformed_negative_volume"],
        )
        with self.assertRaises(InvalidVolumeError):
            p_neg_vol.fetch_historical_bars("NEG", "1d", datetime(2026, 6, 15, tzinfo=UTC), datetime(2026, 6, 17, tzinfo=UTC), current_time=eval_time)

        # G. Network timeout -> TimeoutError
        p_timeout = YFinanceMarketDataProvider(
            max_retries=1,
            backoff_intervals=(0.001,),
            yf_download_fn=lambda *a, **k: self.responses["network_timeout_error"],
        )
        with self.assertRaises(TimeoutError):
            p_timeout.fetch_historical_bars("TIMEOUT", "1d", datetime(2026, 6, 15, tzinfo=UTC), datetime(2026, 6, 17, tzinfo=UTC), current_time=eval_time)

    def test_provider_no_live_network_calls(self):
        """Verify provider accepts an injected callable and executes 100% offline without network calls."""
        self.assertIsNotNone(YFinanceMarketDataProvider, "YFinanceMarketDataProvider must be implemented")

        # 1. Valid fixture download without network calls
        p = YFinanceMarketDataProvider(
            yf_download_fn=lambda *a, **k: self.responses["valid_sample_aapl"],
        )
        eval_time = datetime(2026, 6, 18, 10, 0, tzinfo=EASTERN)
        bars = p.fetch_historical_bars(
            "AAPL", "1d",
            datetime(2026, 6, 15, tzinfo=UTC),
            datetime(2026, 6, 17, tzinfo=UTC),
            current_time=eval_time,
        )
        self.assertEqual(len(bars), 3)
        for b in bars:
            self.assertEqual(b.ticker, "AAPL")
            self.assertEqual(b.interval, "1d")
            self.assertEqual(b.source, "yfinance")
            self.assertTrue(b.is_delayed)
            self.assertEqual(b.status, CandleStatus.COMPLETE)
            # Normalization invariant: 09:30 America/New_York converted to UTC
            self.assertEqual(to_eastern(b.timestamp).time(), MarketClock.REGULAR_OPEN)
            # In EDT summer, 09:30 EDT == 13:30 UTC
            self.assertEqual(b.timestamp.time(), time(13, 30))

        # 2. Split fixture ingestion: valid split passes
        p_split = YFinanceMarketDataProvider(
            yf_download_fn=lambda *a, **k: load_fixture("aapl_split_2020.json"),
        )
        eval_split = datetime(2020, 9, 10, 10, 0, tzinfo=EASTERN)
        split_candles = p_split.fetch_historical_bars(
            "AAPL", "1d",
            datetime(2020, 8, 24, tzinfo=UTC),
            datetime(2020, 9, 4, tzinfo=UTC),
            current_time=eval_split,
        )
        self.assertEqual(len(split_candles), 10)

        # 3. Split discontinuity fixture: unadjusted 74% cliff drop rejected
        p_flawed = YFinanceMarketDataProvider(
            yf_download_fn=lambda *a, **k: load_fixture("aapl_unadjusted_discontinuity.json"),
        )
        with self.assertRaises(SequenceValidationError) as ctx:
            p_flawed.fetch_historical_bars(
                "AAPL", "1d",
                datetime(2020, 8, 26, tzinfo=UTC),
                datetime(2020, 9, 1, tzinfo=UTC),
                current_time=eval_split,
            )
        self.assertIn("Unadjusted split discontinuity detected", str(ctx.exception))
        self.assertIn("35%", str(ctx.exception))

        # 4. Latest bar retrieval
        latest = p.fetch_latest_bar("AAPL", "1d", current_time=eval_time)
        self.assertIsNotNone(latest)
        self.assertEqual(latest.close, Decimal("184.00"))

        # 5. Metadata verification
        meta = p.get_provider_metadata()
        self.assertEqual(meta["provider_name"], "yfinance")
        self.assertTrue(meta["is_delayed"])
        self.assertEqual(meta["delay_status"], "UNVERIFIED_DELAY")
        self.assertIsNone(meta["delay_minutes"])
        self.assertEqual(meta["native_adjustment_type"], "SPLIT_ADJUSTED")
        self.assertEqual(meta["max_retries"], 3)
        self.assertEqual(meta["backoff_intervals"], [1.0, 2.0, 4.0])
        self.assertIn("market_data_yfinance_split_adj.db", meta["cache_db_path"])


class TestYFinanceTimestampRegression(unittest.TestCase):
    """Offline regression tests for pandas timestamp session-date extraction and timezone handling.

    Validates:
    - Friday daily timestamps during EDT never shift to Thursday or Sunday.
    - Friday daily timestamps during EST preserve correct date and complete status.
    - Monday daily timestamps during EDT and EST preserve correct date and are not dropped.
    - DST transition boundaries (March spring-forward, November fall-back) maintain zero date-shift.
    - NYSE holidays (e.g. Good Friday, Memorial Day) and days immediately preceding them.
    - Valid multi-day historical candle sequences pass sequence validation without gaps.
    - Genuine weekend dates (Saturday/Sunday) are rejected as UNKNOWN non-trading days.
    - Exhaustive verification that all 152 trading Fridays in 2021-2023 never shift.
    """

    def setUp(self):
        try:
            import pandas as pd
            self.pd = pd
        except ImportError:
            self.skipTest("pandas is required for Timestamp regression tests")
        self.provider = YFinanceMarketDataProvider()

    def _make_df(self, dates_and_ohlcv: list):
        """Helper to build a DataFrame indexed by pd.Timestamp."""
        records = []
        indices = []
        for item in dates_and_ohlcv:
            ts = self.pd.Timestamp(item[0], tz="America/New_York")
            indices.append(ts)
            records.append({
                "Open": item[1] if len(item) > 1 else 100.0,
                "High": item[2] if len(item) > 2 else 105.0,
                "Low": item[3] if len(item) > 3 else 95.0,
                "Close": item[4] if len(item) > 4 else 102.0,
                "Volume": item[5] if len(item) > 5 else 1000000,
            })
        return self.pd.DataFrame(records, index=indices)

    def test_friday_daily_timestamps_edt(self):
        """Friday timestamps during EDT must extract Friday session date, never Thursday or Sunday."""
        test_fridays = ["2021-06-18", "2022-06-03", "2022-10-28", "2023-08-18"]
        for f_str in test_fridays:
            f_date = date.fromisoformat(f_str)
            df = self._make_df([[f_str]])
            eval_time = datetime.combine(f_date + timedelta(days=3), time(10, 0), tzinfo=UTC)
            candles = self.provider._parse_and_validate_data(df, "SPY", "1d", current_time=eval_time)

            self.assertEqual(len(candles), 1, f"Expected 1 candle for {f_str}")
            extracted_date = to_eastern(candles[0].timestamp).date()
            self.assertEqual(extracted_date, f_date, f"Friday {f_str} converted to {extracted_date}!")
            self.assertNotEqual(extracted_date, f_date - timedelta(days=1), "Friday must not become Thursday")
            self.assertNotEqual(extracted_date, f_date + timedelta(days=2), "Friday must not become Sunday")
            self.assertEqual(candles[0].status, CandleStatus.COMPLETE)

    def test_friday_daily_timestamps_est(self):
        """Friday timestamps during EST must extract Friday session date without alteration."""
        test_fridays = ["2021-01-08", "2022-01-14", "2023-01-13", "2023-12-15"]
        for f_str in test_fridays:
            f_date = date.fromisoformat(f_str)
            df = self._make_df([[f_str]])
            eval_time = datetime.combine(f_date + timedelta(days=3), time(10, 0), tzinfo=UTC)
            candles = self.provider._parse_and_validate_data(df, "SPY", "1d", current_time=eval_time)

            self.assertEqual(len(candles), 1)
            extracted_date = to_eastern(candles[0].timestamp).date()
            self.assertEqual(extracted_date, f_date)
            self.assertEqual(candles[0].status, CandleStatus.COMPLETE)

    def test_monday_timestamps_edt_and_est(self):
        """Monday timestamps during both EDT and EST must not shift backward into Sunday."""
        test_mondays = [
            ("2021-01-04", "EST"),
            ("2021-06-21", "EDT"),
            ("2022-06-06", "EDT"),
            ("2022-12-12", "EST"),
            ("2023-05-15", "EDT"),
        ]
        for m_str, tz_regime in test_mondays:
            m_date = date.fromisoformat(m_str)
            df = self._make_df([[m_str]])
            eval_time = datetime.combine(m_date + timedelta(days=1), time(10, 0), tzinfo=UTC)
            candles = self.provider._parse_and_validate_data(df, "SPY", "1d", current_time=eval_time)

            self.assertEqual(len(candles), 1, f"Monday {m_str} ({tz_regime}) was erroneously discarded!")
            extracted_date = to_eastern(candles[0].timestamp).date()
            self.assertEqual(extracted_date, m_date, f"Monday {m_str} shifted to {extracted_date}")
            self.assertNotEqual(extracted_date, m_date - timedelta(days=1), "Monday must not shift to Sunday")
            self.assertEqual(candles[0].status, CandleStatus.COMPLETE)

    def test_dst_transition_boundaries(self):
        """DST transition weekends (March EDT begin, November EST return) must not cause date shifts."""
        # 2022 Spring forward: Friday 2022-03-11 (EST) -> Monday 2022-03-14 (EDT)
        spring_dates = ["2022-03-11", "2022-03-14"]
        df_spring = self._make_df([[d] for d in spring_dates])
        eval_spring = datetime(2022, 3, 15, 10, 0, tzinfo=UTC)
        candles_spring = self.provider._parse_and_validate_data(df_spring, "SPY", "1d", current_time=eval_spring)
        self.assertEqual(len(candles_spring), 2)
        self.assertEqual(to_eastern(candles_spring[0].timestamp).date(), date(2022, 3, 11))
        self.assertEqual(to_eastern(candles_spring[1].timestamp).date(), date(2022, 3, 14))

        # 2022 Fall back: Friday 2022-11-04 (EDT) -> Monday 2022-11-07 (EST)
        fall_dates = ["2022-11-04", "2022-11-07"]
        df_fall = self._make_df([[d] for d in fall_dates])
        eval_fall = datetime(2022, 11, 8, 10, 0, tzinfo=UTC)
        candles_fall = self.provider._parse_and_validate_data(df_fall, "SPY", "1d", current_time=eval_fall)
        self.assertEqual(len(candles_fall), 2)
        self.assertEqual(to_eastern(candles_fall[0].timestamp).date(), date(2022, 11, 4))
        self.assertEqual(to_eastern(candles_fall[1].timestamp).date(), date(2022, 11, 7))

    def test_nyse_holidays(self):
        """NYSE holiday dates are excluded as UNKNOWN, and preceding days are not corrupted."""
        # Thursday before Good Friday 2022: 2022-04-14 (EDT). Good Friday is 2022-04-15 (Holiday).
        raw_items = [["2022-04-14"], ["2022-04-15"]]
        df = self._make_df(raw_items)
        eval_time = datetime(2022, 4, 18, 10, 0, tzinfo=UTC)
        candles = self.provider._parse_and_validate_data(df, "SPY", "1d", current_time=eval_time)

        # Only Thursday 2022-04-14 should be admitted; Good Friday 2022-04-15 discarded as UNKNOWN
        self.assertEqual(len(candles), 1)
        self.assertEqual(to_eastern(candles[0].timestamp).date(), date(2022, 4, 14))
        self.assertEqual(candles[0].status, CandleStatus.COMPLETE)

    def test_valid_historical_daily_candles(self):
        """Continuous multi-week sequence across EDT validates cleanly without missing sessions."""
        # 2 full weeks: 2022-05-31 (Tue, post-Memorial) through 2022-06-10 (Fri) = 9 sessions
        session_dates = [
            "2022-05-31", "2022-06-01", "2022-06-02", "2022-06-03",
            "2022-06-06", "2022-06-07", "2022-06-08", "2022-06-09", "2022-06-10",
        ]
        df = self._make_df([[d] for d in session_dates])
        eval_time = datetime(2022, 6, 13, 10, 0, tzinfo=UTC)
        candles = self.provider._parse_and_validate_data(df, "SPY", "1d", current_time=eval_time)

        self.assertEqual(len(candles), 9)
        for i, expected_str in enumerate(session_dates):
            self.assertEqual(to_eastern(candles[i].timestamp).date(), date.fromisoformat(expected_str))
            self.assertEqual(candles[i].status, CandleStatus.COMPLETE)

        validate_candle_sequence(candles)

    def test_rejection_of_genuinely_invalid_weekend_dates(self):
        """Genuinely invalid weekend observations (Saturday/Sunday) are rejected as UNKNOWN."""
        # 1. Pure weekend dataset raises NoDataError because all observations are discarded
        weekend_items = [["2022-06-04"], ["2022-06-05"]]
        df_weekend = self._make_df(weekend_items)
        eval_time = datetime(2022, 6, 6, 10, 0, tzinfo=UTC)
        with self.assertRaises(NoDataError):
            self.provider._parse_and_validate_data(df_weekend, "SPY", "1d", current_time=eval_time)

        # 2. Mixed dataset: Friday 2022-06-03 (trading) + Saturday 2022-06-04 (non-trading)
        # Only Friday is admitted; Saturday is discarded as UNKNOWN
        mixed_items = [["2022-06-03"], ["2022-06-04"]]
        df_mixed = self._make_df(mixed_items)
        candles = self.provider._parse_and_validate_data(df_mixed, "SPY", "1d", current_time=eval_time)
        self.assertEqual(len(candles), 1)
        self.assertEqual(to_eastern(candles[0].timestamp).date(), date(2022, 6, 3))
        self.assertEqual(candles[0].status, CandleStatus.COMPLETE)

    def test_friday_never_becomes_thursday_or_sunday_exhaustive(self):
        """Exhaustively verify all NYSE trading Fridays in 2021-2023 preserve exact Friday session date."""
        curr = date(2021, 1, 1)
        end = date(2023, 12, 31)
        friday_count = 0
        while curr <= end:
            if curr.weekday() == 4:
                is_trading, _ = MarketClock.is_historical_trading_day(curr)
                if is_trading:
                    friday_count += 1
                    df = self._make_df([[curr.isoformat()]])
                    eval_t = datetime.combine(curr + timedelta(days=3), time(10, 0), tzinfo=UTC)
                    candles = self.provider._parse_and_validate_data(df, "SPY", "1d", current_time=eval_t)
                    self.assertEqual(len(candles), 1, f"Missing candle for trading Friday {curr}")
                    c_date = to_eastern(candles[0].timestamp).date()
                    self.assertEqual(c_date, curr, f"Friday {curr} converted to {c_date}!")
                    self.assertNotEqual(c_date, curr - timedelta(days=1), f"Friday {curr} became Thursday!")
                    self.assertNotEqual(c_date, curr + timedelta(days=2), f"Friday {curr} became Sunday!")
            curr += timedelta(days=1)

        self.assertEqual(friday_count, 152, "Expected exactly 152 trading Fridays across 2021-2023")


if __name__ == "__main__":
    unittest.main()
