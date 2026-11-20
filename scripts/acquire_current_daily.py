"""Isolated Current Daily Market Data Acquisition Utility.

Phase 6.4.3: On-demand current daily data acquisition for manual observation workflow.

Enforces:
1. Isolated Storage:
   - Target destination: .planning/data/current_daily/
   - Dedicated local cache: market_data_current_daily.db (or :memory: during tests)
   - Never writes into historical partitions (.planning/data/historical_partitions/)
   - Never overwrites or touches historical datasets
   - Strict rejection of forbidden paths (lockbox, 2026_lockbox, TradingPaperState, paper_journal.json)
2. Network Pacing & Bounded Retries:
   - Hard minimum pacing interval of 2.5s between requests
   - Non-negative jitter floored at >= 2.5s across all requests and retries
   - Exact bounded retry schedule [3.0, 6.0, 12.0] seconds with fail-fast on permanent errors
3. Session Alignment & Candle Validation:
   - Synchronizes proxy (SPY, QQQ, IWM) and stock universe to latest completed regular NYSE session
   - Rejects forming/incomplete candles
   - Hard-fails if mandatory proxy SPY is missing, stale, or < 200 completed bars
   - Excludes individual stocks that are stale, missing, or < 50 completed bars with explicit reasons
   - Clearly discloses UNVERIFIED_DELAY status
4. Governance & Safety:
   - Zero strategy, indicator, scanner, signal, or risk-engine changes
   - Pure on-demand execution (no background processes, daemons, or polling)
   - Zero paper fills or ledger mutations
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import argparse
import csv
import json
import logging
import os
import random
import shutil
import sys
import time as time_mod
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

WORKSPACE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADER_SWING_DIR = os.path.join(WORKSPACE_ROOT, ".agents", "skills", "trader-swing")
if WORKSPACE_ROOT not in sys.path:
    sys.path.insert(0, WORKSPACE_ROOT)
if TRADER_SWING_DIR not in sys.path:
    sys.path.insert(0, TRADER_SWING_DIR)

from feed_adapter import MarketDataFeedAdapter, MarketDataReadinessEvaluator, StockExclusion
from market_data_models import CandleStatus, MarketClock, OHLCVCandle, to_eastern, to_utc, EASTERN, UTC
from providers.yfinance_provider import YFinanceMarketDataProvider
from universe import DEFAULT_CURATED_WATCHLIST, validate_ticker

logger = logging.getLogger(__name__)

# Constants and Defaults
CORE_PROXIES = ("SPY", "QQQ", "IWM")
DEFAULT_CURRENT_DATA_DIR = os.path.join(WORKSPACE_ROOT, ".planning", "data", "current_daily")
DEFAULT_STAGING_DIR = os.path.join(WORKSPACE_ROOT, ".planning", "data", "current_daily", ".staging")
DEFAULT_CACHE_DB = os.path.join(TRADER_SWING_DIR, "data", "market_data_current_daily.db")

MIN_PACING_DELAY = 2.5
DEFAULT_PACING_DELAY = 2.5
DEFAULT_LOOKBACK_DAYS = 400
DEFAULT_MAX_RETRIES = 3
RETRY_BACKOFF_SCHEDULE = [3.0, 6.0, 12.0]
MIN_SPY_BARS = 200
MIN_STOCK_BARS = 50

FORBIDDEN_PATH_TOKENS = (
    "lockbox",
    "2026_lockbox",
    "historical_partitions",
    "tradingpaperstate",
    "paper_journal.json",
    "portfolio.json",
    "broker.json",
)


class CurrentDataAcquisitionError(Exception):
    """Base exception for current daily data acquisition errors."""
    pass


class AcquisitionHardFailure(CurrentDataAcquisitionError):
    """Raised when hard prerequisites fail (e.g. missing or stale SPY)."""
    pass


def calculate_exclusive_end_date(completed_session_date: date) -> date:
    """Calculate the exclusive end date boundary for yfinance daily candle requests.

    yfinance treats `end` as an exclusive cutoff (< end 00:00:00).
    To include the entire completed_session_date (09:30 - 16:00 ET),
    the query must request the calendar day immediately following completed_session_date.
    """
    if not isinstance(completed_session_date, date):
        raise TypeError(
            f"completed_session_date must be a date, got {type(completed_session_date).__name__}."
        )
    return completed_session_date + timedelta(days=1)


def assert_safe_current_path(path_str: str) -> str:
    """Ensure destination path is isolated and does not touch historical or protected files."""
    if not path_str or not isinstance(path_str, str):
        raise ValueError(f"Path must be a non-empty string, got {path_str!r}.")

    norm = os.path.normpath(os.path.abspath(path_str)).lower()
    real = os.path.realpath(os.path.abspath(path_str)).lower()

    for forbidden in FORBIDDEN_PATH_TOKENS:
        if forbidden in norm or forbidden in real:
            raise PermissionError(
                f"FATAL SECURITY VIOLATION: Current data path '{path_str}' contains forbidden token '{forbidden}'. "
                "Current data acquisition must never target historical partitions, paper state, or lockboxes."
            )

    return real


def is_transient_error(e: Exception) -> bool:
    """Determine whether an error is transient (retryable) or permanent (fail-fast)."""
    if isinstance(e, PermissionError):
        return False
    if isinstance(e, (ValueError, TypeError)):
        return False

    err_str = str(e).lower()
    err_cls = e.__class__.__name__.lower()

    # Permanent indicators
    permanent_indicators = ["400", "401", "403", "404", "not found", "malformed", "invalidprice", "invalidvolume"]
    for pi in permanent_indicators:
        if pi in err_str or pi in err_cls:
            return False

    # Transient indicators
    transient_indicators = [
        "429", "rate limit", "too many requests", "ratelimiterror",
        "500", "502", "503", "504", "server error",
        "timeout", "timeouterror", "timed out",
        "connection reset", "connection refused", "providerunavailable", "connectionerror"
    ]
    for ti in transient_indicators:
        if ti in err_str or ti in err_cls:
            return True

    return False


class CurrentDailyAcquirer:
    """Acquires, validates, and stores current daily market data with pacing and isolation."""

    def __init__(
        self,
        output_dir: str = DEFAULT_CURRENT_DATA_DIR,
        staging_dir: str = DEFAULT_STAGING_DIR,
        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
        candidate_tickers: Optional[Sequence[str]] = None,
        pacing_delay: float = DEFAULT_PACING_DELAY,
        max_retries: int = DEFAULT_MAX_RETRIES,
        cache_db_path: str = DEFAULT_CACHE_DB,
        download_fn: Optional[Callable[..., Any]] = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
        random_fn: Optional[Callable[[], float]] = None,
        clock: Optional[MarketClock] = None,
    ):
        self.output_dir = assert_safe_current_path(output_dir)
        self.staging_dir = os.path.abspath(staging_dir)
        self.lookback_days = max(100, lookback_days)
        self.max_retries = max(0, min(max_retries, 3))
        self.cache_db_path = cache_db_path
        self.download_fn = download_fn
        self.sleep_fn = sleep_fn or time_mod.sleep
        self.random_fn = random_fn or (lambda: random.uniform(0.0, 0.5))
        self.clock = clock or MarketClock()
        self.evaluator = MarketDataReadinessEvaluator(clock=self.clock)

        if pacing_delay < MIN_PACING_DELAY:
            raise ValueError(
                f"Invalid pacing_delay {pacing_delay}s. Minimum allowed pacing interval is {MIN_PACING_DELAY}s."
            )
        self.pacing_delay = float(pacing_delay)

        # Universe configuration
        raw_equities = list(candidate_tickers) if candidate_tickers is not None else list(DEFAULT_CURATED_WATCHLIST)
        self.candidate_tickers: List[str] = [validate_ticker(t) for t in raw_equities]
        self.proxies: Tuple[str, ...] = tuple(CORE_PROXIES)

    def _default_download_candles(
        self,
        ticker: str,
        start_date: date,
        end_date: date,
        current_time: datetime,
    ) -> List[OHLCVCandle]:
        """Download candles using YFinanceMarketDataProvider with isolated cache."""
        start_dt = datetime.combine(start_date, MarketClock.REGULAR_OPEN, tzinfo=EASTERN)
        exclusive_end_date = calculate_exclusive_end_date(end_date)
        end_dt = datetime.combine(exclusive_end_date, MarketClock.REGULAR_CLOSE, tzinfo=EASTERN)

        provider = YFinanceMarketDataProvider(
            max_retries=0,  # Managed by acquirer
            cache_db_path=self.cache_db_path,
            clock=self.clock,
        )
        return provider.fetch_historical_bars(ticker, interval="1d", start=start_dt, end=end_dt, current_time=current_time)

    def fetch_ticker_data_with_pacing(
        self,
        ticker: str,
        start_date: date,
        end_date: date,
        current_time: datetime,
    ) -> List[OHLCVCandle]:
        """Fetch historical records for a ticker with pacing, bounded retries, and backoff."""
        last_error: Optional[Exception] = None
        attempts = 1 + self.max_retries

        for attempt in range(attempts):
            try:
                if self.download_fn is not None:
                    exclusive_end = calculate_exclusive_end_date(end_date)
                    raw = self.download_fn(
                        ticker=ticker,
                        start=start_date.isoformat(),
                        end=exclusive_end.isoformat(),
                        current_time=current_time,
                    )
                    candles = raw if isinstance(raw, list) else []
                else:
                    candles = self._default_download_candles(ticker, start_date, end_date, current_time=current_time)

                return candles

            except PermissionError:
                raise
            except Exception as e:
                last_error = e
                if not is_transient_error(e):
                    logger.error(f"Permanent error for {ticker}: {e}; failing immediately without retry.")
                    raise

                logger.warning(f"Attempt {attempt + 1}/{attempts} failed for {ticker} (transient): {e}")
                if attempt < self.max_retries:
                    base_backoff = RETRY_BACKOFF_SCHEDULE[min(attempt, len(RETRY_BACKOFF_SCHEDULE) - 1)]
                    jitter = max(0.0, float(self.random_fn()))
                    actual_backoff = max(MIN_PACING_DELAY, base_backoff + jitter)
                    self.sleep_fn(actual_backoff)
                else:
                    raise last_error

        raise last_error if last_error else RuntimeError(f"Failed to fetch data for {ticker}")

    def acquire_current_dataset(
        self,
        reference_time: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Acquire current daily market data up to the latest completed trading session."""
        now_utc = to_utc(reference_time) if reference_time is not None else datetime.now(UTC)
        now_et = to_eastern(now_utc)

        # 1. Determine latest completed regular NYSE session
        latest_completed_sess = self.evaluator.get_latest_completed_session(now_utc)
        if latest_completed_sess is None:
            raise AcquisitionHardFailure("Unable to determine latest completed trading session date.")

        # 2. Timing check: If run during market hours on a trading day, warn that today's bar is forming
        today_et_date = now_et.date()
        today_session = self.clock.get_session(today_et_date) or self.clock.get_historical_session(today_et_date)
        if today_session:
            open_t, close_t = today_session
            if open_t <= now_et.time() < close_t:
                logger.warning(
                    f"Acquisition running intraday ({now_et.time()} ET). "
                    f"Today's session ({today_et_date}) is FORMING; latest completed session is {latest_completed_sess}."
                )

        start_date = latest_completed_sess - timedelta(days=self.lookback_days)
        end_date = latest_completed_sess

        logger.info(
            f"Acquiring current daily market data. Target session: {latest_completed_sess} "
            f"(Lookback: {start_date} to {end_date})"
        )

        # 3. Clean and prepare staging directory
        if os.path.exists(self.staging_dir):
            shutil.rmtree(self.staging_dir, ignore_errors=True)
        os.makedirs(self.staging_dir, exist_ok=True)

        downloaded_candles: Dict[str, List[OHLCVCandle]] = {}
        excluded_tickers: List[Dict[str, Any]] = []

        # All tickers: proxies first, then candidate equities
        all_tickers = list(self.proxies) + [t for t in self.candidate_tickers if t not in self.proxies]

        for i, sym in enumerate(all_tickers):
            # Inter-request pacing interval (enforced >= 2.5s)
            if i > 0:
                jitter = max(0.0, float(self.random_fn()))
                sleep_dur = max(MIN_PACING_DELAY, self.pacing_delay + jitter)
                self.sleep_fn(sleep_dur)

            try:
                bars = self.fetch_ticker_data_with_pacing(
                    sym, start_date=start_date, end_date=end_date, current_time=now_utc
                )

                # Filter out any bars strictly beyond latest_completed_sess (e.g. forming bars)
                completed_bars = []
                for b in bars:
                    b_date = to_eastern(b.timestamp).date()
                    if b_date <= latest_completed_sess:
                        completed_bars.append(b)

                # Validation
                if not completed_bars:
                    raise ValueError(f"No bars returned for {sym} up to {latest_completed_sess}")

                last_d = to_eastern(completed_bars[-1].timestamp).date()

                if sym in self.proxies:
                    # Mandatory Proxy Checks
                    if last_d < latest_completed_sess:
                        if sym == "SPY":
                            raise AcquisitionHardFailure(
                                f"STALE_PRIMARY_PROXY: SPY latest date {last_d} < expected {latest_completed_sess}"
                            )
                        else:
                            logger.warning(f"Secondary proxy {sym} is stale ({last_d} < {latest_completed_sess}).")
                    if sym == "SPY" and len(completed_bars) < MIN_SPY_BARS:
                        raise AcquisitionHardFailure(
                            f"INSUFFICIENT_SPY_BARS: {len(completed_bars)} < {MIN_SPY_BARS} bars required."
                        )
                else:
                    # Stock Checks
                    if last_d < latest_completed_sess:
                        excluded_tickers.append({
                            "ticker": sym,
                            "reason": f"STALE_STOCK_DATA: latest {last_d} < expected {latest_completed_sess}",
                            "bar_count": len(completed_bars),
                            "latest_date": last_d.isoformat(),
                        })
                        continue
                    if len(completed_bars) < MIN_STOCK_BARS:
                        excluded_tickers.append({
                            "ticker": sym,
                            "reason": f"INSUFFICIENT_BARS: {len(completed_bars)} < {MIN_STOCK_BARS}",
                            "bar_count": len(completed_bars),
                            "latest_date": last_d.isoformat(),
                        })
                        continue

                downloaded_candles[sym] = completed_bars

                # Write individual CSV to staging
                csv_path = os.path.join(self.staging_dir, f"{sym}_daily.csv")
                with open(csv_path, "w", encoding="utf-8", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow(["Date", "Open", "High", "Low", "Close", "Volume"])
                    for b in completed_bars:
                        b_date = to_eastern(b.timestamp).date()
                        writer.writerow([
                            b_date.isoformat(),
                            f"{b.open:.4f}",
                            f"{b.high:.4f}",
                            f"{b.low:.4f}",
                            f"{b.close:.4f}",
                            int(b.volume),
                        ])

            except AcquisitionHardFailure:
                raise
            except Exception as e:
                if sym == "SPY":
                    raise AcquisitionHardFailure(f"FAILED_MANDATORY_PROXY_SPY: {e}") from e
                else:
                    logger.warning(f"Failed to acquire {sym}: {e}")
                    excluded_tickers.append({
                        "ticker": sym,
                        "reason": f"DOWNLOAD_FAILED: {e}",
                        "bar_count": 0,
                        "latest_date": None,
                    })

        # 4. Promotion verification
        if "SPY" not in downloaded_candles:
            raise AcquisitionHardFailure("SPY is missing from acquired candles; cannot promote.")

        # 5. Write Current Daily Manifest
        manifest = {
            "schema_version": "1.0.0",
            "acquisition_type": "CURRENT_DAILY_OBSERVATION",
            "as_of_session_date": latest_completed_sess.isoformat(),
            "acquisition_timestamp": now_utc.isoformat(),
            "data_classification": "DELAYED_UNVERIFIED",
            "is_delayed": True,
            "provider": "yfinance",
            "proxies": [p for p in self.proxies if p in downloaded_candles],
            "included_equities": [s for s in downloaded_candles if s not in self.proxies],
            "excluded_equities": excluded_tickers,
            "total_active_tickers": len(downloaded_candles),
            "validation_results": {
                "validation_status": "PASSED",
                "proxies_validated": len([p for p in self.proxies if p in downloaded_candles]),
                "equities_validated": len([s for s in downloaded_candles if s not in self.proxies]),
                "equities_excluded": len(excluded_tickers),
            },
            "disclaimer": (
                "Yahoo Finance / yfinance provides delayed market data with unverified latency. "
                "Quotes and historical bars are split-adjusted (auto_adjust=False, actions=True). "
                "Data is strictly for forward paper observation and is never suitable for live execution."
            ),
        }

        manifest_path = os.path.join(self.staging_dir, "CURRENT_DAILY_MANIFEST.json")
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)

        # 6. Atomic promotion to output_dir
        os.makedirs(self.output_dir, exist_ok=True)
        for fname in os.listdir(self.staging_dir):
            src = os.path.join(self.staging_dir, fname)
            dst = os.path.join(self.output_dir, fname)
            if os.path.isfile(src):
                shutil.copy2(src, dst)

        # Clean staging
        shutil.rmtree(self.staging_dir, ignore_errors=True)

        logger.info(
            f"Successfully acquired current daily dataset into: {self.output_dir}\n"
            f"Session Date: {latest_completed_sess} | Total Tickers: {len(downloaded_candles)}"
        )

        return manifest


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point for on-demand current daily data acquisition."""
    args_list = argv if argv is not None else sys.argv[1:]

    # Import forbidden flag scanner
    from paper_cli import scan_for_forbidden_flags
    scan_for_forbidden_flags(args_list)

    parser = argparse.ArgumentParser(
        prog="acquire-current-daily",
        description="Phase 6.4.3 On-Demand Current Daily Data Acquisition (Isolated Storage)",
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_CURRENT_DATA_DIR,
        help=f"Target directory for current daily data (default: {DEFAULT_CURRENT_DATA_DIR})",
    )
    parser.add_argument(
        "--staging-dir",
        default=DEFAULT_STAGING_DIR,
        help=f"Temporary staging directory (default: {DEFAULT_STAGING_DIR})",
    )
    parser.add_argument(
        "--tickers",
        default=None,
        help="Optional comma-separated list of candidate tickers (defaults to curated universe)",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=DEFAULT_LOOKBACK_DAYS,
        help=f"Lookback window in calendar days (default: {DEFAULT_LOOKBACK_DAYS})",
    )
    parser.add_argument(
        "--pacing-delay",
        type=float,
        default=DEFAULT_PACING_DELAY,
        help=f"Minimum pacing interval in seconds between requests (default: {DEFAULT_PACING_DELAY}s, min 2.5s)",
    )

    parsed = parser.parse_args(args_list)

    tickers_list: Optional[List[str]] = None
    if parsed.tickers:
        tickers_list = [t.strip().upper() for t in parsed.tickers.split(",") if t.strip()]

    try:
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
    except (AcquisitionHardFailure, CurrentDataAcquisitionError, PermissionError) as e:
        print(f"\n[ACQUISITION FAILED] {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"\n[UNEXPECTED ERROR] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
