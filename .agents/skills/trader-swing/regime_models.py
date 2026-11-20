"""Data models and policy definitions for market regime detection in trader-swing.

Enforces:
1. Explicit regime classifications: STRONG_BULLISH, BULLISH, SIDEWAYS, BEARISH, STRONG_BEARISH, UNKNOWN.
2. Deterministic confidence scoring and evidence tracking.
3. Transparent regime policy mapping controlling downstream candidate filtering.
4. Pure Decimal arithmetic and full traceability of timestamps and proxy data.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional


class MarketRegime(str, Enum):
    """Broad US equity market regime classification."""
    STRONG_BULLISH = "STRONG_BULLISH"
    BULLISH = "BULLISH"
    SIDEWAYS = "SIDEWAYS"
    BEARISH = "BEARISH"
    STRONG_BEARISH = "STRONG_BEARISH"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class RegimePolicy:
    """Strategy behavior constraints driven by the detected market regime.

    Note: These policies may tighten candidate filtering or suppress new long positions,
    but they NEVER override or loosen Phase 2 deterministic risk limits.
    """
    regime: MarketRegime
    allow_new_longs: bool
    min_scanner_score: Decimal
    max_open_positions_override: Optional[int]
    description: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "regime": self.regime.value,
            "allow_new_longs": self.allow_new_longs,
            "min_scanner_score": str(self.min_scanner_score),
            "max_open_positions_override": self.max_open_positions_override,
            "description": self.description,
        }


# Standard default regime policies
DEFAULT_REGIME_POLICIES: Dict[MarketRegime, RegimePolicy] = {
    MarketRegime.STRONG_BULLISH: RegimePolicy(
        regime=MarketRegime.STRONG_BULLISH,
        allow_new_longs=True,
        min_scanner_score=Decimal("60.0"),
        max_open_positions_override=None,
        description="Favorable broad-market environment. Standard long strategy candidate generation.",
    ),
    MarketRegime.BULLISH: RegimePolicy(
        regime=MarketRegime.BULLISH,
        allow_new_longs=True,
        min_scanner_score=Decimal("65.0"),
        max_open_positions_override=None,
        description="Constructive market trend. Normal long candidate filtering.",
    ),
    MarketRegime.SIDEWAYS: RegimePolicy(
        regime=MarketRegime.SIDEWAYS,
        allow_new_longs=True,
        min_scanner_score=Decimal("75.0"),
        max_open_positions_override=3,  # Defensive position cap
        description="Consolidation / range-bound market. Strict candidate filtering; prioritize high-quality setups.",
    ),
    MarketRegime.BEARISH: RegimePolicy(
        regime=MarketRegime.BEARISH,
        allow_new_longs=True,
        min_scanner_score=Decimal("85.0"),
        max_open_positions_override=2,  # Highly restricted position cap
        description="Defensive market environment. Only top-decile, highly resilient pullback setups allowed.",
    ),
    MarketRegime.STRONG_BEARISH: RegimePolicy(
        regime=MarketRegime.STRONG_BEARISH,
        allow_new_longs=False,
        min_scanner_score=Decimal("100.0"),
        max_open_positions_override=0,
        description="Severe downtrend / market stress. All new long candidate generation suppressed.",
    ),
    MarketRegime.UNKNOWN: RegimePolicy(
        regime=MarketRegime.UNKNOWN,
        allow_new_longs=False,
        min_scanner_score=Decimal("100.0"),
        max_open_positions_override=0,
        description="Market data insufficient or stale. No strategy-ready candidates generated.",
    ),
}


@dataclass(frozen=True)
class ProxyAnalysis:
    """Quantitative technical assessment of an individual index proxy (e.g. SPY, QQQ, IWM)."""
    ticker: str
    price: Decimal
    as_of_timestamp: datetime
    above_sma_20: Optional[bool]
    above_sma_50: Optional[bool]
    above_sma_200: Optional[bool]
    sma_20_rising: Optional[bool]
    sma_50_rising: Optional[bool]
    rsi: Optional[Decimal]
    macd_histogram: Optional[Decimal]
    atr_14: Optional[Decimal]
    atr_percent: Optional[Decimal]
    score: Decimal  # Normalized -1.0 (severe bear) to +1.0 (strong bull)
    evidence: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "price": str(self.price),
            "as_of_timestamp": self.as_of_timestamp.isoformat(),
            "above_sma_20": self.above_sma_20,
            "above_sma_50": self.above_sma_50,
            "above_sma_200": self.above_sma_200,
            "sma_20_rising": self.sma_20_rising,
            "sma_50_rising": self.sma_50_rising,
            "rsi": str(self.rsi) if self.rsi is not None else None,
            "macd_histogram": str(self.macd_histogram) if self.macd_histogram is not None else None,
            "atr_14": str(self.atr_14) if self.atr_14 is not None else None,
            "atr_percent": str(self.atr_percent) if self.atr_percent is not None else None,
            "score": str(self.score),
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True)
class MarketRegimeResult:
    """Consolidated market regime evaluation result."""
    regime: MarketRegime
    confidence: Decimal  # 0.0000 to 1.0000
    aggregate_score: Decimal  # -1.0000 to +1.0000
    supporting_evidence: List[str]
    contradicting_evidence: List[str]
    proxy_analyses: Dict[str, ProxyAnalysis]
    indicators_used: Dict[str, Any]
    as_of_timestamp: Optional[datetime]
    data_freshness: str
    explanation: str
    warnings: List[str]
    missing_inputs: List[str]
    policy: RegimePolicy

    def to_dict(self) -> Dict[str, Any]:
        return {
            "regime": self.regime.value,
            "confidence": str(self.confidence),
            "aggregate_score": str(self.aggregate_score),
            "supporting_evidence": list(self.supporting_evidence),
            "contradicting_evidence": list(self.contradicting_evidence),
            "proxy_analyses": {k: v.to_dict() for k, v in self.proxy_analyses.items()},
            "indicators_used": self.indicators_used,
            "as_of_timestamp": self.as_of_timestamp.isoformat() if self.as_of_timestamp else None,
            "data_freshness": self.data_freshness,
            "explanation": self.explanation,
            "warnings": list(self.warnings),
            "missing_inputs": list(self.missing_inputs),
            "policy": self.policy.to_dict(),
        }
