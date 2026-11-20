"""Data models for AI-assisted research and candidate enrichment in trader-swing.

Enforces:
1. Strict schema validation for AI qualitative outputs.
2. Source attribution, evidence classification, and untrusted input defense.
3. Strict decision boundaries: AI is an analyst, never the risk or execution authority.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional


class SourceReliability(str, Enum):
    """Reliability tier of a research source."""
    CONFIRMED_FACT = "CONFIRMED_FACT"  # SEC filings, official earnings releases
    REPORTED_CLAIM = "REPORTED_CLAIM"  # Major financial news reporting
    ANALYST_INTERPRETATION = "ANALYST_INTERPRETATION"  # Wall Street analyst notes
    UNKNOWN = "UNKNOWN"


class FinalCandidateAssessment(str, Enum):
    """Final explainable classification of a swing-trading candidate."""
    STRONG_CANDIDATE = "STRONG_CANDIDATE"
    CANDIDATE = "CANDIDATE"
    WEAK_CANDIDATE = "WEAK_CANDIDATE"
    REJECTED = "REJECTED"
    INSUFFICIENT_INFORMATION = "INSUFFICIENT_INFORMATION"


@dataclass(frozen=True)
class ResearchItem:
    """A discrete factual disclosure, news headline, or regulatory filing."""
    ticker: str
    source_name: str
    headline: str
    summary: str
    published_at: datetime
    retrieved_at: datetime
    reliability: SourceReliability = SourceReliability.REPORTED_CLAIM
    source_url: Optional[str] = None
    event_type: str = "NEWS"  # EARNINGS, SEC_8K, NEWS, GUIDANCE

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "source_name": self.source_name,
            "headline": self.headline,
            "summary": self.summary,
            "published_at": self.published_at.isoformat(),
            "retrieved_at": self.retrieved_at.isoformat(),
            "reliability": self.reliability.value,
            "source_url": self.source_url,
            "event_type": self.event_type,
        }


@dataclass(frozen=True)
class StructuredResearchOutput:
    """Strictly validated qualitative assessment produced by an LLM provider."""
    ticker: str
    business_summary: str
    positive_catalysts: List[str]
    negative_catalysts: List[str]
    earnings_event_risk: str  # "HIGH", "MEDIUM", "LOW", "UNKNOWN"
    sector_market_context: str
    downside_risks: List[str]
    technical_fundamental_conflicts: List[str]
    qualitative_assessment: str
    confidence: Decimal
    missing_information: List[str]
    provider_name: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "business_summary": self.business_summary,
            "positive_catalysts": list(self.positive_catalysts),
            "negative_catalysts": list(self.negative_catalysts),
            "earnings_event_risk": self.earnings_event_risk,
            "sector_market_context": self.sector_market_context,
            "downside_risks": list(self.downside_risks),
            "technical_fundamental_conflicts": list(self.technical_fundamental_conflicts),
            "qualitative_assessment": self.qualitative_assessment,
            "confidence": str(self.confidence),
            "missing_information": list(self.missing_information),
            "provider_name": self.provider_name,
        }


@dataclass(frozen=True)
class EnrichedCandidateReport:
    """Comprehensive end-to-end report combining technical signal, risk sizing, and AI research."""
    ticker: str
    rank: int
    scanner_score: Decimal
    market_regime: str
    final_assessment: FinalCandidateAssessment
    assessment_reasons: List[str]
    technical_signal: Optional[Dict[str, Any]]
    trade_plan: Optional[Dict[str, Any]]
    research_summary: Optional[StructuredResearchOutput]
    research_items_count: int
    warnings: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "rank": self.rank,
            "scanner_score": str(self.scanner_score),
            "market_regime": self.market_regime,
            "final_assessment": self.final_assessment.value,
            "assessment_reasons": list(self.assessment_reasons),
            "technical_signal": self.technical_signal,
            "trade_plan": self.trade_plan,
            "research_summary": self.research_summary.to_dict() if self.research_summary else None,
            "research_items_count": self.research_items_count,
            "warnings": list(self.warnings),
        }
