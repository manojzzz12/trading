"""Data models and configuration structures for stock scanning and ranking in trader-swing.

Enforces:
1. Pure Decimal arithmetic for all financial filters, scores, and dollar volumes.
2. Transparent explainable scoring breakdown across 5 dimensions.
3. Explicit candidate eligibility status and rejection reason logging.
4. Regime-aware policy integration.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class ScreeningCriteria:
    """Configurable quantitative screening filters for US swing trading."""
    min_price: Decimal = Decimal("10.00")
    max_price: Optional[Decimal] = Decimal("2000.00")
    min_average_dollar_volume: Decimal = Decimal("10000000.00")  # $10,000,000 ADV
    min_rvol: Decimal = Decimal("0.8000")
    min_rsi: Decimal = Decimal("35.0000")
    max_rsi: Decimal = Decimal("75.0000")
    min_atr_percent: Decimal = Decimal("1.0000")  # Minimum 1% ATR volatility for swing movement
    max_atr_percent: Decimal = Decimal("15.0000")  # Maximum 15% ATR to avoid unmanageable penny-like volatility
    require_above_sma_20: bool = True
    require_above_sma_50: bool = False
    min_history_bars: int = 25  # Needs at least 21 bars for RVOL + moving average

    def to_dict(self) -> Dict[str, Any]:
        return {
            "min_price": str(self.min_price),
            "max_price": str(self.max_price) if self.max_price is not None else None,
            "min_average_dollar_volume": str(self.min_average_dollar_volume),
            "min_rvol": str(self.min_rvol),
            "min_rsi": str(self.min_rsi),
            "max_rsi": str(self.max_rsi),
            "min_atr_percent": str(self.min_atr_percent),
            "max_atr_percent": str(self.max_atr_percent),
            "require_above_sma_20": self.require_above_sma_20,
            "require_above_sma_50": self.require_above_sma_50,
            "min_history_bars": self.min_history_bars,
        }


@dataclass(frozen=True)
class CandidateScore:
    """Detailed score breakdown across 5 explainable quantitative dimensions (0 to 100)."""
    trend_score: Decimal       # Max 25.0
    momentum_score: Decimal    # Max 25.0
    rvol_score: Decimal        # Max 20.0
    volatility_score: Decimal  # Max 15.0
    liquidity_score: Decimal   # Max 15.0
    total_score: Decimal       # Sum (0.0 to 100.0)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trend_score": str(self.trend_score),
            "momentum_score": str(self.momentum_score),
            "rvol_score": str(self.rvol_score),
            "volatility_score": str(self.volatility_score),
            "liquidity_score": str(self.liquidity_score),
            "total_score": str(self.total_score),
        }


@dataclass(frozen=True)
class ScannedCandidate:
    """A ranked or evaluated swing candidate."""
    ticker: str
    rank: int
    score: CandidateScore
    price: Decimal
    indicators: Dict[str, Any]
    positive_reasons: List[str]
    negative_reasons: List[str]
    warnings: List[str]
    market_regime: Optional[str]
    as_of_timestamp: datetime
    is_eligible: bool
    rejection_reasons: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "rank": self.rank,
            "score": self.score.to_dict(),
            "price": str(self.price),
            "indicators": self.indicators,
            "positive_reasons": list(self.positive_reasons),
            "negative_reasons": list(self.negative_reasons),
            "warnings": list(self.warnings),
            "market_regime": self.market_regime,
            "as_of_timestamp": self.as_of_timestamp.isoformat(),
            "is_eligible": self.is_eligible,
            "rejection_reasons": list(self.rejection_reasons),
        }


@dataclass(frozen=True)
class ScanResult:
    """Consolidated output from a stock scanner run."""
    candidates: List[ScannedCandidate]
    total_scanned: int
    total_passed: int
    rejected_candidates: Dict[str, List[str]]
    market_regime: Optional[str]
    as_of_timestamp: Optional[datetime]
    warnings: List[str]
    evaluated_candidates: Dict[str, ScannedCandidate] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidates": [c.to_dict() for c in self.candidates],
            "total_scanned": self.total_scanned,
            "total_passed": self.total_passed,
            "rejected_candidates": self.rejected_candidates,
            "market_regime": self.market_regime,
            "as_of_timestamp": self.as_of_timestamp.isoformat() if self.as_of_timestamp else None,
            "warnings": list(self.warnings),
            "evaluated_candidates": {k: v.to_dict() for k, v in self.evaluated_candidates.items()},
        }
