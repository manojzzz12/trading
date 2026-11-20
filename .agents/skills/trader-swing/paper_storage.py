"""Storage manager and path safety boundary for Phase 6 paper trading.

Enforces:
1. Real paper-session state is stored exclusively under an external directory
   (default: ~/Documents/TradingPaperState or $TRADING_PAPER_STATE_DIR), NEVER inside
   the repository root or within .agents. Resolves real canonical paths
   (including symlinks and junctions).
2. Rejection of any target path matching root protected files (paper_journal.json,
   portfolio.json, broker.json).
3. Two-phase commit manifest protocol ensuring atomic transaction persistence
   and deterministic crash recovery across journal, portfolio, orders, and audit files.
4. Append-only, structured audit logging in audit_events.jsonl.
"""

from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import shutil
import sys
import uuid
from typing import Any, Dict, List, Optional

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from accounting import AccountingLedger
from journal import SwingJournal, JournalError
from paper_models import (
    PaperExitRecord,
    PaperFillRecord,
    PaperPositionMetadata,
    PaperTradeAuditEntry,
)


def get_default_paper_state_dir() -> str:
    """Resolve the default external directory for paper trading state.

    Resolution order:
    1. Environment variable TRADING_PAPER_STATE_DIR.
    2. Portable user directory: Path.home() / "Documents" / "TradingPaperState".
    """
    env_dir = os.environ.get("TRADING_PAPER_STATE_DIR")
    if env_dir and env_dir.strip():
        return os.path.abspath(env_dir.strip())
    return str(Path.home() / "Documents" / "TradingPaperState")


# Standard default external path for paper trading session
DEFAULT_PAPER_STATE_DIR = get_default_paper_state_dir()

# Protected root filenames that can NEVER be written or targeted
FORBIDDEN_ROOT_FILES = {"paper_journal.json", "portfolio.json", "broker.json"}

# Identify repository root from current location (3 levels up from this file)
_REPO_ROOT = os.path.realpath(
    os.path.abspath(os.path.join(CURRENT_DIR, "..", "..", ".."))
)


class PaperStorageSafetyError(ValueError):
    """Raised when an illegal or repository-internal state path is attempted."""
    pass


class PaperStorageManager:
    """Manages external state directory and transactional persistence for paper trading."""

    def __init__(self, state_dir: Optional[str] = None):
        raw_path = state_dir or os.environ.get("TRADING_PAPER_STATE_DIR") or get_default_paper_state_dir()
        self.state_dir = os.path.abspath(raw_path)
        self._validate_state_dir_safety(self.state_dir)

        # File paths inside the external state directory
        self.journal_path = os.path.join(self.state_dir, "trading_paper_journal.json")
        self.portfolio_path = os.path.join(self.state_dir, "trading_paper_portfolio.json")
        self.orders_path = os.path.join(self.state_dir, "trading_paper_orders.json")
        self.authorizations_path = os.path.join(self.state_dir, "trading_paper_authorizations.json")
        self.reservations_path = os.path.join(self.state_dir, "trading_paper_reservations.json")
        self.audit_path = os.path.join(self.state_dir, "audit_events.jsonl")
        self.manifest_path = os.path.join(self.state_dir, "commit_manifest.json")

        # Recover any interrupted transaction from a previous crash before proceeding
        self.recover_interrupted_transaction()

    @staticmethod
    def _validate_state_dir_safety(path: str) -> None:
        """Enforce strict isolation: state dir MUST NOT be inside repo or .agents.

        Resolves real/canonical path (symlinks, junctions) prior to evaluation.
        """
        abs_path = os.path.abspath(path)
        real_path = os.path.realpath(abs_path)
        real_repo = _REPO_ROOT

        # 1. Prohibit state dir inside the repository root (checking both abs and realpath)
        for candidate in (abs_path, real_path):
            try:
                common = os.path.commonpath([candidate, real_repo])
            except ValueError:
                # Different drive letters on Windows -> definitely outside repo
                common = None

            if common and common.lower() == real_repo.lower():
                raise PaperStorageSafetyError(
                    f"CRITICAL SAFETY VIOLATION: State directory '{abs_path}' (resolved: '{candidate}') "
                    f"is inside repository root '{real_repo}'. Paper state must be kept strictly outside the repo."
                )

        # 2. Prohibit .agents in path anywhere (checking both abs and realpath)
        for candidate in (abs_path, real_path):
            path_parts = [p.lower() for p in candidate.replace("\\", "/").split("/")]
            if ".agents" in path_parts:
                raise PaperStorageSafetyError(
                    f"CRITICAL SAFETY VIOLATION: State directory '{abs_path}' (resolved: '{candidate}') "
                    "contains '.agents'. Paper state must never reside inside .agents."
                )

        # 3. Prohibit direct target of forbidden root files
        for candidate in (abs_path, real_path):
            base_name = os.path.basename(candidate).lower()
            if base_name in FORBIDDEN_ROOT_FILES:
                raise PaperStorageSafetyError(
                    f"CRITICAL SAFETY VIOLATION: State directory name '{base_name}' matches protected root file."
                )

    # -------------------------------------------------------------------------
    # Transactional Commit Manifest Protocol & Crash Recovery
    # -------------------------------------------------------------------------

    def recover_interrupted_transaction(self) -> bool:
        """Detect and deterministically recover any interrupted transaction.

        - If commit_manifest.json exists with status == 'COMMITTED':
          The transaction was formally committed before the crash. Re-applies
          all staged files to the canonical locations and marks APPLIED.
        - If staging directories exist without a COMMITTED manifest:
          The transaction crashed before the commit point. Purges uncommitted
          staging files and leaves prior canonical files untouched.
        """
        if not os.path.exists(self.state_dir):
            return False

        recovered = False

        # 1. Check for commit manifest
        if os.path.exists(self.manifest_path):
            try:
                with open(self.manifest_path, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
            except Exception:
                manifest = {}

            if manifest.get("status") == "COMMITTED":
                staging_dir = manifest.get("staging_dir")
                if staging_dir and os.path.exists(staging_dir):
                    # Staging directory exists and was committed: apply files to canonical state
                    staged_journal = os.path.join(staging_dir, "trading_paper_journal.json")
                    staged_portfolio = os.path.join(staging_dir, "trading_paper_portfolio.json")
                    staged_orders = os.path.join(staging_dir, "trading_paper_orders.json")
                    staged_authorizations = os.path.join(staging_dir, "trading_paper_authorizations.json")
                    staged_reservations = os.path.join(staging_dir, "trading_paper_reservations.json")
                    staged_audit = os.path.join(staging_dir, "audit_events.jsonl")

                    if os.path.exists(staged_journal):
                        if os.path.exists(self.journal_path):
                            shutil.copy2(self.journal_path, f"{self.journal_path}.bak")
                        shutil.copy2(staged_journal, self.journal_path)

                    if os.path.exists(staged_portfolio):
                        shutil.copy2(staged_portfolio, self.portfolio_path)

                    if os.path.exists(staged_orders):
                        shutil.copy2(staged_orders, self.orders_path)

                    if os.path.exists(staged_authorizations):
                        shutil.copy2(staged_authorizations, self.authorizations_path)

                    if os.path.exists(staged_reservations):
                        shutil.copy2(staged_reservations, self.reservations_path)

                    if os.path.exists(staged_audit):
                        shutil.copy2(staged_audit, self.audit_path)

                    recovered = True

                # Mark applied and remove manifest and staging
                try:
                    if staging_dir and os.path.exists(staging_dir):
                        shutil.rmtree(staging_dir, ignore_errors=True)
                    if os.path.exists(self.manifest_path):
                        os.remove(self.manifest_path)
                except Exception:
                    pass
            else:
                # Uncommitted or malformed manifest: remove manifest
                try:
                    staging_dir = manifest.get("staging_dir")
                    if staging_dir and os.path.exists(staging_dir):
                        shutil.rmtree(staging_dir, ignore_errors=True)
                    os.remove(self.manifest_path)
                except Exception:
                    pass

        # 2. Clean up any orphaned uncommitted staging directories
        try:
            for item in os.listdir(self.state_dir):
                if item.startswith(".tx_stage_"):
                    full_p = os.path.join(self.state_dir, item)
                    if os.path.isdir(full_p):
                        shutil.rmtree(full_p, ignore_errors=True)
        except Exception:
            pass

        return recovered

    def commit_transaction(
        self,
        journal_payload: Dict[str, Any],
        portfolio_data: Dict[str, Any],
        orders_data: Dict[str, Any],
        audit_entry: PaperTradeAuditEntry,
        failure_injection_point: Optional[str] = None,
        reservations_data: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Atomically commit a multi-file state mutation using a commit manifest.

        Stages all files in a durable transaction directory, atomically sets the
        commit manifest to COMMITTED, and then applies the staged files.
        """
        self.recover_interrupted_transaction()
        os.makedirs(self.state_dir, exist_ok=True)

        tx_id = f"TX-{uuid.uuid4().hex[:8].upper()}"
        staging_dir = os.path.join(self.state_dir, f".tx_stage_{tx_id}")
        os.makedirs(staging_dir, exist_ok=True)

        staged_journal = os.path.join(staging_dir, "trading_paper_journal.json")
        staged_portfolio = os.path.join(staging_dir, "trading_paper_portfolio.json")
        staged_orders = os.path.join(staging_dir, "trading_paper_orders.json")
        staged_reservations = os.path.join(staging_dir, "trading_paper_reservations.json")
        staged_audit = os.path.join(staging_dir, "audit_events.jsonl")

        # 1. Write staged journal file
        journal_payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        with open(staged_journal, "w", encoding="utf-8") as f:
            json.dump(journal_payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())

        if failure_injection_point == "BEFORE_MANIFEST":
            raise RuntimeError("Injected crash: BEFORE_MANIFEST")

        # 2. Write staged portfolio metadata
        portfolio_data["updated_at"] = datetime.now(timezone.utc).isoformat()
        with open(staged_portfolio, "w", encoding="utf-8") as f:
            json.dump(portfolio_data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())

        # 3. Write staged orders
        orders_data["updated_at"] = datetime.now(timezone.utc).isoformat()
        with open(staged_orders, "w", encoding="utf-8") as f:
            json.dump(orders_data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())

        # 3b. Write staged reservations (if supplied)
        if reservations_data is not None:
            reservations_data["updated_at"] = datetime.now(timezone.utc).isoformat()
            with open(staged_reservations, "w", encoding="utf-8") as f:
                json.dump(reservations_data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())

        # 4. Write staged audit trail (copy canonical if exists, then append new entry)
        if os.path.exists(self.audit_path):
            shutil.copy2(self.audit_path, staged_audit)
        audit_line = json.dumps(audit_entry.to_dict()) + "\n"
        with open(staged_audit, "a", encoding="utf-8") as f:
            f.write(audit_line)
            f.flush()
            os.fsync(f.fileno())

        if failure_injection_point == "PRE_COMMIT":
            raise RuntimeError("Injected crash: PRE_COMMIT")

        # 5. ATOMIC COMMIT POINT: Write and atomically replace commit_manifest.json
        manifest_data = {
            "tx_id": tx_id,
            "status": "COMMITTED",
            "staging_dir": staging_dir,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        tmp_manifest = f"{self.manifest_path}.tmp.{os.getpid()}"
        with open(tmp_manifest, "w", encoding="utf-8") as f:
            json.dump(manifest_data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_manifest, self.manifest_path)

        if failure_injection_point == "AFTER_COMMIT_BEFORE_APPLY":
            raise RuntimeError("Injected crash: AFTER_COMMIT_BEFORE_APPLY")

        # 6. Apply phase: copy files to canonical destinations
        if os.path.exists(self.journal_path):
            try:
                shutil.copy2(self.journal_path, f"{self.journal_path}.bak")
            except Exception:
                pass
        shutil.copy2(staged_journal, self.journal_path)

        if failure_injection_point == "MID_APPLY":
            raise RuntimeError("Injected crash: MID_APPLY")

        shutil.copy2(staged_portfolio, self.portfolio_path)
        shutil.copy2(staged_orders, self.orders_path)
        if reservations_data is not None and os.path.exists(staged_reservations):
            shutil.copy2(staged_reservations, self.reservations_path)
        shutil.copy2(staged_audit, self.audit_path)

        # 7. Post-apply cleanup
        try:
            shutil.rmtree(staging_dir, ignore_errors=True)
            if os.path.exists(self.manifest_path):
                os.remove(self.manifest_path)
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # Operational File Loaders & Persistence
    # -------------------------------------------------------------------------

    def is_initialized(self) -> bool:
        """Check if paper journal exists and is initialized."""
        self.recover_interrupted_transaction()
        return os.path.exists(self.journal_path)

    def initialize_state(
        self,
        initial_capital: Decimal = Decimal("5000.00"),
        force: bool = False,
    ) -> AccountingLedger:
        """Initialize external paper trading state with verified capital."""
        self.recover_interrupted_transaction()
        if self.is_initialized() and not force:
            raise PaperStorageSafetyError(
                f"Paper state already initialized at '{self.state_dir}'. Use load or pass force=True to reinitialize."
            )

        os.makedirs(self.state_dir, exist_ok=True)

        # 1. Initialize fresh swing journal
        journal = SwingJournal(self.journal_path)
        ledger = journal.initialize_new(
            initial_capital=str(initial_capital),
            extra_metadata={
                "environment": "PAPER_TRADING_MANUAL",
                "storage_directory": self.state_dir,
                "initial_capital_usd": str(initial_capital),
            },
        )

        # 2. Initialize empty portfolio stops/targets metadata
        initial_portfolio: Dict[str, Any] = {
            "schema_version": "1.0.0",
            "system": "trader-swing-paper",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "lots": {},
        }
        self.save_portfolio(initial_portfolio)

        # 3. Initialize empty orders registry
        initial_orders: Dict[str, Any] = {
            "schema_version": "1.1.0",
            "system": "trader-swing-paper",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "orders": {},
            "idempotency_keys": {},
            "fills": [],
            "exits": [],
        }
        self.save_orders(initial_orders)

        # 4. Log audit initialization
        audit_entry = PaperTradeAuditEntry(
            event_id="INIT-0001",
            timestamp=datetime.now(timezone.utc).isoformat(),
            event_type="INITIALIZE",
            ticker=None,
            shares=None,
            price=None,
            fee=None,
            equity_after=str(initial_capital),
            cash_after=str(initial_capital),
            details={
                "initial_capital": str(initial_capital),
                "state_dir": self.state_dir,
            },
        )
        self.append_audit_event(audit_entry)

        return ledger

    def load_journal(self) -> SwingJournal:
        """Load the SwingJournal instance managing the paper ledger."""
        self.recover_interrupted_transaction()
        if not os.path.exists(self.journal_path):
            raise FileNotFoundError(
                f"Paper trading journal not found at '{self.journal_path}'. Run init first."
            )
        journal = SwingJournal(self.journal_path)
        journal.load()
        return journal

    def load_portfolio(self) -> Dict[str, Any]:
        """Load paper portfolio metadata (stops, targets)."""
        self.recover_interrupted_transaction()
        if not os.path.exists(self.portfolio_path):
            return {"lots": {}, "updated_at": datetime.now(timezone.utc).isoformat()}
        with open(self.portfolio_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def save_portfolio(self, data: Dict[str, Any]) -> None:
        """Atomically persist portfolio metadata."""
        os.makedirs(self.state_dir, exist_ok=True)
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        tmp_path = f"{self.portfolio_path}.tmp.{os.getpid()}"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, self.portfolio_path)

    def load_orders(self) -> Dict[str, Any]:
        """Load paper orders and executions history."""
        self.recover_interrupted_transaction()
        if not os.path.exists(self.orders_path):
            return {
                "schema_version": "1.1.0",
                "system": "trader-swing-paper",
                "orders": {},
                "idempotency_keys": {},
                "fills": [],
                "exits": [],
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        with open(self.orders_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            data.setdefault("orders", {})
            data.setdefault("idempotency_keys", {})
            data.setdefault("fills", [])
            data.setdefault("exits", [])
            return data

    def save_orders(self, data: Dict[str, Any]) -> None:
        """Atomically persist orders data."""
        os.makedirs(self.state_dir, exist_ok=True)
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        tmp_path = f"{self.orders_path}.tmp.{os.getpid()}"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, self.orders_path)

    def get_order(self, order_id: str) -> Optional[Any]:
        """Retrieve a PaperOrder by order ID."""
        from order_models import PaperOrder
        data = self.load_orders()
        raw = data.get("orders", {}).get(order_id)
        if raw is None:
            return None
        return PaperOrder.from_dict(raw)

    def get_order_by_idempotency_key(self, idempotency_key: str) -> Optional[Any]:
        """Retrieve a PaperOrder by its idempotency key."""
        from order_models import PaperOrder
        data = self.load_orders()
        order_id = data.get("idempotency_keys", {}).get(idempotency_key)
        if not order_id:
            for ord_id, o_dict in data.get("orders", {}).items():
                if o_dict.get("idempotency_key") == idempotency_key:
                    return PaperOrder.from_dict(o_dict)
            return None
        raw = data.get("orders", {}).get(order_id)
        if raw is None:
            return None
        return PaperOrder.from_dict(raw)

    def save_order(self, order: Any) -> None:
        """Atomically persist or update a PaperOrder."""
        data = self.load_orders()
        orders_dict = data.setdefault("orders", {})
        idemp_dict = data.setdefault("idempotency_keys", {})

        orders_dict[order.order_id] = order.to_dict()
        idemp_dict[order.idempotency_key] = order.order_id

        self.save_orders(data)

    def load_all_orders(self) -> Dict[str, Any]:
        """Load all PaperOrders as a mapping of order_id -> PaperOrder."""
        from order_models import PaperOrder
        data = self.load_orders()
        result = {}
        for ord_id, o_dict in data.get("orders", {}).items():
            result[ord_id] = PaperOrder.from_dict(o_dict)
        return result

    # -------------------------------------------------------------------------
    # Multi-Gate Human Authorization Persistence (Phase 8.1)
    # -------------------------------------------------------------------------

    def load_authorizations(self) -> Dict[str, Any]:
        """Load authorizations registry from disk."""
        self.recover_interrupted_transaction()
        if not os.path.exists(self.authorizations_path):
            return {
                "schema_version": "1.0.0",
                "system": "trader-swing-auth",
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "authorizations": {},
                "redeemed_tokens": {},
            }
        with open(self.authorizations_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            data.setdefault("authorizations", {})
            data.setdefault("redeemed_tokens", {})
            return data

    def save_authorizations(self, data: Dict[str, Any]) -> None:
        """Atomically persist authorizations registry."""
        os.makedirs(self.state_dir, exist_ok=True)
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        tmp_path = f"{self.authorizations_path}.tmp.{os.getpid()}"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, self.authorizations_path)

    def get_authorization(self, order_id: str) -> Optional[Any]:
        """Retrieve an AuthorizationRecord by order ID."""
        from authorization_models import AuthorizationRecord
        data = self.load_authorizations()
        raw = data.get("authorizations", {}).get(order_id)
        if raw is None:
            return None
        return AuthorizationRecord.from_dict(raw)

    def save_authorization(self, record: Any) -> None:
        """Persist or update an AuthorizationRecord."""
        data = self.load_authorizations()
        data.setdefault("authorizations", {})[record.order_id] = record.to_dict()
        if getattr(record, "token", None) and record.token.used:
            data.setdefault("redeemed_tokens", {})[record.token.token_id] = record.token.used_at
        self.save_authorizations(data)

    def append_audit_event(self, entry: PaperTradeAuditEntry) -> None:
        """Append an immutable audit entry to audit_events.jsonl."""
        os.makedirs(self.state_dir, exist_ok=True)
        line = json.dumps(entry.to_dict()) + "\n"
        with open(self.audit_path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

    def load_audit_events(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Read audit events in chronological order, optionally limited to last N."""
        self.recover_interrupted_transaction()
        if not os.path.exists(self.audit_path):
            return []
        events: List[Dict[str, Any]] = []
        with open(self.audit_path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped:
                    try:
                        events.append(json.loads(stripped))
                    except json.JSONDecodeError:
                        continue
        if limit is not None and limit > 0:
            return events[-limit:]
        return events

    # -------------------------------------------------------------------------
    # Cash Reservation Persistence (Phase 8.2)
    # -------------------------------------------------------------------------

    def load_reservations(self) -> Dict[str, Any]:
        """Load paper cash reservations from disk."""
        self.recover_interrupted_transaction()
        if not os.path.exists(self.reservations_path):
            return {
                "schema_version": "1.0.0",
                "system": "trader-swing-reservations",
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "reservations": {},
            }
        with open(self.reservations_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            data.setdefault("reservations", {})
            return data

    def save_reservations(self, data: Dict[str, Any]) -> None:
        """Atomically persist reservations data."""
        os.makedirs(self.state_dir, exist_ok=True)
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        tmp_path = f"{self.reservations_path}.tmp.{os.getpid()}"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, self.reservations_path)

    def get_reservation(self, order_id: str) -> Optional[Any]:
        """Retrieve a CashReservationRecord by order ID."""
        from cash_reservation import CashReservationRecord
        data = self.load_reservations()
        raw = data.get("reservations", {}).get(order_id)
        if raw is None:
            return None
        return CashReservationRecord.from_dict(raw)

    def save_reservation(self, record: Any) -> None:
        """Persist or update a CashReservationRecord."""
        data = self.load_reservations()
        data.setdefault("reservations", {})[record.order_id] = record.to_dict()
        self.save_reservations(data)
