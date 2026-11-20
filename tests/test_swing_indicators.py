"""Unit test suite for trader-swing technical indicators engine.

Verifies:
1. SMA calculation, warm-up boundaries, and mathematical accuracy.
2. EMA calculation, SMA seed, multiplier recurrence, and precision.
3. RSI Wilder smoothing, warm-up, rising, falling, and flat-market edge cases.
4. MACD line, signal line, histogram, and warm-up milestones (26 bars, 34 bars).
5. ATR Wilder smoothing, gap-up and gap-down true range awareness.
6. Bollinger Bands middle, upper, lower, bandwidth, percent-b, and flat-price handling.
7. Average Volume and Relative Volume (RVOL) with zero-volume guards.
8. Strict look-ahead bias prevention (future bars do not alter past indicator values).
9. Incomplete candle rejection in individual functions and engine.
10. Consolidated TechnicalIndicatorsEngine snapshot generation and serialization.
11. Pure Decimal arithmetic enforcement (no binary floats in results).
"""

from datetime import datetime, timedelta
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

from market_data_models import CandleStatus, OHLCVCandle, UTC
from indicator_models import (
    BollingerBandsResult,
    MACDResult,
    SingleIndicatorValue,
    TechnicalIndicatorSnapshot,
)
from indicator_validation import IncompleteCandleError, InsufficientDataError
from indicators import (
    TechnicalIndicatorsEngine,
    calculate_atr,
    calculate_atr_series,
    calculate_average_volume,
    calculate_average_volume_series,
    calculate_bollinger_bands,
    calculate_bollinger_bands_series,
    calculate_ema,
    calculate_ema_series,
    calculate_macd,
    calculate_macd_series,
    calculate_relative_volume,
    calculate_relative_volume_series,
    calculate_rsi,
    calculate_rsi_series,
    calculate_sma,
    calculate_sma_series,
    quantize_indicator,
)


def make_bar(
    index: int,
    close_p: str,
    open_p: str = None,
    high_p: str = None,
    low_p: str = None,
    volume: int = 1000000,
    ticker: str = "AAPL",
    interval: str = "1d",
    status: CandleStatus = CandleStatus.COMPLETE,
    base_date: datetime = None,
) -> OHLCVCandle:
    if base_date is None:
        base_date = datetime(2026, 1, 5, 20, 0, 0, tzinfo=UTC)
    ts = base_date + timedelta(days=index)
    c_dec = Decimal(close_p)
    o_dec = Decimal(open_p) if open_p is not None else c_dec
    h_dec = Decimal(high_p) if high_p is not None else max(c_dec, o_dec) + Decimal("1.00")
    l_dec = Decimal(low_p) if low_p is not None else min(c_dec, o_dec) - Decimal("1.00")
    return OHLCVCandle(
        ticker=ticker,
        interval=interval,
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


class TestSwingIndicators(unittest.TestCase):
    """Test suite covering deterministic technical indicator mathematics and engine behavior."""

    def setUp(self):
        self.engine = TechnicalIndicatorsEngine()

    # -----------------------------------------------------------------------
    # 1. Simple Moving Average (SMA)
    # -----------------------------------------------------------------------

    def test_01_sma_warmup_unavailable(self):
        bars = [make_bar(i, "100.00") for i in range(19)]
        res = calculate_sma(bars, period=20)
        self.assertFalse(res.is_available)
        self.assertIsNone(res.value)
        self.assertIn("Insufficient warm-up data", res.reason)
        self.assertEqual(res.period, 20)
        self.assertEqual(res.ticker, "AAPL")

    def test_02_sma_exact_calculation(self):
        # 5 bars with closes: 10, 20, 30, 40, 50 -> sum = 150, avg = 30.0000
        bars = [make_bar(i, str((i + 1) * 10)) for i in range(5)]
        res = calculate_sma(bars, period=5)
        self.assertTrue(res.is_available)
        self.assertEqual(res.value, Decimal("30.0000"))
        self.assertIsInstance(res.value, Decimal)
        self.assertEqual(res.as_of_timestamp, bars[-1].timestamp)

    def test_03_sma_series_consistency(self):
        bars = [make_bar(i, str(100 + i)) for i in range(30)]
        series = calculate_sma_series(bars, period=20)
        self.assertEqual(len(series), 30)
        # First 19 bars must be unavailable
        for i in range(19):
            self.assertFalse(series[i].is_available)
            self.assertIsNone(series[i].value)
        # Bar 20 (index 19) onwards must be available
        for i in range(19, 30):
            self.assertTrue(series[i].is_available)
            self.assertIsNotNone(series[i].value)
        # calculate_sma matches series[-1]
        single = calculate_sma(bars, period=20)
        self.assertEqual(single, series[-1])

    # -----------------------------------------------------------------------
    # 2. Exponential Moving Average (EMA)
    # -----------------------------------------------------------------------

    def test_04_ema_seed_and_recurrence(self):
        # Period = 3.
        # Bar 0: 10
        # Bar 1: 20
        # Bar 2: 30 -> Seed EMA = (10 + 20 + 30) / 3 = 20.0000
        # Multiplier alpha = 2 / (3 + 1) = 0.5
        # Bar 3: 40 -> EMA = 40 * 0.5 + 20 * 0.5 = 30.0000
        # Bar 4: 50 -> EMA = 50 * 0.5 + 30 * 0.5 = 40.0000
        bars = [make_bar(i, str((i + 1) * 10)) for i in range(5)]
        series = calculate_ema_series(bars, period=3)
        self.assertFalse(series[0].is_available)
        self.assertFalse(series[1].is_available)
        self.assertTrue(series[2].is_available)
        self.assertEqual(series[2].value, Decimal("20.0000"))
        self.assertEqual(series[3].value, Decimal("30.0000"))
        self.assertEqual(series[4].value, Decimal("40.0000"))

        point = calculate_ema(bars, period=3)
        self.assertEqual(point.value, Decimal("40.0000"))

    # -----------------------------------------------------------------------
    # 3. Relative Strength Index (RSI - Wilder's Smoothing)
    # -----------------------------------------------------------------------

    def test_05_rsi_warmup_unavailable(self):
        bars = [make_bar(i, "100.00") for i in range(14)]
        res = calculate_rsi(bars, period=14)
        self.assertFalse(res.is_available)
        self.assertIsNone(res.value)
        self.assertIn("Insufficient warm-up data", res.reason)

    def test_06_rsi_strictly_rising_reaches_100(self):
        # 16 bars with strictly rising closes
        bars = [make_bar(i, str(100 + i * 2)) for i in range(16)]
        res = calculate_rsi(bars, period=14)
        self.assertTrue(res.is_available)
        self.assertEqual(res.value, Decimal("100.0000"))

    def test_07_rsi_strictly_falling_reaches_0(self):
        # 16 bars with strictly falling closes
        bars = [make_bar(i, str(200 - i * 2)) for i in range(16)]
        res = calculate_rsi(bars, period=14)
        self.assertTrue(res.is_available)
        self.assertEqual(res.value, Decimal("0.0000"))

    def test_08_rsi_flat_market_returns_50(self):
        # 20 bars with exact identical close
        bars = [make_bar(i, "150.00") for i in range(20)]
        res = calculate_rsi(bars, period=14)
        self.assertTrue(res.is_available)
        self.assertEqual(res.value, Decimal("50.0000"))

    def test_09_rsi_alternating_moves_wilder_smoothing(self):
        # Alternating +2 and -1 changes
        prices = ["100.00"]
        curr = Decimal("100.00")
        for i in range(1, 20):
            if i % 2 == 1:
                curr += Decimal("2.00")
            else:
                curr -= Decimal("1.00")
            prices.append(str(curr))
        bars = [make_bar(i, prices[i]) for i in range(len(prices))]
        series = calculate_rsi_series(bars, period=14)
        # Bar 14 (index 14, 15th bar) is the first available RSI
        self.assertFalse(series[13].is_available)
        self.assertTrue(series[14].is_available)
        # Verify RSI is bounded strictly between 0 and 100
        self.assertTrue(Decimal("0.0") < series[14].value < Decimal("100.0"))

    # -----------------------------------------------------------------------
    # 4. Moving Average Convergence Divergence (MACD)
    # -----------------------------------------------------------------------

    def test_10_macd_warmup_phases(self):
        # Test 40 bars
        bars = [make_bar(i, str(100 + i)) for i in range(40)]
        series = calculate_macd_series(bars, fast_period=12, slow_period=26, signal_period=9)

        # 1. Bar 0 to 24 (< 26 bars): MACD line is None, unavailable
        for i in range(25):
            self.assertFalse(series[i].is_available)
            self.assertIsNone(series[i].macd_line)
            self.assertIsNone(series[i].signal_line)
            self.assertIsNone(series[i].histogram)

        # 2. Bar 25 to 32 (26 to 33 bars): MACD line is present, signal line is None
        for i in range(25, 33):
            self.assertFalse(series[i].is_available)
            self.assertIsNotNone(series[i].macd_line)
            self.assertIsNone(series[i].signal_line)
            self.assertIsNone(series[i].histogram)

        # 3. Bar 33 onwards (>= 34 bars): all three lines present, is_available=True
        for i in range(33, 40):
            self.assertTrue(series[i].is_available)
            self.assertIsNotNone(series[i].macd_line)
            self.assertIsNotNone(series[i].signal_line)
            self.assertIsNotNone(series[i].histogram)
            # Histogram must equal macd_line - signal_line
            expected_hist = series[i].macd_line - series[i].signal_line
            self.assertEqual(series[i].histogram, expected_hist)

    def test_11_macd_fast_ge_slow_rejected(self):
        bars = [make_bar(i, "100.00") for i in range(30)]
        with self.assertRaises(ValueError):
            calculate_macd(bars, fast_period=26, slow_period=12)
        with self.assertRaises(ValueError):
            calculate_macd(bars, fast_period=20, slow_period=20)

    # -----------------------------------------------------------------------
    # 5. Average True Range (ATR - Wilder's Smoothing)
    # -----------------------------------------------------------------------

    def test_12_atr_gap_up_and_gap_down_awareness(self):
        # Bar 0: C=100, H=102, L=98 -> TR0 = 4 (unused for seed)
        # Bar 1: Gap up: C=110, H=112, L=108. Prev close = 100.
        # TR1 = max(112-108=4, |112-100|=12, |108-100|=8) = 12
        # Bar 2: Gap down: C=90, H=92, L=88. Prev close = 110.
        # TR2 = max(92-88=4, |92-110|=18, |88-110|=22) = 22
        b0 = make_bar(0, "100.00", open_p="100.00", high_p="102.00", low_p="98.00")
        b1 = make_bar(1, "110.00", open_p="109.00", high_p="112.00", low_p="108.00")
        b2 = make_bar(2, "90.00", open_p="91.00", high_p="92.00", low_p="88.00")

        # Period 2 ATR needs 3 bars (bars 0, 1, 2)
        series = calculate_atr_series([b0, b1, b2], period=2)
        self.assertFalse(series[0].is_available)
        self.assertFalse(series[1].is_available)
        self.assertTrue(series[2].is_available)
        # Average of TR1 (12) and TR2 (22) = 17.0000
        self.assertEqual(series[2].value, Decimal("17.0000"))

    def test_13_atr_warmup_and_wilder_smoothing(self):
        # 16 bars with constant H-L=2.00 and no gaps (C=100, H=101, L=99)
        bars = [
            make_bar(i, "100.00", open_p="100.00", high_p="101.00", low_p="99.00")
            for i in range(16)
        ]
        series = calculate_atr_series(bars, period=14)
        # First 14 bars (index 0..13) unavailable
        self.assertFalse(series[13].is_available)
        # 15th bar (index 14) available, TR is 2.0000
        self.assertTrue(series[14].is_available)
        self.assertEqual(series[14].value, Decimal("2.0000"))
        # 16th bar also 2.0000
        self.assertEqual(series[15].value, Decimal("2.0000"))

    # -----------------------------------------------------------------------
    # 6. Bollinger Bands
    # -----------------------------------------------------------------------

    def test_14_bollinger_bands_calculation(self):
        # 20 bars with known constant price 100.00
        bars = [make_bar(i, "100.00") for i in range(20)]
        res = calculate_bollinger_bands(bars, period=20, num_std_dev=Decimal("2.0"))
        self.assertTrue(res.is_available)
        self.assertEqual(res.middle, Decimal("100.0000"))
        self.assertEqual(res.upper, Decimal("100.0000"))
        self.assertEqual(res.lower, Decimal("100.0000"))
        self.assertEqual(res.bandwidth, Decimal("0.0000"))
        self.assertEqual(res.percent_b, Decimal("0.5000"))  # Centered when bands collapse

    def test_15_bollinger_bands_variance_and_bands(self):
        # 20 bars with alternating 90 and 110:
        # Mean = 100.
        # Deviation for each is +10 or -10. Squared deviation is 100.
        # Variance = 100.
        # Std dev = sqrt(100) = 10.
        # Upper = 100 + 2 * 10 = 120.0000
        # Lower = 100 - 2 * 10 = 80.0000
        # Bandwidth = (120 - 80) / 100 = 0.4000
        # %B for last bar (close=110): (110 - 80) / (120 - 80) = 30 / 40 = 0.7500
        bars = []
        for i in range(20):
            p = "90.00" if i % 2 == 0 else "110.00"
            bars.append(make_bar(i, p))

        res = calculate_bollinger_bands(bars, period=20, num_std_dev=Decimal("2.0"))
        self.assertTrue(res.is_available)
        self.assertEqual(res.middle, Decimal("100.0000"))
        self.assertEqual(res.upper, Decimal("120.0000"))
        self.assertEqual(res.lower, Decimal("80.0000"))
        self.assertEqual(res.bandwidth, Decimal("0.4000"))
        self.assertEqual(res.percent_b, Decimal("0.7500"))

    # -----------------------------------------------------------------------
    # 7. Volume & Relative Volume (RVOL)
    # -----------------------------------------------------------------------

    def test_16_average_volume_and_rvol(self):
        # 20 bars with volume 1,000,000, except the 20th bar has volume 2,500,000
        bars_20 = [make_bar(i, "100.00", volume=1000000) for i in range(19)]
        bars_20.append(make_bar(19, "100.00", volume=2500000))

        # Avg volume: (19 * 1M + 2.5M) / 20 = 21.5M / 20 = 1,075,000.00
        avg_res = calculate_average_volume(bars_20, period=20)
        self.assertTrue(avg_res.is_available)
        self.assertEqual(avg_res.value, Decimal("1075000.00"))

        # RVOL on 20 bars is unavailable (requires 21 bars for exclusive baseline)
        rvol_20 = calculate_relative_volume(bars_20, period=20)
        self.assertFalse(rvol_20.is_available)
        self.assertIsNone(rvol_20.value)

        # 21 bars: 20 prior bars of 1,000,000 and 21st bar of 2,500,000
        bars_21 = [make_bar(i, "100.00", volume=1000000) for i in range(20)]
        bars_21.append(make_bar(20, "100.00", volume=2500000))
        rvol_21 = calculate_relative_volume(bars_21, period=20)
        self.assertTrue(rvol_21.is_available)
        # RVOL: 2,500,000 / 1,000,000 = 2.5000
        self.assertEqual(rvol_21.value, Decimal("2.5000"))

    def test_17_rvol_zero_volume_handling(self):
        # 21 bars with zero volume
        bars = [make_bar(i, "100.00", volume=0) for i in range(21)]
        rvol_res = calculate_relative_volume(bars, period=20)
        self.assertTrue(rvol_res.is_available)
        self.assertEqual(rvol_res.value, Decimal("0.0000"))
        self.assertIn("Zero volume with zero previous average volume", rvol_res.reason)

    # -----------------------------------------------------------------------
    # 7.1. Phase 4.1.1 RVOL Benchmark Invariant Tests
    # -----------------------------------------------------------------------

    def test_17a_rvol_spike_twenty_one_to_one(self):
        # Scenario 1: Previous 20 candles each have volume 1,000,000; current candle has volume 21,000,000
        bars = [make_bar(i, "100.00", volume=1000000) for i in range(20)]
        bars.append(make_bar(20, "100.00", volume=21000000))
        res = calculate_relative_volume(bars, period=20)
        self.assertTrue(res.is_available)
        self.assertEqual(res.value, Decimal("21.0000"))

    def test_17b_rvol_equal_to_previous_average(self):
        # Scenario 2: Current volume equals the previous 20-period average -> RVOL = 1.0000
        bars = [make_bar(i, "100.00", volume=1000000) for i in range(20)]
        bars.append(make_bar(20, "100.00", volume=1000000))
        res = calculate_relative_volume(bars, period=20)
        self.assertTrue(res.is_available)
        self.assertEqual(res.value, Decimal("1.0000"))

    def test_17c_rvol_half_of_previous_average(self):
        # Scenario 3: Current volume is half the previous average -> RVOL = 0.5000
        bars = [make_bar(i, "100.00", volume=1000000) for i in range(20)]
        bars.append(make_bar(20, "100.00", volume=500000))
        res = calculate_relative_volume(bars, period=20)
        self.assertTrue(res.is_available)
        self.assertEqual(res.value, Decimal("0.5000"))

    def test_17d_rvol_twenty_candles_insufficient(self):
        # Scenario 4: Exactly 20 candles -> INSUFFICIENT_DATA
        bars = [make_bar(i, "100.00", volume=1000000) for i in range(20)]
        res = calculate_relative_volume(bars, period=20)
        self.assertFalse(res.is_available)
        self.assertIsNone(res.value)
        self.assertIn("Insufficient warm-up data: 20 bars available, 21 required.", res.reason)

    def test_17e_rvol_twenty_one_candles_first_valid(self):
        # Scenario 5: Exactly 21 candles -> first valid RVOL
        bars = [make_bar(i, "100.00", volume=1000000) for i in range(21)]
        res = calculate_relative_volume(bars, period=20)
        self.assertTrue(res.is_available)
        self.assertIsNotNone(res.value)
        self.assertEqual(res.value, Decimal("1.0000"))

    def test_17f_rvol_zero_previous_average_with_nonzero_current(self):
        # Scenario 6: Zero previous average volume with non-zero current volume -> safe undefined handling
        bars = [make_bar(i, "100.00", volume=0) for i in range(20)]
        bars.append(make_bar(20, "100.00", volume=500000))
        res = calculate_relative_volume(bars, period=20)
        self.assertFalse(res.is_available)
        self.assertIsNone(res.value)
        self.assertIn("Undefined RVOL: previous average volume is zero", res.reason)

    def test_17g_rvol_current_volume_excluded_from_own_denominator(self):
        # Scenario 7: Current volume is strictly excluded from denominator
        # Prior 20 bars: volume 1,000,000. Prior avg = 1,000,000.
        # Bar 20 has volume 50,000,000. RVOL must be exactly 50.0000 (not diluted to ~14.49)
        bars = [make_bar(i, "100.00", volume=1000000) for i in range(20)]
        bars.append(make_bar(20, "100.00", volume=50000000))
        res = calculate_relative_volume(bars, period=20)
        self.assertTrue(res.is_available)
        self.assertEqual(res.value, Decimal("50.0000"))

    def test_17h_rvol_series_lookahead_prevention(self):
        # Scenario 8: Historical series look-ahead prevention
        bars_25 = [make_bar(i, "100.00", volume=1000000 + i * 10000) for i in range(25)]
        series_25 = calculate_relative_volume_series(bars_25, period=20)

        # Append 15 future bars with wild volumes
        bars_40 = list(bars_25) + [make_bar(25 + i, "100.00", volume=99999999) for i in range(15)]
        series_40 = calculate_relative_volume_series(bars_40, period=20)

        # Values for indices 0..24 must remain identical
        for i in range(25):
            self.assertEqual(series_25[i].is_available, series_40[i].is_available)
            self.assertEqual(series_25[i].value, series_40[i].value)

    def test_17i_rvol_forming_and_unknown_rejected(self):
        # Scenario 9: FORMING and UNKNOWN candles rejected
        bars_forming = [make_bar(i, "100.00", volume=1000000) for i in range(21)]
        bars_forming[-1] = make_bar(20, "100.00", volume=1000000, status=CandleStatus.FORMING)
        with self.assertRaises(IncompleteCandleError):
            calculate_relative_volume(bars_forming, period=20, require_complete=True)

        bars_unknown = [make_bar(i, "100.00", volume=1000000) for i in range(21)]
        bars_unknown[10] = make_bar(10, "100.00", volume=1000000, status=CandleStatus.UNKNOWN)
        with self.assertRaises(IncompleteCandleError):
            calculate_relative_volume(bars_unknown, period=20, require_complete=True)

    def test_17j_rvol_decimal_precision_and_rounding(self):
        # Scenario 10: Pure Decimal precision and rounding (e.g. 1/3 -> 0.3333)
        # Prior 20 bars each have volume 300,000 -> Prior average = 300,000.00
        # Current bar has volume 100,000 -> 100,000 / 300,000 = 0.333333... -> 0.3333
        bars = [make_bar(i, "100.00", volume=300000) for i in range(20)]
        bars.append(make_bar(20, "100.00", volume=100000))
        res = calculate_relative_volume(bars, period=20)
        self.assertTrue(res.is_available)
        self.assertIsInstance(res.value, Decimal)
        self.assertEqual(res.value, Decimal("0.3333"))

    # -----------------------------------------------------------------------
    # 8. Strict Look-Ahead Bias Prevention
    # -----------------------------------------------------------------------

    def test_18_look_ahead_bias_prevention(self):
        # Build 30 bars
        bars_short = [make_bar(i, str(100 + i)) for i in range(25)]
        # Snapshot at bar 24 (index 24)
        snap_short = self.engine.compute_all(bars_short)

        # Now append 10 future bars
        bars_long = list(bars_short) + [make_bar(25 + i, str(125 + i * 5)) for i in range(10)]
        series_long = self.engine.compute_series(bars_long)

        # Snapshot at index 24 in series_long MUST MATCH snap_short exactly
        snap_from_series = series_long[24]
        self.assertEqual(snap_short.sma_20.value, snap_from_series.sma_20.value)
        self.assertEqual(snap_short.ema_9.value, snap_from_series.ema_9.value)
        self.assertEqual(snap_short.rsi_14.value, snap_from_series.rsi_14.value)
        self.assertEqual(snap_short.bollinger_bands.upper, snap_from_series.bollinger_bands.upper)
        self.assertEqual(snap_short.average_volume_20.value, snap_from_series.average_volume_20.value)

    # -----------------------------------------------------------------------
    # 9. Incomplete Candle Rejection
    # -----------------------------------------------------------------------

    def test_19_incomplete_forming_candle_rejected(self):
        bars = [make_bar(i, "100.00") for i in range(20)]
        bars[-1] = make_bar(19, "100.00", status=CandleStatus.FORMING)

        with self.assertRaises(IncompleteCandleError):
            self.engine.compute_all(bars, require_complete=True)

        with self.assertRaises(IncompleteCandleError):
            calculate_sma(bars, period=20, require_complete=True)

    def test_20_unknown_candle_rejected(self):
        bars = [make_bar(i, "100.00") for i in range(20)]
        bars[10] = make_bar(10, "100.00", status=CandleStatus.UNKNOWN)

        with self.assertRaises(IncompleteCandleError):
            self.engine.compute_all(bars, require_complete=True)

    # -----------------------------------------------------------------------
    # 10. Consolidated Engine Snapshot & Pure Decimal Invariant
    # -----------------------------------------------------------------------

    def test_21_engine_compute_all_and_dict_serialization(self):
        # 60 bars to satisfy SMA 20, 50, EMA 9, 20, 50, RSI 14, MACD, ATR, BB, RVOL
        bars = [make_bar(i, str(100 + (i % 10))) for i in range(60)]
        snap = self.engine.compute_all(bars)

        self.assertEqual(snap.ticker, "AAPL")
        self.assertEqual(snap.interval, "1d")
        self.assertEqual(snap.bar_count, 60)
        self.assertEqual(snap.as_of_timestamp, bars[-1].timestamp)

        # Available indicators
        self.assertTrue(snap.sma_20.is_available)
        self.assertTrue(snap.sma_50.is_available)
        self.assertFalse(snap.sma_200.is_available)  # 60 bars < 200 required
        self.assertTrue(snap.ema_9.is_available)
        self.assertTrue(snap.ema_20.is_available)
        self.assertTrue(snap.ema_50.is_available)
        self.assertTrue(snap.rsi_14.is_available)
        self.assertTrue(snap.macd.is_available)
        self.assertTrue(snap.atr_14.is_available)
        self.assertTrue(snap.bollinger_bands.is_available)
        self.assertTrue(snap.average_volume_20.is_available)
        self.assertTrue(snap.relative_volume.is_available)

        # Pure Decimal verification: ensure none are binary floats
        for ind in [snap.sma_20, snap.sma_50, snap.ema_9, snap.ema_20, snap.ema_50, snap.rsi_14, snap.atr_14, snap.average_volume_20, snap.relative_volume]:
            if ind.value is not None:
                self.assertIsInstance(ind.value, Decimal)
        self.assertIsInstance(snap.macd.macd_line, Decimal)
        self.assertIsInstance(snap.macd.signal_line, Decimal)
        self.assertIsInstance(snap.macd.histogram, Decimal)
        self.assertIsInstance(snap.bollinger_bands.upper, Decimal)
        self.assertIsInstance(snap.bollinger_bands.middle, Decimal)
        self.assertIsInstance(snap.bollinger_bands.lower, Decimal)

        # Serialization to dict
        d = snap.to_dict()
        self.assertEqual(d["ticker"], "AAPL")
        self.assertEqual(d["bar_count"], 60)
        self.assertIsNone(d["sma_200"]["value"])
        self.assertFalse(d["sma_200"]["is_available"])
        self.assertIsNotNone(d["sma_20"]["value"])


if __name__ == "__main__":
    unittest.main()
