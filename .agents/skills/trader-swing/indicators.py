"""Deterministic technical indicators engine for trader-swing.

Enforces:
1. Pure Decimal arithmetic for all calculations (no binary floating point conversions).
2. Built-in Decimal.sqrt() for standard deviation in Bollinger Bands.
3. Strict look-ahead bias prevention: indicator at index t depends solely on bars 0..t.
4. Finalized candle requirement: rejects FORMING and UNKNOWN bars when require_complete=True.
5. Explicit unavailable representation: is_available=False, value=None, with explicit reason.
6. Guarded division for edge cases (flat price RSI=50.0, zero volume, zero bandwidth).
"""

from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
import os
import sys
from typing import List, Optional, Sequence, Tuple

# Ensure local imports work regardless of execution context
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from market_data_models import CandleStatus, OHLCVCandle
from indicator_models import (
    BollingerBandsResult,
    MACDResult,
    SingleIndicatorValue,
    TechnicalIndicatorSnapshot,
)
from indicator_validation import (
    IncompleteCandleError,
    IndicatorError,
    InsufficientDataError,
    validate_indicator_candles,
)

DEFAULT_INDICATOR_PRECISION = Decimal("0.0001")
DEFAULT_VOLUME_PRECISION = Decimal("0.01")


def quantize_indicator(
    val: Optional[Decimal],
    precision: Decimal = DEFAULT_INDICATOR_PRECISION,
) -> Optional[Decimal]:
    """Quantize a Decimal value using ROUND_HALF_UP, preserving None."""
    if val is None:
        return None
    return val.quantize(precision, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Simple Moving Average (SMA)
# ---------------------------------------------------------------------------

def calculate_sma_series(
    candles: Sequence[OHLCVCandle],
    period: int = 20,
    precision: Decimal = DEFAULT_INDICATOR_PRECISION,
    require_complete: bool = True,
    validate: bool = True,
) -> List[SingleIndicatorValue]:
    """Calculate SMA series across all candles in chronological order."""
    if period <= 0:
        raise ValueError(f"SMA period must be positive integer, got {period}.")
    if validate:
        validate_indicator_candles(candles, min_count=1, require_complete=require_complete)

    results: List[SingleIndicatorValue] = []
    running_sum = Decimal("0.0")

    for i, candle in enumerate(candles):
        running_sum += candle.close
        if i >= period:
            running_sum -= candles[i - period].close

        if i + 1 < period:
            results.append(
                SingleIndicatorValue(
                    name=f"SMA_{period}",
                    value=None,
                    is_available=False,
                    as_of_timestamp=candle.timestamp,
                    period=period,
                    ticker=candle.ticker,
                    interval=candle.interval,
                    reason=f"Insufficient warm-up data: {i + 1} bars available, {period} required.",
                )
            )
        else:
            raw_sma = running_sum / Decimal(period)
            results.append(
                SingleIndicatorValue(
                    name=f"SMA_{period}",
                    value=quantize_indicator(raw_sma, precision),
                    is_available=True,
                    as_of_timestamp=candle.timestamp,
                    period=period,
                    ticker=candle.ticker,
                    interval=candle.interval,
                    reason="",
                )
            )

    return results


def calculate_sma(
    candles: Sequence[OHLCVCandle],
    period: int = 20,
    precision: Decimal = DEFAULT_INDICATOR_PRECISION,
    require_complete: bool = True,
    validate: bool = True,
) -> SingleIndicatorValue:
    """Calculate SMA for the terminal candle in the sequence."""
    series = calculate_sma_series(
        candles,
        period=period,
        precision=precision,
        require_complete=require_complete,
        validate=validate,
    )
    return series[-1]


# ---------------------------------------------------------------------------
# Exponential Moving Average (EMA)
# ---------------------------------------------------------------------------

def calculate_ema_series(
    candles: Sequence[OHLCVCandle],
    period: int = 20,
    precision: Decimal = DEFAULT_INDICATOR_PRECISION,
    require_complete: bool = True,
    validate: bool = True,
) -> List[SingleIndicatorValue]:
    """Calculate EMA series across all candles using SMA seed and pure Decimal smoothing."""
    if period <= 0:
        raise ValueError(f"EMA period must be positive integer, got {period}.")
    if validate:
        validate_indicator_candles(candles, min_count=1, require_complete=require_complete)

    results: List[SingleIndicatorValue] = []
    alpha = Decimal(2) / Decimal(period + 1)
    one_minus_alpha = Decimal(1) - alpha

    current_ema: Optional[Decimal] = None
    initial_sum = Decimal("0.0")

    for i, candle in enumerate(candles):
        if i + 1 < period:
            initial_sum += candle.close
            results.append(
                SingleIndicatorValue(
                    name=f"EMA_{period}",
                    value=None,
                    is_available=False,
                    as_of_timestamp=candle.timestamp,
                    period=period,
                    ticker=candle.ticker,
                    interval=candle.interval,
                    reason=f"Insufficient warm-up data: {i + 1} bars available, {period} required.",
                )
            )
        elif i + 1 == period:
            initial_sum += candle.close
            current_ema = initial_sum / Decimal(period)
            results.append(
                SingleIndicatorValue(
                    name=f"EMA_{period}",
                    value=quantize_indicator(current_ema, precision),
                    is_available=True,
                    as_of_timestamp=candle.timestamp,
                    period=period,
                    ticker=candle.ticker,
                    interval=candle.interval,
                    reason="",
                )
            )
        else:
            assert current_ema is not None
            current_ema = (candle.close * alpha) + (current_ema * one_minus_alpha)
            results.append(
                SingleIndicatorValue(
                    name=f"EMA_{period}",
                    value=quantize_indicator(current_ema, precision),
                    is_available=True,
                    as_of_timestamp=candle.timestamp,
                    period=period,
                    ticker=candle.ticker,
                    interval=candle.interval,
                    reason="",
                )
            )

    return results


def calculate_ema(
    candles: Sequence[OHLCVCandle],
    period: int = 20,
    precision: Decimal = DEFAULT_INDICATOR_PRECISION,
    require_complete: bool = True,
    validate: bool = True,
) -> SingleIndicatorValue:
    """Calculate EMA for the terminal candle in the sequence."""
    series = calculate_ema_series(
        candles,
        period=period,
        precision=precision,
        require_complete=require_complete,
        validate=validate,
    )
    return series[-1]


# ---------------------------------------------------------------------------
# Relative Strength Index (RSI - Wilder's Smoothing)
# ---------------------------------------------------------------------------

def calculate_rsi_series(
    candles: Sequence[OHLCVCandle],
    period: int = 14,
    precision: Decimal = DEFAULT_INDICATOR_PRECISION,
    require_complete: bool = True,
    validate: bool = True,
) -> List[SingleIndicatorValue]:
    """Calculate RSI series using Wilder's smoothing and pure Decimal arithmetic."""
    if period <= 0:
        raise ValueError(f"RSI period must be positive integer, got {period}.")
    if validate:
        validate_indicator_candles(candles, min_count=1, require_complete=require_complete)

    results: List[SingleIndicatorValue] = []
    gains: List[Decimal] = []
    losses: List[Decimal] = []

    avg_gain: Optional[Decimal] = None
    avg_loss: Optional[Decimal] = None
    period_dec = Decimal(period)
    period_minus_one_dec = Decimal(period - 1)

    for i, candle in enumerate(candles):
        if i == 0:
            results.append(
                SingleIndicatorValue(
                    name=f"RSI_{period}",
                    value=None,
                    is_available=False,
                    as_of_timestamp=candle.timestamp,
                    period=period,
                    ticker=candle.ticker,
                    interval=candle.interval,
                    reason=f"Insufficient warm-up data: 1 bar available, {period + 1} required.",
                )
            )
            continue

        change = candle.close - candles[i - 1].close
        gain = change if change > Decimal("0.0") else Decimal("0.0")
        loss = -change if change < Decimal("0.0") else Decimal("0.0")

        if i < period:
            gains.append(gain)
            losses.append(loss)
            results.append(
                SingleIndicatorValue(
                    name=f"RSI_{period}",
                    value=None,
                    is_available=False,
                    as_of_timestamp=candle.timestamp,
                    period=period,
                    ticker=candle.ticker,
                    interval=candle.interval,
                    reason=f"Insufficient warm-up data: {i + 1} bars available, {period + 1} required.",
                )
            )
        elif i == period:
            gains.append(gain)
            losses.append(loss)
            avg_gain = sum(gains) / period_dec
            avg_loss = sum(losses) / period_dec

            # Handle flat market or extreme values
            if avg_gain == Decimal("0.0") and avg_loss == Decimal("0.0"):
                rsi_raw = Decimal("50.0")
            elif avg_loss == Decimal("0.0"):
                rsi_raw = Decimal("100.0")
            elif avg_gain == Decimal("0.0"):
                rsi_raw = Decimal("0.0")
            else:
                rsi_raw = Decimal("100.0") * avg_gain / (avg_gain + avg_loss)

            results.append(
                SingleIndicatorValue(
                    name=f"RSI_{period}",
                    value=quantize_indicator(rsi_raw, precision),
                    is_available=True,
                    as_of_timestamp=candle.timestamp,
                    period=period,
                    ticker=candle.ticker,
                    interval=candle.interval,
                    reason="",
                )
            )
        else:
            assert avg_gain is not None and avg_loss is not None
            avg_gain = ((avg_gain * period_minus_one_dec) + gain) / period_dec
            avg_loss = ((avg_loss * period_minus_one_dec) + loss) / period_dec

            if avg_gain == Decimal("0.0") and avg_loss == Decimal("0.0"):
                rsi_raw = Decimal("50.0")
            elif avg_loss == Decimal("0.0"):
                rsi_raw = Decimal("100.0")
            elif avg_gain == Decimal("0.0"):
                rsi_raw = Decimal("0.0")
            else:
                rsi_raw = Decimal("100.0") * avg_gain / (avg_gain + avg_loss)

            results.append(
                SingleIndicatorValue(
                    name=f"RSI_{period}",
                    value=quantize_indicator(rsi_raw, precision),
                    is_available=True,
                    as_of_timestamp=candle.timestamp,
                    period=period,
                    ticker=candle.ticker,
                    interval=candle.interval,
                    reason="",
                )
            )

    return results


def calculate_rsi(
    candles: Sequence[OHLCVCandle],
    period: int = 14,
    precision: Decimal = DEFAULT_INDICATOR_PRECISION,
    require_complete: bool = True,
    validate: bool = True,
) -> SingleIndicatorValue:
    """Calculate RSI for the terminal candle in the sequence."""
    series = calculate_rsi_series(
        candles,
        period=period,
        precision=precision,
        require_complete=require_complete,
        validate=validate,
    )
    return series[-1]


# ---------------------------------------------------------------------------
# Moving Average Convergence Divergence (MACD)
# ---------------------------------------------------------------------------

def calculate_macd_series(
    candles: Sequence[OHLCVCandle],
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
    precision: Decimal = DEFAULT_INDICATOR_PRECISION,
    require_complete: bool = True,
    validate: bool = True,
) -> List[MACDResult]:
    """Calculate MACD series (line, signal, and histogram) in pure Decimal."""
    if fast_period <= 0 or slow_period <= 0 or signal_period <= 0:
        raise ValueError("MACD periods must be positive integers.")
    if fast_period >= slow_period:
        raise ValueError(f"Fast period ({fast_period}) must be strictly less than slow period ({slow_period}).")
    if validate:
        validate_indicator_candles(candles, min_count=1, require_complete=require_complete)

    # 1. Compute high-precision EMA series for fast and slow
    alpha_fast = Decimal(2) / Decimal(fast_period + 1)
    one_minus_alpha_fast = Decimal(1) - alpha_fast
    alpha_slow = Decimal(2) / Decimal(slow_period + 1)
    one_minus_alpha_slow = Decimal(1) - alpha_slow

    fast_emas: List[Optional[Decimal]] = []
    slow_emas: List[Optional[Decimal]] = []

    curr_fast_ema: Optional[Decimal] = None
    curr_slow_ema: Optional[Decimal] = None
    fast_sum = Decimal("0.0")
    slow_sum = Decimal("0.0")

    for i, candle in enumerate(candles):
        # Fast EMA
        if i + 1 < fast_period:
            fast_sum += candle.close
            fast_emas.append(None)
        elif i + 1 == fast_period:
            fast_sum += candle.close
            curr_fast_ema = fast_sum / Decimal(fast_period)
            fast_emas.append(curr_fast_ema)
        else:
            assert curr_fast_ema is not None
            curr_fast_ema = (candle.close * alpha_fast) + (curr_fast_ema * one_minus_alpha_fast)
            fast_emas.append(curr_fast_ema)

        # Slow EMA
        if i + 1 < slow_period:
            slow_sum += candle.close
            slow_emas.append(None)
        elif i + 1 == slow_period:
            slow_sum += candle.close
            curr_slow_ema = slow_sum / Decimal(slow_period)
            slow_emas.append(curr_slow_ema)
        else:
            assert curr_slow_ema is not None
            curr_slow_ema = (candle.close * alpha_slow) + (curr_slow_ema * one_minus_alpha_slow)
            slow_emas.append(curr_slow_ema)

    # 2. Compute raw MACD line = fast EMA - slow EMA
    raw_macd_lines: List[Optional[Decimal]] = []
    for i in range(len(candles)):
        f = fast_emas[i]
        s = slow_emas[i]
        if f is not None and s is not None:
            raw_macd_lines.append(f - s)
        else:
            raw_macd_lines.append(None)

    # 3. Compute Signal line = EMA(raw_macd_lines, signal_period)
    alpha_sig = Decimal(2) / Decimal(signal_period + 1)
    one_minus_alpha_sig = Decimal(1) - alpha_sig

    raw_signal_lines: List[Optional[Decimal]] = []
    curr_sig_ema: Optional[Decimal] = None
    sig_window: List[Decimal] = []

    for i in range(len(candles)):
        m = raw_macd_lines[i]
        if m is None:
            raw_signal_lines.append(None)
            continue

        if len(sig_window) + 1 < signal_period:
            sig_window.append(m)
            raw_signal_lines.append(None)
        elif len(sig_window) + 1 == signal_period:
            sig_window.append(m)
            curr_sig_ema = sum(sig_window) / Decimal(signal_period)
            raw_signal_lines.append(curr_sig_ema)
        else:
            assert curr_sig_ema is not None
            curr_sig_ema = (m * alpha_sig) + (curr_sig_ema * one_minus_alpha_sig)
            raw_signal_lines.append(curr_sig_ema)

    # 4. Assemble MACDResults
    results: List[MACDResult] = []
    total_warmup = slow_period + signal_period - 1

    for i, candle in enumerate(candles):
        m = raw_macd_lines[i]
        s = raw_signal_lines[i]
        h = (m - s) if (m is not None and s is not None) else None

        is_avail = (m is not None and s is not None and h is not None)
        if is_avail:
            reason = ""
        elif m is None:
            reason = f"Insufficient data for slow EMA ({slow_period} bars required, got {i + 1})."
        elif s is None:
            valid_m_count = i + 1 - (slow_period - 1)
            reason = f"Insufficient data for signal line ({signal_period} MACD points required, got {valid_m_count})."
        else:
            reason = "Calculation unavailable."

        results.append(
            MACDResult(
                macd_line=quantize_indicator(m, precision),
                signal_line=quantize_indicator(s, precision),
                histogram=quantize_indicator(h, precision),
                is_available=is_avail,
                as_of_timestamp=candle.timestamp,
                fast_period=fast_period,
                slow_period=slow_period,
                signal_period=signal_period,
                ticker=candle.ticker,
                interval=candle.interval,
                reason=reason,
            )
        )

    return results


def calculate_macd(
    candles: Sequence[OHLCVCandle],
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
    precision: Decimal = DEFAULT_INDICATOR_PRECISION,
    require_complete: bool = True,
    validate: bool = True,
) -> MACDResult:
    """Calculate MACD for the terminal candle in the sequence."""
    series = calculate_macd_series(
        candles,
        fast_period=fast_period,
        slow_period=slow_period,
        signal_period=signal_period,
        precision=precision,
        require_complete=require_complete,
        validate=validate,
    )
    return series[-1]


# ---------------------------------------------------------------------------
# Average True Range (ATR - Wilder's Smoothing)
# ---------------------------------------------------------------------------

def calculate_atr_series(
    candles: Sequence[OHLCVCandle],
    period: int = 14,
    precision: Decimal = DEFAULT_INDICATOR_PRECISION,
    require_complete: bool = True,
    validate: bool = True,
) -> List[SingleIndicatorValue]:
    """Calculate ATR series using Wilder's smoothing with full gap awareness."""
    if period <= 0:
        raise ValueError(f"ATR period must be positive integer, got {period}.")
    if validate:
        validate_indicator_candles(candles, min_count=1, require_complete=require_complete)

    results: List[SingleIndicatorValue] = []
    true_ranges: List[Decimal] = []

    curr_atr: Optional[Decimal] = None
    period_dec = Decimal(period)
    period_minus_one_dec = Decimal(period - 1)

    for i, candle in enumerate(candles):
        if i == 0:
            tr = candle.high - candle.low
            true_ranges.append(tr)
            results.append(
                SingleIndicatorValue(
                    name=f"ATR_{period}",
                    value=None,
                    is_available=False,
                    as_of_timestamp=candle.timestamp,
                    period=period,
                    ticker=candle.ticker,
                    interval=candle.interval,
                    reason=f"Insufficient warm-up data: 1 bar available, {period + 1} required.",
                )
            )
            continue

        prev_close = candles[i - 1].close
        h_minus_l = candle.high - candle.low
        h_minus_pc = abs(candle.high - prev_close)
        l_minus_pc = abs(candle.low - prev_close)
        tr = max(h_minus_l, h_minus_pc, l_minus_pc)
        true_ranges.append(tr)

        if i < period:
            results.append(
                SingleIndicatorValue(
                    name=f"ATR_{period}",
                    value=None,
                    is_available=False,
                    as_of_timestamp=candle.timestamp,
                    period=period,
                    ticker=candle.ticker,
                    interval=candle.interval,
                    reason=f"Insufficient warm-up data: {i + 1} bars available, {period + 1} required.",
                )
            )
        elif i == period:
            # Seed ATR: average of true ranges from index 1 to period (14 ranges with prior close)
            curr_atr = sum(true_ranges[1 : period + 1]) / period_dec
            results.append(
                SingleIndicatorValue(
                    name=f"ATR_{period}",
                    value=quantize_indicator(curr_atr, precision),
                    is_available=True,
                    as_of_timestamp=candle.timestamp,
                    period=period,
                    ticker=candle.ticker,
                    interval=candle.interval,
                    reason="",
                )
            )
        else:
            assert curr_atr is not None
            curr_atr = ((curr_atr * period_minus_one_dec) + tr) / period_dec
            results.append(
                SingleIndicatorValue(
                    name=f"ATR_{period}",
                    value=quantize_indicator(curr_atr, precision),
                    is_available=True,
                    as_of_timestamp=candle.timestamp,
                    period=period,
                    ticker=candle.ticker,
                    interval=candle.interval,
                    reason="",
                )
            )

    return results


def calculate_atr(
    candles: Sequence[OHLCVCandle],
    period: int = 14,
    precision: Decimal = DEFAULT_INDICATOR_PRECISION,
    require_complete: bool = True,
    validate: bool = True,
) -> SingleIndicatorValue:
    """Calculate ATR for the terminal candle in the sequence."""
    series = calculate_atr_series(
        candles,
        period=period,
        precision=precision,
        require_complete=require_complete,
        validate=validate,
    )
    return series[-1]


# ---------------------------------------------------------------------------
# Bollinger Bands
# ---------------------------------------------------------------------------

def calculate_bollinger_bands_series(
    candles: Sequence[OHLCVCandle],
    period: int = 20,
    num_std_dev: Decimal = Decimal("2.0"),
    precision: Decimal = DEFAULT_INDICATOR_PRECISION,
    require_complete: bool = True,
    validate: bool = True,
) -> List[BollingerBandsResult]:
    """Calculate Bollinger Bands series using pure Decimal arithmetic and Decimal.sqrt()."""
    if period <= 0:
        raise ValueError(f"Bollinger Bands period must be positive integer, got {period}.")
    if num_std_dev <= Decimal("0.0"):
        raise ValueError(f"Bollinger Bands num_std_dev must be positive, got {num_std_dev}.")
    if validate:
        validate_indicator_candles(candles, min_count=1, require_complete=require_complete)

    results: List[BollingerBandsResult] = []
    period_dec = Decimal(period)

    for i, candle in enumerate(candles):
        if i + 1 < period:
            results.append(
                BollingerBandsResult(
                    upper=None,
                    middle=None,
                    lower=None,
                    bandwidth=None,
                    percent_b=None,
                    is_available=False,
                    as_of_timestamp=candle.timestamp,
                    period=period,
                    num_std_dev=num_std_dev,
                    ticker=candle.ticker,
                    interval=candle.interval,
                    reason=f"Insufficient warm-up data: {i + 1} bars available, {period} required.",
                )
            )
            continue

        window = candles[i - period + 1 : i + 1]
        closes = [c.close for c in window]
        middle_raw = sum(closes) / period_dec

        # Pure Decimal population variance and standard deviation
        variance = sum((c - middle_raw) ** 2 for c in closes) / period_dec
        if variance < Decimal("0.0"):
            variance = Decimal("0.0")
        std_dev = variance.sqrt()

        upper_raw = middle_raw + (num_std_dev * std_dev)
        lower_raw = middle_raw - (num_std_dev * std_dev)

        # Bandwidth = (Upper - Lower) / Middle
        if middle_raw == Decimal("0.0"):
            bandwidth_raw = Decimal("0.0")
        else:
            bandwidth_raw = (upper_raw - lower_raw) / middle_raw

        # %B = (Close - Lower) / (Upper - Lower)
        band_diff = upper_raw - lower_raw
        if band_diff == Decimal("0.0"):
            percent_b_raw = Decimal("0.5")  # Centered at middle when bands collapse
        else:
            percent_b_raw = (candle.close - lower_raw) / band_diff

        results.append(
            BollingerBandsResult(
                upper=quantize_indicator(upper_raw, precision),
                middle=quantize_indicator(middle_raw, precision),
                lower=quantize_indicator(lower_raw, precision),
                bandwidth=quantize_indicator(bandwidth_raw, precision),
                percent_b=quantize_indicator(percent_b_raw, precision),
                is_available=True,
                as_of_timestamp=candle.timestamp,
                period=period,
                num_std_dev=num_std_dev,
                ticker=candle.ticker,
                interval=candle.interval,
                reason="",
            )
        )

    return results


def calculate_bollinger_bands(
    candles: Sequence[OHLCVCandle],
    period: int = 20,
    num_std_dev: Decimal = Decimal("2.0"),
    precision: Decimal = DEFAULT_INDICATOR_PRECISION,
    require_complete: bool = True,
    validate: bool = True,
) -> BollingerBandsResult:
    """Calculate Bollinger Bands for the terminal candle in the sequence."""
    series = calculate_bollinger_bands_series(
        candles,
        period=period,
        num_std_dev=num_std_dev,
        precision=precision,
        require_complete=require_complete,
        validate=validate,
    )
    return series[-1]


# ---------------------------------------------------------------------------
# Volume & Relative Volume (RVOL)
# ---------------------------------------------------------------------------

def calculate_average_volume_series(
    candles: Sequence[OHLCVCandle],
    period: int = 20,
    precision: Decimal = DEFAULT_VOLUME_PRECISION,
    require_complete: bool = True,
    validate: bool = True,
) -> List[SingleIndicatorValue]:
    """Calculate Average Volume series over period bars."""
    if period <= 0:
        raise ValueError(f"Average volume period must be positive integer, got {period}.")
    if validate:
        validate_indicator_candles(candles, min_count=1, require_complete=require_complete)

    results: List[SingleIndicatorValue] = []
    running_vol_sum = Decimal("0")

    for i, candle in enumerate(candles):
        running_vol_sum += Decimal(candle.volume)
        if i >= period:
            running_vol_sum -= Decimal(candles[i - period].volume)

        if i + 1 < period:
            results.append(
                SingleIndicatorValue(
                    name=f"AVG_VOL_{period}",
                    value=None,
                    is_available=False,
                    as_of_timestamp=candle.timestamp,
                    period=period,
                    ticker=candle.ticker,
                    interval=candle.interval,
                    reason=f"Insufficient warm-up data: {i + 1} bars available, {period} required.",
                )
            )
        else:
            avg_vol = running_vol_sum / Decimal(period)
            results.append(
                SingleIndicatorValue(
                    name=f"AVG_VOL_{period}",
                    value=quantize_indicator(avg_vol, precision),
                    is_available=True,
                    as_of_timestamp=candle.timestamp,
                    period=period,
                    ticker=candle.ticker,
                    interval=candle.interval,
                    reason="",
                )
            )

    return results


def calculate_average_volume(
    candles: Sequence[OHLCVCandle],
    period: int = 20,
    precision: Decimal = DEFAULT_VOLUME_PRECISION,
    require_complete: bool = True,
    validate: bool = True,
) -> SingleIndicatorValue:
    """Calculate Average Volume for the terminal candle in the sequence."""
    series = calculate_average_volume_series(
        candles,
        period=period,
        precision=precision,
        require_complete=require_complete,
        validate=validate,
    )
    return series[-1]


def calculate_relative_volume_series(
    candles: Sequence[OHLCVCandle],
    period: int = 20,
    precision: Decimal = DEFAULT_INDICATOR_PRECISION,
    require_complete: bool = True,
    validate: bool = True,
) -> List[SingleIndicatorValue]:
    """Calculate Relative Volume (RVOL) series benchmarked against previous period completed bars.

    Formula:
        RVOL(t) = Volume(t) / AverageVolume(t-period ... t-1)
        where AverageVolume = sum(previous period completed candle volumes) / period

    The current candle's volume is strictly EXCLUDED from its own baseline denominator.
    Warm-up requirement:
        period + 1 completed candles (e.g., 21 completed candles for period=20).
    """
    if period <= 0:
        raise ValueError(f"RVOL period must be positive integer, got {period}.")
    if validate:
        validate_indicator_candles(candles, min_count=1, require_complete=require_complete)

    # Average volume series computed with high intermediate precision
    avg_vol_series = calculate_average_volume_series(
        candles,
        period=period,
        precision=Decimal("0.00000001"),
        require_complete=require_complete,
        validate=False,
    )

    results: List[SingleIndicatorValue] = []

    for i, candle in enumerate(candles):
        # Exclude current candle from denominator: baseline is previous candle's rolling average
        if i < period:
            results.append(
                SingleIndicatorValue(
                    name=f"RVOL_{period}",
                    value=None,
                    is_available=False,
                    as_of_timestamp=candle.timestamp,
                    period=period,
                    ticker=candle.ticker,
                    interval=candle.interval,
                    reason=f"Insufficient warm-up data: {i + 1} bars available, {period + 1} required.",
                )
            )
            continue

        prior_avg_item = avg_vol_series[i - 1]
        if not prior_avg_item.is_available or prior_avg_item.value is None:
            results.append(
                SingleIndicatorValue(
                    name=f"RVOL_{period}",
                    value=None,
                    is_available=False,
                    as_of_timestamp=candle.timestamp,
                    period=period,
                    ticker=candle.ticker,
                    interval=candle.interval,
                    reason=prior_avg_item.reason,
                )
            )
            continue

        curr_vol = Decimal(candle.volume)
        prior_avg_vol = prior_avg_item.value

        if prior_avg_vol == Decimal("0.0"):
            if curr_vol == Decimal("0.0"):
                rvol_raw = Decimal("0.0")
                is_avail = True
                reason = "Zero volume with zero previous average volume."
            else:
                rvol_raw = None
                is_avail = False
                reason = "Undefined RVOL: previous average volume is zero with non-zero current volume."
        else:
            rvol_raw = curr_vol / prior_avg_vol
            is_avail = True
            reason = ""

        results.append(
            SingleIndicatorValue(
                name=f"RVOL_{period}",
                value=quantize_indicator(rvol_raw, precision),
                is_available=is_avail,
                as_of_timestamp=candle.timestamp,
                period=period,
                ticker=candle.ticker,
                interval=candle.interval,
                reason=reason,
            )
        )

    return results


def calculate_relative_volume(
    candles: Sequence[OHLCVCandle],
    period: int = 20,
    precision: Decimal = DEFAULT_INDICATOR_PRECISION,
    require_complete: bool = True,
    validate: bool = True,
) -> SingleIndicatorValue:
    """Calculate Relative Volume (RVOL) for the terminal candle benchmarked against previous period bars."""
    series = calculate_relative_volume_series(
        candles,
        period=period,
        precision=precision,
        require_complete=require_complete,
        validate=validate,
    )
    return series[-1]


# ---------------------------------------------------------------------------
# Consolidated Technical Indicators Engine
# ---------------------------------------------------------------------------

class TechnicalIndicatorsEngine:
    """Deterministic, pure-Decimal technical indicator calculation engine for trader-swing."""

    def __init__(self, precision: Decimal = DEFAULT_INDICATOR_PRECISION):
        self.precision = precision

    def compute_all(
        self,
        candles: Sequence[OHLCVCandle],
        require_complete: bool = True,
        current_time: Optional[datetime] = None,
    ) -> TechnicalIndicatorSnapshot:
        """Compute all standard indicators for the terminal candle in the sequence.

        Validates sequence once and evaluates:
        - SMA: 20, 50, 200
        - EMA: 9, 20, 50
        - Momentum: RSI 14, MACD (12, 26, 9)
        - Volatility: ATR 14, Bollinger Bands (20, 2.0)
        - Volume: Average Volume 20, RVOL 20
        """
        validate_indicator_candles(candles, min_count=1, require_complete=require_complete, current_time=current_time)

        terminal = candles[-1]
        ticker = terminal.ticker
        interval = terminal.interval
        as_of = terminal.timestamp
        bar_count = len(candles)

        # Trend Indicators
        sma_20 = calculate_sma(candles, period=20, precision=self.precision, require_complete=require_complete, validate=False)
        sma_50 = calculate_sma(candles, period=50, precision=self.precision, require_complete=require_complete, validate=False)
        sma_200 = calculate_sma(candles, period=200, precision=self.precision, require_complete=require_complete, validate=False)

        ema_9 = calculate_ema(candles, period=9, precision=self.precision, require_complete=require_complete, validate=False)
        ema_20 = calculate_ema(candles, period=20, precision=self.precision, require_complete=require_complete, validate=False)
        ema_50 = calculate_ema(candles, period=50, precision=self.precision, require_complete=require_complete, validate=False)

        # Momentum Indicators
        rsi_14 = calculate_rsi(candles, period=14, precision=self.precision, require_complete=require_complete, validate=False)
        macd = calculate_macd(candles, fast_period=12, slow_period=26, signal_period=9, precision=self.precision, require_complete=require_complete, validate=False)

        # Volatility Indicators
        atr_14 = calculate_atr(candles, period=14, precision=self.precision, require_complete=require_complete, validate=False)
        bollinger_bands = calculate_bollinger_bands(candles, period=20, num_std_dev=Decimal("2.0"), precision=self.precision, require_complete=require_complete, validate=False)

        # Volume Indicators
        average_volume_20 = calculate_average_volume(candles, period=20, precision=DEFAULT_VOLUME_PRECISION, require_complete=require_complete, validate=False)
        relative_volume = calculate_relative_volume(candles, period=20, precision=self.precision, require_complete=require_complete, validate=False)

        return TechnicalIndicatorSnapshot(
            ticker=ticker,
            interval=interval,
            as_of_timestamp=as_of,
            bar_count=bar_count,
            sma_20=sma_20,
            sma_50=sma_50,
            sma_200=sma_200,
            ema_9=ema_9,
            ema_20=ema_20,
            ema_50=ema_50,
            rsi_14=rsi_14,
            macd=macd,
            atr_14=atr_14,
            bollinger_bands=bollinger_bands,
            average_volume_20=average_volume_20,
            relative_volume=relative_volume,
        )

    def compute_series(
        self,
        candles: Sequence[OHLCVCandle],
        require_complete: bool = True,
    ) -> List[TechnicalIndicatorSnapshot]:
        """Compute all standard indicators across every candle in the sequence."""
        validate_indicator_candles(candles, min_count=1, require_complete=require_complete)

        # Calculate all individual series
        sma_20_s = calculate_sma_series(candles, period=20, precision=self.precision, require_complete=require_complete, validate=False)
        sma_50_s = calculate_sma_series(candles, period=50, precision=self.precision, require_complete=require_complete, validate=False)
        sma_200_s = calculate_sma_series(candles, period=200, precision=self.precision, require_complete=require_complete, validate=False)

        ema_9_s = calculate_ema_series(candles, period=9, precision=self.precision, require_complete=require_complete, validate=False)
        ema_20_s = calculate_ema_series(candles, period=20, precision=self.precision, require_complete=require_complete, validate=False)
        ema_50_s = calculate_ema_series(candles, period=50, precision=self.precision, require_complete=require_complete, validate=False)

        rsi_14_s = calculate_rsi_series(candles, period=14, precision=self.precision, require_complete=require_complete, validate=False)
        macd_s = calculate_macd_series(candles, fast_period=12, slow_period=26, signal_period=9, precision=self.precision, require_complete=require_complete, validate=False)

        atr_14_s = calculate_atr_series(candles, period=14, precision=self.precision, require_complete=require_complete, validate=False)
        bb_s = calculate_bollinger_bands_series(candles, period=20, num_std_dev=Decimal("2.0"), precision=self.precision, require_complete=require_complete, validate=False)

        avg_vol_20_s = calculate_average_volume_series(candles, period=20, precision=DEFAULT_VOLUME_PRECISION, require_complete=require_complete, validate=False)
        rvol_s = calculate_relative_volume_series(candles, period=20, precision=self.precision, require_complete=require_complete, validate=False)

        snapshots: List[TechnicalIndicatorSnapshot] = []
        for i, candle in enumerate(candles):
            snapshots.append(
                TechnicalIndicatorSnapshot(
                    ticker=candle.ticker,
                    interval=candle.interval,
                    as_of_timestamp=candle.timestamp,
                    bar_count=i + 1,
                    sma_20=sma_20_s[i],
                    sma_50=sma_50_s[i],
                    sma_200=sma_200_s[i],
                    ema_9=ema_9_s[i],
                    ema_20=ema_20_s[i],
                    ema_50=ema_50_s[i],
                    rsi_14=rsi_14_s[i],
                    macd=macd_s[i],
                    atr_14=atr_14_s[i],
                    bollinger_bands=bb_s[i],
                    average_volume_20=avg_vol_20_s[i],
                    relative_volume=rvol_s[i],
                )
            )

        return snapshots
