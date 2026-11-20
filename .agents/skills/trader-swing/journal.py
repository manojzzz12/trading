"""Journal persistence engine for trader-swing.

Manages atomic disk persistence, rolling backups, non-destructive recovery,
versioned JSON serialization, and strict equity invariant validation.
Never touches or overwrites root historical journal or broker files.
"""

import os
import json
import shutil
import datetime
from typing import Dict, Any, Optional

from accounting import AccountingLedger, AccountingInvariantViolation, AccountingError


SCHEMA_VERSION = "1.0.0"
SYSTEM_IDENTIFIER = "trader-swing"

PROTECTED_ROOT_FILES = {"paper_journal.json", "portfolio.json", "broker.json"}


class JournalError(Exception):
    """Base exception for journal operations."""
    pass


class JournalValidationError(JournalError):
    """Raised when journal structure, schema, or invariant fails validation."""
    pass


class JournalRecoveryError(JournalError):
    """Raised when both the primary journal and all backups are corrupted or unrecoverable."""
    pass


class SwingJournal:
    """Manages transactional disk persistence for trader-swing accounting state."""

    def __init__(self, journal_path: str):
        self.journal_path = os.path.abspath(journal_path)
        self._validate_path_safety(self.journal_path)
        self.ledger: Optional[AccountingLedger] = None
        self.metadata: Dict[str, Any] = {}

    @staticmethod
    def _validate_path_safety(path: str) -> None:
        """Prevent any caller from pointing the swing journal at protected historical root files."""
        base_name = os.path.basename(path).lower()
        if base_name in PROTECTED_ROOT_FILES:
            raise JournalError(
                f"Prohibited journal path '{path}': access to historical root "
                f"'{base_name}' is strictly forbidden in trader-swing."
            )

    def initialize_new(self, initial_capital: Any = "5000.00", extra_metadata: Optional[Dict[str, Any]] = None) -> AccountingLedger:
        """Initialize a fresh ledger and save it to disk."""
        self.ledger = AccountingLedger(initial_capital=initial_capital)
        self.metadata = {
            "schema_version": SCHEMA_VERSION,
            "system": SYSTEM_IDENTIFIER,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "description": "Autonomous US Stock Swing Trading Paper Journal",
        }
        if extra_metadata:
            self.metadata.update(extra_metadata)
        self.save()
        return self.ledger

    def validate_payload(self, data: Dict[str, Any]) -> None:
        """Validate top-level JSON structure and schema."""
        if not isinstance(data, dict):
            raise JournalValidationError("Journal payload must be a JSON object.")
        if data.get("system") != SYSTEM_IDENTIFIER:
            raise JournalValidationError(f"Invalid system identifier: expected '{SYSTEM_IDENTIFIER}', got '{data.get('system')}'")
        if "schema_version" not in data:
            raise JournalValidationError("Missing 'schema_version' in journal payload.")
        if "ledger" not in data or not isinstance(data["ledger"], dict):
            raise JournalValidationError("Missing or invalid 'ledger' object in journal payload.")

    def save(self) -> None:
        """Atomically persist journal state to disk with backup rotation."""
        if self.ledger is None:
            raise JournalError("Cannot save uninitialized ledger.")

        # Re-verify invariant prior to persisting
        self.ledger.validate_equity_invariant()

        self.metadata["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        payload = {
            "schema_version": self.metadata.get("schema_version", SCHEMA_VERSION),
            "system": SYSTEM_IDENTIFIER,
            "metadata": self.metadata,
            "ledger": self.ledger.to_dict(),
        }

        # Validate structure before writing
        self.validate_payload(payload)

        parent_dir = os.path.dirname(self.journal_path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)

        tmp_path = f"{self.journal_path}.tmp.{os.getpid()}"
        bak_path = f"{self.journal_path}.bak"

        # 1. Write to temp file and flush to disk
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
        except Exception as err:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise JournalError(f"Failed writing temporary journal file '{tmp_path}': {err}") from err

        # 2. Maintain rolling backup of existing file
        if os.path.exists(self.journal_path):
            try:
                shutil.copy2(self.journal_path, bak_path)
            except Exception as err:
                # Do not block save if backup copy fails, but log/warn
                pass

        # 3. Atomic replace
        try:
            os.replace(tmp_path, self.journal_path)
        except Exception as err:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise JournalError(f"Failed atomic replacement of '{self.journal_path}': {err}") from err

    def load(self) -> AccountingLedger:
        """Load journal from disk with non-destructive recovery if corrupted."""
        if not os.path.exists(self.journal_path):
            raise FileNotFoundError(f"Journal file not found: {self.journal_path}")

        try:
            data = self._read_and_validate_file(self.journal_path)
            self.metadata = data.get("metadata", {})
            self.ledger = AccountingLedger.from_dict(data["ledger"])
            return self.ledger
        except Exception as primary_err:
            # Primary journal corrupted or invalid - initiate non-destructive recovery
            return self._recover_from_backup(primary_err)

    def _read_and_validate_file(self, path: str) -> Dict[str, Any]:
        """Read a JSON journal file, validate schema, and assert invariant."""
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.validate_payload(data)
        # Attempt ledger reconstruction to assert invariant
        AccountingLedger.from_dict(data["ledger"])
        return data

    def _recover_from_backup(self, primary_err: Exception) -> AccountingLedger:
        """Quarantine corrupted file and restore from .bak."""
        bak_path = f"{self.journal_path}.bak"
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
        corrupt_quarantine_path = f"{self.journal_path}.corrupt.{ts}"

        # 1. Non-destructive quarantine: move corrupted file rather than overwriting
        try:
            shutil.copy2(self.journal_path, corrupt_quarantine_path)
        except Exception:
            pass

        # 2. Check if backup exists
        if not os.path.exists(bak_path):
            raise JournalRecoveryError(
                f"Primary journal '{self.journal_path}' is invalid ({primary_err}) "
                f"and no backup file exists at '{bak_path}'. "
                f"Corrupted file preserved at '{corrupt_quarantine_path}'."
            ) from primary_err

        # 3. Validate backup file
        try:
            bak_data = self._read_and_validate_file(bak_path)
        except Exception as bak_err:
            raise JournalRecoveryError(
                f"CRITICAL: Both primary journal and backup are invalid! "
                f"Primary error: {primary_err}. Backup error: {bak_err}. "
                f"Corrupted file preserved at '{corrupt_quarantine_path}'."
            ) from bak_err

        # 4. Restore valid backup as primary
        shutil.copy2(bak_path, self.journal_path)
        self.metadata = bak_data.get("metadata", {})
        self.ledger = AccountingLedger.from_dict(bak_data["ledger"])
        return self.ledger
