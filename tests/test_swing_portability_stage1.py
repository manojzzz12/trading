"""Unit tests for Trader-Swing Portability & Configuration Isolation (Stage 1).

Tests:
1. Pathlib-based resolution of default paper state and simulation directories.
2. Environment-variable overrides for TRADING_PAPER_STATE_DIR and TRADING_PAPER_SIMULATION_DIR.
3. Independent user state initialization across different Windows user directories.
4. Fail-closed handling of missing, empty, short, or placeholder TRADER_AUTH_SECRET.
5. Secure isolated keyfile generation and persistence per user state directory.
6. Rejection of corrupted keyfiles (< 16 bytes).
7. Strict absence of default or shared production secrets.
"""

from datetime import datetime, timezone
from decimal import Decimal
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SWING_DIR = os.path.join(PROJECT_ROOT, ".agents", "skills", "trader-swing")
if SWING_DIR not in sys.path:
    sys.path.insert(0, SWING_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from authorization_models import SecretKeyUnavailableError
from authorization_token import HMACApprovalTokenManager, SecretKeyManager
from paper_broker_simulator import (
    PaperBrokerSimulator,
    get_default_simulation_state_dir,
    DEFAULT_SIMULATION_STATE_DIR,
)
from paper_storage import (
    PaperStorageManager,
    get_default_paper_state_dir,
    DEFAULT_PAPER_STATE_DIR,
)


class TestSwingPortabilityStage1(unittest.TestCase):
    """Portability and configuration isolation test suite."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="trader_portability_test_")

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # 1. Pathlib Resolution and Environment Overrides
    # -------------------------------------------------------------------------

    def test_01_default_paper_state_dir_resolves_to_user_home_documents(self):
        """When TRADING_PAPER_STATE_DIR is unset, default path resolves to ~/Documents/TradingPaperState."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TRADING_PAPER_STATE_DIR", None)
            expected = str(Path.home() / "Documents" / "TradingPaperState")
            actual = get_default_paper_state_dir()
            self.assertEqual(actual, expected)
            self.assertFalse("mmano" in actual and "mmano" not in expected)

    def test_02_env_override_paper_state_dir(self):
        """TRADING_PAPER_STATE_DIR overrides the default paper state directory."""
        custom_dir = os.path.join(self.temp_dir, "CustomPaperState")
        with patch.dict(os.environ, {"TRADING_PAPER_STATE_DIR": custom_dir}):
            actual = get_default_paper_state_dir()
            self.assertEqual(actual, os.path.abspath(custom_dir))

            storage = PaperStorageManager()
            self.assertEqual(storage.state_dir, os.path.abspath(custom_dir))

    def test_03_default_simulation_state_dir_resolves_to_user_home_documents(self):
        """When TRADING_PAPER_SIMULATION_DIR is unset, default path resolves to ~/Documents/TradingPaperSimulation."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TRADING_PAPER_SIMULATION_DIR", None)
            expected = str(Path.home() / "Documents" / "TradingPaperSimulation")
            actual = get_default_simulation_state_dir()
            self.assertEqual(actual, expected)

    def test_04_env_override_simulation_state_dir(self):
        """TRADING_PAPER_SIMULATION_DIR overrides the default simulation directory."""
        custom_sim_dir = os.path.join(self.temp_dir, "CustomSimulationState")
        with patch.dict(os.environ, {"TRADING_PAPER_SIMULATION_DIR": custom_sim_dir}):
            actual = get_default_simulation_state_dir()
            self.assertEqual(actual, os.path.abspath(custom_sim_dir))

            sim = PaperBrokerSimulator()
            self.assertEqual(sim.storage.state_dir, os.path.abspath(custom_sim_dir))

    def test_05_multi_user_isolation(self):
        """Simulate two distinct Windows users and verify independent state paths."""
        user_a_home = Path(self.temp_dir) / "Users" / "Alice"
        user_b_home = Path(self.temp_dir) / "Users" / "Bob"
        user_a_home.mkdir(parents=True)
        user_b_home.mkdir(parents=True)

        with patch("pathlib.Path.home", return_value=user_a_home):
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("TRADING_PAPER_STATE_DIR", None)
                path_a = get_default_paper_state_dir()
                self.assertEqual(path_a, str(user_a_home / "Documents" / "TradingPaperState"))

        with patch("pathlib.Path.home", return_value=user_b_home):
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("TRADING_PAPER_STATE_DIR", None)
                path_b = get_default_paper_state_dir()
                self.assertEqual(path_b, str(user_b_home / "Documents" / "TradingPaperState"))

        self.assertNotEqual(path_a, path_b)
        self.assertIn("Alice", path_a)
        self.assertIn("Bob", path_b)

    # -------------------------------------------------------------------------
    # 2. Authorization Secret Fail-Closed Governance
    # -------------------------------------------------------------------------

    def test_06_missing_auth_secret_fails_closed(self):
        """Missing TRADER_AUTH_SECRET with no secret_dir must fail closed with SecretKeyUnavailableError."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TRADER_AUTH_SECRET", None)
            mgr = SecretKeyManager(secret_dir=None, allow_ephemeral=False)
            with self.assertRaises(SecretKeyUnavailableError) as ctx:
                mgr.get_signing_secret()
            self.assertIn("Missing authorization secret", str(ctx.exception))

    def test_07_empty_or_whitespace_auth_secret_fails_closed(self):
        """Empty or whitespace-only TRADER_AUTH_SECRET must fail closed."""
        for empty_val in ["", "   ", "\t\n"]:
            with patch.dict(os.environ, {"TRADER_AUTH_SECRET": empty_val}):
                mgr = SecretKeyManager(secret_dir=None, allow_ephemeral=False)
                with self.assertRaises(SecretKeyUnavailableError) as ctx:
                    mgr.get_signing_secret()
                self.assertIn("empty or whitespace", str(ctx.exception))

    def test_08_short_auth_secret_fails_closed(self):
        """TRADER_AUTH_SECRET shorter than 16 bytes must fail closed."""
        with patch.dict(os.environ, {"TRADER_AUTH_SECRET": "too_short"}):
            mgr = SecretKeyManager(secret_dir=None, allow_ephemeral=False)
            with self.assertRaises(SecretKeyUnavailableError) as ctx:
                mgr.get_signing_secret()
            self.assertIn("too short", str(ctx.exception))

    def test_09_insecure_placeholder_auth_secret_fails_closed(self):
        """Known placeholder or default secrets must fail closed."""
        placeholders = [
            "your_secret_here",
            "default_secret",
            "changeme",
            "production_secret",
            "password12345678",
        ]
        for ph in placeholders:
            with patch.dict(os.environ, {"TRADER_AUTH_SECRET": ph}):
                mgr = SecretKeyManager(secret_dir=None, allow_ephemeral=False)
                with self.assertRaises(SecretKeyUnavailableError) as ctx:
                    mgr.get_signing_secret()
                self.assertIn("insecure default/placeholder", str(ctx.exception))

    def test_10_valid_auth_secret_accepted(self):
        """Valid TRADER_AUTH_SECRET (>= 16 bytes, non-placeholder) is accepted."""
        valid_secret = "a_very_secure_production_secret_key_32_bytes_long"
        with patch.dict(os.environ, {"TRADER_AUTH_SECRET": valid_secret}):
            mgr = SecretKeyManager(secret_dir=None, allow_ephemeral=False)
            secret_bytes = mgr.get_signing_secret()
            self.assertEqual(secret_bytes, valid_secret.encode("utf-8"))

    def test_11_secret_dir_generates_isolated_persistent_keyfile(self):
        """In absence of TRADER_AUTH_SECRET, secret_dir generates and persists an isolated keyfile."""
        secret_dir = os.path.join(self.temp_dir, "UserPaperState")
        os.makedirs(secret_dir, exist_ok=True)

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TRADER_AUTH_SECRET", None)

            # First instance: generates keyfile
            mgr1 = SecretKeyManager(secret_dir=secret_dir)
            sec1 = mgr1.get_signing_secret()
            self.assertIsInstance(sec1, bytes)
            self.assertGreaterEqual(len(sec1), 32)

            key_file = os.path.join(secret_dir, ".secrets", "auth_signing.key")
            self.assertTrue(os.path.exists(key_file))

            # Second instance: loads identical keyfile
            mgr2 = SecretKeyManager(secret_dir=secret_dir)
            sec2 = mgr2.get_signing_secret()
            self.assertEqual(sec1, sec2)

    def test_12_corrupted_keyfile_fails_closed(self):
        """Keyfile with < 16 bytes must raise SecretKeyUnavailableError rather than proceeding."""
        secret_dir = os.path.join(self.temp_dir, "CorruptPaperState")
        secrets_folder = os.path.join(secret_dir, ".secrets")
        os.makedirs(secrets_folder, exist_ok=True)
        key_file = os.path.join(secrets_folder, "auth_signing.key")

        with open(key_file, "wb") as f:
            f.write(b"corrupt_short")  # 13 bytes (< 16)

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TRADER_AUTH_SECRET", None)
            mgr = SecretKeyManager(secret_dir=secret_dir)
            with self.assertRaises(SecretKeyUnavailableError) as ctx:
                mgr.get_signing_secret()
            self.assertIn("invalid secret (< 16 bytes)", str(ctx.exception))

    def test_13_token_manager_fails_closed_without_secret(self):
        """HMACApprovalTokenManager fails closed when neither env secret nor secret_dir is configured."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TRADER_AUTH_SECRET", None)
            token_mgr = HMACApprovalTokenManager()
            with self.assertRaises(SecretKeyUnavailableError):
                token_mgr.key_manager.get_signing_secret()

    def test_14_no_hardcoded_production_secret(self):
        """Assert there is no hardcoded default secret attribute on SecretKeyManager or module."""
        import authorization_token
        for attr in ["DEFAULT_SECRET", "DEFAULT_AUTH_SECRET", "PRODUCTION_SECRET", "SECRET_KEY"]:
            self.assertFalse(hasattr(authorization_token, attr), f"Forbidden secret attribute found: {attr}")
            self.assertFalse(hasattr(SecretKeyManager, attr), f"Forbidden secret attribute found on class: {attr}")


if __name__ == "__main__":
    unittest.main()
