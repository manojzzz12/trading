"""Trader-Swing Forward Evaluation CLI Entrypoint.

Direct entrypoint for Phase 9 forward paper trading evaluation.
Delegates to .agents/skills/trader-swing/forward_evaluation_cli.py.
"""

import os
import sys

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.join(CURRENT_DIR, ".agents", "skills", "trader-swing")
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)

from forward_evaluation_cli import main

if __name__ == "__main__":
    main()
