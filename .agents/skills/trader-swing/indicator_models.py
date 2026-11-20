"""Data models and container structures for technical indicators in trader-swing.

Enforces:
1. Pure Decimal indicator values with deterministic precision.
2. Explicit availability status distinguishing insufficient historical data from numeric zero.
3. Strict as-of timestamps tracing the exact latest candle used.
4. Ticker, interval, and warm-up metadata persistence.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional


class IndicatorStatus(str, Enum):
    """Availability status of an indicator calculation."""
    AVAILABLE = "AVAILABLE"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    ERROR = "ERROR"


@dataclass(frozen=True)
class SingleIndicatorValue:
    """Normalized output for scalar indicators (SMA, EMA, RSI, ATR, Volume)."""
    name: str
    value: Optional[Decimal]
    is_available: bool
    as_of_timestamp: Optional[datetime]
    period: int
    ticker: str
    interval: str
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "value": str(self.value) if self.value is not None else None,
            "is_available": self.is_available,
            "as_of_timestamp": self.as_of_timestamp.isoformat() if self.as_of_timestamp else None,
            "period": self.period,
            "ticker": self.ticker,
            "interval": self.interval,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class MACDResult:
    """Structured output for MACD indicator containing line, signal, and histogram."""
    macd_line: Optional[Decimal]
    signal_line: Optional[Decimal]
    histogram: Optional[Decimal]
    is_available: bool
    as_of_timestamp: Optional[datetime]
    fast_period: int = 12
    slow_period: int = 26
    signal_period: int = 9
    ticker: str = ""
    interval: str = ""
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "macd_line": str(self.macd_line) if self.macd_line is not None else None,
            "signal_line": str(self.signal_line) if self.signal_line is not None else None,
            "histogram": str(self.histogram) if self.histogram is not None else None,
            "is_available": self.is_available,
            "as_of_timestamp": self.as_of_timestamp.isoformat() if self.as_of_timestamp else None,
            "fast_period": self.fast_period,
            "slow_period": self.slow_period,
            "signal_period": self.signal_period,
            "ticker": self.ticker,
            "interval": self.interval,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class BollingerBandsResult:
    """Structured output for Bollinger Bands containing upper, middle, lower bands and metrics."""
    upper: Optional[Decimal]
    middle: Optional[Decimal]
    lower: Optional[Decimal]
    bandwidth: Optional[Decimal]
    percent_b: Optional[Decimal]
    is_available: bool
    as_of_timestamp: Optional[datetime]
    period: int = 20
    num_std_dev: Decimal = Decimal("2.0")
    ticker: str = ""
    interval: str = ""
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "upper": str(self.upper) if self.upper is not None else None,
            "middle": str(self.middle) if self.middle is not None else None,
            "lower": str(self.lower) if self.lower is not None else None,
            "bandwidth": str(self.bandwidth) if self.bandwidth is not None else None,
            "percent_b": str(self.percent_b) if self.percent_b is not None else None,
            "is_available": self.is_available,
            "as_of_timestamp": self.as_of_timestamp.isoformat() if self.as_of_timestamp else None,
            "period": self.period,
            "num_std_dev": str(self.num_std_dev),
            "ticker": self.ticker,
            "interval": self.interval,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class TechnicalIndicatorSnapshot:
    """Consolidated snapshot of all standard technical indicators as of a single bar."""
    ticker: str
    interval: str
    as_of_timestamp: datetime
    bar_count: int

    # Trend Indicators
    sma_20: SingleIndicatorValue
    sma_50: SingleIndicatorValue
    sma_200: SingleIndicatorValue
    ema_9: SingleIndicatorValue
    ema_20: SingleIndicatorValue
    ema_50: SingleIndicatorValue

    # Momentum Indicators
    rsi_14: SingleIndicatorValue
    macd: MACDResult

    # Volatility Indicators
    atr_14: SingleIndicatorValue
    bollinger_bands: BollingerBandsResult

    # Volume Indicators
    average_volume_20: SingleIndicatorValue
    relative_volume: SingleIndicatorValue

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "interval": self.interval,
            "as_of_timestamp": self.as_of_timestamp.isoformat(),
            "bar_count": self.bar_count,
            "sma_20": self.sma_20.to_dict(),
            "sma_50": self.sma_50.to_dict(),
            "sma_200": self.sma_200.to_dict(),
            "ema_9": self.ema_9.to_dict(),
            "ema_20": self.ema_20.to_dict(),
            "ema_50": self.ema_50.to_dict(),
            "rsi_14": self.rsi_14.to_dict(),
            "macd": self.macd.to_dict(),
            "atr_14": self.atr_14.to_dict(),
            "bollinger_bands": self.bollinger_bands.to_dict(),
            "average_volume_20": self.average_volume_20.to_dict(),
            "relative_volume": self.relative_volume.to_dict(),
        }
