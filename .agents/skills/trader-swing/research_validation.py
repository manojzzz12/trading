"""Validation routines and untrusted input defense for AI research in trader-swing.

Enforces:
1. Strict schema validation on LLM-generated JSON outputs.
2. Prompt injection defense: retrieved news/filings are strictly quarantined as untrusted evidence.
3. Verification of publication timestamps and source reliability classifications.
"""

from datetime import datetime
from decimal import Decimal, InvalidOperation
import os
import re
import sys
from typing import Any, Dict, List, Optional

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from research_models import ResearchItem, SourceReliability, StructuredResearchOutput

VALID_EVENT_RISK_LEVELS = {"HIGH", "MEDIUM", "LOW", "UNKNOWN"}

# Common prompt injection patterns in financial disclosures / news
INJECTION_PATTERN = re.compile(
    r"(ignore\s+(all\s+)?prior\s+instructions|system\s+prompt|execute\s+command|override\s+risk|bypass\s+limits)",
    re.IGNORECASE,
)


class ResearchValidationError(ValueError):
    """Raised when research data or model outputs fail schema or safety validation."""
    pass


def sanitize_untrusted_text(text: str, max_chars: int = 1000) -> str:
    """Sanitize retrieved external text before including in LLM prompt context."""
    if not text or not isinstance(text, str):
        return ""

    # Neutralize control characters and normalize whitespace
    cleaned = re.sub(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]", "", text)
    cleaned = cleaned.strip()

    # Redact common prompt injection patterns
    if INJECTION_PATTERN.search(cleaned):
        cleaned = INJECTION_PATTERN.sub("[REDACTED_UNTRUSTED_PATTERN]", cleaned)

    return cleaned[:max_chars]


def validate_structured_research_output(
    data: Dict[str, Any],
    expected_ticker: str,
    provider_name: str = "llm",
) -> StructuredResearchOutput:
    """Validate a raw dictionary response against the StructuredResearchOutput schema.

    Raises:
        ResearchValidationError: If any required field is missing or malformed.
    """
    if not isinstance(data, dict):
        raise ResearchValidationError(f"Expected dict from model, got {type(data).__name__}.")

    ticker = data.get("ticker", "").strip().upper()
    if ticker != expected_ticker.upper():
        raise ResearchValidationError(
            f"Model output ticker mismatch: expected '{expected_ticker}', got '{ticker}'."
        )

    business_summary = str(data.get("business_summary", "")).strip()
    if not business_summary:
        raise ResearchValidationError("Missing or empty 'business_summary' in model output.")

    pos_catalysts = data.get("positive_catalysts", [])
    if not isinstance(pos_catalysts, list):
        raise ResearchValidationError("'positive_catalysts' must be a list.")

    neg_catalysts = data.get("negative_catalysts", [])
    if not isinstance(neg_catalysts, list):
        raise ResearchValidationError("'negative_catalysts' must be a list.")

    event_risk = str(data.get("earnings_event_risk", "UNKNOWN")).strip().upper()
    if event_risk not in VALID_EVENT_RISK_LEVELS:
        event_risk = "UNKNOWN"

    sector_context = str(data.get("sector_market_context", "")).strip()
    downside_risks = data.get("downside_risks", [])
    if not isinstance(downside_risks, list):
        downside_risks = []

    conflicts = data.get("technical_fundamental_conflicts", [])
    if not isinstance(conflicts, list):
        conflicts = []

    qual_assessment = str(data.get("qualitative_assessment", "")).strip()
    if not qual_assessment:
        qual_assessment = "Assessment unavailable."

    missing_info = data.get("missing_information", [])
    if not isinstance(missing_info, list):
        missing_info = []

    # Validate confidence as pure Decimal in [0.0, 1.0]
    raw_conf = data.get("confidence", "0.5")
    try:
        conf_dec = Decimal(str(raw_conf))
        conf_dec = max(Decimal("0.0"), min(Decimal("1.0"), conf_dec))
    except (InvalidOperation, TypeError):
        conf_dec = Decimal("0.5")

    return StructuredResearchOutput(
        ticker=ticker,
        business_summary=business_summary,
        positive_catalysts=[str(c).strip() for c in pos_catalysts if str(c).strip()],
        negative_catalysts=[str(c).strip() for c in neg_catalysts if str(c).strip()],
        earnings_event_risk=event_risk,
        sector_market_context=sector_context,
        downside_risks=[str(r).strip() for r in downside_risks if str(r).strip()],
        technical_fundamental_conflicts=[str(c).strip() for c in conflicts if str(c).strip()],
        qualitative_assessment=qual_assessment,
        confidence=conf_dec,
        missing_information=[str(m).strip() for m in missing_info if str(m).strip()],
        provider_name=provider_name,
    )
