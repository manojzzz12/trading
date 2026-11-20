"""Integrated swing-trading signal engine uniting entry, exit, and Phase 2 risk evaluation.

Enforces:
1. Deterministic risk engine as the final, immutable authority on position sizing and trade approval.
2. Clear distinction between technically valid signals, risk-approved trade plans, and risk-rejected candidates.
3. Informational exit recommendations without actual position mutation.
4. Pure Decimal arithmetic for all financial calculations.
"""

from dataclasses import dataclass
from decimal import Decimal
import os
import sys
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from accounting import AccountingLedger
from market_data_models import OHLCVCandle
from regime_models import MarketRegimeResult
from risk import RiskConfig, RiskEngine, SizingResult
from scanner_models import ScannedCandidate
from entry_signals import EntrySignalGenerator
from exit_signals import ExitSignalGenerator
from signal_models import EntrySignal, ExitSignal, ProposedTradePlan


@dataclass(frozen=True)
class SignalGenerationSummary:
    """Consolidated result of a signal generation pass across candidate stocks."""
    risk_approved_plans: List[ProposedTradePlan]
    risk_rejected_plans: List[ProposedTradePlan]
    technically_invalid_tickers: List[str]
    exit_signals: List[ExitSignal]
    total_evaluated: int
    total_approved: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "risk_approved_plans": [p.to_dict() for p in self.risk_approved_plans],
            "risk_rejected_plans": [p.to_dict() for p in self.risk_rejected_plans],
            "technically_invalid_tickers": list(self.technically_invalid_tickers),
            "exit_signals": [e.to_dict() for e in self.exit_signals],
            "total_evaluated": self.total_evaluated,
            "total_approved": self.total_approved,
        }


class SignalEngine:
    """Orchestrates entry signal generation, exit monitoring, and Phase 2 risk evaluation."""

    def __init__(
        self,
        risk_engine: Optional[RiskEngine] = None,
        entry_generator: Optional[EntrySignalGenerator] = None,
        exit_generator: Optional[ExitSignalGenerator] = None,
    ):
        self.risk_engine = risk_engine or RiskEngine()
        self.entry_generator = entry_generator or EntrySignalGenerator()
        self.exit_generator = exit_generator or ExitSignalGenerator()

    def evaluate_candidates(
        self,
        candidates: Sequence[ScannedCandidate],
        candles_map: Dict[str, Sequence[OHLCVCandle]],
        ledger: Optional[AccountingLedger] = None,
        require_complete: bool = True,
        pending_tickers: Optional[Sequence[str]] = None,
    ) -> SignalGenerationSummary:
        """Convert eligible scanned candidates into deterministic, risk-evaluated trade plans."""
        if ledger is None:
            # Default virtual capital ledger ($5,000 USD)
            ledger = AccountingLedger(initial_capital=self.risk_engine.risk_config.starting_virtual_capital_usd)

        approved_plans: List[ProposedTradePlan] = []
        rejected_plans: List[ProposedTradePlan] = []
        technically_invalid: List[str] = []

        current_open_tickers = set(t for t, p in ledger.positions.items() if p.total_shares > 0)
        pending_set = set(pending_tickers) if pending_tickers else set()
        approved_in_batch: Set[str] = set()
        max_positions = self.risk_engine.risk_config.max_simultaneous_positions

        for cand in candidates:
            ticker = cand.ticker
            if not cand.is_eligible:
                continue
            if ticker not in candles_map or not candles_map[ticker]:
                technically_invalid.append(ticker)
                continue

            candles = candles_map[ticker]
            entry_sig = self.entry_generator.evaluate_entry(
                candles,
                require_complete=require_complete,
            )

            if entry_sig is None or not entry_sig.is_technically_valid:
                technically_invalid.append(ticker)
                continue

            # Batch-aware position capacity reservation:
            # Track open positions, pending orders, and newly approved candidates in the batch
            effective_committed_tickers = current_open_tickers | pending_set | approved_in_batch
            if ticker not in effective_committed_tickers and len(effective_committed_tickers) >= max_positions:
                plan = ProposedTradePlan(
                    ticker=ticker,
                    strategy_type=entry_sig.strategy,
                    entry_price=entry_sig.reference_price,
                    entry_zone_low=entry_sig.entry_zone_low,
                    entry_zone_high=entry_sig.entry_zone_high,
                    stop_loss_price=entry_sig.suggested_stop_loss,
                    target_price=entry_sig.suggested_target,
                    reward_to_risk=entry_sig.reward_to_risk,
                    shares=0,
                    total_capital_required=Decimal("0.00"),
                    total_buffered_risk=Decimal("0.00"),
                    risk_approved=False,
                    binding_constraint="MAX_POSITIONS_EXCEEDED",
                    risk_rejection_reasons=[
                        f"Position count limit reached: currently holding/committed {len(effective_committed_tickers)} unique tickers (max: {max_positions})"
                    ],
                )
                rejected_plans.append(plan)
                continue

            # Deterministic risk evaluation and position sizing via Phase 2 RiskEngine
            sizing_res: SizingResult = self.risk_engine.size_position(
                ledger=ledger,
                ticker=ticker,
                entry_price=entry_sig.reference_price,
                stop_loss_price=entry_sig.suggested_stop_loss,
            )

            plan = ProposedTradePlan(
                ticker=ticker,
                strategy_type=entry_sig.strategy,
                entry_price=entry_sig.reference_price,
                entry_zone_low=entry_sig.entry_zone_low,
                entry_zone_high=entry_sig.entry_zone_high,
                stop_loss_price=entry_sig.suggested_stop_loss,
                target_price=entry_sig.suggested_target,
                reward_to_risk=entry_sig.reward_to_risk,
                shares=sizing_res.shares,
                total_capital_required=sizing_res.total_capital_required,
                total_buffered_risk=sizing_res.total_buffered_risk,
                risk_approved=sizing_res.approved,
                binding_constraint=sizing_res.binding_constraint,
                risk_rejection_reasons=sizing_res.rejection_reasons,
            )

            if sizing_res.approved and sizing_res.shares > 0:
                approved_plans.append(plan)
                approved_in_batch.add(ticker)
            else:
                rejected_plans.append(plan)

        return SignalGenerationSummary(
            risk_approved_plans=approved_plans,
            risk_rejected_plans=rejected_plans,
            technically_invalid_tickers=technically_invalid,
            exit_signals=[],  # Populated via evaluate_exits
            total_evaluated=len(candidates),
            total_approved=len(approved_plans),
        )

    def evaluate_exits(
        self,
        ledger: AccountingLedger,
        candles_map: Dict[str, Sequence[OHLCVCandle]],
        lot_stops: Optional[Dict[str, Decimal]] = None,
        lot_targets: Optional[Dict[str, Decimal]] = None,
        regime_result: Optional[MarketRegimeResult] = None,
        require_complete: bool = True,
    ) -> List[ExitSignal]:
        """Generate exit recommendations for currently held lots in the portfolio."""
        return self.exit_generator.evaluate_exits(
            ledger=ledger,
            candles_map=candles_map,
            lot_stops=lot_stops,
            lot_targets=lot_targets,
            regime_result=regime_result,
            require_complete=require_complete,
        )
