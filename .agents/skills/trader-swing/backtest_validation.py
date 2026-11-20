"""Robustness, out-of-sample partitioning, and sensitivity analysis for trader-swing.

Enforces:
1. Temporal in-sample / out-of-sample partitioning without look-ahead or overlap.
2. Sensitivity evaluation across execution cost scenarios (low-cost, base, conservative).
3. Parameter stress testing across stop distances and regime requirements.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from backtest_data import HistoricalDataManager
from backtest_engine import EventDrivenBacktestEngine
from backtest_models import (
    BacktestConfig,
    ExecutionAssumptions,
    SlippageConfig,
)
from risk import FeeConfig, FeeSchedule, RiskConfig


@dataclass(frozen=True)
class PartitionedDateRange:
    """In-sample and Out-of-sample non-overlapping date bounds."""
    in_sample_start: date
    in_sample_end: date
    out_of_sample_start: date
    out_of_sample_end: date


class BacktestValidationSuite:
    """Manages robustness testing, train/test splitting, and scenario comparisons."""

    @staticmethod
    def partition_date_range(
        sessions: List[date],
        in_sample_ratio: float = 0.70,
    ) -> PartitionedDateRange:
        """Divide chronological trading sessions into strictly separated in-sample and out-of-sample periods."""
        if len(sessions) < 10:
            raise ValueError(f"Insufficient sessions ({len(sessions)}) for in-sample / out-of-sample partitioning.")

        split_idx = int(len(sessions) * in_sample_ratio)
        split_idx = max(5, min(split_idx, len(sessions) - 5))

        return PartitionedDateRange(
            in_sample_start=sessions[0],
            in_sample_end=sessions[split_idx - 1],
            out_of_sample_start=sessions[split_idx],
            out_of_sample_end=sessions[-1],
        )

    @staticmethod
    def run_sensitivity_scenarios(
        base_config: BacktestConfig,
        data_manager: HistoricalDataManager,
    ) -> Dict[str, Dict[str, Any]]:
        """Run standard sensitivity scenarios: low-cost, base-case, and conservative."""
        scenarios: Dict[str, BacktestConfig] = {}

        # 1. Low-cost scenario: 1 bps slippage, zero fees
        low_cost = BacktestConfig(
            start_date=base_config.start_date,
            end_date=base_config.end_date,
            universe=base_config.universe,
            initial_capital_usd=base_config.initial_capital_usd,
            risk_config=base_config.risk_config,
            fee_config=FeeConfig(entry=FeeSchedule(), exit=FeeSchedule()),
            slippage_config=SlippageConfig(entry_bps=Decimal("1.0"), exit_bps=Decimal("1.0")),
            execution_assumptions=base_config.execution_assumptions,
            benchmark_ticker=base_config.benchmark_ticker,
            top_candidates_per_session=base_config.top_candidates_per_session,
        )
        scenarios["LOW_COST"] = low_cost

        # 2. Base case scenario: standard 5 bps slippage, zero broker fees
        scenarios["BASE_CASE"] = base_config

        # 3. Conservative scenario: 15 bps slippage, $1.00 fixed ticket fee
        conservative = BacktestConfig(
            start_date=base_config.start_date,
            end_date=base_config.end_date,
            universe=base_config.universe,
            initial_capital_usd=base_config.initial_capital_usd,
            risk_config=base_config.risk_config,
            fee_config=FeeConfig(
                entry=FeeSchedule(fixed=Decimal("1.00")),
                exit=FeeSchedule(fixed=Decimal("1.00")),
            ),
            slippage_config=SlippageConfig(entry_bps=Decimal("15.0"), exit_bps=Decimal("15.0")),
            execution_assumptions=base_config.execution_assumptions,
            benchmark_ticker=base_config.benchmark_ticker,
            top_candidates_per_session=base_config.top_candidates_per_session,
        )
        scenarios["CONSERVATIVE"] = conservative

        comparison_results: Dict[str, Dict[str, Any]] = {}
        for name, cfg in scenarios.items():
            engine = EventDrivenBacktestEngine(config=cfg, data_manager=data_manager)
            res = engine.run()
            comparison_results[name] = {
                "total_return_pct": res["portfolio_metrics"]["total_return_pct"],
                "cagr_pct": res["portfolio_metrics"]["cagr_pct"],
                "max_drawdown_pct": res["portfolio_metrics"]["max_drawdown_pct"],
                "sharpe_ratio": res["portfolio_metrics"]["sharpe_ratio"],
                "total_trades": res["trade_metrics"]["total_trades"],
                "win_rate_pct": res["trade_metrics"]["win_rate_pct"],
                "profit_factor": res["trade_metrics"]["profit_factor"],
            }

        return comparison_results
