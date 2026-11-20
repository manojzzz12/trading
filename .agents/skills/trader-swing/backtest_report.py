"""Report generator and persistence for trader-swing backtesting results.

Enforces:
1. Strict repository safety: Writes ONLY to dedicated reports directory.
2. Structured machine-readable JSON output and CSV trade ledger.
3. Completely reproducible reports containing exact configuration, metrics, and audit trail.
"""

import csv
import json
import os
import sys
from typing import Any, Dict, Optional

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)


class BacktestReportGenerator:
    """Serializes backtest execution results to JSON and CSV artifacts."""

    def __init__(self, output_dir: Optional[str] = None):
        if output_dir is None:
            # Dedicated local backtest reports directory under trader-swing
            output_dir = os.path.join(CURRENT_DIR, "reports")
        self.output_dir = os.path.abspath(output_dir)
        os.makedirs(self.output_dir, exist_ok=True)

    def save_json_report(self, results: Dict[str, Any], filename: str = "backtest_report.json") -> str:
        """Export comprehensive backtest report to JSON."""
        filepath = os.path.join(self.output_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)
        return filepath

    def save_trades_csv(self, trades: list, filename: str = "backtest_trades.csv") -> str:
        """Export round-trip closed trades to CSV."""
        filepath = os.path.join(self.output_dir, filename)
        fieldnames = [
            "trade_id",
            "ticker",
            "lot_id",
            "entry_timestamp",
            "exit_timestamp",
            "holding_sessions",
            "shares",
            "entry_price",
            "exit_price",
            "initial_stop_price",
            "initial_target_price",
            "gross_pnl_usd",
            "total_fees_usd",
            "total_slippage_usd",
            "net_pnl_usd",
            "return_pct",
            "r_multiple",
            "exit_reason",
        ]

        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for trade in trades:
                # trade can be a dict or a dataclass
                row = trade if isinstance(trade, dict) else trade.to_dict()
                writer.writerow({k: row.get(k, "") for k in fieldnames})

        return filepath
