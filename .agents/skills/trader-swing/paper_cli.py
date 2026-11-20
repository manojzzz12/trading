"""Command-line interface for Phase 6 manual paper trading in trader-swing.

Enforces:
1. Rejection of forbidden flags (--live, --broker, --credential, --key, --secret,
   --webhook, --url, --auto, --automated, --poll, --watch, --daemon, --stream,
   --real, --alpaca, --ibkr).
2. Mandatory requirement for --paper-only flag on all state-mutating commands.
3. On-demand manual execution only (zero scheduling, polling, or daemon threads).
4. Full validation of manual TradingView paper fills and exits.
"""

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
import sys
from typing import List, Optional

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from paper_models import PaperExitReason
from authorization_models import AuthorizationError
from paper_service import (
    MarketSessionValidationError,
    PaperSafetyViolation,
    PaperTradingService,
    RiskViolationError,
)
from paper_storage import DEFAULT_PAPER_STATE_DIR, PaperStorageManager, PaperStorageSafetyError, _REPO_ROOT

if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


FORBIDDEN_CLI_FLAGS = {
    "--live",
    "--broker",
    "--credential",
    "--key",
    "--secret",
    "--webhook",
    "--url",
    "--auto",
    "--automated",
    "--poll",
    "--watch",
    "--daemon",
    "--stream",
    "--real",
    "--alpaca",
    "--ibkr",
}


def scan_for_forbidden_flags(args: List[str]) -> None:
    """Prohibit any flag hinting at live execution, credentials, or automation."""
    for arg in args:
        arg_lower = arg.lower()
        for forbidden in FORBIDDEN_CLI_FLAGS:
            if arg_lower == forbidden or arg_lower.startswith(f"{forbidden}="):
                print(
                    f"\n[FATAL SAFETY VIOLATION] Prohibited argument '{arg}' detected.\n"
                    "Automated polling, broker connectors, credentials, and live execution "
                    "are strictly forbidden in Phase 6 paper trading.\n",
                    file=sys.stderr,
                )
                sys.exit(1)


def build_parser() -> argparse.ArgumentParser:
    """Build command line argument parser for manual paper operations."""
    parser = argparse.ArgumentParser(
        prog="trader-swing-paper",
        description="Phase 6 Manual Paper Trading CLI (Autonomous Swing Strategy)",
    )
    parser.add_argument(
        "--state-dir",
        default=DEFAULT_PAPER_STATE_DIR,
        help="External directory for paper state (default: $TRADING_PAPER_STATE_DIR or user Documents/TradingPaperState)",
    )

    subparsers = parser.add_subparsers(dest="subcommand", help="Available subcommands")

    # 1. init
    p_init = subparsers.add_parser("init", help="Initialize paper trading state with $5,000.00")
    p_init.add_argument("--capital", default="5000.00", help="Initial capital in USD (default: 5000.00)")
    p_init.add_argument("--force", action="store_true", help="Force re-initialization if already initialized")
    p_init.add_argument("--paper-only", action="store_true", help="Explicit confirmation of paper trading mode")

    # 2. status
    p_status = subparsers.add_parser("status", help="Inspect current paper portfolio, equity, and open lots")

    # 3. record-entry
    p_entry = subparsers.add_parser("record-entry", help="Record user-confirmed TradingView manual paper entry fill")
    p_entry.add_argument("--ticker", required=True, help="US equity ticker symbol (e.g. AAPL)")
    p_entry.add_argument("--shares", required=True, type=int, help="Number of shares filled")
    p_entry.add_argument("--price", required=True, help="Fill price in USD")
    p_entry.add_argument("--stop", required=True, help="Protective stop-loss price in USD (must be < entry)")
    p_entry.add_argument("--target", default=None, help="Optional take-profit target price in USD")
    p_entry.add_argument("--fee", default="0.00", help="Entry transaction fee in USD (default: 0.00)")
    p_entry.add_argument("--timestamp", required=True, help="Execution timestamp (ISO-8601 or YYYY-MM-DD HH:MM:SS)")
    p_entry.add_argument("--max-backdating-hours", type=float, default=24.0, help="Max allowed backdating in hours (default: 24.0)")
    p_entry.add_argument("--allow-stale", action="store_true", help="Documented exception to permit older historical fills")
    p_entry.add_argument("--notes", default="", help="Optional notes (e.g. TradingView paper order ID)")
    p_entry.add_argument("--paper-only", action="store_true", help="Explicit confirmation of paper trading mode")

    # 4. record-exit
    p_exit = subparsers.add_parser("record-exit", help="Record user-confirmed TradingView manual paper exit")
    p_exit.add_argument("--ticker", required=True, help="US equity ticker symbol")
    p_exit.add_argument("--shares", default=None, type=int, help="Shares closed (defaults to all open shares in lot)")
    p_exit.add_argument("--price", required=True, help="Exit price in USD (0.00 for delisting zero-recovery)")
    p_exit.add_argument("--fee", default="0.00", help="Exit transaction fee in USD (must be 0.00 for zero-recovery)")
    p_exit.add_argument("--timestamp", required=True, help="Execution timestamp (ISO-8601)")
    p_exit.add_argument(
        "--reason",
        default="MANUAL",
        choices=[r.value for r in PaperExitReason],
        help="Reason for exit (default: MANUAL)",
    )
    p_exit.add_argument("--lot-id", default=None, help="Specific lot ID to close (required if multiple open lots)")
    p_exit.add_argument("--max-backdating-hours", type=float, default=24.0, help="Max allowed backdating in hours (default: 24.0)")
    p_exit.add_argument("--allow-stale", action="store_true", help="Documented exception to permit older historical exits")
    p_exit.add_argument("--notes", default="", help="Optional exit notes")
    p_exit.add_argument("--paper-only", action="store_true", help="Explicit confirmation of paper trading mode")

    # 5. audit
    p_audit = subparsers.add_parser("audit", help="Inspect recent immutable audit events")
    p_audit.add_argument("--limit", type=int, default=20, help="Number of recent events to display (default: 20)")

    # 6. observe
    p_observe = subparsers.add_parser(
        "observe",
        help="Run on-demand manual research scan and generate forward observation records (read-only)",
    )
    p_observe.add_argument(
        "--current",
        action="store_true",
        help="Observe current daily market data from isolated directory (.planning/data/current_daily)",
    )
    p_observe.add_argument(
        "--data-dir",
        default=os.path.join(_REPO_ROOT, ".planning", "data", "historical_partitions", "2021_2023_primary_corrected"),
        help="Directory containing daily CSV candle files",
    )
    p_observe.add_argument(
        "--as-of-date",
        default=None,
        help="Trading session date to scan (YYYY-MM-DD, defaults to latest available session)",
    )
    p_observe.add_argument(
        "--tickers",
        default=None,
        help="Optional comma-separated list of tickers to scan (defaults to all available in data-dir)",
    )
    p_observe.add_argument(
        "--top-n",
        type=int,
        default=5,
        help="Number of top candidates to highlight (default: 5)",
    )
    p_observe.add_argument(
        "--record-file",
        default=None,
        help="Path to append observation records (default: .planning/data/observations/observation_log.jsonl)",
    )
    p_observe.add_argument(
        "--no-record",
        action="store_true",
        help="Do not write observation records to disk (display report only)",
    )

    # 7. acquire-current
    p_acq_current = subparsers.add_parser(
        "acquire-current",
        help="Acquire current daily market data into isolated directory (.planning/data/current_daily)",
    )
    p_acq_current.add_argument(
        "--output-dir",
        default=os.path.join(_REPO_ROOT, ".planning", "data", "current_daily"),
        help="Target directory for current daily data",
    )
    p_acq_current.add_argument(
        "--staging-dir",
        default=os.path.join(_REPO_ROOT, ".planning", "data", "current_daily", ".staging"),
        help="Staging directory for downloads",
    )
    p_acq_current.add_argument(
        "--tickers",
        default=None,
        help="Optional comma-separated list of candidate tickers (defaults to curated universe)",
    )
    p_acq_current.add_argument(
        "--lookback-days",
        type=int,
        default=400,
        help="Lookback window in calendar days (default: 400)",
    )
    p_acq_current.add_argument(
        "--pacing-delay",
        type=float,
        default=2.5,
        help="Minimum pacing interval in seconds between requests (default: 2.5s, min 2.5s)",
    )

    # 8. ticket (Phase 8.1)
    p_ticket = subparsers.add_parser("ticket", help="View human authorization ticket for a paper order")
    p_ticket.add_argument("--order-id", required=True, help="Deterministic order ID to render ticket for")

    # 9. authorize (Phase 8.1)
    p_auth = subparsers.add_parser("authorize", help="Execute 3-gate authorization for a proposed paper order")
    p_auth.add_argument("--order-id", required=True, help="Deterministic order ID to authorize")
    p_auth.add_argument("--confirm", default=None, help="Explicit confirmation response (YES / CONFIRM)")
    p_auth.add_argument("--paper-only", action="store_true", help="Explicit confirmation of paper trading mode")

    return parser


def format_status_output(status: dict) -> str:
    """Format paper trading status dictionary into a readable report."""
    lines = [
        "==================================================================",
        "          TRADER-SWING: PAPER TRADING STATUS (PHASE 6.1)          ",
        "==================================================================",
        f"  Total Portfolio Equity : ${status['total_equity_usd']} USD",
        f"  Available Cash Balance : ${status['cash_usd']} USD ({status['cash_reserve_pct']}% of equity)",
        f"  Total Market Value     : ${status['total_market_value_usd']} USD ({status['invested_capital_pct']}% of equity)",
        f"  Realized P&L to Date   : ${status['realized_pnl_usd']} USD",
        f"  Total Fees Incurred    : ${status['total_fees_usd']} USD",
        f"  Open Positions Count   : {status['open_positions_count']} / {status['max_simultaneous_positions_limit']} max",
        f"  Active Tickers         : {', '.join(status['active_tickers']) if status['active_tickers'] else 'None'}",
        f"  Accounting Invariant   : {'HOLDS (PASSED)' if status['equity_invariant_holds'] else 'VIOLATED (CORRUPT)'}",
        "------------------------------------------------------------------",
        "  ACTIVE LOTS & STOPS:",
    ]
    if not status["active_lots"]:
        lines.append("    (No open paper positions held)")
    else:
        for lot in status["active_lots"]:
            lines.append(
                f"    - [{lot['lot_id']}] {lot['ticker']} | {lot['shares']} shares @ ${lot['entry_price']} | "
                f"Stop: ${lot['stop_loss']} | Target: ${lot['target']} | Basis: ${lot['cost_basis']}"
            )
    lines.append("==================================================================")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    """CLI execution entrypoint."""
    args_list = argv if argv is not None else sys.argv[1:]
    scan_for_forbidden_flags(args_list)

    parser = build_parser()
    if not args_list:
        parser.print_help()
        return 0

    parsed = parser.parse_args(args_list)

    try:
        storage = PaperStorageManager(state_dir=parsed.state_dir)
        service = PaperTradingService(storage=storage)

        if parsed.subcommand == "init":
            service.assert_paper_only(parsed.paper_only)
            capital = Decimal(str(parsed.capital))
            storage.initialize_state(initial_capital=capital, force=parsed.force)
            print(f"[SUCCESS] Initialized paper trading state at: {storage.state_dir}")
            print(f"Initial Capital: ${capital} USD")
            return 0

        elif parsed.subcommand == "status":
            status = service.get_status()
            print(format_status_output(status))
            return 0

        elif parsed.subcommand == "record-entry":
            res = service.record_paper_entry(
                ticker=parsed.ticker,
                shares=parsed.shares,
                price=parsed.price,
                stop_loss=parsed.stop,
                timestamp=parsed.timestamp,
                fee=parsed.fee,
                target=parsed.target,
                order_notes=parsed.notes,
                paper_only=parsed.paper_only,
                max_backdating_window=timedelta(hours=parsed.max_backdating_hours),
                allow_stale=parsed.allow_stale,
            )
            print("\n[SUCCESS] Recorded TradingView Paper Entry Fill:")
            for k, v in res.items():
                print(f"  {k:15}: {v}")
            return 0

        elif parsed.subcommand == "record-exit":
            res = service.record_paper_exit(
                ticker=parsed.ticker,
                shares=parsed.shares,
                price=parsed.price,
                timestamp=parsed.timestamp,
                fee=parsed.fee,
                reason=parsed.reason,
                lot_id=parsed.lot_id,
                order_notes=parsed.notes,
                paper_only=parsed.paper_only,
                max_backdating_window=timedelta(hours=parsed.max_backdating_hours),
                allow_stale=parsed.allow_stale,
            )
            print("\n[SUCCESS] Recorded TradingView Paper Exit:")
            for k, v in res.items():
                print(f"  {k:15}: {v}")
            return 0

        elif parsed.subcommand == "audit":
            events = storage.load_audit_events(limit=parsed.limit)
            print(f"\n================ AUDIT EVENT LOG (Last {len(events)}) ================")
            for evt in events:
                print(f"[{evt['timestamp']}] {evt['event_type']} - Ticker: {evt.get('ticker')} | "
                      f"Cash: ${evt['cash_after']} | Equity: ${evt['equity_after']}")
            return 0

        elif parsed.subcommand == "observe":
            from observation_workflow import run_observation_cli
            return run_observation_cli(parsed)

        elif parsed.subcommand == "acquire-current":
            from scripts.acquire_current_daily import CurrentDailyAcquirer
            tickers_list = [t.strip().upper() for t in parsed.tickers.split(",") if t.strip()] if parsed.tickers else None
            acquirer = CurrentDailyAcquirer(
                output_dir=parsed.output_dir,
                staging_dir=parsed.staging_dir,
                lookback_days=parsed.lookback_days,
                candidate_tickers=tickers_list,
                pacing_delay=parsed.pacing_delay,
            )
            manifest = acquirer.acquire_current_dataset()
            print("\n==================================================================")
            print("     CURRENT DAILY MARKET DATA ACQUISITION SUCCESSFUL             ")
            print("==================================================================")
            print(f"  Target Destination : {parsed.output_dir}")
            print(f"  Session Date       : {manifest['as_of_session_date']}")
            print(f"  Total Validated    : {manifest['total_active_tickers']} tickers")
            print(f"  Proxies Acquired   : {', '.join(manifest['proxies'])}")
            print(f"  Equities Acquired  : {len(manifest['included_equities'])} tickers")
            print(f"  Equities Excluded  : {len(manifest['excluded_equities'])} tickers")
            print(f"  Data Delay Status  : {manifest['data_classification']} (Delayed)")
            print("==================================================================")
            return 0

        elif parsed.subcommand == "ticket":
            service = PaperTradingService(storage=storage)
            ticket_str = service.render_authorization_ticket(parsed.order_id)
            print(ticket_str)
            return 0

        elif parsed.subcommand == "authorize":
            service = PaperTradingService(storage=storage)
            service.assert_paper_only(parsed.paper_only)
            ticket_str = service.render_authorization_ticket(parsed.order_id)
            print(ticket_str)
            user_input = parsed.confirm
            if not user_input:
                user_input = input("Enter 'YES' to confirm and authorize this exact order: ")
            rec = service.authorize_order(parsed.order_id, user_confirmation=user_input)
            print(f"\n[SUCCESS] Order '{parsed.order_id}' successfully AUTHORIZED across all 3 gates!")
            print(f"  Authorization State : {rec.state.value}")
            print(f"  HMAC Token ID       : {rec.token.token_id if rec.token else 'N/A'}")
            return 0

        else:
            parser.print_help()
            return 0

    except (PaperSafetyViolation, PaperStorageSafetyError) as err:
        print(f"\n[SAFETY ERROR] {err}", file=sys.stderr)
        return 1
    except (MarketSessionValidationError, RiskViolationError, AuthorizationError) as err:
        print(f"\n[VALIDATION / RISK REJECTION] {err}", file=sys.stderr)
        return 1
    except Exception as err:
        print(f"\n[ERROR] Operation failed: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
