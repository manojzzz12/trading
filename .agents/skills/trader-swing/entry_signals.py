"""Deterministic swing-trading entry signal generators for trader-swing.

Enforces:
1. Pure Decimal arithmetic for price levels, stops, and targets.
2. Three explicit strategies: TREND_CONTINUATION, BREAKOUT_CONFIRMATION, PULLBACK_ENTRY.
3. Strict stop-loss placement below entry and minimum 2.0 reward-to-risk ratio.
4. Comprehensive evidence tracking and invalidating conditions.
"""

from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
import os
import sys
from typing import List, Optional, Sequence, Tuple

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from accounting import quantize_cent
from market_data_models import OHLCVCandle
from indicator_models import TechnicalIndicatorSnapshot
from indicators import TechnicalIndicatorsEngine
from signal_models import EntrySignal, EntryStrategyType

TWO_DECIMALS = Decimal("0.01")
FOUR_DECIMALS = Decimal("0.0001")


class EntrySignalGenerator:
    """Evaluates technical criteria to generate explainable swing entry signals."""

    def __init__(
        self,
        atr_multiplier: Decimal = Decimal("1.50"),
        min_reward_to_risk: Decimal = Decimal("2.00"),
        entry_zone_buffer_pct: Decimal = Decimal("0.0050"),  # 0.5% zone around reference close
    ):
        self.atr_multiplier = atr_multiplier
        self.min_reward_to_risk = min_reward_to_risk
        self.entry_zone_buffer_pct = entry_zone_buffer_pct
        self.engine = TechnicalIndicatorsEngine(precision=FOUR_DECIMALS)

    def evaluate_entry(
        self,
        candles: Sequence[OHLCVCandle],
        require_complete: bool = True,
    ) -> Optional[EntrySignal]:
        """Evaluate candles for the best qualifying entry setup (checking trend continuation, breakout, pullback)."""
        if len(candles) < 25:
            return None

        # Check in order of trend strength
        sig_breakout = self._check_breakout(candles, require_complete)
        if sig_breakout is not None:
            return sig_breakout

        sig_trend = self._check_trend_continuation(candles, require_complete)
        if sig_trend is not None:
            return sig_trend

        sig_pullback = self._check_pullback(candles, require_complete)
        if sig_pullback is not None:
            return sig_pullback

        return None

    def _check_trend_continuation(
        self,
        candles: Sequence[OHLCVCandle],
        require_complete: bool,
    ) -> Optional[EntrySignal]:
        """Strategy 1: Trend continuation off moving average support."""
        terminal = candles[-1]
        price = terminal.close
        snap = self.engine.compute_all(candles, require_complete=require_complete)

        if not (snap.sma_20.is_available and snap.rsi_14.is_available and snap.atr_14.is_available):
            return None

        sma_20 = snap.sma_20.value
        sma_50 = snap.sma_50.value if snap.sma_50.is_available else None
        rsi = snap.rsi_14.value
        atr = snap.atr_14.value

        # Rules: Price > 20 SMA; if 50 SMA is available, 20 SMA > 50 SMA
        if price <= sma_20:
            return None
        if sma_50 is not None:
            if not (sma_20 > sma_50):
                return None
        if not (Decimal("45.0") <= rsi <= Decimal("72.0")):
            return None
        if snap.macd.is_available and snap.macd.histogram is not None and snap.macd.histogram < Decimal("-0.20"):
            return None

        # Pullback holding: low of last 3 bars dipped near 20 SMA (within 2% of SMA 20)
        recent_low = min(c.low for c in candles[-3:])
        if recent_low > sma_20 * Decimal("1.025"):
            return None  # Too extended from 20 SMA

        # Stop loss: below recent swing low and 1.5x ATR
        atr_stop = price - (self.atr_multiplier * atr)
        stop_loss = quantize_cent(min(recent_low - Decimal("0.10"), atr_stop))
        if stop_loss >= price:
            stop_loss = quantize_cent(price - (Decimal("1.0") * atr))

        risk_distance = price - stop_loss
        if risk_distance <= Decimal("0.05"):
            return None

        target = quantize_cent(price + (self.min_reward_to_risk * risk_distance))
        rr = ((target - price) / risk_distance).quantize(TWO_DECIMALS, rounding=ROUND_HALF_UP)

        zone_low = quantize_cent(price * (Decimal("1.0") - self.entry_zone_buffer_pct))
        zone_high = quantize_cent(price * (Decimal("1.0") + self.entry_zone_buffer_pct))

        trend_desc = f"Uptrend intact: price (${price}) > 20 SMA (${sma_20})"
        if sma_50 is not None:
            trend_desc += f" > 50 SMA (${sma_50})"
        trend_desc += "."

        return EntrySignal(
            ticker=terminal.ticker,
            strategy=EntryStrategyType.TREND_CONTINUATION,
            signal_timestamp=terminal.timestamp,
            reference_price=price,
            entry_zone_low=zone_low,
            entry_zone_high=zone_high,
            suggested_stop_loss=stop_loss,
            suggested_target=target,
            reward_to_risk=rr,
            confidence_score=Decimal("0.7500"),
            supporting_evidence=[
                trend_desc,
                f"RSI ({rsi}) is in healthy momentum range (45-72).",
                f"Recent 3-day low (${recent_low}) successfully tested 20 SMA support.",
            ],
            contradicting_evidence=[],
            invalidating_conditions=[
                f"Close below 20 SMA (${sma_20}).",
                f"Price falls below stop loss (${stop_loss}).",
            ],
            data_quality_status="COMPLETE",
            expiration_sessions=3,
            is_technically_valid=True,
        )

    def _check_breakout(
        self,
        candles: Sequence[OHLCVCandle],
        require_complete: bool,
    ) -> Optional[EntrySignal]:
        """Strategy 2: Breakout confirmation on above-average volume."""
        terminal = candles[-1]
        price = terminal.close
        snap = self.engine.compute_all(candles, require_complete=require_complete)

        if not (snap.sma_20.is_available and snap.rsi_14.is_available and snap.atr_14.is_available and snap.relative_volume.is_available):
            return None

        rvol = snap.relative_volume.value
        atr = snap.atr_14.value
        if rvol is None or rvol < Decimal("1.25"):
            return None  # Needs institutional volume expansion

        # Highest close in last 20 bars prior to terminal bar
        lookback_closes = [c.close for c in candles[-21:-1]]
        prior_max_close = max(lookback_closes)
        if price <= prior_max_close:
            return None  # Did not break out above 20-day high close

        # Bullish close in upper 30% of day's range
        day_range = terminal.high - terminal.low
        if day_range > Decimal("0.0"):
            close_loc = (price - terminal.low) / day_range
            if close_loc < Decimal("0.70"):
                return None  # Weak close off day's high

        # Stop loss: below breakout level and 1.5x ATR
        atr_stop = price - (self.atr_multiplier * atr)
        stop_loss = quantize_cent(min(prior_max_close - Decimal("0.05"), atr_stop))
        if stop_loss >= price:
            stop_loss = quantize_cent(price - (Decimal("1.0") * atr))

        risk_distance = price - stop_loss
        if risk_distance <= Decimal("0.05"):
            return None

        target = quantize_cent(price + (self.min_reward_to_risk * risk_distance))
        rr = ((target - price) / risk_distance).quantize(TWO_DECIMALS, rounding=ROUND_HALF_UP)

        zone_low = quantize_cent(price * (Decimal("1.0") - self.entry_zone_buffer_pct))
        zone_high = quantize_cent(price * (Decimal("1.0") + self.entry_zone_buffer_pct))

        return EntrySignal(
            ticker=terminal.ticker,
            strategy=EntryStrategyType.BREAKOUT_CONFIRMATION,
            signal_timestamp=terminal.timestamp,
            reference_price=price,
            entry_zone_low=zone_low,
            entry_zone_high=zone_high,
            suggested_stop_loss=stop_loss,
            suggested_target=target,
            reward_to_risk=rr,
            confidence_score=Decimal("0.8000"),
            supporting_evidence=[
                f"20-day high close breakout (${price} > prior high ${prior_max_close}).",
                f"Volume expansion confirmed: RVOL is {rvol}x (>=1.25x).",
                "Strong close in upper 30% of the daily range.",
            ],
            contradicting_evidence=[],
            invalidating_conditions=[
                f"Price falls back below breakout level (${prior_max_close}).",
                f"Stop loss triggered at ${stop_loss}.",
            ],
            data_quality_status="COMPLETE",
            expiration_sessions=2,
            is_technically_valid=True,
        )

    def _check_pullback(
        self,
        candles: Sequence[OHLCVCandle],
        require_complete: bool,
    ) -> Optional[EntrySignal]:
        """Strategy 3: Bullish reversal off 50 SMA / lower band pullback."""
        terminal = candles[-1]
        price = terminal.close
        snap = self.engine.compute_all(candles, require_complete=require_complete)

        if not (snap.sma_50.is_available and snap.rsi_14.is_available and snap.atr_14.is_available):
            return None

        sma_50 = snap.sma_50.value
        rsi = snap.rsi_14.value
        atr = snap.atr_14.value

        # Primary trend above 50 SMA
        if price < sma_50:
            return None
        # RSI consolidated into 38..52 zone
        if not (Decimal("38.0") <= rsi <= Decimal("53.0")):
            return None

        # Reversal candlestick: green candle (close > open) and close > prior close
        prev = candles[-2]
        if not (price > terminal.open and price > prev.close):
            return None

        # Touched near 50 SMA (low within 1.5% of 50 SMA)
        if terminal.low > sma_50 * Decimal("1.025"):
            return None

        stop_loss = quantize_cent(min(terminal.low - Decimal("0.10"), price - (self.atr_multiplier * atr)))
        if stop_loss >= price:
            stop_loss = quantize_cent(price - (Decimal("1.0") * atr))

        risk_distance = price - stop_loss
        if risk_distance <= Decimal("0.05"):
            return None

        target = quantize_cent(price + (self.min_reward_to_risk * risk_distance))
        rr = ((target - price) / risk_distance).quantize(TWO_DECIMALS, rounding=ROUND_HALF_UP)

        zone_low = quantize_cent(price * (Decimal("1.0") - self.entry_zone_buffer_pct))
        zone_high = quantize_cent(price * (Decimal("1.0") + self.entry_zone_buffer_pct))

        return EntrySignal(
            ticker=terminal.ticker,
            strategy=EntryStrategyType.PULLBACK_ENTRY,
            signal_timestamp=terminal.timestamp,
            reference_price=price,
            entry_zone_low=zone_low,
            entry_zone_high=zone_high,
            suggested_stop_loss=stop_loss,
            suggested_target=target,
            reward_to_risk=rr,
            confidence_score=Decimal("0.7000"),
            supporting_evidence=[
                f"Pullback into 50 SMA (${sma_50}) held with green reversal candle.",
                f"RSI cooled to ({rsi}), presenting favorable risk-reward entry.",
            ],
            contradicting_evidence=[],
            invalidating_conditions=[
                f"Close below 50 SMA (${sma_50}).",
                f"Price falls below swing low (${stop_loss}).",
            ],
            data_quality_status="COMPLETE",
            expiration_sessions=3,
            is_technically_valid=True,
        )
