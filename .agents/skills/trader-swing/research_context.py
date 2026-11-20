"""Final research-aware candidate assessment and enrichment for trader-swing.

Enforces:
1. Strict decision boundary: AI CAN NEVER OVERRIDE A RISK ENGINE REJECTION.
2. Deterministic candidate qualification merging technical scores, risk plans, and research.
3. Event-risk awareness (downgrading swing setups facing binary earnings/gap events).
"""

from decimal import Decimal
import os
import sys
from typing import Any, Dict, List, Optional, Sequence

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from ai_research import BaseLLMProvider, DeterministicRuleBasedProvider
from research_models import (
    EnrichedCandidateReport,
    FinalCandidateAssessment,
    ResearchItem,
    StructuredResearchOutput,
)
from scanner_models import ScannedCandidate
from signal_models import ProposedTradePlan


class ResearchContextCoordinator:
    """Enriches deterministic candidate trade plans with qualitative research."""

    def __init__(self, llm_provider: Optional[BaseLLMProvider] = None):
        self.llm_provider = llm_provider or DeterministicRuleBasedProvider()

    def assess_candidate(
        self,
        candidate: ScannedCandidate,
        trade_plan: Optional[ProposedTradePlan],
        research_items: Sequence[ResearchItem],
    ) -> EnrichedCandidateReport:
        """Combine quantitative scanner results, risk trade plan, and research into a final assessment."""
        ticker = candidate.ticker
        assessment_reasons: List[str] = []
        warnings: List[str] = list(candidate.warnings)

        # 1. Evaluate AI qualitative research
        research_out = self.llm_provider.generate_research(
            ticker=ticker,
            research_items=research_items,
            technical_summary=candidate.indicators,
        )

        # 2. Strict Boundary Check: Phase 2 Risk Engine is the Final Authority
        if trade_plan is None:
            assessment = FinalCandidateAssessment.INSUFFICIENT_INFORMATION
            assessment_reasons.append("No technical entry signal or trade plan generated.")
        elif not trade_plan.risk_approved:
            # Deterministic risk engine rejected the trade -> AI CANNOT OVERRIDE
            assessment = FinalCandidateAssessment.REJECTED
            assessment_reasons.append(
                f"Trade plan rejected by deterministic risk engine: {', '.join(trade_plan.risk_rejection_reasons)}."
            )
            if trade_plan.binding_constraint:
                assessment_reasons.append(f"Binding constraint: {trade_plan.binding_constraint}.")
        else:
            # Trade plan passed risk engine. Now integrate research context and event risk.
            if research_out.earnings_event_risk == "HIGH":
                assessment = FinalCandidateAssessment.WEAK_CANDIDATE
                assessment_reasons.append(
                    "High earnings or corporate event risk flagged; binary overnight gap risk conflicts with swing profile."
                )
                warnings.append("High event risk: consider waiting until after earnings disclosure.")
            elif candidate.score.total_score >= Decimal("70.0") and candidate.market_regime in ["STRONG_BULLISH", "BULLISH"]:
                assessment = FinalCandidateAssessment.STRONG_CANDIDATE
                assessment_reasons.append(
                    f"Strong technical score ({candidate.score.total_score}), bullish regime, and risk-approved allocation of {trade_plan.shares} shares."
                )
                assessment_reasons.append(f"Research assessment: {research_out.qualitative_assessment}")
            else:
                assessment = FinalCandidateAssessment.CANDIDATE
                assessment_reasons.append(
                    f"Risk-approved candidate with acceptable technical score ({candidate.score.total_score}) and {trade_plan.shares} shares allocated."
                )

        return EnrichedCandidateReport(
            ticker=ticker,
            rank=candidate.rank,
            scanner_score=candidate.score.total_score,
            market_regime=candidate.market_regime or "UNKNOWN",
            final_assessment=assessment,
            assessment_reasons=assessment_reasons,
            technical_signal={"price": str(candidate.price), "indicators": candidate.indicators},
            trade_plan=trade_plan.to_dict() if trade_plan else None,
            research_summary=research_out,
            research_items_count=len(research_items),
            warnings=warnings,
        )
