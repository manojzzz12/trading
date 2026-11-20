"""Command-Line Interface for Trader-Swing Phase 9 Forward Paper Evaluation.

Enforces:
1. On-demand manual research execution; zero daemons, scheduling, or background polling.
2. Complete rejection of prohibited arguments (--live, --broker, --auto, --stream, etc.).
3. Pure offline execution using permitted local market data fixtures.
4. Transparent candidate funnel reporting and statistical performance assessment.
5. Strict isolation from root paper_journal.json and external TradingPaperState.
"""

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
import sys
from typing import Any, Dict, List, Optional

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, "..", "..", ".."))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from forward_evaluation_models import (
    ForwardDataMode,
    MARKER_FORWARD_EVALUATION,
    MARKER_SIMULATED_TRADE,
)
from forward_evaluation_service import ForwardEvaluationService
from forward_evaluation_storage import (
    DEFAULT_FORWARD_EVALUATION_DIR,
    DuplicateObservationError,
    ForwardEvaluationStorage,
)
from feed_adapter import MarketDataReadinessEvaluator
from market_data_models import MarketClock
from pilot_simulation_runner import make_pilot_series

PROHIBITED_CLI_ARGS = {
    "--live",
    "--broker",
    "--real",
    "--auto",
    "--poll",
    "--daemon",
    "--stream",
    "--webhook",
    "--alpaca",
    "--ibkr",
    "--secret",
    "--key",
}


def validate_cli_safety() -> None:
    """Enforce fail-closed safety by blocking any argument suggesting automated or live execution."""
    for arg in sys.argv[1:]:
        lower_arg = arg.lower().split("=")[0]
        if lower_arg in PROHIBITED_CLI_ARGS:
            print(f"\n[FATAL SAFETY VIOLATION] Prohibited argument '{arg}' detected.")
            print("Live brokers, automated execution, and cloud daemons are strictly forbidden in Phase 9 Forward Evaluation.\n")
            sys.exit(1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Trader-Swing Phase 9 Forward Paper Trading Evaluation CLI",
    )
    parser.add_argument(
        "--session-date",
        type=str,
        default=None,
        help="Session date to evaluate (YYYY-MM-DD, defaults to latest available session)",
    )
    parser.add_argument(
        "--mode",
        type=str,
        choices=["DAILY", "FIXTURE", "ALL"],
        default="DAILY",
        help="Evaluation data mode (DAILY by default; FIXTURE or ALL for diagnostic inspection)",
    )
    parser.add_argument(
        "--data-dir",
        type=str,
        default=None,
        help="Custom current daily market data directory (defaults to .planning/data/current_daily)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Include all records (genuine DAILY and diagnostic FIXTURE) in report or summary",
    )
    parser.add_argument(
        "--storage-dir",
        type=str,
        default=None,
        help="Custom isolated storage directory for evaluation artifacts",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Generate and print comprehensive performance evaluation report (strictly read-only)",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print summary of historical forward evaluation sessions (strictly read-only)",
    )
    return parser.parse_args()


def main() -> None:
    validate_cli_safety()
    args = parse_args()

    print("==================================================================")
    print("   TRADER-SWING: PHASE 9 FORWARD PAPER TRADING EVALUATION TERMINAL")
    print("             [FORWARD EVALUATION ONLY — NOT EXECUTED]             ")
    print("==================================================================")

    storage = ForwardEvaluationStorage(base_dir=args.storage_dir)
    service = ForwardEvaluationService(storage=storage)

    # -------------------------------------------------------------------------
    # Strictly Read-Only Report Path (zero acquisition, zero file mutations)
    # -------------------------------------------------------------------------
    if args.report:
        genuine_only = not (args.all or args.mode in ("FIXTURE", "ALL"))
        filter_mode = "FIXTURE" if args.mode == "FIXTURE" else None

        report = service.generate_performance_report(
            data_mode=filter_mode,
            genuine_only=genuine_only,
        )

        if genuine_only:
            print("\n--- Consolidated Forward Performance Assessment (Genuine DAILY Only) ---")
        elif filter_mode == "FIXTURE":
            print("\n--- DIAGNOSTIC Assessment (Synthetic FIXTURE Test Data Only — NOT Performance Evidence) ---")
            print("  ⚠️ WARNING: Viewing synthetic test fixtures. These records do NOT constitute forward evaluation evidence.")
        else:
            print("\n--- COMBINED Diagnostic Performance Assessment (Includes Synthetic Fixtures) ---")
            print("  ⚠️ WARNING: Diagnostic records included. These records do NOT constitute forward evaluation evidence.")

        print(f"  Evaluation Window      : {report.evaluation_start_date} -> {report.evaluation_end_date}")
        print(f"  Scope Filter           : {report.data_mode_filter}")
        print(f"  Sessions Evaluated     : {report.total_sessions_evaluated}")
        print(f"  Total Screened         : {report.total_symbols_screened}")
        print(f"  Eligible Setups        : {report.total_eligible_candidates} ({report.candidate_conversion_pct}% conversion)")
        print(f"  Active Positions       : {report.open_positions_count}")
        print(f"  Closed Trades          : {report.closed_trades_count}")
        print(f"  Win Rate               : {report.win_rate_pct}% (95% CI: [{report.win_rate_ci_lower_pct}%, {report.win_rate_ci_upper_pct}%])")
        print(f"  Profit Factor          : {report.profit_factor if report.profit_factor is not None else 'N/A'}")
        print(f"  Net Realized P&L       : ${report.net_realized_pnl:,.2f}")
        print(f"  Current Unrealized P&L : ${report.current_unrealized_pnl:,.2f}")
        print(f"  Total Net P&L          : ${report.total_net_pnl:,.2f}")
        print(f"  Max Drawdown           : ${report.max_drawdown_usd:,.2f} ({report.max_drawdown_pct}%)")
        print(f"  Average Holding Period : {report.average_holding_days} sessions")
        print(f"  Average MFE / MAE      : ${report.average_mfe_usd} / ${report.average_mae_usd}")
        print(f"\n  Statistical Status     : {'CONFIRMED (N>=30)' if report.is_sample_statistically_significant else 'CAUTION (N<30)'}")
        print(f"  Caveat Note            : {report.statistical_caveat}")
        print("==================================================================")
        return

    # -------------------------------------------------------------------------
    # Strictly Read-Only Summary Path (zero acquisition, zero file mutations)
    # -------------------------------------------------------------------------
    if args.summary:
        genuine_only = not (args.all or args.mode in ("FIXTURE", "ALL"))
        filter_mode = "FIXTURE" if args.mode == "FIXTURE" else None

        summaries = storage.load_session_summaries(
            data_mode=filter_mode,
            genuine_only=genuine_only,
        )

        scope_label = "Genuine DAILY Only" if genuine_only else ("Diagnostic FIXTURE Only" if filter_mode == "FIXTURE" else "Combined All")
        print(f"\n--- Historical Forward Evaluation Sessions ({scope_label}) ---")
        if not genuine_only:
            print("  ⚠️ WARNING: Diagnostic records included. These do NOT constitute forward evaluation evidence.")

        if not summaries:
            print("  No evaluation session summaries found matching the requested scope.")
        else:
            print(f"  {'Date':<12} | {'Mode':<8} | {'Regime':<10} | {'Screened':<8} | {'Eligible':<8} | {'Trades':<6} | {'Equity':<10}")
            print("  " + "-" * 72)
            for s in summaries:
                print(f"  {s.session_date:<12} | {s.data_mode:<8} | {s.market_regime:<10} | {s.tickers_screened:<8} | {s.eligible_candidates:<8} | {s.hypothetical_trades_opened:<6} | ${s.virtual_equity:<10}")
        print("==================================================================")
        return

    # -------------------------------------------------------------------------
    # Session Evaluation Runner
    # -------------------------------------------------------------------------
    if args.mode == "FIXTURE":
        # Pure offline diagnostic fixture run (NOT performance evidence)
        target_date = args.session_date or "2026-11-20"
        print(f"\n⚠️  [DIAGNOSTIC NOTICE] Running controlled offline synthetic fixture session ({target_date}).")
        print("    These records are tagged 'FIXTURE' and strictly excluded from genuine forward metrics.")
        proxy_map = {
            "SPY": make_pilot_series("SPY", 450.0, trend="bullish"),
            "QQQ": make_pilot_series("QQQ", 380.0, trend="bullish"),
            "IWM": make_pilot_series("IWM", 200.0, trend="bullish"),
        }
        stock_map = {
            "AAPL": make_pilot_series("AAPL", 150.0, trend="bullish"),
            "MSFT": make_pilot_series("MSFT", 220.0, trend="bullish"),
        }
        summary = service.run_forward_session(
            session_date=target_date,
            proxy_candles_map=proxy_map,
            stock_candles_map=stock_map,
            data_mode=ForwardDataMode.FIXTURE.value,
            force_idempotent_overwrite=True,
        )
    elif args.mode == "DAILY":
        # Genuine Daily Observation Path (100% free of synthetic pilot data)
        data_dir = args.data_dir or os.path.join(PROJECT_ROOT, ".planning", "data", "current_daily")
        if not os.path.exists(data_dir):
            print(f"\n[FATAL ERROR] Current daily data directory not found: '{data_dir}'")
            print("Run 'python scripts/acquire_current_daily.py' after 4:15 PM Eastern to acquire daily data.\n")
            sys.exit(1)

        print(f"\nLoading Genuine Daily Candles from: {data_dir}")
        try:
            proxy_map, stock_map, effective_date_str, load_warnings = service.load_candles_from_daily_partition(
                data_dir=data_dir,
                as_of_date=args.session_date,
            )
        except Exception as e:
            print(f"\n[FATAL DATA FAILURE] Failed to load daily partition: {e}\n")
            sys.exit(1)

        for w in load_warnings:
            print(f"  [DATA WARNING] {w}")

        eval_session_date = args.session_date or effective_date_str
        if args.session_date and args.session_date != effective_date_str:
            print(f"\n[FATAL SESSION DATE MISMATCH] Requested date '{args.session_date}' does not match latest data '{effective_date_str}'.")
            print("Forward evaluation requires exact session alignment. Stale or missing data detected.\n")
            sys.exit(1)

        # Enforce completed session constraint
        clock = MarketClock()
        evaluator = MarketDataReadinessEvaluator(clock=clock)
        now_utc = datetime.now(timezone.utc)
        latest_completed_sess = evaluator.get_latest_completed_session(now_utc)
        if latest_completed_sess is not None and eval_session_date > latest_completed_sess.isoformat():
            print(f"\n[FATAL INCOMPLETE SESSION] Session date '{eval_session_date}' has not yet completed regular trading hours.")
            print(f"Latest completed regular session is {latest_completed_sess.isoformat()}. Run after 4:15 PM Eastern.\n")
            sys.exit(1)

        # Enforce retrospective replay gate
        if eval_session_date <= "2026-11-16":
            print(f"\n[FATAL RETROSPECTIVE REPLAY] Session date '{eval_session_date}' has already been evaluated historically.")
            print("Phase 6.4 baseline ends on 2026-11-16. Genuine forward evaluation strictly requires fresh forward sessions (> 2026-11-16).\n")
            sys.exit(1)

        print(f"Evaluating Genuine Forward Session: {eval_session_date} (Mode: DAILY)")
        try:
            summary = service.run_forward_session(
                session_date=eval_session_date,
                proxy_candles_map=proxy_map,
                stock_candles_map=stock_map,
                data_mode=ForwardDataMode.DAILY.value,
                force_idempotent_overwrite=False,
            )
        except DuplicateObservationError as doe:
            print(f"\n[FATAL DUPLICATE SESSION] {doe}\n")
            sys.exit(1)
        except Exception as e:
            print(f"\n[FATAL EVALUATION FAILURE] {e}\n")
            sys.exit(1)
    else:
        print(f"\n[FATAL ERROR] Mode '{args.mode}' is not valid for session evaluation. Use --mode DAILY or FIXTURE.\n")
        sys.exit(1)

    print("\n--- Session Evaluation Summary ---")
    print(f"  Session Date         : {summary.session_date}")
    print(f"  Data Mode            : {summary.data_mode}")
    print(f"  Market Regime        : {summary.market_regime}")
    print(f"  Tickers Screened     : {summary.tickers_screened}")
    print(f"  Eligible Candidates  : {summary.eligible_candidates}")
    print(f"  Rejected Candidates  : {summary.rejected_candidates}")
    print(f"  Proposals Created    : {summary.proposals_created}")
    print(f"  Trades Opened        : {summary.hypothetical_trades_opened}")
    print(f"  Trades Closed Today  : {summary.hypothetical_trades_closed}")
    print(f"  Active Positions     : {summary.active_positions_count}")
    print(f"  Virtual Cash         : ${summary.virtual_cash:,.2f}")
    print(f"  Virtual Total Equity : ${summary.virtual_equity:,.2f}")
    print(f"  Realized Net P&L     : ${summary.realized_net_pnl:,.2f}")
    print(f"  Unrealized P&L       : ${summary.unrealized_pnl:,.2f}")
    print("==================================================================")


if __name__ == "__main__":
    main()
