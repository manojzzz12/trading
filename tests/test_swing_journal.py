"""Unit test suite for trader-swing journal persistence and recovery.

Verifies:
1. Initial journal creation.
2. Decimal serialization and deserialization fidelity.
3. Schema validation and error handling on malformed JSON.
4. Atomic write via temporary file replacement.
5. Backup snapshot rotation (.bak).
6. Non-destructive recovery from a corrupted main journal.
7. Recovery when backup is valid even if primary is broken.
8. Controlled failure when neither journal nor backup is recoverable.
9. Protection of root files (rejection of paper_journal.json, portfolio.json, broker.json).
"""

from decimal import Decimal
import json
import os
import sys
import tempfile
import unittest

SWING_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".agents",
    "skills",
    "trader-swing",
)
if SWING_DIR not in sys.path:
    sys.path.insert(0, SWING_DIR)

from journal import (
    SwingJournal,
    JournalError,
    JournalValidationError,
    JournalRecoveryError,
)
from accounting import AccountingLedger


class TestSwingJournal(unittest.TestCase):
    """Test suite for SwingJournal persistence, recovery, and safety."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.journal_file = os.path.join(self.temp_dir.name, "swing_paper_journal_us.json")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_01_initial_journal_creation(self):
        """1. Initial journal creation on disk."""
        journal = SwingJournal(self.journal_file)
        ledger = journal.initialize_new(initial_capital="5000.00")

        self.assertTrue(os.path.exists(self.journal_file))
        self.assertEqual(ledger.cash_usd, Decimal("5000.00"))

        with open(self.journal_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["system"], "trader-swing")
        self.assertEqual(data["schema_version"], "1.0.0")
        self.assertEqual(data["ledger"]["cash_usd"], "5000.00")

    def test_02_decimal_serialization_deserialization(self):
        """2. Exact Decimal string serialization and deserialization."""
        journal = SwingJournal(self.journal_file)
        ledger = journal.initialize_new("5000.00")
        ledger.buy("AAPL", 10, "150.25", fee="1.50")
        journal.save()

        # Load fresh instance from disk
        journal_loaded = SwingJournal(self.journal_file)
        loaded_ledger = journal_loaded.load()

        self.assertIsInstance(loaded_ledger.cash_usd, Decimal)
        self.assertEqual(loaded_ledger.cash_usd, Decimal("3496.00"))
        self.assertEqual(loaded_ledger.positions["AAPL"].total_shares, 10)
        self.assertEqual(loaded_ledger.positions["AAPL"].lots[0].entry_price, Decimal("150.25"))

    def test_03_schema_validation(self):
        """3. Schema validation rejects malformed structures."""
        journal = SwingJournal(self.journal_file)
        journal.initialize_new()

        # Corrupt system identifier
        with open(self.journal_file, "w", encoding="utf-8") as f:
            json.dump({"system": "wrong-system", "schema_version": "1.0.0"}, f)

        # Primary has invalid schema and no backup exists, so raises JournalRecoveryError wrapping JournalValidationError
        with self.assertRaises((JournalValidationError, JournalRecoveryError)):
            journal.load()

        # Direct payload validation raises JournalValidationError
        with self.assertRaises(JournalValidationError):
            journal.validate_payload({"system": "wrong-system", "schema_version": "1.0.0"})

    def test_04_atomic_write(self):
        """4. Atomic write creates and replaces temporary files cleanly."""
        journal = SwingJournal(self.journal_file)
        journal.initialize_new()

        # Perform save and check temporary file does not remain
        journal.ledger.buy("MSFT", 5, "200.00", fee="1.00")
        journal.save()

        tmp_files = [f for f in os.listdir(self.temp_dir.name) if ".tmp." in f]
        self.assertEqual(len(tmp_files), 0, "Temporary write files must not linger after atomic replacement.")

    def test_05_backup_rotation(self):
        """5. Backup rotation maintains .bak copy."""
        journal = SwingJournal(self.journal_file)
        journal.initialize_new()
        bak_file = f"{self.journal_file}.bak"

        # Initially before second save, backup may not exist or exists after update
        journal.ledger.buy("GOOG", 2, "100.00", fee="1.00")
        journal.save()

        self.assertTrue(os.path.exists(bak_file), "Rolling backup .bak must be created on update.")

    def test_06_recovery_from_corrupted_main_journal(self):
        """6. Recovery from corrupted main journal restores from .bak."""
        journal = SwingJournal(self.journal_file)
        journal.initialize_new("5000.00")
        journal.ledger.buy("AMZN", 10, "100.00", fee="1.00")
        journal.save()  # Creates backup of previous state

        bak_file = f"{self.journal_file}.bak"
        self.assertTrue(os.path.exists(bak_file))

        # Corrupt the primary journal with invalid JSON garbage
        with open(self.journal_file, "w", encoding="utf-8") as f:
            f.write("{ INVALID JSON DATA TRUNCATED")

        # Load must recover from backup and quarantine corrupt file
        journal_recover = SwingJournal(self.journal_file)
        recovered_ledger = journal_recover.load()

        self.assertIsNotNone(recovered_ledger)
        self.assertEqual(recovered_ledger.initial_capital_usd, Decimal("5000.00"))

        # Verify quarantine file was created
        quarantine_files = [f for f in os.listdir(self.temp_dir.name) if ".corrupt." in f]
        self.assertEqual(len(quarantine_files), 1, "Corrupted file must be preserved in quarantine.")

    def test_07_recovery_when_backup_valid_even_if_primary_corrupt(self):
        """7. Recovery succeeds when backup is valid and asserts invariant."""
        journal = SwingJournal(self.journal_file)
        journal.initialize_new("5000.00")
        journal.ledger.buy("TSLA", 5, "200.00", fee="1.00")
        journal.save()
        # Second save ensures .bak contains the TSLA position
        journal.ledger.update_market_price("TSLA", "205.00")
        journal.save()

        # Invalidate primary JSON
        with open(self.journal_file, "w", encoding="utf-8") as f:
            f.write("Corrupt gibberish")

        loaded = SwingJournal(self.journal_file).load()
        self.assertEqual(loaded.positions["TSLA"].total_shares, 5)

    def test_08_failure_when_no_valid_journal_or_backup(self):
        """8. Controlled failure when neither journal nor backup is recoverable."""
        journal = SwingJournal(self.journal_file)
        journal.initialize_new("5000.00")

        # Corrupt both primary and delete backup
        bak_file = f"{self.journal_file}.bak"
        if os.path.exists(bak_file):
            os.remove(bak_file)

        with open(self.journal_file, "w", encoding="utf-8") as f:
            f.write("Corrupted")

        with self.assertRaises(JournalRecoveryError):
            SwingJournal(self.journal_file).load()

    def test_09_prohibition_of_protected_root_files(self):
        """9. Strict rejection of protected root file names."""
        for forbidden in ["paper_journal.json", "portfolio.json", "broker.json"]:
            path = os.path.join(self.temp_dir.name, forbidden)
            with self.assertRaises(JournalError):
                SwingJournal(path)

    def test_10_load_from_static_fixture(self):
        """10. Verify loading from static test fixture."""
        fixture_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "tests",
            "fixtures",
            "trader-swing",
            "sample_ledger.json",
        )
        self.assertTrue(os.path.exists(fixture_path))
        journal = SwingJournal(fixture_path)
        ledger = journal.load()

        self.assertEqual(ledger.initial_capital_usd, Decimal("5000.00"))
        self.assertEqual(ledger.cash_usd, Decimal("2997.50"))
        self.assertEqual(ledger.total_equity_usd, Decimal("5147.50"))
        self.assertEqual(ledger.positions["AAPL"].total_shares, 10)
        inv = ledger.validate_equity_invariant()
        self.assertTrue(inv["holds"])


if __name__ == "__main__":
    unittest.main()
