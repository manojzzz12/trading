"""Isolated Storage Subsystem for Trader-Swing Phase 9 Forward Evaluation.

Enforces:
1. Strict storage isolation: defaults to .planning/data/forward_evaluation/.
2. Hard safety boundaries: blocks any access or writing to root paper_journal.json,
   portfolio.json, broker.json, or external TradingPaperState.
3. Atomic two-stage persistence (staged write -> atomic replace) to prevent corruption.
4. Idempotency on (session_date, ticker, data_mode): rejects duplicate observations
   within the same data mode while permitting distinct data modes.
5. Append-only auditable event logging.
6. Schema versioning and explicit deserialization.
"""

from datetime import datetime, timezone
from decimal import Decimal
import json
import os
import shutil
import sys
from typing import Any, Dict, List, Optional, Set, Tuple

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, "..", "..", ".."))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from forward_evaluation_models import (
    EvaluationSessionSummary,
    ForwardDataMode,
    ForwardObservationRecord,
    ForwardTradeProposal,
    HypotheticalClosedTrade,
    HypotheticalPosition,
    MARKER_FORWARD_EVALUATION,
)

DEFAULT_FORWARD_EVALUATION_DIR = os.path.join(PROJECT_ROOT, ".planning", "data", "forward_evaluation")

PROHIBITED_FILENAMES = {
    "paper_journal.json",
    "portfolio.json",
    "broker.json",
    "active_reservations.json",
}

GENUINE_DATA_MODES: Set[str] = {"DAILY", "LIVE"}


def is_genuine_daily_mode(data_mode: Any) -> bool:
    """Fail-closed validation for genuine daily forward evaluation mode.
    
    Rejects None, empty string, 'FIXTURE', 'UNKNOWN', synthetic mocks, or invalid types.
    Only permits 'DAILY' and 'LIVE'.
    """
    if not data_mode or not isinstance(data_mode, str):
        return False
    return data_mode.strip().upper() in GENUINE_DATA_MODES


class ForwardStorageSafetyError(ValueError):
    """Raised when an operation attempts to breach storage isolation boundaries."""
    pass


class DuplicateObservationError(ValueError):
    """Raised when attempting to overwrite an observation with identical date, ticker, and mode."""
    pass


class ForwardEvaluationStorage:
    """Manages transactional disk persistence for forward evaluation artifacts."""

    def __init__(
        self,
        base_dir: Optional[str] = None,
        legacy_log_path: Optional[str] = None,
    ):
        self.base_dir = os.path.abspath(base_dir or DEFAULT_FORWARD_EVALUATION_DIR)
        self.legacy_log_path = legacy_log_path or os.path.join(PROJECT_ROOT, ".planning", "data", "observations", "observation_log.jsonl")
        self._validate_directory_safety(self.base_dir)

        # Subdirectories
        self.obs_dir = os.path.join(self.base_dir, "observations")
        self.props_dir = os.path.join(self.base_dir, "proposals")
        self.pos_dir = os.path.join(self.base_dir, "positions")
        self.trades_dir = os.path.join(self.base_dir, "trades")
        self.sums_dir = os.path.join(self.base_dir, "summaries")
        self.audit_dir = os.path.join(self.base_dir, "audit")

        self._initialize_directories()

    def _validate_directory_safety(self, path: str) -> None:
        """Prevent caller from pointing forward evaluation storage to prohibited directories."""
        norm_path = os.path.normpath(path).lower()
        if "tradingpaperstate" in norm_path:
            raise ForwardStorageSafetyError(
                f"Prohibited storage path '{path}': connection to external 'TradingPaperState' is forbidden."
            )
        base_name = os.path.basename(norm_path)
        if base_name in PROHIBITED_FILENAMES:
            raise ForwardStorageSafetyError(
                f"Prohibited storage file '{path}': target matches protected file '{base_name}'."
            )

    def _initialize_directories(self) -> None:
        """Ensure all required subdirectories and safety marker exist."""
        for d in [self.obs_dir, self.props_dir, self.pos_dir, self.trades_dir, self.sums_dir, self.audit_dir]:
            os.makedirs(d, exist_ok=True)

        marker_path = os.path.join(self.base_dir, "FORWARD_EVALUATION_MARKER.json")
        if not os.path.exists(marker_path):
            marker_data = {
                "marker": MARKER_FORWARD_EVALUATION,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "schema_version": "1.0.0",
                "system": "trader-swing-forward-evaluation",
                "disclaimer": "EVALUATION ONLY - ZERO REAL MONEY OR PAPER EXECUTION",
            }
            self._atomic_write_json(marker_path, marker_data)

    def _atomic_write_json(self, target_path: str, data: Any) -> None:
        """Write JSON data atomically using a temporary file and atomic rename."""
        target_dir = os.path.dirname(target_path)
        os.makedirs(target_dir, exist_ok=True)
        tmp_path = os.path.join(target_dir, f".tmp_{os.path.basename(target_path)}")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, target_path)
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

    # -------------------------------------------------------------------------
    # Observations Persistence & Idempotency
    # -------------------------------------------------------------------------

    def get_observation_key(self, session_date: str, ticker: str, data_mode: str) -> str:
        """Format unique idempotency key for an observation."""
        return f"{session_date}_{ticker.upper()}_{data_mode.upper()}"

    def is_legacy_observation(self, session_date: str, ticker: str) -> bool:
        """Check if an observation for (session_date, ticker) is already logged in legacy observation_log.jsonl."""
        if not self.legacy_log_path or not os.path.exists(self.legacy_log_path):
            return False
        try:
            with open(self.legacy_log_path, "r", encoding="utf-8") as f:
                for line in f:
                    line_s = line.strip()
                    if not line_s:
                        continue
                    entry = json.loads(line_s)
                    if entry.get("record_type") == "FORWARD_OBSERVATION":
                        if entry.get("date") == session_date and str(entry.get("ticker", "")).upper() == ticker.upper():
                            return True
        except Exception:
            return False
        return False

    def save_observation(self, record: ForwardObservationRecord) -> str:
        """Save a forward observation record with duplicate rejection.
        
        Raises:
            DuplicateObservationError: If record with same (session_date, ticker, data_mode) already exists.
        """
        if record.data_mode in GENUINE_DATA_MODES and self.is_legacy_observation(record.session_date, record.ticker):
            raise DuplicateObservationError(
                f"Duplicate observation rejected: date='{record.session_date}', ticker='{record.ticker}' already recorded in historical observation log."
            )

        key = self.get_observation_key(record.session_date, record.ticker, record.data_mode)
        filename = f"OBS_{key}.json"
        target_path = os.path.join(self.obs_dir, filename)

        if os.path.exists(target_path):
            raise DuplicateObservationError(
                f"Duplicate observation rejected: date='{record.session_date}', ticker='{record.ticker}', mode='{record.data_mode}'."
            )

        self._atomic_write_json(target_path, record.to_dict())
        self._append_audit_event("SAVE_OBSERVATION", {
            "observation_id": record.observation_id,
            "session_date": record.session_date,
            "ticker": record.ticker,
            "data_mode": record.data_mode,
            "decision": record.screening_decision,
        })
        return record.observation_id

    def load_observation(self, session_date: str, ticker: str, data_mode: str) -> Optional[ForwardObservationRecord]:
        """Load an observation by its unique triplet."""
        key = self.get_observation_key(session_date, ticker, data_mode)
        target_path = os.path.join(self.obs_dir, f"OBS_{key}.json")
        if not os.path.exists(target_path):
            return None
        with open(target_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return ForwardObservationRecord.from_dict(data)

    def list_observations(
        self,
        session_date: Optional[str] = None,
        data_mode: Optional[str] = None,
        genuine_only: bool = False,
    ) -> List[ForwardObservationRecord]:
        """List all saved observation records, optionally filtered by session date and/or data mode."""
        results: List[ForwardObservationRecord] = []
        if not os.path.exists(self.obs_dir):
            return results

        for fname in os.listdir(self.obs_dir):
            if fname.startswith("OBS_") and fname.endswith(".json"):
                fpath = os.path.join(self.obs_dir, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        rec = ForwardObservationRecord.from_dict(json.load(f))
                    if session_date and rec.session_date != session_date:
                        continue
                    if genuine_only:
                        if not is_genuine_daily_mode(rec.data_mode):
                            continue
                    elif data_mode:
                        if not rec.data_mode or rec.data_mode.strip().upper() != data_mode.strip().upper():
                            continue
                    results.append(rec)
                except Exception:
                    continue

        results.sort(key=lambda r: (r.session_date, r.ticker))
        return results

    # -------------------------------------------------------------------------
    # Trade Proposals Persistence
    # -------------------------------------------------------------------------

    def save_proposal(self, proposal: ForwardTradeProposal) -> None:
        """Persist a hypothetical trade proposal atomically."""
        filename = f"PROP_{proposal.session_date}_{proposal.ticker}_{proposal.data_mode}.json"
        target_path = os.path.join(self.props_dir, filename)
        self._atomic_write_json(target_path, proposal.to_dict())
        self._append_audit_event("SAVE_PROPOSAL", {
            "proposal_id": proposal.proposal_id,
            "session_date": proposal.session_date,
            "ticker": proposal.ticker,
            "shares": proposal.shares,
        })

    def load_proposals(
        self,
        session_date: Optional[str] = None,
        data_mode: Optional[str] = None,
        genuine_only: bool = False,
    ) -> List[ForwardTradeProposal]:
        """List all hypothetical trade proposals."""
        results: List[ForwardTradeProposal] = []
        if not os.path.exists(self.props_dir):
            return results

        for fname in os.listdir(self.props_dir):
            if fname.startswith("PROP_") and fname.endswith(".json"):
                fpath = os.path.join(self.props_dir, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        prop = ForwardTradeProposal.from_dict(json.load(f))
                    if session_date and prop.session_date != session_date:
                        continue
                    if genuine_only:
                        if not is_genuine_daily_mode(prop.data_mode):
                            continue
                    elif data_mode:
                        if not prop.data_mode or prop.data_mode.strip().upper() != data_mode.strip().upper():
                            continue
                    results.append(prop)
                except Exception:
                    continue
        return results

    # -------------------------------------------------------------------------
    # Hypothetical Positions Persistence
    # -------------------------------------------------------------------------

    def save_position(self, pos: HypotheticalPosition) -> None:
        """Save or update an active hypothetical position."""
        target_path = os.path.join(self.pos_dir, f"POS_{pos.position_id}.json")
        self._atomic_write_json(target_path, pos.to_dict())

    def delete_position(self, position_id: str) -> None:
        """Remove a closed position from active positions."""
        target_path = os.path.join(self.pos_dir, f"POS_{position_id}.json")
        if os.path.exists(target_path):
            os.remove(target_path)

    def load_active_positions(
        self,
        data_mode: Optional[str] = None,
        genuine_only: bool = False,
    ) -> List[HypotheticalPosition]:
        """Load all currently open hypothetical positions."""
        results: List[HypotheticalPosition] = []
        if not os.path.exists(self.pos_dir):
            return results

        for fname in os.listdir(self.pos_dir):
            if fname.startswith("POS_") and fname.endswith(".json"):
                fpath = os.path.join(self.pos_dir, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        pos = HypotheticalPosition.from_dict(json.load(f))
                    if genuine_only:
                        if not is_genuine_daily_mode(pos.data_mode):
                            continue
                    elif data_mode:
                        if not pos.data_mode or pos.data_mode.strip().upper() != data_mode.strip().upper():
                            continue
                    results.append(pos)
                except Exception:
                    continue
        results.sort(key=lambda p: p.opened_at)
        return results

    # -------------------------------------------------------------------------
    # Closed Trades Persistence
    # -------------------------------------------------------------------------

    def save_closed_trade(self, trade: HypotheticalClosedTrade) -> None:
        """Save record of a closed hypothetical trade."""
        target_path = os.path.join(self.trades_dir, f"TRADE_{trade.trade_id}.json")
        self._atomic_write_json(target_path, trade.to_dict())
        self._append_audit_event("CLOSE_TRADE", {
            "trade_id": trade.trade_id,
            "ticker": trade.ticker,
            "net_pnl": str(trade.net_pnl),
            "exit_reason": trade.exit_reason,
        })

    def load_closed_trades(
        self,
        data_mode: Optional[str] = None,
        genuine_only: bool = False,
    ) -> List[HypotheticalClosedTrade]:
        """Load all closed hypothetical trades."""
        results: List[HypotheticalClosedTrade] = []
        if not os.path.exists(self.trades_dir):
            return results

        for fname in os.listdir(self.trades_dir):
            if fname.startswith("TRADE_") and fname.endswith(".json"):
                fpath = os.path.join(self.trades_dir, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        t = HypotheticalClosedTrade.from_dict(json.load(f))
                    if genuine_only:
                        if not is_genuine_daily_mode(t.data_mode):
                            continue
                    elif data_mode:
                        if not t.data_mode or t.data_mode.strip().upper() != data_mode.strip().upper():
                            continue
                    results.append(t)
                except Exception:
                    continue
        results.sort(key=lambda t: t.closed_at)
        return results

    # -------------------------------------------------------------------------
    # Session Summaries Persistence
    # -------------------------------------------------------------------------

    def has_session_summary(self, session_date: str, data_mode: str) -> bool:
        """Check if an evaluation session summary already exists for (session_date, data_mode)."""
        target_path = os.path.join(self.sums_dir, f"SUM_{session_date}_{data_mode.strip().upper()}.json")
        return os.path.exists(target_path)

    def load_session_summary(self, session_date: str, data_mode: str) -> Optional[EvaluationSessionSummary]:
        """Load session evaluation summary for exact session date and data mode."""
        target_path = os.path.join(self.sums_dir, f"SUM_{session_date}_{data_mode.strip().upper()}.json")
        if not os.path.exists(target_path):
            return None
        with open(target_path, "r", encoding="utf-8") as f:
            return EvaluationSessionSummary.from_dict(json.load(f))

    def save_session_summary(self, summary: EvaluationSessionSummary, force_overwrite: bool = False) -> None:
        """Save session evaluation summary atomically with duplicate rejection."""
        target_path = os.path.join(self.sums_dir, f"SUM_{summary.session_date}_{summary.data_mode}.json")
        if not force_overwrite and os.path.exists(target_path):
            raise DuplicateObservationError(
                f"Duplicate evaluation session summary rejected: date='{summary.session_date}', mode='{summary.data_mode}'."
            )
        self._atomic_write_json(target_path, summary.to_dict())

    def load_session_summaries(
        self,
        data_mode: Optional[str] = None,
        genuine_only: bool = False,
    ) -> List[EvaluationSessionSummary]:
        """Load all session evaluation summaries sorted by date."""
        results: List[EvaluationSessionSummary] = []
        if not os.path.exists(self.sums_dir):
            return results

        for fname in os.listdir(self.sums_dir):
            if fname.startswith("SUM_") and fname.endswith(".json"):
                fpath = os.path.join(self.sums_dir, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        s = EvaluationSessionSummary.from_dict(json.load(f))
                    if genuine_only:
                        if not is_genuine_daily_mode(s.data_mode):
                            continue
                    elif data_mode:
                        if not s.data_mode or s.data_mode.strip().upper() != data_mode.strip().upper():
                            continue
                    results.append(s)
                except Exception:
                    continue
        results.sort(key=lambda s: s.session_date)
        return results

    # -------------------------------------------------------------------------
    # Append-Only Audit Logging
    # -------------------------------------------------------------------------

    def _append_audit_event(self, event_type: str, details: Dict[str, Any]) -> None:
        """Append an auditable event to the append-only audit log."""
        audit_file = os.path.join(self.audit_dir, "audit_events.jsonl")
        evt = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "marker": MARKER_FORWARD_EVALUATION,
            "details": details,
        }
        with open(audit_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(evt) + "\n")

    def load_audit_events(self) -> List[Dict[str, Any]]:
        """Load all audit events."""
        audit_file = os.path.join(self.audit_dir, "audit_events.jsonl")
        events: List[Dict[str, Any]] = []
        if not os.path.exists(audit_file):
            return events
        with open(audit_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except Exception:
                        continue
        return events
