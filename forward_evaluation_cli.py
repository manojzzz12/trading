"""Trader-Swing Phase 9 Forward Paper Trading Evaluation CLI (Root Wrapper).

Delegates directly to .agents/skills/trader-swing/forward_evaluation_cli.py
with strict safety prohibitions and zero broker execution.
"""

import importlib.util
import os
import sys

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.join(CURRENT_DIR, ".agents", "skills", "trader-swing")
SKILL_FILE = os.path.join(SKILL_DIR, "forward_evaluation_cli.py")

if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)

# Load underlying implementation directly by file to avoid circular module shadowing
spec = importlib.util.spec_from_file_location("_swing_eval_cli_impl", SKILL_FILE)
_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_mod)

# Re-export module attributes so test imports and CLI callers work without circular import
for _k, _v in _mod.__dict__.items():
    if not _k.startswith("__"):
        globals()[_k] = _v

if __name__ == "__main__":
    _mod.main()
