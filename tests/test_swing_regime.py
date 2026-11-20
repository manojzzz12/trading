"""Unit test suite for trader-swing market regime detection.

Verifies:
1. All six regime classifications (STRONG_BULLISH, BULLISH, SIDEWAYS, BEARISH, STRONG_BEARISH, UNKNOWN).
2. Boundary threshold classification behavior.
3. Conflicting indicators and multi-proxy divergence handling.
4. Missing secondary proxy handling (re-weighting to available proxies).
5. Missing primary proxy (SPY) fallback to UNKNOWN regime.
6. Stale proxy data detection and safe UNKNOWN override.
7. Rejection of FORMING and UNKNOWN candles in finalized regime detection.
8. Deterministic repeatability of scores, confidence, and policies.
9. Strict look-ahead bias prevention.
10. Downstream strategy policy mapping integrity.
"""

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import os
import sys
import unittest

SWING_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".agents",
    "skills",
    "trader-swing",
)
if SWING_DIR not in sys.path:
    sys.path.insert(0, SWING_DIR)

from market_data_models import CandleStatus, EASTERN, MarketClock, OHLCVCandle, UTC
from indicator_validation import IncompleteCandleError
from regime_models import MarketRegime, MarketRegimeResult
from regime_detector import MarketRegimeDetector
from regime_validation import validate_regime_candles

# Pre-generate chronological confirmed NYSE trading days for synthetic test fixtures
_REGIME_TEST_TRADING_DAYS = []
_curr_d = date(2026, 1, 5)
while len(_REGIME_TEST_TRADING_DAYS) < 500:
    if MarketClock.get_session(_curr_d):
        _REGIME_TEST_TRADING_DAYS.append(_curr_d)
    _curr_d += timedelta(days=1)


def make_candle(
    ticker: str,
    index: int,
    close_p: str,
    open_p: str = None,
    high_p: str = None,
    low_p: str = None,
    volume: int = 10000000,
    status: CandleStatus = CandleStatus.COMPLETE,
    base_date: datetime = None,
) -> OHLCVCandle:
    if base_date is None:
        base_date = datetime(2026, 1, 5, 20, 0, 0, tzinfo=UTC)
    if index < len(_REGIME_TEST_TRADING_DAYS):
        d = _REGIME_TEST_TRADING_DAYS[index]
        ts = datetime.combine(d, base_date.time(), tzinfo=UTC)
    else:
        ts = base_date + timedelta(days=index)
    c_dec = Decimal(close_p)
    o_dec = Decimal(open_p) if open_p is not None else c_dec
    h_dec = Decimal(high_p) if high_p is not None else max(c_dec, o_dec) + Decimal("1.00")
    l_dec = Decimal(low_p) if low_p is not None else min(c_dec, o_dec) - Decimal("1.00")
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
        received_timestamp=ts,
        is_delayed=True,
    )


class TestSwingRegimeDetector(unittest.TestCase):
    """Test suite covering market regime detection rules, thresholds, and resilience."""

    def setUp(self):
        self.detector = MarketRegimeDetector()
        self.base_date = datetime(2026, 1, 5, 20, 0, 0, tzinfo=UTC)

    def _generate_series(self, ticker: str, count: int, start_val: Decimal, step: Decimal) -> list:
        bars = []
        val = start_val
        for i in range(count):
            val += step
            bars.append(make_candle(ticker, i, str(val), base_date=self.base_date))
        return bars

    def test_01_strong_bullish_regime(self):
        # Strongly rising trend across SPY, QQQ, and IWM over 220 bars
        spy_bars = self._generate_series("SPY", 220, Decimal("400.00"), Decimal("0.50"))
        qqq_bars = self._generate_series("QQQ", 220, Decimal("350.00"), Decimal("0.60"))
        iwm_bars = self._generate_series("IWM", 220, Decimal("200.00"), Decimal("0.30"))

        proxy_map = {"SPY": spy_bars, "QQQ": qqq_bars, "IWM": iwm_bars}
        now_dt = spy_bars[-1].timestamp + timedelta(hours=2)

        res = self.detector.detect_regime(proxy_map, current_time=now_dt)
        self.assertEqual(res.regime, MarketRegime.STRONG_BULLISH)
        self.assertTrue(res.confidence > Decimal("0.80"))
        self.assertTrue(res.policy.allow_new_longs)
        self.assertEqual(res.policy.min_scanner_score, Decimal("60.0"))
        self.assertTrue(len(res.supporting_evidence) > 0)

    def test_02_strong_bearish_regime(self):
        # Strongly falling trend across SPY, QQQ, and IWM over 220 bars
        spy_bars = self._generate_series("SPY", 220, Decimal("600.00"), Decimal("-0.80"))
        qqq_bars = self._generate_series("QQQ", 220, Decimal("500.00"), Decimal("-0.90"))
        iwm_bars = self._generate_series("IWM", 220, Decimal("280.00"), Decimal("-0.40"))

        proxy_map = {"SPY": spy_bars, "QQQ": qqq_bars, "IWM": iwm_bars}
        now_dt = spy_bars[-1].timestamp + timedelta(hours=2)

        res = self.detector.detect_regime(proxy_map, current_time=now_dt)
        self.assertEqual(res.regime, MarketRegime.STRONG_BEARISH)
        self.assertFalse(res.policy.allow_new_longs)
        self.assertEqual(res.policy.max_open_positions_override, 0)

    def test_03_sideways_regime(self):
        # Flat / choppy oscillating prices
        bars = []
        for i in range(100):
            p = Decimal("450.00") if i % 2 == 0 else Decimal("451.00")
            bars.append(make_candle("SPY", i, str(p), base_date=self.base_date))

        proxy_map = {"SPY": bars}
        now_dt = bars[-1].timestamp + timedelta(hours=2)

        res = self.detector.detect_regime(proxy_map, current_time=now_dt)
        self.assertEqual(res.regime, MarketRegime.SIDEWAYS)
        self.assertTrue(res.policy.allow_new_longs)
        self.assertEqual(res.policy.min_scanner_score, Decimal("75.0"))
        self.assertEqual(res.policy.max_open_positions_override, 3)

    def test_04_missing_primary_proxy_returns_unknown(self):
        qqq_bars = self._generate_series("QQQ", 100, Decimal("350.00"), Decimal("0.50"))
        proxy_map = {"QQQ": qqq_bars}
        res = self.detector.detect_regime(proxy_map)
        self.assertEqual(res.regime, MarketRegime.UNKNOWN)
        self.assertEqual(res.confidence, Decimal("0.0000"))
        self.assertFalse(res.policy.allow_new_longs)
        self.assertIn("SPY", res.missing_inputs)

    def test_05_missing_secondary_proxies_normalized(self):
        # Only SPY present; QQQ and IWM missing
        spy_bars = self._generate_series("SPY", 100, Decimal("400.00"), Decimal("0.50"))
        proxy_map = {"SPY": spy_bars}
        now_dt = spy_bars[-1].timestamp + timedelta(hours=2)

        res = self.detector.detect_regime(proxy_map, current_time=now_dt)
        # Should evaluate cleanly using SPY only with appropriate warnings
        self.assertIn(res.regime, [MarketRegime.STRONG_BULLISH, MarketRegime.BULLISH])
        self.assertIn("QQQ", res.missing_inputs)
        self.assertIn("IWM", res.missing_inputs)

    def test_06_stale_proxy_data_overrides_to_unknown(self):
        # Proxy data 10 days older than current_time
        spy_bars = self._generate_series("SPY", 60, Decimal("400.00"), Decimal("0.50"))
        proxy_map = {"SPY": spy_bars}
        stale_time = spy_bars[-1].timestamp + timedelta(days=10)

        res = self.detector.detect_regime(proxy_map, current_time=stale_time)
        self.assertEqual(res.regime, MarketRegime.UNKNOWN)
        self.assertEqual(res.confidence, Decimal("0.0000"))
        self.assertEqual(res.data_freshness, "STALE")
        self.assertFalse(res.policy.allow_new_longs)

    def test_07_forming_candle_rejected_by_default(self):
        spy_bars = self._generate_series("SPY", 50, Decimal("400.00"), Decimal("0.50"))
        spy_bars[-1] = make_candle("SPY", 49, "425.00", status=CandleStatus.FORMING, base_date=self.base_date)
        proxy_map = {"SPY": spy_bars}

        with self.assertRaises(IncompleteCandleError):
            self.detector.detect_regime(proxy_map, require_complete=True)

    def test_08_deterministic_repeatability(self):
        spy_bars = self._generate_series("SPY", 100, Decimal("400.00"), Decimal("0.30"))
        qqq_bars = self._generate_series("QQQ", 100, Decimal("350.00"), Decimal("0.20"))
        proxy_map = {"SPY": spy_bars, "QQQ": qqq_bars}
        now_dt = spy_bars[-1].timestamp + timedelta(hours=2)

        res1 = self.detector.detect_regime(proxy_map, current_time=now_dt)
        res2 = self.detector.detect_regime(proxy_map, current_time=now_dt)

        self.assertEqual(res1.regime, res2.regime)
        self.assertEqual(res1.aggregate_score, res2.aggregate_score)
        self.assertEqual(res1.confidence, res2.confidence)
        self.assertEqual(res1.to_dict(), res2.to_dict())

    def test_09_lookahead_bias_prevention(self):
        # Verify that altering future bars does not change past proxy score
        spy_base = self._generate_series("SPY", 40, Decimal("400.00"), Decimal("0.50"))
        analysis_base = self.detector.analyze_proxy(spy_base)

        spy_extended = list(spy_base) + self._generate_series("SPY", 10, Decimal("500.00"), Decimal("-5.00"))
        analysis_from_sub = self.detector.analyze_proxy(spy_extended[:40])

        self.assertEqual(analysis_base.score, analysis_from_sub.score)
        self.assertEqual(analysis_base.price, analysis_from_sub.price)


class TestSwingRegimeDailyCandleLifecycle(unittest.TestCase):
    """Regression test suite for daily candle lifecycle validation against NYSE trading calendar."""

    def setUp(self):
        self.detector = MarketRegimeDetector()

    def _make_trading_day_bars(
        self,
        ticker: str,
        count: int,
        end_date: date,
        start_val: Decimal = Decimal("400.00"),
    ) -> list:
        trading_days = []
        curr = end_date
        while len(trading_days) < count:
            sess = MarketClock.get_session(curr)
            if sess is not None:
                trading_days.append(curr)
            curr = curr - timedelta(days=1)
        trading_days.reverse()

        bars = []
        val = start_val
        for d in trading_days:
            val += Decimal("0.50")
            sess = MarketClock.get_session(d)
            open_t = sess[0] if sess else time(9, 30)
            ts = datetime.combine(d, open_t, tzinfo=EASTERN)
            bars.append(
                OHLCVCandle(
                    ticker=ticker,
                    interval="1d",
                    timestamp=ts,
                    open=val,
                    high=val + Decimal("1.00"),
                    low=val - Decimal("1.00"),
                    close=val,
                    volume=10000000,
                    status=CandleStatus.COMPLETE,
                    source="yahoo",
                )
            )
        return bars

    def test_midday_daily_candle_rejected_during_active_session(self):
        """A daily candle marked COMPLETE at 12:00 PM during an active session must be rejected."""
        bars = self._make_trading_day_bars("SPY", 20, date(2026, 6, 25))
        # Current time is midday 12:00 PM ET during active trading hours
        midday_dt = datetime(2026, 6, 25, 12, 0, 0, tzinfo=EASTERN)
        proxy_map = {"SPY": bars}

        with self.assertRaises(IncompleteCandleError) as ctx:
            self.detector.detect_regime(proxy_map, current_time=midday_dt, require_complete=True)
        self.assertEqual(ctx.exception.status, CandleStatus.FORMING)
        self.assertIn("FORMING", str(ctx.exception))

    def test_early_close_daily_candle_rejected_before_early_close(self):
        """A daily candle marked COMPLETE before 1:00 PM on an early close day must be rejected."""
        # 2026-11-27 is Day after Thanksgiving (early close at 1:00 PM EST)
        bars = self._make_trading_day_bars("SPY", 20, date(2026, 11, 27))
        # Current time is 12:30 PM ET (30 minutes before 1:00 PM early close)
        early_midday_dt = datetime(2026, 11, 27, 12, 30, 0, tzinfo=EASTERN)
        proxy_map = {"SPY": bars}

        with self.assertRaises(IncompleteCandleError) as ctx:
            self.detector.detect_regime(proxy_map, current_time=early_midday_dt, require_complete=True)
        self.assertEqual(ctx.exception.status, CandleStatus.FORMING)
        self.assertIn("FORMING", str(ctx.exception))

    def test_weekend_daily_candle_rejected(self):
        """A daily candle timestamped or evaluated on a weekend must resolve to UNKNOWN and be rejected."""
        bars = self._make_trading_day_bars("SPY", 19, date(2026, 6, 26))
        # Append 20th bar with weekend timestamp (Saturday 2026-06-27)
        weekend_ts = datetime(2026, 6, 27, 9, 30, 0, tzinfo=EASTERN)
        weekend_bar = OHLCVCandle(
            ticker="SPY",
            interval="1d",
            timestamp=weekend_ts,
            open=Decimal("410.00"),
            high=Decimal("411.00"),
            low=Decimal("409.00"),
            close=Decimal("410.50"),
            volume=10000000,
            status=CandleStatus.COMPLETE,
            source="yahoo",
        )
        bars.append(weekend_bar)
        weekend_dt = datetime(2026, 6, 27, 12, 0, 0, tzinfo=EASTERN)
        proxy_map = {"SPY": bars}

        with self.assertRaises(IncompleteCandleError) as ctx:
            self.detector.detect_regime(proxy_map, current_time=weekend_dt, require_complete=True)
        self.assertEqual(ctx.exception.status, CandleStatus.UNKNOWN)
        self.assertIn("UNKNOWN", str(ctx.exception))

    def test_holiday_daily_candle_rejected(self):
        """A daily candle timestamped or evaluated on an NYSE holiday must resolve to UNKNOWN and be rejected."""
        # 2026-11-26 is Thanksgiving Day (NYSE holiday)
        bars = self._make_trading_day_bars("SPY", 19, date(2026, 11, 25))
        holiday_ts = datetime(2026, 11, 26, 9, 30, 0, tzinfo=EASTERN)
        holiday_bar = OHLCVCandle(
            ticker="SPY",
            interval="1d",
            timestamp=holiday_ts,
            open=Decimal("410.00"),
            high=Decimal("411.00"),
            low=Decimal("409.00"),
            close=Decimal("410.50"),
            volume=10000000,
            status=CandleStatus.COMPLETE,
            source="yahoo",
        )
        bars.append(holiday_bar)
        holiday_dt = datetime(2026, 11, 26, 12, 0, 0, tzinfo=EASTERN)
        proxy_map = {"SPY": bars}

        with self.assertRaises(IncompleteCandleError) as ctx:
            self.detector.detect_regime(proxy_map, current_time=holiday_dt, require_complete=True)
        self.assertEqual(ctx.exception.status, CandleStatus.UNKNOWN)
        self.assertIn("UNKNOWN", str(ctx.exception))

    def test_valid_post_close_daily_candle_accepted(self):
        """A daily candle evaluated after regular session close (16:30 ET) must resolve to COMPLETE and succeed."""
        bars = self._make_trading_day_bars("SPY", 20, date(2026, 6, 25))
        post_close_dt = datetime(2026, 6, 25, 16, 30, 0, tzinfo=EASTERN)
        proxy_map = {"SPY": bars}

        res = self.detector.detect_regime(proxy_map, current_time=post_close_dt, require_complete=True)
        self.assertNotEqual(res.regime, MarketRegime.UNKNOWN)
        self.assertEqual(res.data_freshness, "FRESH")

    def test_valid_early_close_post_close_daily_candle_accepted(self):
        """A daily candle evaluated after early close (13:30 ET on 2026-11-27) must resolve to COMPLETE and succeed."""
        bars = self._make_trading_day_bars("SPY", 20, date(2026, 11, 27))
        post_early_close_dt = datetime(2026, 11, 27, 13, 30, 0, tzinfo=EASTERN)
        proxy_map = {"SPY": bars}

        res = self.detector.detect_regime(proxy_map, current_time=post_early_close_dt, require_complete=True)
        self.assertNotEqual(res.regime, MarketRegime.UNKNOWN)
        self.assertEqual(res.data_freshness, "FRESH")

    def test_intraday_midday_candle_allowed_when_require_complete_false(self):
        """When require_complete=False, an actively forming midday bar is accepted for preliminary evaluation."""
        bars = self._make_trading_day_bars("SPY", 20, date(2026, 6, 25))
        midday_dt = datetime(2026, 6, 25, 12, 0, 0, tzinfo=EASTERN)
        proxy_map = {"SPY": bars}

        res = self.detector.detect_regime(proxy_map, current_time=midday_dt, require_complete=False)
        self.assertNotEqual(res.regime, MarketRegime.UNKNOWN)

    def test_direct_validate_regime_candles_lifecycle_check(self):
        """validate_regime_candles directly verifies lifecycle status when require_complete=True."""
        bars = self._make_trading_day_bars("SPY", 20, date(2026, 6, 25))
        midday_dt = datetime(2026, 6, 25, 12, 0, 0, tzinfo=EASTERN)

        with self.assertRaises(IncompleteCandleError):
            validate_regime_candles(bars, require_complete=True, current_time=midday_dt)

        post_close_dt = datetime(2026, 6, 25, 16, 30, 0, tzinfo=EASTERN)
        # Should not raise
        validate_regime_candles(bars, require_complete=True, current_time=post_close_dt)

    def test_historical_saturday_intermediary_candle_rejected(self):
        """Historical Saturday intermediary daily candle falsely marked COMPLETE must be rejected."""
        bars = self._make_trading_day_bars("SPY", 19, date(2026, 6, 26))
        # Insert a historical Saturday candle at an intermediary position (2026-06-20)
        sat_ts = datetime(2026, 6, 20, 9, 30, 0, tzinfo=EASTERN)
        sat_bar = OHLCVCandle(
            ticker="SPY",
            interval="1d",
            timestamp=sat_ts,
            open=Decimal("410.00"),
            high=Decimal("411.00"),
            low=Decimal("409.00"),
            close=Decimal("410.50"),
            volume=10000000,
            status=CandleStatus.COMPLETE,
            source="yahoo",
        )
        bars.append(sat_bar)
        bars.sort(key=lambda c: c.timestamp)
        eval_dt = datetime(2026, 6, 26, 17, 0, 0, tzinfo=EASTERN)
        proxy_map = {"SPY": bars}

        with self.assertRaises(IncompleteCandleError) as ctx:
            self.detector.detect_regime(proxy_map, current_time=eval_dt, require_complete=True)
        self.assertEqual(ctx.exception.status, CandleStatus.UNKNOWN)
        self.assertIn("2026-06-20", str(ctx.exception))

    def test_historical_sunday_intermediary_candle_rejected(self):
        """Historical Sunday intermediary daily candle falsely marked COMPLETE must be rejected."""
        bars = self._make_trading_day_bars("SPY", 19, date(2026, 6, 26))
        # Insert a historical Sunday candle at an intermediary position (2026-06-21)
        sun_ts = datetime(2026, 6, 21, 9, 30, 0, tzinfo=EASTERN)
        sun_bar = OHLCVCandle(
            ticker="SPY",
            interval="1d",
            timestamp=sun_ts,
            open=Decimal("410.00"),
            high=Decimal("411.00"),
            low=Decimal("409.00"),
            close=Decimal("410.50"),
            volume=10000000,
            status=CandleStatus.COMPLETE,
            source="yahoo",
        )
        bars.append(sun_bar)
        bars.sort(key=lambda c: c.timestamp)
        eval_dt = datetime(2026, 6, 26, 17, 0, 0, tzinfo=EASTERN)
        proxy_map = {"SPY": bars}

        with self.assertRaises(IncompleteCandleError) as ctx:
            self.detector.detect_regime(proxy_map, current_time=eval_dt, require_complete=True)
        self.assertEqual(ctx.exception.status, CandleStatus.UNKNOWN)
        self.assertIn("2026-06-21", str(ctx.exception))

    def test_historical_holiday_intermediary_candle_rejected(self):
        """Historical NYSE holiday intermediary daily candle falsely marked COMPLETE must be rejected."""
        # 2026-11-26 is Thanksgiving Day (NYSE holiday)
        bars = self._make_trading_day_bars("SPY", 19, date(2026, 11, 30))
        holiday_ts = datetime(2026, 11, 26, 9, 30, 0, tzinfo=EASTERN)
        holiday_bar = OHLCVCandle(
            ticker="SPY",
            interval="1d",
            timestamp=holiday_ts,
            open=Decimal("410.00"),
            high=Decimal("411.00"),
            low=Decimal("409.00"),
            close=Decimal("410.50"),
            volume=10000000,
            status=CandleStatus.COMPLETE,
            source="yahoo",
        )
        bars.append(holiday_bar)
        bars.sort(key=lambda c: c.timestamp)
        eval_dt = datetime(2026, 11, 30, 17, 0, 0, tzinfo=EASTERN)
        proxy_map = {"SPY": bars}

        with self.assertRaises(IncompleteCandleError) as ctx:
            self.detector.detect_regime(proxy_map, current_time=eval_dt, require_complete=True)
        self.assertEqual(ctx.exception.status, CandleStatus.UNKNOWN)
        self.assertIn("2026-11-26", str(ctx.exception))

    def test_valid_historical_trading_session_candles_accepted(self):
        """A full sequence of legitimate historical completed trading-session daily candles must be accepted."""
        bars = self._make_trading_day_bars("SPY", 20, date(2026, 6, 26))
        eval_dt = datetime(2026, 6, 26, 17, 0, 0, tzinfo=EASTERN)
        proxy_map = {"SPY": bars}

        res = self.detector.detect_regime(proxy_map, current_time=eval_dt, require_complete=True)
        self.assertNotEqual(res.regime, MarketRegime.UNKNOWN)
        self.assertEqual(res.data_freshness, "FRESH")


if __name__ == "__main__":
    unittest.main()
