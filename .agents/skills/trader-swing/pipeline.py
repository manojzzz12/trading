"""Unified end-to-end strategy analysis pipeline for trader-swing.

Orchestrates all 5 subphases into a single deterministic, explainable workflow:
1. Phase 4.2: Market Regime Detection (SPY, QQQ, IWM alignment and confidence).
2. Phase 4.3: Stock Universe Screening and 5-Pillar Ranking.
3. Phase 4.4: Entry and Exit Signal Generation with Phase 2 Risk Sizing.
4. Phase 4.5: AI Research Enrichment, Event Risk Awareness, and Boundary Enforcement.
5. Consolidated Strategy Analysis Report with zero ledger or disk mutations.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
import os
import sys
from typing import Any, Dict, List, Optional, Sequence

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from accounting import AccountingLedger
from ai_research import BaseLLMProvider, DeterministicRuleBasedProvider
from market_data_models import OHLCVCandle
from regime_detector import MarketRegimeDetector
from regime_models import MarketRegimeResult
from research_context import ResearchContextCoordinator
from research_models import EnrichedCandidateReport, ResearchItem
from risk import RiskEngine
from scanner import StockScanner
from scanner_models import ScannedCandidate, ScanResult, ScreeningCriteria
from signal_engine import SignalEngine, SignalGenerationSummary
from signal_models import ExitSignal, ProposedTradePlan
from universe import DEFAULT_CURATED_WATCHLIST, normalize_universe
from feed_adapter import FeedReadinessResult, MarketDataFeedAdapter, ReadinessStatus


@dataclass(frozen=True)
class IntegratedStrategyAnalysisReport:
    """Report combining market data feed readiness evaluation and strategy analysis output."""
    as_of_timestamp: datetime
    readiness: FeedReadinessResult
    strategy_report: Optional["StrategyAnalysisReport"] = None
    warnings: List[str] = field(default_factory=list)
    failure_reasons: List[str] = field(default_factory=list)

    @property
    def is_ready(self) -> bool:
        return self.readiness.is_ready

    @property
    def status(self) -> str:
        return self.readiness.status.value

    def to_dict(self) -> Dict[str, Any]:
        return {
            "as_of_timestamp": self.as_of_timestamp.isoformat(),
            "readiness": self.readiness.to_dict(),
            "strategy_report": self.strategy_report.to_dict() if self.strategy_report else None,
            "warnings": list(self.warnings),
            "failure_reasons": list(self.failure_reasons),
        }


@dataclass(frozen=True)
class StrategyAnalysisReport:
    """Comprehensive, immutable strategy analysis report produced by the swing pipeline."""
    as_of_timestamp: datetime
    market_regime: MarketRegimeResult
    scan_result: ScanResult
    signal_summary: SignalGenerationSummary
    enriched_candidates: List[EnrichedCandidateReport]
    exit_signals: List[ExitSignal]
    total_universe_count: int
    eligible_candidates_count: int
    warnings: List[str]

    def to_dict(self) -> Dict[str, Any]:
        """Serialize complete report to plain dictionary for JSON export and audit."""
        return {
            "as_of_timestamp": self.as_of_timestamp.isoformat(),
            "market_regime": self.market_regime.to_dict(),
            "scan_result": self.scan_result.to_dict(),
            "signal_summary": self.signal_summary.to_dict(),
            "enriched_candidates": [c.to_dict() for c in self.enriched_candidates],
            "exit_signals": [e.to_dict() for e in self.exit_signals],
            "total_universe_count": self.total_universe_count,
            "eligible_candidates_count": self.eligible_candidates_count,
            "warnings": list(self.warnings),
        }


class SwingTradingStrategyService:
    """Unified service orchestrating the complete swing-trading intelligence pipeline."""

    def __init__(
        self,
        regime_detector: Optional[MarketRegimeDetector] = None,
        scanner: Optional[StockScanner] = None,
        signal_engine: Optional[SignalEngine] = None,
        research_coordinator: Optional[ResearchContextCoordinator] = None,
        llm_provider: Optional[BaseLLMProvider] = None,
        feed_adapter: Optional[MarketDataFeedAdapter] = None,
    ):
        self.regime_detector = regime_detector or MarketRegimeDetector()
        self.scanner = scanner or StockScanner()
        self.signal_engine = signal_engine or SignalEngine()
        llm = llm_provider or DeterministicRuleBasedProvider()
        self.research_coordinator = research_coordinator or ResearchContextCoordinator(llm_provider=llm)
        self.feed_adapter = feed_adapter

    def run_analysis(
        self,
        proxy_candles_map: Dict[str, Sequence[OHLCVCandle]],
        stock_candles_map: Dict[str, Sequence[OHLCVCandle]],
        universe: Optional[Sequence[str]] = None,
        ledger: Optional[AccountingLedger] = None,
        research_map: Optional[Dict[str, Sequence[ResearchItem]]] = None,
        lot_stops: Optional[Dict[str, Decimal]] = None,
        lot_targets: Optional[Dict[str, Decimal]] = None,
        criteria: Optional[ScreeningCriteria] = None,
        top_n: int = 10,
        current_time: Optional[datetime] = None,
        require_complete: bool = True,
    ) -> StrategyAnalysisReport:
        """Execute end-to-end swing-trading pipeline in a strictly read-only manner."""
        warnings: List[str] = []
        now = current_time or datetime.now(timezone.utc)

        # 1. Detect Macro Market Regime (SPY, QQQ, IWM)
        regime_result = self.regime_detector.detect_regime(
            proxy_candles_map=proxy_candles_map,
            current_time=now,
            require_complete=require_complete,
        )

        if not regime_result.policy.allow_new_longs:
            warnings.append(
                f"Market regime '{regime_result.regime.value}' disallows new long entries under current policy."
            )

        # 2. Determine Universe and Screen/Rank Stocks
        if universe:
            target_universe, drop_warnings = normalize_universe(universe, allow_empty=True)
            warnings.extend(drop_warnings)
        else:
            target_universe = DEFAULT_CURATED_WATCHLIST

        active_scanner = self.scanner if criteria is None else StockScanner(criteria=criteria)

        scan_result = active_scanner.scan_universe(
            universe=target_universe,
            candles_map=stock_candles_map,
            regime_result=regime_result,
            top_n=top_n,
            require_complete=require_complete,
        )

        eligible_candidates = scan_result.candidates
        top_candidates = eligible_candidates[:top_n]

        # 3. Generate Signals and Risk Sizing
        signal_summary = self.signal_engine.evaluate_candidates(
            candidates=top_candidates,
            candles_map=stock_candles_map,
            ledger=ledger,
            require_complete=require_complete,
        )

        # Map trade plans by ticker
        plans_by_ticker: Dict[str, ProposedTradePlan] = {}
        for plan in signal_summary.risk_approved_plans:
            plans_by_ticker[plan.ticker] = plan
        for plan in signal_summary.risk_rejected_plans:
            plans_by_ticker[plan.ticker] = plan

        # 4. Evaluate Exits for Open Positions (if ledger provided)
        exit_signals: List[ExitSignal] = []
        if ledger is not None:
            exit_signals = self.signal_engine.evaluate_exits(
                ledger=ledger,
                candles_map=stock_candles_map,
                lot_stops=lot_stops,
                lot_targets=lot_targets,
                regime_result=regime_result,
                require_complete=require_complete,
            )

        # 5. Enrich Top Candidates with AI Research Context and Boundary Enforcement
        enriched_candidates: List[EnrichedCandidateReport] = []
        research_lookup = research_map or {}

        for cand in top_candidates:
            trade_plan = plans_by_ticker.get(cand.ticker)
            items = research_lookup.get(cand.ticker, [])
            enriched = self.research_coordinator.assess_candidate(
                candidate=cand,
                trade_plan=trade_plan,
                research_items=items,
            )
            enriched_candidates.append(enriched)

        return StrategyAnalysisReport(
            as_of_timestamp=now,
            market_regime=regime_result,
            scan_result=scan_result,
            signal_summary=signal_summary,
            enriched_candidates=enriched_candidates,
            exit_signals=exit_signals,
            total_universe_count=len(target_universe),
            eligible_candidates_count=len(eligible_candidates),
            warnings=warnings,
        )

    def run_analysis_with_feed(
        self,
        stock_universe: Optional[Sequence[str]] = None,
        proxy_candles_map: Optional[Dict[str, Sequence[OHLCVCandle]]] = None,
        stock_candles_map: Optional[Dict[str, Sequence[OHLCVCandle]]] = None,
        feed_adapter: Optional[MarketDataFeedAdapter] = None,
        ledger: Optional[AccountingLedger] = None,
        research_map: Optional[Dict[str, Sequence[ResearchItem]]] = None,
        lot_stops: Optional[Dict[str, Decimal]] = None,
        lot_targets: Optional[Dict[str, Decimal]] = None,
        criteria: Optional[ScreeningCriteria] = None,
        top_n: int = 10,
        current_time: Optional[datetime] = None,
        require_complete: bool = True,
    ) -> IntegratedStrategyAnalysisReport:
        """Execute swing analysis guarded by the market data feed adapter readiness gate.

        Strictly read-only: does not record paper entries, exits, or state mutations.
        If feed readiness fails (e.g. missing SPY, < 200 bars, stale/incomplete candles),
        downstream analysis is aborted to prevent operating on invalid data.
        """
        now = current_time or datetime.now(timezone.utc)
        adapter = feed_adapter or self.feed_adapter
        if adapter is None:
            adapter = MarketDataFeedAdapter()

        # Evaluate feed readiness
        if proxy_candles_map is not None:
            readiness = adapter.evaluate_readiness(
                proxy_candles_map=proxy_candles_map,
                stock_candles_map=stock_candles_map,
                current_time=now,
            )
        elif stock_universe is not None:
            readiness = adapter.fetch_and_evaluate(
                stock_universe=stock_universe,
                current_time=now,
            )
        else:
            readiness = adapter.fetch_and_evaluate(
                stock_universe=DEFAULT_CURATED_WATCHLIST,
                current_time=now,
            )

        # Readiness Gate: If feed readiness fails, halt immediately before running pipeline
        if not readiness.is_ready or readiness.status != ReadinessStatus.READY:
            return IntegratedStrategyAnalysisReport(
                as_of_timestamp=now,
                readiness=readiness,
                strategy_report=None,
                warnings=list(readiness.warnings),
                failure_reasons=list(readiness.failure_reasons),
            )

        # Pass only validated, completed, fresh daily candles to analysis pipeline
        strategy_report = self.run_analysis(
            proxy_candles_map=readiness.proxy_candles,
            stock_candles_map=readiness.stock_candles,
            universe=readiness.included_tickers,
            ledger=ledger,
            research_map=research_map,
            lot_stops=lot_stops,
            lot_targets=lot_targets,
            criteria=criteria,
            top_n=top_n,
            current_time=now,
            require_complete=require_complete,
        )

        combined_warnings = list(readiness.warnings) + list(strategy_report.warnings)

        return IntegratedStrategyAnalysisReport(
            as_of_timestamp=now,
            readiness=readiness,
            strategy_report=strategy_report,
            warnings=combined_warnings,
            failure_reasons=[],
        )
