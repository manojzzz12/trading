"""Historical Partition Data Acquisition Utility.

Phase 6.2C — Step 3.4.3: Targeted Safety Remediation

Enforces:
1. Hardcoded MAX_ALLOWED_DATE ceiling = date(2023, 12, 31).
   - Validates requested start_date and end_date BEFORE making any network call.
   - Validates all returned dates AFTER parsing response.
   - Strictly blocks access to 2024-2026 data and 2026 lockbox.
2. Mandatory Core Proxies (DEFECT-01 Remediation):
   - SPY, QQQ, and IWM are unconditionally mandatory for primary partition acquisition.
   - Removal of all bypass mechanisms (--skip-proxies, include_proxies flag).
   - Complete 100% proxy verification required for promotion.
3. Network Pacing & Enforced Floor (DEFECT-02 Remediation):
   - Enforces a hard minimum pacing interval of 2.5s between every request.
   - Exact bounded retry schedule [3.0, 6.0, 12.0] seconds.
   - Non-negative jitter strictly floored at >= 2.5s across all requests and retries.
   - Provider max_retries=0 prevents compounding retry attempts.
   - Retries restricted to transient errors; permanent errors fail fast immediately.
4. Constrained Staging Cleanup (DEFECT-03 Remediation):
   - Canonicalizes and validates staging path via validate_safe_staging_dir().
   - Enforces containment strictly within .planning/data/historical_partitions/.
   - Enforces directory name strictly starting with '.staging_'.
   - Rejects symlinks, junctions, path traversal, unexpected locations, and forbidden tokens.
   - Fails closed before any invocation of shutil.rmtree().
5. Non-Destructive Overwrite Rejection:
   - If destination directory exists, raises FileExistsError and preserves destination byte-for-byte.
   - Preserves staging data and diagnostic evidence on failed promotion.
6. Safe Atomic Promotion:
   - Verifies staging and destination parent reside on the same filesystem before promotion.
   - Rejects cross-filesystem promotion safely without touching destination.
   - Uses atomic os.replace() directory promotion on verified same filesystem.
   - Zero production cache writes (uses :memory: for provider).
"""

from datetime import date, datetime, timezone
from decimal import Decimal
import argparse
import csv
import json
import logging
import os
import random
import sys
import time as time_mod
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

# Ensure workspace and trader-swing modules are accessible
WORKSPACE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADER_SWING_DIR = os.path.join(WORKSPACE_ROOT, ".agents", "skills", "trader-swing")
if WORKSPACE_ROOT not in sys.path:
    sys.path.insert(0, WORKSPACE_ROOT)
if TRADER_SWING_DIR not in sys.path:
    sys.path.insert(0, TRADER_SWING_DIR)

from market_data_models import MarketClock, to_eastern
from universe import DEFAULT_CURATED_WATCHLIST, validate_ticker

# Import verification engine
try:
    from scripts.verify_historical_partitions import PartitionVerifier, CORE_PROXIES, BENCHMARK_TICKER
except ImportError:
    from verify_historical_partitions import PartitionVerifier, CORE_PROXIES, BENCHMARK_TICKER

logger = logging.getLogger(__name__)

# Security guardrails: Hard ceiling for Phase 6.2C
MAX_ALLOWED_DATE = date(2023, 12, 31)
DEFAULT_START_DATE = date(2021, 1, 1)
DEFAULT_END_DATE = date(2023, 12, 31)

DEFAULT_PARTITIONS_DIR = os.path.join(WORKSPACE_ROOT, ".planning", "data", "historical_partitions")
DEFAULT_STAGING_DIR = os.path.join(DEFAULT_PARTITIONS_DIR, ".staging_2021_2023")
DEFAULT_PRIMARY_DIR = os.path.join(DEFAULT_PARTITIONS_DIR, "2021_2023_primary")

MIN_PACING_DELAY = 2.5
DEFAULT_PACING_DELAY = 2.5
DEFAULT_CHUNK_SIZE = 3
DEFAULT_MAX_RETRIES = 3
RETRY_BACKOFF_SCHEDULE = [3.0, 6.0, 12.0]
MIN_EQUITIES_ACCEPTED = 45


def assert_safe_path(path_str: str) -> str:
    """Ensure path is canonically safe and does not resolve to protected directories."""
    if not path_str or not isinstance(path_str, str):
        raise ValueError(f"Path must be a non-empty string, got {path_str!r}.")

    norm = os.path.normpath(os.path.abspath(path_str)).lower()
    canonical = os.path.realpath(os.path.abspath(path_str)).lower()

    forbidden_tokens = [
        "lockbox",
        "2026",
        "sealed_test",
        "sealed",
        "tradingpaperstate",
        "paper_state",
        "paper_journal.json",
    ]

    for tok in forbidden_tokens:
        if tok in norm or tok in canonical:
            raise PermissionError(
                f"FATAL SECURITY VIOLATION: Path '{path_str}' contains forbidden token '{tok}'. "
                "Access to 2026 lockbox, sealed test partitions, or paper-trading state is strictly prohibited."
            )

    return canonical


def validate_safe_staging_dir(staging_path: str, expected_parent_dir: Optional[str] = None) -> str:
    """Validate that staging directory is strictly safe for rmtree deletion.

    Enforces:
    1. Target path is a non-empty string.
    2. Resolves and canonicalizes real path (resolves symlinks and normalizes traversal).
    3. assert_safe_path check passes (rejects 2026 lockbox, paper trading state, workspace root).
    4. Rejects symlinks and directory junctions (os.path.islink, os.path.isjunction).
    5. Directory name strictly begins with '.staging_' followed by an identifier.
    6. Directory is strictly inside an authorized '.planning/data/historical_partitions' directory.
    7. Fails closed with PermissionError or ValueError if any check fails.
    """
    if not staging_path or not isinstance(staging_path, str):
        raise ValueError(f"Staging path must be a non-empty string, got {staging_path!r}.")

    abs_path = os.path.abspath(staging_path)
    real_path = os.path.realpath(abs_path)

    # 1. Symlink and junction rejection
    if os.path.islink(abs_path) or os.path.islink(real_path):
        raise PermissionError(f"FATAL SECURITY VIOLATION: Staging path '{staging_path}' is a symlink.")
    if hasattr(os.path, "isjunction"):
        if os.path.isjunction(abs_path) or os.path.isjunction(real_path):
            raise PermissionError(f"FATAL SECURITY VIOLATION: Staging path '{staging_path}' is a directory junction.")

    # 2. Forbidden tokens check
    assert_safe_path(abs_path)
    assert_safe_path(real_path)

    # 3. Directory basename check
    basename = os.path.basename(real_path)
    if not basename.startswith(".staging_") or len(basename) <= len(".staging_"):
        raise PermissionError(
            f"FATAL SECURITY VIOLATION: Staging directory name must begin with '.staging_' followed by an identifier. "
            f"Got directory name '{basename}' from path '{staging_path}'."
        )

    # 4. Containment check within .planning/data/historical_partitions
    parent_dir = os.path.dirname(real_path)
    expected_suffix = os.path.normcase(os.path.join(".planning", "data", "historical_partitions"))
    norm_parent = os.path.normcase(os.path.normpath(parent_dir))

    if not norm_parent.endswith(expected_suffix):
        raise PermissionError(
            f"FATAL SECURITY VIOLATION: Staging directory '{staging_path}' is not inside an authorized "
            f"'.planning/data/historical_partitions' directory (parent is '{parent_dir}')."
        )

    # 5. Optional explicit parent check
    if expected_parent_dir is not None:
        real_expected = os.path.realpath(os.path.abspath(expected_parent_dir))
        if os.path.normcase(parent_dir) != os.path.normcase(real_expected):
            raise PermissionError(
                f"FATAL SECURITY VIOLATION: Staging directory parent '{parent_dir}' does not match expected '{real_expected}'."
            )

    return real_path


def assert_safe_date(d: date, context: str = "request") -> None:
    """Ensure date does not exceed MAX_ALLOWED_DATE ceiling."""
    if d > MAX_ALLOWED_DATE:
        raise PermissionError(
            f"FATAL SECURITY VIOLATION: {context} date {d} exceeds MAX_ALLOWED_DATE ({MAX_ALLOWED_DATE}). "
            "Data ingestion is strictly constrained to historical periods <= 2023-12-31."
        )


def verify_same_filesystem(staging_path: str, dest_path: str) -> None:
    """Verify staging and destination parent directory reside on the same filesystem.

    Raises OSError if staging and destination are on different volumes or filesystems,
    preventing non-atomic or cross-device promotion.
    """
    staging_real = os.path.realpath(os.path.abspath(staging_path))
    dest_parent = os.path.realpath(os.path.abspath(os.path.dirname(dest_path)))

    if not os.path.exists(staging_real):
        raise FileNotFoundError(f"Staging path does not exist: {staging_real}")

    os.makedirs(dest_parent, exist_ok=True)

    # Check drive letters on Windows
    drive_s, _ = os.path.splitdrive(staging_real)
    drive_d, _ = os.path.splitdrive(dest_parent)
    if drive_s.upper() != drive_d.upper():
        raise OSError(
            f"Cross-filesystem promotion detected: staging on drive '{drive_s}', destination on drive '{drive_d}'. "
            "Atomic directory promotion requires the same filesystem."
        )

    # Check st_dev
    st_dev_staging = os.stat(staging_real).st_dev
    st_dev_dest = os.stat(dest_parent).st_dev
    if st_dev_staging != st_dev_dest:
        raise OSError(
            f"Cross-filesystem promotion detected (st_dev mismatch: {st_dev_staging} vs {st_dev_dest}). "
            "Atomic directory promotion requires the same filesystem."
        )


def is_transient_error(e: Exception) -> bool:
    """Determine whether an error is transient (retryable) or permanent (fail-fast).

    Transient: HTTP 429 rate limits, 5xx server errors, network timeouts, connection resets.
    Permanent: 4xx client errors (400, 401, 403, 404), PermissionError, ValueError,
               data validation corruption errors.
    """
    if isinstance(e, PermissionError):
        return False
    if isinstance(e, (ValueError, TypeError)):
        return False

    err_str = str(e).lower()
    err_cls = e.__class__.__name__.lower()

    # Explicit permanent indicators
    permanent_indicators = ["400", "401", "403", "404", "not found", "malformed", "invalidprice", "invalidvolume"]
    for pi in permanent_indicators:
        if pi in err_str or pi in err_cls:
            return False

    # Explicit transient indicators
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


class HistoricalPartitionAcquirer:
    """Acquires, validates, and atomically promotes historical market data partitions."""

    def __init__(
        self,
        staging_dir: str = DEFAULT_STAGING_DIR,
        output_dir: str = DEFAULT_PRIMARY_DIR,
        start_date: date = DEFAULT_START_DATE,
        end_date: date = DEFAULT_END_DATE,
        candidate_tickers: Optional[Sequence[str]] = None,
        pacing_delay: float = DEFAULT_PACING_DELAY,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        max_retries: int = DEFAULT_MAX_RETRIES,
        download_fn: Optional[Callable[..., Any]] = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
        random_fn: Optional[Callable[[], float]] = None,
    ):
        self.output_dir = os.path.abspath(output_dir)
        self.start_date = start_date
        self.end_date = end_date
        self.chunk_size = max(1, chunk_size)
        self.max_retries = max(0, min(max_retries, 3))  # Max 3 retries (4 total attempts)
        self.download_fn = download_fn
        self.sleep_fn = sleep_fn or time_mod.sleep
        self.random_fn = random_fn or (lambda: random.uniform(0.0, 0.5))

        # Enforce hard minimum network pacing interval (Section 2)
        if pacing_delay < MIN_PACING_DELAY:
            raise ValueError(
                f"Invalid pacing_delay {pacing_delay}s. Minimum allowed pacing interval is {MIN_PACING_DELAY} seconds. "
                "Sub-2.5s pacing configurations are strictly rejected."
            )
        self.pacing_delay = float(pacing_delay)

        # Security check: paths
        assert_safe_path(self.output_dir)
        self.staging_dir = validate_safe_staging_dir(staging_dir)

        # Non-destructive overwrite protection check at init (Section 3)
        if os.path.exists(self.output_dir):
            raise FileExistsError(
                f"Destination directory '{self.output_dir}' already exists. Overwriting is strictly prohibited."
            )

        # Security check: dates
        assert_safe_date(self.start_date, "start_date")
        assert_safe_date(self.end_date, "end_date")

        # Set target universe
        raw_equities = list(candidate_tickers) if candidate_tickers is not None else list(DEFAULT_CURATED_WATCHLIST)
        self.candidate_tickers: List[str] = [validate_ticker(t) for t in raw_equities]
        # Mandatory core proxies: unconditionally required for primary partition (DEFECT-01)
        self.proxies: Tuple[str, ...] = tuple(CORE_PROXIES)

    def _default_download_candles(
        self,
        ticker: str,
        start_date: date,
        end_date: date,
    ) -> List[Dict[str, Any]]:
        """Download candles using YFinanceMarketDataProvider with isolated memory DB.

        Note: max_retries is passed as 0 to the provider layer to prevent multiplying
        retry attempts across layers. The acquirer layer strictly controls the retry budget.
        """
        from providers.yfinance_provider import YFinanceMarketDataProvider
        start_dt = datetime.combine(start_date, MarketClock.REGULAR_OPEN, tzinfo=timezone.utc)
        end_dt = datetime.combine(end_date, MarketClock.REGULAR_CLOSE, tzinfo=timezone.utc)

        provider = YFinanceMarketDataProvider(
            max_retries=0,  # Acquirer manages retry schedule; do not compound retries
            cache_db_path=":memory:",  # Zero disk writes to production DB
        )
        bars = provider.fetch_historical_bars(ticker, interval="1d", start=start_dt, end=end_dt)
        results = []
        for b in bars:
            d = to_eastern(b.timestamp).date()
            results.append({
                "Date": d.isoformat(),
                "Open": f"{b.open:.4f}",
                "High": f"{b.high:.4f}",
                "Low": f"{b.low:.4f}",
                "Close": f"{b.close:.4f}",
                "Volume": int(b.volume),
            })
        return results

    def fetch_ticker_data_with_retries(self, ticker: str) -> List[Dict[str, Any]]:
        """Fetch historical records for a ticker with bounded retries and exact backoff schedule."""
        last_error: Optional[Exception] = None
        attempts = 1 + self.max_retries  # exactly 1 initial + up to 3 retries = 4 attempts max

        for attempt in range(attempts):
            try:
                if self.download_fn is not None:
                    try:
                        raw = self.download_fn(
                            ticker=ticker,
                            start=self.start_date.isoformat(),
                            end=self.end_date.isoformat(),
                        )
                    except TypeError:
                        try:
                            raw = self.download_fn(ticker, self.start_date.isoformat(), self.end_date.isoformat())
                        except TypeError:
                            raw = self.download_fn(ticker)
                else:
                    raw = self._default_download_candles(ticker, self.start_date, self.end_date)

                # Post-response date ceiling validation
                formatted_records: List[Dict[str, Any]] = []
                for row in raw:
                    raw_d = row.get("Date") or row.get("date")
                    if isinstance(raw_d, (datetime, date)):
                        row_date = raw_d.date() if isinstance(raw_d, datetime) else raw_d
                    else:
                        row_date = date.fromisoformat(str(raw_d)[:10])

                    # SECURITY INVARIANT: Reject any candle exceeding MAX_ALLOWED_DATE immediately
                    assert_safe_date(row_date, f"Returned bar for {ticker}")

                    formatted_records.append({
                        "Date": row_date.isoformat(),
                        "Open": f"{Decimal(str(row.get('Open', row.get('open')))):.4f}",
                        "High": f"{Decimal(str(row.get('High', row.get('high')))):.4f}",
                        "Low": f"{Decimal(str(row.get('Low', row.get('low')))):.4f}",
                        "Close": f"{Decimal(str(row.get('Close', row.get('close')))):.4f}",
                        "Volume": int(float(str(row.get("Volume", row.get("volume", 0))))),
                    })

                return formatted_records

            except PermissionError:
                # Fatal security boundary violation: never retry, re-raise immediately
                raise
            except Exception as e:
                last_error = e
                # Check retryability: do not retry permanent errors
                if not is_transient_error(e):
                    logger.error(f"Permanent error encountered for {ticker}: {e}; failing immediately without retry.")
                    raise

                logger.warning(f"Attempt {attempt+1}/{attempts} failed for {ticker} (transient): {e}")
                if attempt < self.max_retries:
                    # Exact backoff schedule: [3.0, 6.0, 12.0] with mandatory >= 2.5s pacing floor (DEFECT-02)
                    base_backoff = RETRY_BACKOFF_SCHEDULE[min(attempt, len(RETRY_BACKOFF_SCHEDULE) - 1)]
                    jitter = max(0.0, float(self.random_fn()))
                    actual_backoff = max(MIN_PACING_DELAY, base_backoff + jitter)
                    self.sleep_fn(actual_backoff)
                    continue

        if last_error is not None:
            raise last_error
        raise RuntimeError(f"Failed to fetch data for {ticker} after {attempts} attempts.")

    def _write_staging_csv(self, ticker: str, records: List[Dict[str, Any]]) -> str:
        """Write acquired records to staging CSV."""
        os.makedirs(self.staging_dir, exist_ok=True)
        csv_path = os.path.join(self.staging_dir, f"{ticker}_daily.csv")
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["Date", "Open", "High", "Low", "Close", "Volume"])
            writer.writeheader()
            for r in records:
                writer.writerow(r)
        return csv_path

    def run_acquisition(self) -> Dict[str, Any]:
        """Execute acquisition workflow: Download to staging, verify, and promote atomically."""
        logger.info(f"Starting historical acquisition: {self.start_date} to {self.end_date}")
        logger.info(f"Staging directory: {self.staging_dir}")
        logger.info(f"Target destination: {self.output_dir}")

        # Check overwrite protection on final output directory (Section 3)
        if os.path.exists(self.output_dir):
            raise FileExistsError(
                f"Destination directory '{self.output_dir}' already exists. Overwriting is strictly prohibited."
            )

        # Clear staging directory if present from prior aborted run (DEFECT-03)
        safe_staging = validate_safe_staging_dir(self.staging_dir)
        if os.path.exists(safe_staging):
            import shutil
            shutil.rmtree(safe_staging)
        os.makedirs(safe_staging, exist_ok=True)

        all_target_tickers = list(self.proxies) + [t for t in self.candidate_tickers if t not in self.proxies]
        successful_downloads: Dict[str, str] = {}
        failed_downloads: Dict[str, str] = {}
        requests_dispatched = 0

        # Paced ingestion: Enforce pacing between EVERY request (Section 2)
        for ticker in all_target_tickers:
            # Pacing sleep between consecutive requests: no delay before first request
            if requests_dispatched > 0:
                jitter = max(0.0, float(self.random_fn()))  # Non-negative jitter
                actual_delay = max(MIN_PACING_DELAY, self.pacing_delay + jitter)
                self.sleep_fn(actual_delay)

            try:
                records = self.fetch_ticker_data_with_retries(ticker)
                requests_dispatched += 1
                if not records:
                    failed_downloads[ticker] = "No records returned"
                    continue
                csv_path = self._write_staging_csv(ticker, records)
                successful_downloads[ticker] = csv_path
                logger.info(f"Acquired {ticker}: {len(records)} bars written to {csv_path}")
            except Exception as e:
                requests_dispatched += 1
                failed_downloads[ticker] = str(e)
                logger.error(f"Failed to acquire {ticker}: {e}")

        # Phase 2: Completion & Promotion Gate Evaluation
        # 1. Proxies must 100% succeed
        missing_proxies = [p for p in self.proxies if p not in successful_downloads]
        proxies_intact = len(missing_proxies) == 0

        # 2. Candidate equities threshold
        candidate_success_count = sum(1 for t in self.candidate_tickers if t in successful_downloads)
        min_accepted = min(MIN_EQUITIES_ACCEPTED, len(self.candidate_tickers))
        equities_threshold_met = candidate_success_count >= min_accepted

        promotion_authorized = proxies_intact and equities_threshold_met

        if not promotion_authorized:
            reason = []
            if not proxies_intact:
                reason.append(f"Missing core proxies: {missing_proxies}")
            if not equities_threshold_met:
                reason.append(
                    f"Candidate equities deficit: {candidate_success_count}/{len(self.candidate_tickers)} "
                    f"succeeded, {min_accepted} required"
                )
            logger.error(f"Promotion gate REJECTED: {'; '.join(reason)}")
            # Staging and evidence preserved for diagnostics (Section 4)
            return {
                "status": "PROMOTION_REJECTED",
                "reason": "; ".join(reason),
                "proxies_intact": proxies_intact,
                "missing_proxies": missing_proxies,
                "candidate_success_count": candidate_success_count,
                "candidate_target_count": len(self.candidate_tickers),
                "staging_dir": self.staging_dir,
                "output_dir": self.output_dir,
                "failed_downloads": failed_downloads,
            }

        # Run verification engine on staging directory before promotion
        verifier = PartitionVerifier(
            partition_dir=self.staging_dir,
            min_equities=min_accepted,
            target_equities=self.candidate_tickers,
            required_proxies=self.proxies,
        )
        verify_result = verifier.verify_partition()

        if not verify_result["overall_passed"]:
            logger.error("Verification engine rejected staging directory; aborting promotion.")
            # Staging and evidence preserved for diagnostics (Section 4)
            return {
                "status": "VERIFICATION_FAILED",
                "reason": "PartitionVerifier rejected staging data",
                "verify_details": verify_result,
                "staging_dir": self.staging_dir,
                "output_dir": self.output_dir,
            }

        # SAFE ATOMIC PROMOTION (Section 4)
        # 1. Destination check
        if os.path.exists(self.output_dir):
            raise FileExistsError(
                f"Destination directory '{self.output_dir}' already exists. Overwrite strictly forbidden."
            )

        # 2. Filesystem check: staging and destination parent must be on same filesystem
        verify_same_filesystem(self.staging_dir, self.output_dir)

        # 3. Atomic rename via os.replace
        os.makedirs(os.path.dirname(self.output_dir), exist_ok=True)
        try:
            os.replace(self.staging_dir, self.output_dir)
        except Exception as e:
            logger.error(f"Atomic directory promotion failed: {e}")
            raise OSError(f"Atomic promotion failed during os.replace: {e}") from e

        logger.info(f"Promotion successful: Staging atomically promoted to {self.output_dir}")

        return {
            "status": "SUCCESS",
            "promoted_directory": self.output_dir,
            "manifest_status": verify_result["manifest_status"],
            "manifest_path": os.path.join(self.output_dir, "DATASET_MANIFEST.json"),
            "audit_path": os.path.join(self.output_dir, "DATASET_AUDIT.md"),
            "proxies_passed": verify_result["proxies_passed"],
            "equities_passed_count": verify_result["equities_passed_count"],
            "total_validated_files": verify_result["total_validated_files"],
            "quarantined_count": verify_result["quarantined_count"],
            "failed_downloads": failed_downloads,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Acquire and promote historical market data partitions.")
    parser.add_argument("--start", default=DEFAULT_START_DATE.isoformat(), help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", default=DEFAULT_END_DATE.isoformat(), help="End date (YYYY-MM-DD)")
    parser.add_argument("--staging", default=DEFAULT_STAGING_DIR, help="Temporary download staging directory")
    parser.add_argument("--output", default=DEFAULT_PRIMARY_DIR, help="Final destination partition directory")
    parser.add_argument("--tickers", nargs="*", default=None, help="List of candidate equity tickers (optional)")
    parser.add_argument("--pacing-delay", type=float, default=DEFAULT_PACING_DELAY, help="Pacing delay in seconds (>= 2.5s)")
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE, help="Ticker batch chunk size")
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES, help="Max retry attempts on error (max 3)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    acquirer = HistoricalPartitionAcquirer(
        staging_dir=args.staging,
        output_dir=args.output,
        start_date=date.fromisoformat(args.start),
        end_date=date.fromisoformat(args.end),
        candidate_tickers=args.tickers,
        pacing_delay=args.pacing_delay,
        chunk_size=args.chunk_size,
        max_retries=args.max_retries,
    )

    result = acquirer.run_acquisition()
    print(json.dumps(result, indent=2))
    if result.get("status") != "SUCCESS":
        sys.exit(1)


if __name__ == "__main__":
    main()
