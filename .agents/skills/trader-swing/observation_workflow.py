"""Manual Paper Observation Workflow for Phase 6.4.1 (trader-swing).

Enforces:
1. On-demand manual research scan with zero scheduling, polling, or daemons.
2. Read-only inspection of paper trading state; zero mutations to journals,
   ledgers, orders, or portfolio files.
3. Transparent reporting of market regime, eligible candidates, rejected candidates
   with explicit reasons, and Phase 2 deterministic risk calculations.
4. Detection and clear disclosure of stale, missing, or unavailable market data.
5. Generation of structured ObservationRecords strictly distinguished from executed
   trades (executed_fill=False, record_type="FORWARD_OBSERVATION").
6. Complete rejection of forbidden flags (--live, --broker, --key, --auto, etc.).
7. Strict preservation of frozen strategy rules, parameters, indicators, and risk limits.
"""

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timezone
from decimal import Decimal
import argparse
import csv
import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

REPO_ROOT = os.path.realpath(os.path.abspath(os.path.join(CURRENT_DIR, "..", "..", "..")))

from accounting import AccountingLedger, quantize_cent, to_decimal
from feed_adapter import (
    DualSourceFeedAdapter,
    FeedReadinessResult,
    MarketDataFeedAdapter,
    MarketDataReadinessEvaluator,
    ReadinessStatus,
    StockExclusion,
    UnconsolidatedVolumeError,
)
from dual_source_sync_gateway import DualSourceSyncGateway, FeedState, ScreeningEligibilityResult
from market_data_models import (
    CandleStatus,
    EASTERN,
    MarketClock,
    OHLCVCandle,
    to_eastern,
    to_utc,
    UTC,
)
from paper_models import PaperExitReason
from paper_service import PaperTradingService
from paper_storage import DEFAULT_PAPER_STATE_DIR, PaperStorageManager
from regime_detector import MarketRegimeDetector
from regime_models import MarketRegime, MarketRegimeResult
from risk import RiskConfig, RiskEngine, SizingResult
from scanner import StockScanner
from scanner_models import ScannedCandidate, ScanResult, ScreeningCriteria
from signal_engine import SignalEngine, SignalGenerationSummary
from signal_models import ExitSignal, ProposedTradePlan
from universe import DEFAULT_CURATED_WATCHLIST, normalize_universe

logger = logging.getLogger(__name__)

# Default paths
DEFAULT_DATA_DIR = os.path.join(
    REPO_ROOT, ".planning", "data", "historical_partitions", "2021_2023_primary_corrected"
)
DEFAULT_CURRENT_DATA_DIR = os.path.join(
    REPO_ROOT, ".planning", "data", "current_daily"
)
DEFAULT_RECORD_FILE = os.path.join(
    REPO_ROOT, ".planning", "data", "observations", "observation_log.jsonl"
)
DEFAULT_LIVE_RECORD_FILE = os.path.join(
    REPO_ROOT, ".planning", "data", "observations", "live_observation_log.jsonl"
)

CORE_PROXIES = ("SPY", "QQQ", "IWM")


@dataclass(frozen=True)
class ObservationRecord:
    """Structured record of an observed market signal during manual forward observation.

    CRITICAL DISTINCTION:
    This is an OBSERVATION ONLY. It is NOT an executed order, fill, or portfolio position.
    It does not commit capital, modify cash balances, or alter the paper trading ledger.
    """
    date: str
    ticker: str
    market_regime: str
    signal: str
    entry_reference: Optional[str] = None
    stop_reference: Optional[str] = None
    risk_estimate: Optional[str] = None
    decision: str = "OBSERVE_ONLY"
    notes: str = ""
    record_type: str = "FORWARD_OBSERVATION"
    executed_fill: bool = False
    scanner_decision: Optional[str] = None
    candidate_score: Optional[str] = None
    rejection_reasons: Optional[List[str]] = None
    technical_metrics: Optional[Dict[str, Any]] = None
    reached_signal_generation: bool = False
    reached_risk_sizing: bool = False
    signal_decision: Optional[str] = None
    risk_decision: Optional[str] = None
    data_mode: Optional[str] = None
    data_classification: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "record_type": self.record_type,
            "date": self.date,
            "ticker": self.ticker,
            "market_regime": self.market_regime,
            "scanner_decision": self.scanner_decision,
            "candidate_score": self.candidate_score,
            "rejection_reasons": list(self.rejection_reasons) if self.rejection_reasons is not None else [],
            "technical_metrics": self.technical_metrics,
            "reached_signal_generation": self.reached_signal_generation,
            "reached_risk_sizing": self.reached_risk_sizing,
            "signal_decision": self.signal_decision,
            "risk_decision": self.risk_decision,
            "signal": self.signal,
            "entry_reference": self.entry_reference,
            "stop_reference": self.stop_reference,
            "risk_estimate": self.risk_estimate,
            "decision": self.decision,
            "notes": self.notes,
            "executed_fill": self.executed_fill,
        }
        if self.data_mode is not None:
            d["data_mode"] = self.data_mode
        if self.data_classification is not None:
            d["data_classification"] = self.data_classification
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict())


@dataclass(frozen=True)
class ObservationSessionRecord:
    """Session-level auditable summary of an observation scan run."""
    date: str
    market_regime: str
    tickers_evaluated_count: int
    eligible_count: int
    rejected_count: int
    signal_generation_count: int
    risk_sizing_count: int
    observation_records_count: int
    data_classification: str
    record_type: str = "SESSION_RECORD"
    executed_fill: bool = False
    notes: str = ""
    data_mode: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "record_type": self.record_type,
            "date": self.date,
            "market_regime": self.market_regime,
            "tickers_evaluated_count": self.tickers_evaluated_count,
            "eligible_count": self.eligible_count,
            "rejected_count": self.rejected_count,
            "signal_generation_count": self.signal_generation_count,
            "risk_sizing_count": self.risk_sizing_count,
            "observation_records_count": self.observation_records_count,
            "data_classification": self.data_classification,
            "executed_fill": self.executed_fill,
            "notes": self.notes,
        }
        if self.data_mode is not None:
            d["data_mode"] = self.data_mode
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict())


def read_observation_log(record_file: str) -> List[Dict[str, Any]]:
    """Read JSON lines from observation log file."""
    if not os.path.exists(record_file):
        return []
    records: List[Dict[str, Any]] = []
    with open(record_file, "r", encoding="utf-8") as f:
        for line in f:
            line_str = line.strip()
            if line_str:
                records.append(json.loads(line_str))
    return records


@dataclass
class DailyResearchReport:
    """Comprehensive, immutable daily research scan report for manual paper observation."""
    as_of_date: str
    scan_timestamp: str
    data_dir: str
    feed_status: str
    market_regime: Optional[MarketRegimeResult]
    portfolio_status: Dict[str, Any]
    eligible_candidates: List[ProposedTradePlan]
    risk_rejected_candidates: List[ProposedTradePlan]
    scanner_rejected_candidates: Dict[str, List[str]]
    feed_excluded_tickers: List[StockExclusion]
    exit_signals: List[ExitSignal]
    observation_records: List[ObservationRecord]
    warnings: List[str]
    failure_reasons: List[str]
    records_saved_to: Optional[str] = None
    data_mode: str = "HISTORICAL_PARTITION_REPLAY"
    data_classification: str = "VERIFIED_HISTORICAL (SPLIT_ADJUSTED)"
    session_record: Optional[ObservationSessionRecord] = None


def load_daily_candles_from_dir(
    data_dir: str,
    as_of_date: Optional[date] = None,
    tickers: Optional[Sequence[str]] = None,
) -> Tuple[Dict[str, List[OHLCVCandle]], Dict[str, List[OHLCVCandle]], date, List[str]]:
    """Load daily candles from CSV partition directory up to as_of_date.

    Guarantees:
    - Strict chronological ordering.
    - Zero look-ahead: candles with session date > as_of_date are strictly excluded.
    - Separates proxy candles (SPY, QQQ, IWM) from stock universe.
    - Returns effective as_of_date (inferred from SPY if not provided).
    """
    if not os.path.exists(data_dir):
        raise FileNotFoundError(f"Market data directory not found: {data_dir}")

    warnings: List[str] = []
    target_tickers = set(tickers) if tickers else None

    # Step 1: Discover available CSV files
    all_files = os.listdir(data_dir)
    csv_files = {}
    for fname in all_files:
        if fname.endswith("_daily.csv"):
            sym = fname[:-10].upper()
            csv_files[sym] = os.path.join(data_dir, fname)
        elif fname.endswith(".csv") and not fname.startswith("DATA_") and not fname.startswith("MANIFEST"):
            sym = fname[:-4].upper()
            csv_files[sym] = os.path.join(data_dir, fname)

    if "SPY" not in csv_files:
        raise ValueError(f"Mandatory proxy 'SPY' CSV missing in market data directory: {data_dir}")

    # Step 2: Determine effective as_of_date if not specified
    effective_date = as_of_date
    if effective_date is None:
        # Scan SPY for max date
        max_d: Optional[date] = None
        with open(csv_files["SPY"], "r", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if "Date" in r:
                    d = date.fromisoformat(r["Date"])
                    if max_d is None or d > max_d:
                        max_d = d
        if max_d is None:
            raise ValueError(f"No valid Date rows found in SPY CSV: {csv_files['SPY']}")
        effective_date = max_d

    # Step 3: Load proxy and stock candles
    proxy_map: Dict[str, List[OHLCVCandle]] = {}
    stock_map: Dict[str, List[OHLCVCandle]] = {}

    for sym, fpath in csv_files.items():
        is_proxy = sym in CORE_PROXIES
        if not is_proxy and target_tickers and sym not in target_tickers:
            continue

        candles: List[OHLCVCandle] = []
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for r in reader:
                    sess_d = date.fromisoformat(r["Date"])
                    if sess_d > effective_date:
                        continue  # Zero look-ahead enforcement

                    open_px = Decimal(str(r["Open"]))
                    high_px = Decimal(str(r["High"]))
                    low_px = Decimal(str(r["Low"]))
                    close_px = Decimal(str(r["Close"]))
                    vol = int(r["Volume"])

                    # Invariant checks
                    if high_px < low_px or high_px < open_px or high_px < close_px or low_px > open_px or low_px > close_px:
                        warnings.append(f"Corrupted OHLC on {sess_d} for {sym}; skipping candle.")
                        continue

                    start_et = datetime.combine(sess_d, MarketClock.REGULAR_OPEN, tzinfo=EASTERN)
                    ts_utc = to_utc(start_et)

                    candles.append(
                        OHLCVCandle(
                            ticker=sym,
                            interval="1d",
                            timestamp=ts_utc,
                            open=open_px,
                            high=high_px,
                            low=low_px,
                            close=close_px,
                            volume=vol,
                            status=CandleStatus.COMPLETE,
                        )
                    )
        except Exception as e:
            warnings.append(f"Failed to read CSV for {sym} ({fpath}): {e}")
            continue

        # Sort chronologically
        candles.sort(key=lambda c: c.timestamp)

        if is_proxy:
            proxy_map[sym] = candles
        else:
            stock_map[sym] = candles

    return proxy_map, stock_map, effective_date, warnings


def run_observation_scan(
    data_dir: str = DEFAULT_DATA_DIR,
    as_of_date: Optional[date] = None,
    tickers: Optional[Sequence[str]] = None,
    state_dir: str = DEFAULT_PAPER_STATE_DIR,
    top_n: int = 5,
    record_file: Optional[str] = None,
    write_records: bool = True,
    gateway: Optional[Any] = None,
    data_mode: Optional[str] = None,
    current_time: Optional[datetime] = None,
) -> DailyResearchReport:
    """Execute on-demand manual research scan and generate forward observation records.

    Strictly read-only: zero mutations to paper journal, orders, or portfolio files.
    """
    now_utc = to_utc(current_time) if current_time is not None else datetime.now(timezone.utc)
    warnings: List[str] = []
    failure_reasons: List[str] = []

    if as_of_date is not None:
        eval_time = datetime.combine(as_of_date, MarketClock.REGULAR_CLOSE, tzinfo=EASTERN)
    else:
        eval_time = now_utc

    is_live = (gateway is not None) or (data_mode == "LIVE_IEX_HYBRID")

    if is_live:
        data_mode = "LIVE_IEX_HYBRID"
        data_class = "LIVE_IEX_HYBRID (ALPACA_IEX_PRICE + YAHOO_CONSOLIDATED_VOLUME)"
        if record_file is None:
            record_file = DEFAULT_LIVE_RECORD_FILE
    else:
        manifest_path = os.path.join(data_dir, "CURRENT_DAILY_MANIFEST.json")
        is_current = os.path.exists(manifest_path) or "current_daily" in os.path.normpath(data_dir).lower()
        data_mode = (
            "CURRENT_DAILY_OBSERVATION"
            if is_current
            else "HISTORICAL_PARTITION_REPLAY"
        )
        data_class = (
            "DELAYED_UNVERIFIED (UNVERIFIED_DELAY — NOT REAL-TIME)"
            if is_current
            else "VERIFIED_HISTORICAL (SPLIT_ADJUSTED)"
        )
        if record_file is None:
            record_file = DEFAULT_RECORD_FILE

    # 1. Read-only paper trading portfolio status inspection
    portfolio_status: Dict[str, Any] = {}
    ledger: Optional[AccountingLedger] = None
    try:
        storage = PaperStorageManager(state_dir=state_dir)
        service = PaperTradingService(storage=storage)
        portfolio_status = service.get_status()
        journal = storage.load_journal()
        ledger = journal.ledger
    except Exception as e:
        warnings.append(f"External paper state inspection note: {e}. Using virtual $5,000 ledger for sizing.")
        ledger = AccountingLedger(initial_capital=Decimal("5000.00"))
        portfolio_status = {
            "initial_capital_usd": "5000.00",
            "cash_usd": "5000.00",
            "total_market_value_usd": "0.00",
            "total_equity_usd": "5000.00",
            "open_positions_count": 0,
            "max_simultaneous_positions_limit": 4,
            "active_tickers": [],
            "equity_invariant_holds": True,
            "active_lots": [],
            "note": "Virtual default ledger used (external state uninitialized or unreadable in read-only mode).",
        }

    # 2 & 3. Market Data Loading & Readiness Evaluation
    if is_live:
        display_data_dir = "LIVE_GATEWAY (DualSourceSyncGateway)"
        clock = getattr(gateway, "clock", None) or MarketClock()
        if gateway is None:
            gateway = DualSourceSyncGateway(clock=clock)

        target_universe = list(tickers) if tickers else list(DEFAULT_CURATED_WATCHLIST)
        adapter = DualSourceFeedAdapter(gateway=gateway, clock=clock)

        try:
            readiness = adapter.fetch_and_evaluate(
                stock_universe=target_universe,
                current_time=eval_time,
            )
            warnings.extend(readiness.warnings)
            failure_reasons.extend(readiness.failure_reasons)
        except Exception as e:
            failure_reasons.append(f"Live dual-source feed evaluation failed: {e}")
            return DailyResearchReport(
                as_of_date=as_of_date.isoformat() if as_of_date else "UNKNOWN",
                scan_timestamp=now_utc.isoformat(),
                data_dir=display_data_dir,
                feed_status="HARD_FAIL",
                market_regime=None,
                portfolio_status=portfolio_status,
                eligible_candidates=[],
                risk_rejected_candidates=[],
                scanner_rejected_candidates={},
                feed_excluded_tickers=[],
                exit_signals=[],
                observation_records=[],
                warnings=warnings,
                failure_reasons=failure_reasons,
                data_mode=data_mode,
                data_classification=data_class,
            )

        effective_date = as_of_date
        if effective_date is None:
            effective_date = readiness.latest_completed_session
            if effective_date is None and "SPY" in readiness.proxy_candles and readiness.proxy_candles["SPY"]:
                effective_date = to_eastern(readiness.proxy_candles["SPY"][-1].timestamp).date()
            if effective_date is None:
                effective_date = to_eastern(now_utc).date()

        # Enforce completed session constraint
        evaluator = MarketDataReadinessEvaluator(clock=clock)
        real_latest_completed_sess = evaluator.get_latest_completed_session(now_utc)
        if real_latest_completed_sess is None or effective_date > real_latest_completed_sess:
            failure_reasons.append(
                f"INCOMPLETE_SESSION: Session date {effective_date} has not yet completed as of {now_utc.isoformat()} "
                f"(latest completed regular NYSE session is {real_latest_completed_sess}). "
                "Dual-source live screening strictly requires completed sessions after 16:00 ET."
            )
            return DailyResearchReport(
                as_of_date=effective_date.isoformat(),
                scan_timestamp=now_utc.isoformat(),
                data_dir=display_data_dir,
                feed_status="HARD_FAIL",
                market_regime=None,
                portfolio_status=portfolio_status,
                eligible_candidates=[],
                risk_rejected_candidates=[],
                scanner_rejected_candidates={},
                feed_excluded_tickers=readiness.excluded_tickers,
                exit_signals=[],
                observation_records=[],
                warnings=warnings,
                failure_reasons=failure_reasons,
                data_mode=data_mode,
                data_classification=data_class,
            )

        if not readiness.is_ready or readiness.status != ReadinessStatus.READY:
            return DailyResearchReport(
                as_of_date=effective_date.isoformat(),
                scan_timestamp=now_utc.isoformat(),
                data_dir=display_data_dir,
                feed_status=readiness.status.value,
                market_regime=None,
                portfolio_status=portfolio_status,
                eligible_candidates=[],
                risk_rejected_candidates=[],
                scanner_rejected_candidates={},
                feed_excluded_tickers=readiness.excluded_tickers,
                exit_signals=[],
                observation_records=[],
                warnings=warnings,
                failure_reasons=failure_reasons,
                data_mode=data_mode,
                data_classification=data_class,
            )

        # Verification of screening eligibility per ticker
        verified_included = []
        for sym in readiness.included_tickers:
            if hasattr(gateway, "get_screening_eligibility"):
                elig = gateway.get_screening_eligibility(sym, current_time=now_utc)
                if not elig.is_eligible or elig.state != FeedState.LIVE_IEX_HYBRID:
                    warnings.append(f"Ticker {sym} excluded from live screening: {elig.reason}")
                    continue
            verified_included.append(sym)
        screening_universe = verified_included

    else:
        display_data_dir = data_dir
        # 2. Load daily market candles up to as_of_date
        try:
            proxy_map, stock_map, effective_date, load_warnings = load_daily_candles_from_dir(
                data_dir=data_dir,
                as_of_date=as_of_date,
                tickers=tickers,
            )
            warnings.extend(load_warnings)
        except Exception as e:
            failure_reasons.append(f"Failed to load market data: {e}")
            return DailyResearchReport(
                as_of_date=as_of_date.isoformat() if as_of_date else "UNKNOWN",
                scan_timestamp=now_utc.isoformat(),
                data_dir=display_data_dir,
                feed_status="HARD_FAIL",
                market_regime=None,
                portfolio_status=portfolio_status,
                eligible_candidates=[],
                risk_rejected_candidates=[],
                scanner_rejected_candidates={},
                feed_excluded_tickers=[],
                exit_signals=[],
                observation_records=[],
                warnings=warnings,
                failure_reasons=failure_reasons,
                data_mode=data_mode,
                data_classification=data_class,
            )

        # 2b. If observing current market data, strictly enforce completed-session requirement
        if is_current:
            clock = MarketClock()
            evaluator = MarketDataReadinessEvaluator(clock=clock)
            real_latest_completed_sess = evaluator.get_latest_completed_session(now_utc)
            if real_latest_completed_sess is None or effective_date > real_latest_completed_sess:
                failure_reasons.append(
                    f"INCOMPLETE_SESSION: Requested session date {effective_date} has not yet completed as of {now_utc.isoformat()} "
                    f"(latest completed regular NYSE session is {real_latest_completed_sess}). "
                    "Current observation requires completed sessions after 16:00 ET."
                )
                return DailyResearchReport(
                    as_of_date=effective_date.isoformat(),
                    scan_timestamp=now_utc.isoformat(),
                    data_dir=display_data_dir,
                    feed_status="HARD_FAIL",
                    market_regime=None,
                    portfolio_status=portfolio_status,
                    eligible_candidates=[],
                    risk_rejected_candidates=[],
                    scanner_rejected_candidates={},
                    feed_excluded_tickers=[],
                    exit_signals=[],
                    observation_records=[],
                    warnings=warnings,
                    failure_reasons=failure_reasons,
                    data_mode=data_mode,
                    data_classification=data_class,
                )

        # 3. Market Data Feed Readiness Evaluation
        adapter = MarketDataFeedAdapter()
        readiness = adapter.evaluate_readiness(
            proxy_candles_map=proxy_map,
            stock_candles_map=stock_map,
            current_time=datetime.combine(effective_date, MarketClock.REGULAR_CLOSE, tzinfo=EASTERN),
        )

        warnings.extend(readiness.warnings)
        failure_reasons.extend(readiness.failure_reasons)

        if not readiness.is_ready or readiness.status != ReadinessStatus.READY:
            return DailyResearchReport(
                as_of_date=effective_date.isoformat(),
                scan_timestamp=now_utc.isoformat(),
                data_dir=display_data_dir,
                feed_status=readiness.status.value,
                market_regime=None,
                portfolio_status=portfolio_status,
                eligible_candidates=[],
                risk_rejected_candidates=[],
                scanner_rejected_candidates={},
                feed_excluded_tickers=readiness.excluded_tickers,
                exit_signals=[],
                observation_records=[],
                warnings=warnings,
                failure_reasons=failure_reasons,
                data_mode=data_mode,
                data_classification=data_class,
            )
        screening_universe = readiness.included_tickers

    # 4. Detect Market Regime
    regime_detector = MarketRegimeDetector()
    regime_result = regime_detector.detect_regime(
        proxy_candles_map=readiness.proxy_candles,
        current_time=datetime.combine(effective_date, MarketClock.REGULAR_CLOSE, tzinfo=EASTERN),
        require_complete=True,
    )
    warnings.extend(regime_result.warnings)

    # 5. Screen Universe and Rank Candidates
    target_universe = screening_universe
    scanner = StockScanner()
    scan_result = scanner.scan_universe(
        universe=target_universe,
        candles_map=readiness.stock_candles,
        regime_result=regime_result,
        top_n=top_n,
        require_complete=True,
    )
    warnings.extend(scan_result.warnings)

    # 6. Signal Generation and Phase 2 Risk Sizing
    signal_engine = SignalEngine()
    signal_summary = signal_engine.evaluate_candidates(
        candidates=scan_result.candidates[:top_n],
        candles_map=readiness.stock_candles,
        ledger=ledger,
        require_complete=True,
    )

    # 7. Evaluate Exits for Open Positions (Read-only)
    exit_signals: List[ExitSignal] = []
    if ledger is not None and len(ledger.positions) > 0:
        exit_signals = signal_engine.evaluate_exits(
            ledger=ledger,
            candles_map=readiness.stock_candles,
            regime_result=regime_result,
            require_complete=True,
        )

    # 8. Build Structured Observation Records (One per evaluated candidate, avoiding duplicates)
    observation_records: List[ObservationRecord] = []
    effective_date_str = effective_date.isoformat()
    regime_str = regime_result.regime.value

    # Map trade plans by ticker
    approved_plans_map = {p.ticker: p for p in signal_summary.risk_approved_plans}
    risk_rejected_plans_map = {p.ticker: p for p in signal_summary.risk_rejected_plans}
    technically_invalid_set = set(getattr(signal_summary, "technically_invalid_tickers", []))

    evaluated_candidates_map = getattr(scan_result, "evaluated_candidates", {})

    # Order evaluated tickers: ranked eligible candidates first, followed by scanner-rejected candidates
    eligible_tickers = [c.ticker for c in scan_result.candidates]
    rejected_tickers = [
        t for t in evaluated_candidates_map.keys() if t not in eligible_tickers
    ]
    for t in scan_result.rejected_candidates.keys():
        if t not in evaluated_candidates_map and t not in rejected_tickers:
            rejected_tickers.append(t)

    ordered_tickers = eligible_tickers + rejected_tickers
    recorded_tickers: Set[str] = set()

    for ticker in ordered_tickers:
        if ticker in recorded_tickers:
            continue
        recorded_tickers.add(ticker)

        candidate = evaluated_candidates_map.get(ticker)
        candidate_score_str = (
            str(candidate.score.total_score)
            if candidate is not None and candidate.score is not None
            else None
        )
        technical_metrics = candidate.indicators if candidate is not None else None

        if ticker in approved_plans_map:
            plan = approved_plans_map[ticker]
            rec = ObservationRecord(
                date=effective_date_str,
                ticker=ticker,
                market_regime=regime_str,
                signal="ENTRY_SIGNAL",
                entry_reference=str(plan.entry_price),
                stop_reference=str(plan.stop_loss_price),
                risk_estimate=f"${plan.total_buffered_risk} total risk ({plan.shares} shs @ ${plan.entry_price})",
                decision="OBSERVE_ONLY",
                notes=(
                    f"Approved plan; Target=${plan.target_price} ({plan.reward_to_risk}R); "
                    f"Capital req=${plan.total_capital_required}; Pending manual review"
                ),
                record_type="FORWARD_OBSERVATION",
                executed_fill=False,
                scanner_decision="ELIGIBLE",
                candidate_score=candidate_score_str,
                rejection_reasons=[],
                technical_metrics=technical_metrics,
                reached_signal_generation=True,
                reached_risk_sizing=True,
                signal_decision="ENTRY_SIGNAL",
                risk_decision="RISK_APPROVED",
                data_mode=data_mode,
                data_classification=data_class,
            )
            observation_records.append(rec)

        elif ticker in risk_rejected_plans_map:
            plan = risk_rejected_plans_map[ticker]
            rec = ObservationRecord(
                date=effective_date_str,
                ticker=ticker,
                market_regime=regime_str,
                signal="REJECTED_RISK",
                entry_reference=str(plan.entry_price),
                stop_reference=str(plan.stop_loss_price),
                risk_estimate="$0.00",
                decision="SKIP",
                notes=f"Binding constraint: {plan.binding_constraint}; Reasons: {'; '.join(plan.risk_rejection_reasons)}",
                record_type="FORWARD_OBSERVATION",
                executed_fill=False,
                scanner_decision="ELIGIBLE",
                candidate_score=candidate_score_str,
                rejection_reasons=[],
                technical_metrics=technical_metrics,
                reached_signal_generation=True,
                reached_risk_sizing=True,
                signal_decision="ENTRY_SIGNAL",
                risk_decision="REJECTED_RISK",
                data_mode=data_mode,
                data_classification=data_class,
            )
            observation_records.append(rec)

        elif ticker in eligible_tickers:
            # Eligible candidate that did not reach risk sizing
            in_eval_batch = any(c.ticker == ticker for c in scan_result.candidates[:top_n])
            if in_eval_batch and ticker in technically_invalid_set:
                rec = ObservationRecord(
                    date=effective_date_str,
                    ticker=ticker,
                    market_regime=regime_str,
                    signal="NO_SIGNAL",
                    entry_reference=None,
                    stop_reference=None,
                    risk_estimate="$0.00",
                    decision="SKIP",
                    notes="Signal engine evaluated candidate: technical entry setup criteria not satisfied.",
                    record_type="FORWARD_OBSERVATION",
                    executed_fill=False,
                    scanner_decision="ELIGIBLE",
                    candidate_score=candidate_score_str,
                    rejection_reasons=[],
                    technical_metrics=technical_metrics,
                    reached_signal_generation=True,
                    reached_risk_sizing=False,
                    signal_decision="NO_SIGNAL",
                    risk_decision=None,
                    data_mode=data_mode,
                    data_classification=data_class,
                )
            else:
                rec = ObservationRecord(
                    date=effective_date_str,
                    ticker=ticker,
                    market_regime=regime_str,
                    signal="NO_SIGNAL",
                    entry_reference=None,
                    stop_reference=None,
                    risk_estimate="$0.00",
                    decision="SKIP",
                    notes=f"Eligible candidate ranked beyond top_n limit ({top_n}); excluded from signal generation batch.",
                    record_type="FORWARD_OBSERVATION",
                    executed_fill=False,
                    scanner_decision="ELIGIBLE",
                    candidate_score=candidate_score_str,
                    rejection_reasons=[],
                    technical_metrics=technical_metrics,
                    reached_signal_generation=False,
                    reached_risk_sizing=False,
                    signal_decision=None,
                    risk_decision=None,
                    data_mode=data_mode,
                    data_classification=data_class,
                )
            observation_records.append(rec)

        else:
            # Scanner-rejected candidate
            reasons = (
                candidate.rejection_reasons
                if candidate is not None
                else scan_result.rejected_candidates.get(ticker, ["Candidate rejected by scanner."])
            )
            rec = ObservationRecord(
                date=effective_date_str,
                ticker=ticker,
                market_regime=regime_str,
                signal="REJECTED_SCANNER",
                entry_reference=None,
                stop_reference=None,
                risk_estimate="$0.00",
                decision="SKIP",
                notes=f"Scanner rejected: {'; '.join(reasons)}",
                record_type="FORWARD_OBSERVATION",
                executed_fill=False,
                scanner_decision="REJECTED",
                candidate_score=candidate_score_str,
                rejection_reasons=list(reasons),
                technical_metrics=technical_metrics,
                reached_signal_generation=False,
                reached_risk_sizing=False,
                signal_decision=None,
                risk_decision=None,
                data_mode=data_mode,
                data_classification=data_class,
            )
            observation_records.append(rec)

    # Build Session-Level Summary Record
    tickers_evaluated = len(ordered_tickers)
    eligible_count = len(eligible_tickers)
    rejected_count = len(rejected_tickers)
    signal_gen_count = len([r for r in observation_records if r.reached_signal_generation])
    risk_sizing_count = len([r for r in observation_records if r.reached_risk_sizing])

    if tickers_evaluated == 0:
        session_note = "NO_CANDIDATES_EVALUATED: Target universe was empty or all tickers were excluded prior to scanning."
    elif eligible_count == 0:
        session_note = f"ALL_CANDIDATES_REJECTED: All {tickers_evaluated} evaluated tickers were rejected by scanner (Market regime '{regime_str}' policy active)."
    else:
        session_note = f"EVALUATION_COMPLETED: {eligible_count} eligible, {rejected_count} rejected out of {tickers_evaluated} evaluated."

    # Determine data mode and classification if not already determined in live mode
    if not is_live:
        is_current = "current_daily" in os.path.normpath(data_dir).lower()
        data_mode = "CURRENT_DAILY_OBSERVATION" if is_current else "HISTORICAL_PARTITION_REPLAY"
        data_class = (
            "DELAYED_UNVERIFIED (UNVERIFIED_DELAY — NOT REAL-TIME)"
            if is_current
            else "VERIFIED_HISTORICAL (SPLIT_ADJUSTED)"
        )

    session_record = ObservationSessionRecord(
        date=effective_date_str,
        market_regime=regime_str,
        tickers_evaluated_count=tickers_evaluated,
        eligible_count=eligible_count,
        rejected_count=rejected_count,
        signal_generation_count=signal_gen_count,
        risk_sizing_count=risk_sizing_count,
        observation_records_count=len(observation_records),
        data_classification=data_class,
        record_type="SESSION_RECORD",
        executed_fill=False,
        notes=session_note,
        data_mode=data_mode,
    )

    # 9. Optionally append observation records and session record to record file
    saved_path: Optional[str] = None
    if write_records and record_file:
        rec_dir = os.path.dirname(os.path.abspath(record_file))
        os.makedirs(rec_dir, exist_ok=True)

        existing_obs_keys: Set[Tuple[str, str, str]] = set()
        existing_sess_keys: Set[Tuple[str, str]] = set()

        if os.path.exists(record_file):
            try:
                existing_entries = read_observation_log(record_file)
                for entry in existing_entries:
                    r_type = entry.get("record_type")
                    r_date = entry.get("date")
                    r_mode = entry.get("data_mode") or "HISTORICAL_PARTITION_REPLAY"
                    if r_type == "FORWARD_OBSERVATION":
                        r_sym = entry.get("ticker")
                        if r_date and r_sym:
                            if entry.get("data_mode") is None:
                                existing_obs_keys.add((r_date, r_sym, "CURRENT_DAILY_OBSERVATION"))
                                existing_obs_keys.add((r_date, r_sym, "HISTORICAL_PARTITION_REPLAY"))
                                existing_obs_keys.add((r_date, r_sym, "DAILY"))
                                existing_obs_keys.add((r_date, r_sym, "LIVE"))
                            else:
                                existing_obs_keys.add((r_date, r_sym, r_mode))
                    elif r_type == "SESSION_RECORD":
                        if r_date:
                            if entry.get("data_mode") is None:
                                existing_sess_keys.add((r_date, "CURRENT_DAILY_OBSERVATION"))
                                existing_sess_keys.add((r_date, "HISTORICAL_PARTITION_REPLAY"))
                                existing_sess_keys.add((r_date, "DAILY"))
                                existing_sess_keys.add((r_date, "LIVE"))
                            else:
                                existing_sess_keys.add((r_date, r_mode))
            except Exception as e:
                warnings.append(f"Failed to inspect existing observation log for idempotency: {e}")

        # Filter new records not already present in the log (idempotency key: session_date, ticker, data_mode)
        new_obs_to_write = [
            rec for rec in observation_records
            if (rec.date, rec.ticker, rec.data_mode or data_mode or "HISTORICAL_PARTITION_REPLAY") not in existing_obs_keys
        ]

        sess_key = (
            session_record.date,
            session_record.data_mode or data_mode or "HISTORICAL_PARTITION_REPLAY",
        ) if session_record else None

        if new_obs_to_write or (session_record and sess_key not in existing_sess_keys):
            with open(record_file, "a", encoding="utf-8") as f:
                for rec in new_obs_to_write:
                    f.write(rec.to_json() + "\n")
                if session_record and sess_key not in existing_sess_keys:
                    f.write(session_record.to_json() + "\n")

        saved_path = record_file

    return DailyResearchReport(
        as_of_date=effective_date_str,
        scan_timestamp=now_utc.isoformat(),
        data_dir=display_data_dir,
        feed_status=readiness.status.value,
        market_regime=regime_result,
        portfolio_status=portfolio_status,
        eligible_candidates=signal_summary.risk_approved_plans,
        risk_rejected_candidates=signal_summary.risk_rejected_plans,
        scanner_rejected_candidates=scan_result.rejected_candidates,
        feed_excluded_tickers=readiness.excluded_tickers,
        exit_signals=exit_signals,
        observation_records=observation_records,
        warnings=warnings,
        failure_reasons=failure_reasons,
        records_saved_to=saved_path,
        data_mode=data_mode,
        data_classification=data_class,
        session_record=session_record,
    )


def format_observation_report(report: DailyResearchReport) -> str:
    """Format the DailyResearchReport into a clean, human-readable terminal output."""
    lines = [
        "=" * 80,
        "           TRADER-SWING: DAILY RESEARCH OBSERVATION REPORT",
        "=" * 80,
        f"As-of Session Date : {report.as_of_date}",
        f"Scan Timestamp     : {report.scan_timestamp}",
        f"Data Partition     : {report.data_dir}",
        f"Data Mode          : {report.data_mode}",
        f"Data Classification: {report.data_classification}",
        "Execution Mode     : ON-DEMAND MANUAL RESEARCH SCAN (READ-ONLY)",
        "",
        "-" * 80,
        "GOVERNANCE & BOUNDARY DISCLOSURES",
        "-" * 80,
        "[FORWARD PAPER OBSERVATION ONLY — NOT AN EXECUTED TRADE — NO MUTATION TO LEDGER]",
        (
            "[NOTICE: LIVE DUAL-SOURCE DATA — ALPACA IEX + YAHOO CONSOLIDATED — FORWARD OBSERVATION ONLY]"
            if report.data_mode == "LIVE_IEX_HYBRID"
            else (
                "[NOTICE: CURRENT DELAYED DATA — UNVERIFIED_DELAY — NOT SUITABLE FOR LIVE EXECUTION]"
                if report.data_mode == "CURRENT_DAILY_OBSERVATION"
                else "[NOTICE: IN-SAMPLE HISTORICAL BASELINE — FORWARD OBSERVATION SEPARATE]"
            )
        ),
        "Historical Backtests (2022-2023): In-sample baseline (INCONCLUSIVE; 6.74% Max DD).",
        "Forward Observation: Pure observation tracking; zero live capital risk.",
        "Active Rules: Stock-only, long-only, max 4 positions, 0.50% risk per trade.",
        "-" * 80,
    ]

    # 1. Market Regime
    lines.append("1. MARKET REGIME & MACRO CONTEXT")
    lines.append("-" * 80)
    if report.market_regime:
        mr = report.market_regime
        lines.append(f"Broad Market Regime  : {mr.regime.value}")
        lines.append(f"Confidence Score     : {(mr.confidence * Decimal('100.0')).quantize(Decimal('0.1'))}%")
        lines.append(f"Aggregate Score      : {mr.aggregate_score}")
        lines.append(f"Policy Action        : {mr.policy.description}")
        lines.append(f"Allow New Longs      : {'YES' if mr.policy.allow_new_longs else 'NO (BLOCKED BY REGIME POLICY)'}")
        lines.append("Proxy Status:")
        for sym, pa in mr.proxy_analyses.items():
            sma20_str = "Above SMA20" if pa.above_sma_20 else "Below SMA20"
            sma50_str = "Above SMA50" if pa.above_sma_50 else "Below SMA50"
            sma200_str = "Above SMA200" if pa.above_sma_200 else "Below SMA200"
            lines.append(f"  - {sym:4}: ${pa.price} | {sma20_str}, {sma50_str}, {sma200_str} | Score: {pa.score}")
    else:
        lines.append(f"Market Regime        : UNAVAILABLE (Feed status: {report.feed_status})")
    lines.append("")

    # 2. Portfolio Status (Read-only)
    lines.append("-" * 80)
    lines.append("2. CURRENT PAPER PORTFOLIO STATUS (READ-ONLY INSPECTION)")
    lines.append("-" * 80)
    ps = report.portfolio_status
    lines.append(f"Total Equity         : ${ps.get('total_equity_usd', 'N/A')} USD")
    lines.append(f"Available Cash       : ${ps.get('cash_usd', 'N/A')} USD ({ps.get('cash_reserve_pct', '100.0')}%)")
    lines.append(f"Invested Capital     : ${ps.get('total_market_value_usd', '0.00')} USD ({ps.get('invested_capital_pct', '0.0')}%)")
    open_cnt = ps.get("open_positions_count", 0)
    max_lim = ps.get("max_simultaneous_positions_limit", 4)
    lines.append(f"Open Positions Count : {open_cnt} / {max_lim} max limit ({max_lim - open_cnt} slots available)")
    active_tickers = ps.get("active_tickers", [])
    lines.append(f"Active Holdings      : {', '.join(active_tickers) if active_tickers else 'None (0 open positions)'}")
    if ps.get("active_lots"):
        lines.append("Active Open Lots:")
        for lot in ps["active_lots"]:
            lines.append(
                f"  * [{lot['lot_id']}] {lot['ticker']}: {lot['shares']} shares @ ${lot['entry_price']} | Stop: ${lot['stop_loss']}"
            )
    lines.append("")

    # 3. Market Data Feed & Exclusions
    lines.append("-" * 80)
    lines.append("3. MARKET DATA FEED & STALENESS REPORT")
    lines.append("-" * 80)
    lines.append(f"Feed Readiness       : {report.feed_status}")
    if report.feed_excluded_tickers:
        lines.append(f"Stale / Excluded     : {len(report.feed_excluded_tickers)} tickers excluded from universe")
        for exc in report.feed_excluded_tickers:
            latest_d = exc.latest_candle_date.isoformat() if exc.latest_candle_date else "None"
            lines.append(f"  * {exc.ticker:5}: {exc.reason} (Bars: {exc.bar_count}, Latest: {latest_d})")
    else:
        lines.append("Stale / Excluded     : None (All loaded tickers passed completeness & freshness checks)")
    lines.append("")

    # 4. Eligible Candidates & Risk Sizing
    lines.append("-" * 80)
    lines.append("4. ELIGIBLE CANDIDATES & FROZEN RISK SIZING")
    lines.append("-" * 80)
    if report.eligible_candidates:
        for idx, plan in enumerate(report.eligible_candidates, 1):
            lines.append(f"[Candidate #{idx}] {plan.ticker} | Strategy: {plan.strategy_type}")
            lines.append(f"  - Reference Entry  : ${plan.entry_price}")
            lines.append(f"  - Suggested Stop   : ${plan.stop_loss_price}")
            lines.append(f"  - Suggested Target : ${plan.target_price} (Reward:Risk = {plan.reward_to_risk}R)")
            lines.append(f"  - Sized Shares     : {plan.shares} shares")
            lines.append(f"  - Capital Required : ${plan.total_capital_required}")
            lines.append(f"  - Buffered Risk    : ${plan.total_buffered_risk} (0.50% account risk limit)")
            lines.append(f"  - Disposition      : OBSERVE_ONLY (Awaiting user manual decision)")
            lines.append("")
    else:
        lines.append("No eligible candidates passed technical signal filters and risk sizing for this session.")
        lines.append("")

    # 5. Rejected Candidates & Reasons
    lines.append("-" * 80)
    lines.append("5. REJECTED CANDIDATES & REJECTION REASONS")
    lines.append("-" * 80)
    if report.risk_rejected_candidates:
        lines.append("Risk Engine Rejections:")
        for plan in report.risk_rejected_candidates:
            lines.append(f"  * {plan.ticker:5} (Entry: ${plan.entry_price}, Stop: ${plan.stop_loss_price}):")
            lines.append(f"      Binding Constraint: {plan.binding_constraint}")
            for r in plan.risk_rejection_reasons:
                lines.append(f"      Reason: {r}")

    if report.scanner_rejected_candidates:
        lines.append("Scanner Technical Rejections (Sample):")
        for sym, reasons in list(report.scanner_rejected_candidates.items())[:5]:
            lines.append(f"  * {sym:5}: {'; '.join(reasons)}")

    if not report.risk_rejected_candidates and not report.scanner_rejected_candidates:
        lines.append("None.")
    lines.append("")

    # 6. Exit Signals for Open Positions
    if report.exit_signals:
        lines.append("-" * 80)
        lines.append("6. EXIT SIGNALS FOR OPEN POSITIONS")
        lines.append("-" * 80)
        for sig in report.exit_signals:
            lines.append(f"  * {sig.ticker:5} | Exit Type: {sig.exit_type} | Reason: {sig.reason}")
        lines.append("")

    # 7. Observation Records Summary
    lines.append("-" * 80)
    lines.append("7. FORWARD OBSERVATION RECORDS")
    lines.append("-" * 80)
    lines.append(f"Observation Records Generated : {len(report.observation_records)}")
    if report.session_record:
        sr = report.session_record
        lines.append(f"Tickers Evaluated by Scanner  : {sr.tickers_evaluated_count}")
        lines.append(f"Eligible Candidates           : {sr.eligible_count}")
        lines.append(f"Rejected Candidates           : {sr.rejected_count}")
        lines.append(f"Reached Signal Generation     : {sr.signal_generation_count}")
        lines.append(f"Reached Risk Sizing           : {sr.risk_sizing_count}")
        lines.append(f"Session Summary Record        : Written ({sr.record_type})")
    if report.records_saved_to:
        lines.append(f"Appended To Disk File         : {report.records_saved_to}")
    else:
        lines.append("Appended To Disk File         : NONE (Display mode or write_records disabled)")
    lines.append("Paper Ledger State Mutations  : ZERO (0 fills recorded, external state untouched)")
    lines.append("=" * 80)

    return "\n".join(lines)


def run_observation_cli(parsed: argparse.Namespace) -> int:
    """CLI handler for observation workflow."""
    is_live = getattr(parsed, "dual_source", False)
    data_mode = "LIVE_IEX_HYBRID" if is_live else None

    if is_live:
        data_dir = "LIVE_GATEWAY"
        rec_file = None if parsed.no_record else (parsed.record_file or DEFAULT_LIVE_RECORD_FILE)
    else:
        data_dir = DEFAULT_CURRENT_DATA_DIR if getattr(parsed, "current", False) else parsed.data_dir
        rec_file = None if parsed.no_record else (parsed.record_file or DEFAULT_RECORD_FILE)

    as_of_d: Optional[date] = None
    if parsed.as_of_date:
        try:
            as_of_d = date.fromisoformat(parsed.as_of_date)
        except ValueError:
            print(f"[ERROR] Invalid --as-of-date '{parsed.as_of_date}'. Expected format: YYYY-MM-DD", file=sys.stderr)
            return 1

    tickers_list: Optional[List[str]] = None
    if parsed.tickers:
        tickers_list = [t.strip().upper() for t in parsed.tickers.split(",") if t.strip()]

    write_rec = not parsed.no_record

    try:
        report = run_observation_scan(
            data_dir=data_dir,
            as_of_date=as_of_d,
            tickers=tickers_list,
            state_dir=parsed.state_dir,
            top_n=parsed.top_n,
            record_file=rec_file,
            write_records=write_rec,
            gateway=None,
            data_mode=data_mode,
        )
        print(format_observation_report(report))
        if report.feed_status == "HARD_FAIL":
            return 1
        return 0
    except Exception as e:
        print(f"\n[FATAL ERROR] Observation scan failed: {e}", file=sys.stderr)
        return 1


def main(argv: Optional[List[str]] = None) -> int:
    """Standalone CLI entry point for observation workflow."""
    args_list = argv if argv is not None else sys.argv[1:]

    # Import forbidden flag scanner from paper_cli
    from paper_cli import scan_for_forbidden_flags
    scan_for_forbidden_flags(args_list)

    parser = argparse.ArgumentParser(
        prog="trader-swing-observe",
        description="Phase 6.4.1 Manual Paper Observation Workflow (Autonomous Swing Strategy)",
    )
    parser.add_argument(
        "--dual-source",
        action="store_true",
        help="Execute observation scan using live DualSourceSyncGateway (Alpaca IEX price + Yahoo consolidated volume)",
    )
    parser.add_argument(
        "--current",
        action="store_true",
        help=f"Observe current daily market data from isolated directory ({DEFAULT_CURRENT_DATA_DIR})",
    )
    parser.add_argument(
        "--data-dir",
        default=DEFAULT_DATA_DIR,
        help=f"Directory containing daily CSV candle files (default: {DEFAULT_DATA_DIR})",
    )
    parser.add_argument(
        "--as-of-date",
        default=None,
        help="Trading session date to scan (YYYY-MM-DD, defaults to latest available)",
    )
    parser.add_argument(
        "--tickers",
        default=None,
        help="Comma-separated list of tickers to scan (defaults to all available in data-dir)",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=5,
        help="Number of top candidates to highlight (default: 5)",
    )
    parser.add_argument(
        "--state-dir",
        default=DEFAULT_PAPER_STATE_DIR,
        help=f"External directory for paper state (read-only) (default: {DEFAULT_PAPER_STATE_DIR})",
    )
    parser.add_argument(
        "--record-file",
        default=None,
        help=f"Path to append observation records (default: {DEFAULT_RECORD_FILE})",
    )
    parser.add_argument(
        "--no-record",
        action="store_true",
        help="Do not write observation records to disk (display report only)",
    )

    parsed = parser.parse_args(args_list)
    return run_observation_cli(parsed)


if __name__ == "__main__":
    sys.exit(main())
