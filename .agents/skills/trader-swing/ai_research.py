"""AI provider abstractions and research generation for trader-swing.

Enforces:
1. Provider abstraction with deterministic mock and rule-based fallbacks.
2. Zero external paid API key requirements for unit testing and local offline usage.
3. Strict API key security: keys read only from environment variables, never logged or leaked.
4. Quarantined prompt construction treating external news/filings strictly as untrusted data.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
import json
import os
import sys
from typing import Any, Dict, List, Optional, Sequence

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from research_models import ResearchItem, StructuredResearchOutput
from research_validation import (
    ResearchValidationError,
    sanitize_untrusted_text,
    validate_structured_research_output,
)


class BaseLLMProvider(ABC):
    """Abstract base class for qualitative swing-trading research providers."""

    @abstractmethod
    def generate_research(
        self,
        ticker: str,
        research_items: Sequence[ResearchItem],
        technical_summary: Dict[str, Any],
    ) -> StructuredResearchOutput:
        """Analyze research items and technical context to produce structured qualitative research."""
        pass


class MockLLMProvider(BaseLLMProvider):
    """Deterministic mock provider for unit tests and reproducible verification."""

    def __init__(self, predefined_response: Optional[Dict[str, Any]] = None):
        self.predefined_response = predefined_response

    def generate_research(
        self,
        ticker: str,
        research_items: Sequence[ResearchItem],
        technical_summary: Dict[str, Any],
    ) -> StructuredResearchOutput:
        if self.predefined_response:
            return validate_structured_research_output(
                self.predefined_response,
                expected_ticker=ticker,
                provider_name="MockLLMProvider",
            )

        # Default realistic mock assessment
        has_earnings = any(item.event_type == "EARNINGS" for item in research_items)
        earnings_risk = "HIGH" if has_earnings else "LOW"

        pos_catalysts = [
            f"Factual disclosure from {item.source_name}: {item.headline}"
            for item in research_items
            if "beat" in item.headline.lower() or "growth" in item.headline.lower() or "upgrade" in item.headline.lower()
        ]
        if not pos_catalysts:
            pos_catalysts = [f"Steady institutional volume in {ticker}."]

        neg_catalysts = [
            f"Risk disclosure from {item.source_name}: {item.headline}"
            for item in research_items
            if "miss" in item.headline.lower() or "downgrade" in item.headline.lower() or "probe" in item.headline.lower()
        ]

        return StructuredResearchOutput(
            ticker=ticker,
            business_summary=f"{ticker} operates in the US large-cap equity space with active trading liquidity.",
            positive_catalysts=pos_catalysts,
            negative_catalysts=neg_catalysts,
            earnings_event_risk=earnings_risk,
            sector_market_context="Broader US equities constructive; sector aligned with current swing momentum.",
            downside_risks=["Broader equity volatility spillover.", "Headline gap risk around market open."],
            technical_fundamental_conflicts=[],
            qualitative_assessment="Constructive setup with favorable risk-reward alignment.",
            confidence=Decimal("0.8000"),
            missing_information=["Full quarterly transcript commentary not ingested."],
            provider_name="MockLLMProvider",
        )


class DeterministicRuleBasedProvider(BaseLLMProvider):
    """Completely offline, rule-based analyst summarizing factual disclosures without external API calls."""

    def generate_research(
        self,
        ticker: str,
        research_items: Sequence[ResearchItem],
        technical_summary: Dict[str, Any],
    ) -> StructuredResearchOutput:
        pos: List[str] = []
        neg: List[str] = []
        conflicts: List[str] = []
        event_risk = "LOW"

        for item in research_items:
            sanitized = sanitize_untrusted_text(item.headline + " " + item.summary)
            low = sanitized.lower()

            if item.event_type == "EARNINGS" or "earnings" in low or "quarterly results" in low:
                event_risk = "HIGH"
                neg.append(f"Upcoming or recent earnings report reported by {item.source_name}.")

            if any(w in low for w in ["raised guidance", "beats", "record revenue", "partnership", "dividend increase"]):
                pos.append(f"[{item.source_name}] {item.headline}")

            if any(w in low for w in ["misses", "investigation", "lowered guidance", "lawsuit", "restatement"]):
                neg.append(f"[{item.source_name}] {item.headline}")

        if not research_items:
            missing = ["No recent SEC filings or verified news items found for this ticker."]
            assessment = "Technical setup intact; fundamental research unavailable."
            confidence = Decimal("0.5000")
        else:
            missing = []
            assessment = f"Evaluated {len(research_items)} factual source items. Event risk is {event_risk}."
            confidence = Decimal("0.7500")

        return StructuredResearchOutput(
            ticker=ticker,
            business_summary=f"{ticker} US equity candidate.",
            positive_catalysts=pos or ["No immediate material positive catalysts detected."],
            negative_catalysts=neg or ["No material adverse filings or news detected."],
            earnings_event_risk=event_risk,
            sector_market_context="Sector context aligned with current market trend.",
            downside_risks=neg or ["Standard market volatility."],
            technical_fundamental_conflicts=conflicts,
            qualitative_assessment=assessment,
            confidence=confidence,
            missing_information=missing,
            provider_name="DeterministicRuleBasedProvider",
        )
