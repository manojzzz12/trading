"""Forward Paper Trading Evaluation Service for Trader-Swing Phase 9.

Orchestrates the complete forward-evaluation protocol per market session:
1. Validates market data completeness, session alignment, and volume provenance.
2. Evaluates macro market regime via MarketRegimeDetector.
3. Screens universe symbols through StockScanner (5-pillar scoring).
4. Evaluates eligible setups through deterministic RiskEngine.
5. Records granular ForwardObservationRecords with rejection reasons.
6. Opens hypothetical positions for eligible setups passing all constraints.
7. Updates active hypothetical positions with new completed daily bars.
8. Evaluates stop, target, same-bar ambiguity (stop-first), and max holding period.
9. Reconciles virtual reference portfolio equity and cash ($5,000 capital).
10. Generates auditable EvaluationSessionSummary and audit logs.
"""

from datetime import date, datetime, timezone
from decimal import Decimal
import json
import os
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, "..", "..", ".."))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from accounting import quantize_cent, to_decimal
from forward_evaluation_models import (
    EvaluationSessionSummary,
    ForwardDataMode,
    ForwardObservationRecord,
    ForwardScreeningDecision,
    ForwardTradeProposal,
    HypotheticalClosedTrade,
    HypotheticalPosition,
    MARKER_FORWARD_EVALUATION,
    MARKER_SIMULATED_TRADE,
)
from forward_evaluation_storage import (
    DuplicateObservationError,
    ForwardEvaluationStorage,
)
from forward_performance import ForwardPerformanceEngine, ForwardPerformanceReport
from forward_trade_tracker import ForwardTradeTracker
from market_data_models import CandleStatus, OHLCVCandle, to_eastern
from regime_detector import MarketRegimeDetector
from regime_models import MarketRegimeResult
from risk import RiskConfig, RiskEngine
from scanner import StockScanner
from scanner_models import ScanResult, ScreeningCriteria
from signal_engine import SignalEngine
from signal_models import EntryStrategyType, ProposedTradePlan


class ForwardEvaluationService:
    """Consolidated service managing manual forward-evaluation cycles."""

    def __init__(
        self,
        storage: Optional[ForwardEvaluationStorage] = None,
        virtual_starting_capital: Decimal = Decimal("5000.00"),
        risk_config: Optional[RiskConfig] = None,
    ):
        self.storage = storage or ForwardEvaluationStorage()
        self.starting_capital = quantize_cent(to_decimal(virtual_starting_capital))
        self.risk_config = risk_config or RiskConfig()

        # Deterministic engines
        self.regime_detector = MarketRegimeDetector()
        self.scanner = StockScanner()
        self.risk_engine = RiskEngine(risk_config=self.risk_config)
        self.signal_engine = SignalEngine(risk_engine=self.risk_engine)
        self.trade_tracker = ForwardTradeTracker(
            storage=self.storage,
            initial_capital=self.starting_capital,
            risk_config=self.risk_config,
        )
        self.performance_engine = ForwardPerformanceEngine(
            storage=self.storage,
            virtual_starting_capital=self.starting_capital,
        )

    def load_candles_from_daily_partition(
        self,
        data_dir: str,
        as_of_date: Optional[str] = None,
        tickers: Optional[Sequence[str]] = None,
    ) -> Tuple[Dict[str, List[OHLCVCandle]], Dict[str, List[OHLCVCandle]], str, List[str]]:
        """Load and validate daily candles from current_daily CSV partition directory."""
        if not os.path.exists(data_dir):
            raise FileNotFoundError(f"Daily market data directory not found: '{data_dir}'.")

        # Check CURRENT_DAILY_MANIFEST.json if present
        manifest_path = os.path.join(data_dir, "CURRENT_DAILY_MANIFEST.json")
        if os.path.exists(manifest_path):
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
                val_status = manifest.get("validation_results", {}).get("validation_status") or manifest.get("validation_status")
                if val_status and val_status != "PASSED":
                    raise ValueError(f"DATASET_VALIDATION_FAILED: CURRENT_DAILY_MANIFEST validation status is '{val_status}'.")
            except (json.JSONDecodeError, OSError) as e:
                raise ValueError(f"DATASET_MANIFEST_CORRUPT: Failed to read CURRENT_DAILY_MANIFEST.json: {e}")

        from observation_workflow import load_daily_candles_from_dir
        as_of_d = date.fromisoformat(as_of_date) if as_of_date else None
        proxy_map, stock_map, effective_date, warnings = load_daily_candles_from_dir(
            data_dir=data_dir,
            as_of_date=as_of_d,
            tickers=tickers,
        )
        return proxy_map, stock_map, effective_date.isoformat(), warnings

    def run_forward_session(
        self,
        session_date: str,
        proxy_candles_map: Dict[str, Sequence[OHLCVCandle]],
        stock_candles_map: Dict[str, Sequence[OHLCVCandle]],
        data_mode: str = ForwardDataMode.LIVE.value,
        force_idempotent_overwrite: bool = False,
    ) -> EvaluationSessionSummary:
        """Execute a forward observation session on completed daily candles.
        
        Fail-Closed Stop Conditions:
        - Incomplete, forming, or missing candles raise ValueError.
        - Non-positive prices or malformed OHLC relationships raise ValueError.
        """
        now_utc_str = datetime.now(timezone.utc).isoformat()

        # 1. Validate Data Freshness, Completion, and Provenance
        all_candles_map = {**proxy_candles_map, **stock_candles_map}
        # 1a. Ensure zero-candle failure is detected first across all symbols
        for ticker, candles in all_candles_map.items():
            if not candles:
                raise ValueError(f"CRITICAL DATA FAILURE: Zero candles provided for '{ticker}'.")

        # 1b. Validate candle integrity, geometry, and status
        for ticker, candles in all_candles_map.items():
            latest_bar = candles[-1]
            if latest_bar.status != CandleStatus.COMPLETE:
                raise ValueError(
                    f"CRITICAL SAFETY VIOLATION: Incomplete or forming candle detected for '{ticker}' at {latest_bar.timestamp}. Forward evaluation strictly requires completed bars."
                )
            if latest_bar.open <= 0 or latest_bar.high <= 0 or latest_bar.low <= 0 or latest_bar.close <= 0:
                raise ValueError(f"CRITICAL DATA FAILURE: Non-positive price in candle for '{ticker}'.")
            if latest_bar.low > latest_bar.high or latest_bar.open > latest_bar.high or latest_bar.close > latest_bar.high:
                raise ValueError(f"CRITICAL DATA FAILURE: Malformed OHLC geometry for '{ticker}'.")

        # 1c. Validate session date alignment across all symbols
        for ticker, candles in all_candles_map.items():
            latest_bar = candles[-1]
            bar_date = (
                latest_bar.timestamp.date().isoformat()
                if (latest_bar.timestamp.hour == 0 and latest_bar.timestamp.minute == 0)
                else to_eastern(latest_bar.timestamp).date().isoformat()
            )
            if bar_date != session_date:
                raise ValueError(
                    f"SESSION_DATE_MISMATCH: Candle for '{ticker}' at {latest_bar.timestamp.isoformat()} (date '{bar_date}') "
                    f"does not match requested session date '{session_date}'."
                )

        # 1b. Safety and Idempotency Gates for Genuine Forward Modes (DAILY / LIVE)
        if data_mode in (ForwardDataMode.DAILY.value, ForwardDataMode.LIVE.value):
            # Retrospective replay protection
            if session_date <= "2026-11-16":
                raise ValueError(
                    f"RETROSPECTIVE_REPLAY_REJECTED: Session date '{session_date}' has already been evaluated historically "
                    f"(Phase 6.4 baseline ends on 2026-11-16). Forward evaluation strictly requires fresh sessions (> 2026-11-16)."
                )

            # Duplicate session summary check
            if not force_idempotent_overwrite and self.storage.has_session_summary(session_date, data_mode):
                raise DuplicateObservationError(
                    f"Duplicate forward session rejected: session_date='{session_date}', mode='{data_mode}' has already been evaluated."
                )

        # 1c. Indicator Warm-up Verification for DAILY Mode
        if data_mode == ForwardDataMode.DAILY.value:
            spy_candles = proxy_candles_map.get("SPY")
            if not spy_candles:
                raise ValueError("CRITICAL DATA FAILURE: Mandatory proxy 'SPY' missing from proxy candles.")
            if len(spy_candles) < 200:
                raise ValueError(
                    f"INSUFFICIENT_HISTORY: Mandatory proxy 'SPY' requires at least 200 completed bars for 200-SMA regime detection, got {len(spy_candles)}."
                )

        # 2. Evaluate Macro Market Regime
        regime_result: MarketRegimeResult = self.regime_detector.detect_regime(
            proxy_candles_map=proxy_candles_map,
            require_complete=False,
        )
        regime_val = regime_result.regime.value
        regime_allows_longs = regime_result.policy.allow_new_longs

        # 3. Update Active Hypothetical Positions with Today's Bar
        active_positions = self.storage.load_active_positions()
        trades_closed_today = 0

        for pos in list(active_positions):
            candles = stock_candles_map.get(pos.ticker)
            if candles:
                today_candle = candles[-1]
                closed_trade, _ = self.trade_tracker.update_position_with_bar(
                    position=pos,
                    candle=today_candle,
                )
                if closed_trade is not None:
                    trades_closed_today += 1

        # Re-fetch active positions after updates
        active_positions = self.storage.load_active_positions()

        # 4. Compute Virtual Available Cash
        closed_trades_all = self.storage.load_closed_trades()
        cumulative_realized_pnl = sum((t.net_pnl for t in closed_trades_all), Decimal("0.00"))
        open_cost_basis = sum((p.cost_basis_usd for p in active_positions), Decimal("0.00"))
        virtual_available_cash = self.starting_capital + cumulative_realized_pnl - open_cost_basis

        # 5. Run Screener on Stock Universe
        universe = list(stock_candles_map.keys())
        scan_result: ScanResult = self.scanner.scan_universe(
            candles_map=stock_candles_map,
            universe=universe,
        )
        ranked_candidates_map = {c.ticker: c for c in scan_result.candidates}

        # 6. Generate Trade Signals & Risk Sizing
        signal_summary = self.signal_engine.evaluate_candidates(
            candidates=scan_result.candidates,
            candles_map=stock_candles_map,
            require_complete=False,
        )
        approved_plans_map = {p.ticker: p for p in signal_summary.risk_approved_plans}
        rejected_plans_map = {p.ticker: p for p in signal_summary.risk_rejected_plans}

        # 7. Record Forward Observations per Symbol
        eligible_count = 0
        rejected_count = 0
        proposals_created_count = 0
        trades_opened_count = 0

        for ticker in universe:
            candles = stock_candles_map[ticker]
            latest_bar = candles[-1]
            obs_id = f"OBS-{session_date}-{ticker}-{data_mode}"

            scanned_cand = ranked_candidates_map.get(ticker)
            approved_plan = approved_plans_map.get(ticker)
            rejected_plan = rejected_plans_map.get(ticker)

            five_pillar_scores: Dict[str, Any] = {}
            rejection_reasons: List[str] = []
            decision = ForwardScreeningDecision.REJECTED.value
            is_eligible = False
            signal_type: Optional[str] = None
            proposed_entry: Optional[Decimal] = None
            proposed_stop: Optional[Decimal] = None
            proposed_target: Optional[Decimal] = None
            risk_sizing: Optional[Dict[str, Any]] = None

            if scanned_cand is not None:
                five_pillar_scores = {
                    "score": str(scanned_cand.score),
                    "rank": scanned_cand.rank,
                    "price": str(scanned_cand.price),
                    "indicators": {k: str(v) for k, v in (scanned_cand.indicators or {}).items()},
                }

            if not regime_allows_longs:
                rejection_reasons.append(f"MARKET_REGIME_BEARISH: '{regime_val}' policy forbids new long entries.")

            if approved_plan is not None and regime_allows_longs:
                decision = ForwardScreeningDecision.ELIGIBLE.value
                is_eligible = True
                signal_type = approved_plan.strategy_type.value
                proposed_entry = approved_plan.entry_price
                proposed_stop = approved_plan.stop_loss_price
                proposed_target = approved_plan.target_price
                risk_sizing = {
                    "shares": approved_plan.shares,
                    "capital_required": str(approved_plan.total_capital_required),
                    "buffered_risk": str(approved_plan.total_buffered_risk),
                    "risk_approved": True,
                }
            else:
                if rejected_plan is not None:
                    rejection_reasons.extend(rejected_plan.risk_rejection_reasons)
                elif scanned_cand is None:
                    rejection_reasons.append("SCANNER_FILTER_FAILED: Candidate did not satisfy technical screening criteria.")

            provenance = {
                "source": latest_bar.source,
                "interval": latest_bar.interval,
                "candle_count": len(candles),
                "latest_bar_timestamp": latest_bar.timestamp.isoformat(),
                "volume": latest_bar.volume,
                "close_price": str(latest_bar.close),
            }

            obs_record = ForwardObservationRecord(
                observation_id=obs_id,
                session_date=session_date,
                observation_timestamp=now_utc_str,
                ticker=ticker,
                data_mode=data_mode,
                data_provenance=provenance,
                market_regime=regime_val,
                regime_allows_longs=regime_allows_longs,
                screening_decision=decision,
                five_pillar_scores=five_pillar_scores,
                rejection_reasons=rejection_reasons,
                signal_type=signal_type,
                proposed_entry=proposed_entry,
                proposed_stop=proposed_stop,
                proposed_target=proposed_target,
                risk_sizing=risk_sizing,
                is_eligible=is_eligible,
            )

            try:
                self.storage.save_observation(obs_record)
            except DuplicateObservationError:
                if not force_idempotent_overwrite:
                    # In normal operation, repeated observations within same mode raise DuplicateObservationError
                    pass

            if is_eligible:
                eligible_count += 1
                # Attempt to open hypothetical position
                prop, pos, reject_reason = self.trade_tracker.create_proposal_and_open_position(
                    observation_id=obs_id,
                    session_date=session_date,
                    ticker=ticker,
                    data_mode=data_mode,
                    shares=approved_plan.shares,
                    entry_price=proposed_entry,
                    stop_loss=proposed_stop,
                    target=proposed_target,
                    virtual_available_cash=virtual_available_cash,
                    active_positions=active_positions,
                )
                if prop is not None:
                    proposals_created_count += 1
                if pos is not None:
                    trades_opened_count += 1
                    active_positions.append(pos)
                    virtual_available_cash -= pos.cost_basis_usd
            else:
                rejected_count += 1

        # 8. Reconcile Virtual Equity
        active_positions_final = self.storage.load_active_positions()
        curr_unrealized = sum((p.unrealized_pnl for p in active_positions_final), Decimal("0.00"))
        open_market_val = sum((p.market_value_usd for p in active_positions_final), Decimal("0.00"))
        final_virtual_cash = self.starting_capital + cumulative_realized_pnl - sum((p.cost_basis_usd for p in active_positions_final), Decimal("0.00"))
        final_virtual_equity = final_virtual_cash + open_market_val
        total_fees = sum((t.entry_fee + t.exit_fee for t in closed_trades_all), Decimal("0.00")) + sum((p.entry_fee for p in active_positions_final), Decimal("0.00"))

        summary = EvaluationSessionSummary(
            session_date=session_date,
            data_mode=data_mode,
            market_regime=regime_val,
            tickers_screened=len(universe),
            eligible_candidates=eligible_count,
            rejected_candidates=rejected_count,
            proposals_created=proposals_created_count,
            hypothetical_trades_opened=trades_opened_count,
            hypothetical_trades_closed=trades_closed_today,
            active_positions_count=len(active_positions_final),
            virtual_equity=quantize_cent(final_virtual_equity),
            virtual_cash=quantize_cent(final_virtual_cash),
            realized_net_pnl=quantize_cent(cumulative_realized_pnl),
            unrealized_pnl=quantize_cent(curr_unrealized),
            total_fees_paid=quantize_cent(total_fees),
        )
        self.storage.save_session_summary(summary, force_overwrite=force_idempotent_overwrite)
        return summary

    def generate_performance_report(
        self,
        data_mode: Optional[str] = None,
        genuine_only: bool = True,
    ) -> ForwardPerformanceReport:
        """Generate a statistically honest forward performance evaluation report.
        
        Default (genuine_only=True) includes only genuine DAILY/LIVE records.
        """
        return self.performance_engine.evaluate_performance(
            data_mode=data_mode,
            genuine_only=genuine_only,
        )
