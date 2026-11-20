"""Historical Partition Verification and Integrity Engine.

Phase 6.2C — Step 3.4: Verification Script

Enforces:
1. Calendar & Monotonicity Verification (MarketClock.is_historical_trading_day, zero duplicates, monotonic ascending).
2. Geometric & Price Invariants (Low <= Open, Close <= High; High >= Low; Open, High, Low, Close > 0; Volume >= 0).
3. Mathematical Corporate-Action Split Verification:
   - Equation: |P_T / P_{T-1} - 1/K| < 0.15 * (1/K) => REJECT (UNADJUSTED_SPLIT_ERROR).
   - Known splits: NVDA (2021-07-20, K=4), AMZN (2022-06-06, K=20), GOOGL (2022-07-18, K=20), TSLA (2022-08-25, K=3).
4. Dollar-Volume ADV Continuity Invariant:
   - 0.40 <= Median(P * V)_{[T, T+20]} / Median(P * V)_{[T-20, T-1]} <= 2.50.
5. 4-Tier Discontinuity Taxonomy & Anomaly Quarantine:
   - Corrupted series moved to partition quarantine/ subfolder without silent price repair.
6. Universe Acceptance Threshold (SPY, QQQ, IWM 100% required; >= 45 of 50 equities).
7. Cryptographic SHA-256 Checksum Manifest (DATASET_MANIFEST.json) & DATASET_AUDIT.md.
"""

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import argparse
import csv
import hashlib
import json
import logging
import os
import shutil
import sys
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

# Ensure workspace and trader-swing modules are accessible
WORKSPACE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADER_SWING_DIR = os.path.join(WORKSPACE_ROOT, ".agents", "skills", "trader-swing")
if WORKSPACE_ROOT not in sys.path:
    sys.path.insert(0, WORKSPACE_ROOT)
if TRADER_SWING_DIR not in sys.path:
    sys.path.insert(0, TRADER_SWING_DIR)

from market_data_models import MarketClock
from universe import DEFAULT_CURATED_WATCHLIST, SURVIVORSHIP_BIAS_DISCLOSURE

logger = logging.getLogger(__name__)

# Known stock splits in the 2021-2023 window with split factor K
# K = post-split shares per pre-split share (e.g. 4 for 4-for-1 forward split)
KNOWN_SPLITS: Dict[str, List[Dict[str, Any]]] = {
    "NVDA": [{"date": date(2021, 7, 20), "split_factor": Decimal("4.0")}],
    "AMZN": [{"date": date(2022, 6, 6), "split_factor": Decimal("20.0")}],
    "GOOGL": [{"date": date(2022, 7, 18), "split_factor": Decimal("20.0")}],
    "GOOG": [{"date": date(2022, 7, 18), "split_factor": Decimal("20.0")}],
    "TSLA": [{"date": date(2022, 8, 25), "split_factor": Decimal("3.0")}],
}

CORE_PROXIES = ("SPY", "QQQ", "IWM")
BENCHMARK_TICKER = "SPY"
EXPECTED_NYSE_SESSIONS_2021_2023 = 753
MIN_EQUITIES_ACCEPTED = 45


@dataclass
class TickerValidationResult:
    ticker: str
    filename: str
    status: str  # "VALIDATED", "EXCLUDED_QUARANTINED", "EXCLUDED_DEFICIT"
    role: str    # "PRIMARY_PROXY_BENCHMARK", "PROXY", "CANDIDATE_STOCK"
    row_count: int
    start_session: Optional[str]
    end_session: Optional[str]
    byte_size: int
    sha256: str
    missing_sessions: int
    quarantine_path: Optional[str] = None
    exclusion_reason: Optional[str] = None
    discontinuity_class: Optional[str] = None


def compute_sha256(filepath: str) -> str:
    """Compute standard SHA-256 hex digest of file."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def compute_median(values: List[Decimal]) -> Decimal:
    """Compute exact median of a list of Decimal values."""
    if not values:
        return Decimal("0.00")
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    mid = n // 2
    if n % 2 == 1:
        return sorted_vals[mid]
    return (sorted_vals[mid - 1] + sorted_vals[mid]) / Decimal("2.0")


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


class PartitionVerifier:
    """Verifies data integrity, calendar alignment, split adjustments, and volume invariants."""

    def __init__(
        self,
        partition_dir: str,
        manifest_path: Optional[str] = None,
        quarantine_dir: Optional[str] = None,
        expected_sessions: Optional[int] = None,
        min_equities: int = MIN_EQUITIES_ACCEPTED,
        target_equities: Optional[Sequence[str]] = None,
        required_proxies: Optional[Sequence[str]] = None,
    ):
        self.partition_dir = os.path.abspath(partition_dir)
        self.quarantine_dir = os.path.abspath(quarantine_dir or os.path.join(self.partition_dir, "quarantine"))
        self.manifest_path = os.path.abspath(manifest_path or os.path.join(self.partition_dir, "DATASET_MANIFEST.json"))
        self.audit_md_path = os.path.join(self.partition_dir, "DATASET_AUDIT.md")
        self.expected_sessions = expected_sessions
        self.min_equities = min_equities
        self.target_equities = list(target_equities) if target_equities is not None else list(DEFAULT_CURATED_WATCHLIST)
        self.required_proxies = tuple(required_proxies) if required_proxies is not None else CORE_PROXIES

        # Enforce canonical path containment (Section 8)
        assert_safe_path(self.partition_dir)
        assert_safe_path(self.quarantine_dir)
        assert_safe_path(self.manifest_path)

    def _quarantine_file(self, csv_path: str, filename: str, reason: str) -> str:
        """Move corrupted file to quarantine directory without modifying or overwriting existing evidence."""
        os.makedirs(self.quarantine_dir, exist_ok=True)
        dest_path = os.path.join(self.quarantine_dir, filename)
        if os.path.exists(dest_path):
            base, ext = os.path.splitext(filename)
            counter = 1
            while True:
                coll_filename = f"{base}_dup{counter}{ext}"
                dest_path = os.path.join(self.quarantine_dir, coll_filename)
                if not os.path.exists(dest_path):
                    break
                counter += 1
        shutil.move(csv_path, dest_path)
        logger.warning(f"Quarantined {filename} to {dest_path}: {reason}")
        return dest_path

    def verify_single_csv(
        self,
        filepath: str,
        ticker: str,
    ) -> TickerValidationResult:
        """Verify an individual CSV file against all 5 integrity gates."""
        filename = os.path.basename(filepath)
        role = "PRIMARY_PROXY_BENCHMARK" if ticker == BENCHMARK_TICKER else ("PROXY" if ticker in CORE_PROXIES else "CANDIDATE_STOCK")
        file_sha256 = compute_sha256(filepath)
        byte_size = os.path.getsize(filepath)

        rows: List[Dict[str, Any]] = []
        with open(filepath, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            headers = [h.strip() for h in (reader.fieldnames or [])]
            required_headers = {"Date", "Open", "High", "Low", "Close", "Volume"}
            missing_headers = required_headers - set(headers)
            if missing_headers:
                qpath = self._quarantine_file(filepath, filename, f"Missing required CSV headers: {missing_headers}")
                return TickerValidationResult(
                    ticker=ticker,
                    filename=filename,
                    status="EXCLUDED_QUARANTINED",
                    role=role,
                    row_count=0,
                    start_session=None,
                    end_session=None,
                    byte_size=byte_size,
                    sha256=file_sha256,
                    missing_sessions=0,
                    quarantine_path=qpath,
                    exclusion_reason=f"Missing CSV headers: {missing_headers}",
                    discontinuity_class="Class 4 (Corruption)",
                )

            for line_no, r in enumerate(reader, start=2):
                rows.append(r)

        if not rows:
            qpath = self._quarantine_file(filepath, filename, "Empty CSV file (0 data rows)")
            return TickerValidationResult(
                ticker=ticker,
                filename=filename,
                status="EXCLUDED_QUARANTINED",
                role=role,
                row_count=0,
                start_session=None,
                end_session=None,
                byte_size=byte_size,
                sha256=file_sha256,
                missing_sessions=0,
                quarantine_path=qpath,
                exclusion_reason="Empty CSV file",
                discontinuity_class="Class 4 (Corruption)",
            )

        # Parse & Validate Gate 1 (Calendar & Monotonicity) and Gate 2 (Geometry)
        parsed_bars: List[Dict[str, Any]] = []
        prev_date: Optional[date] = None

        for idx, row in enumerate(rows):
            raw_date = row.get("Date", "").strip()
            try:
                curr_date = date.fromisoformat(raw_date)
            except Exception as e:
                qpath = self._quarantine_file(filepath, filename, f"Malformed date '{raw_date}' at row {idx+2}: {e}")
                return TickerValidationResult(
                    ticker=ticker,
                    filename=filename,
                    status="EXCLUDED_QUARANTINED",
                    role=role,
                    row_count=len(rows),
                    start_session=None,
                    end_session=None,
                    byte_size=byte_size,
                    sha256=file_sha256,
                    missing_sessions=0,
                    quarantine_path=qpath,
                    exclusion_reason=f"Malformed date '{raw_date}'",
                    discontinuity_class="Class 4 (Corruption)",
                )

            # MarketClock calendar check
            is_trading, reason = MarketClock.is_historical_trading_day(curr_date)
            if not is_trading:
                qpath = self._quarantine_file(
                    filepath, filename, f"Non-trading day present ({curr_date}, reason: {reason}) at row {idx+2}"
                )
                return TickerValidationResult(
                    ticker=ticker,
                    filename=filename,
                    status="EXCLUDED_QUARANTINED",
                    role=role,
                    row_count=len(rows),
                    start_session=rows[0]["Date"],
                    end_session=raw_date,
                    byte_size=byte_size,
                    sha256=file_sha256,
                    missing_sessions=0,
                    quarantine_path=qpath,
                    exclusion_reason=f"Calendar violation: {curr_date} is {reason}",
                    discontinuity_class="Class 4 (Corruption)",
                )

            # Monotonicity check
            if prev_date is not None:
                if curr_date <= prev_date:
                    qpath = self._quarantine_file(
                        filepath, filename, f"Date monotonicity violation: {curr_date} <= {prev_date} at row {idx+2}"
                    )
                    return TickerValidationResult(
                        ticker=ticker,
                        filename=filename,
                        status="EXCLUDED_QUARANTINED",
                        role=role,
                        row_count=len(rows),
                        start_session=rows[0]["Date"],
                        end_session=raw_date,
                        byte_size=byte_size,
                        sha256=file_sha256,
                        missing_sessions=0,
                        quarantine_path=qpath,
                        exclusion_reason="Date sequence non-monotonic or duplicate",
                        discontinuity_class="Class 4 (Corruption)",
                    )

            # Parse & validate numeric prices: reject NaN, Infinity, negative/zero prices (Section 6)
            prices_map: Dict[str, Decimal] = {}
            for col_name in ["Open", "High", "Low", "Close"]:
                raw_val = str(row.get(col_name, "")).strip()
                if not raw_val or "nan" in raw_val.lower() or "inf" in raw_val.lower():
                    qpath = self._quarantine_file(
                        filepath, filename, f"Non-numeric price (NaN/Inf/Empty) for {col_name} at row {idx+2}: {raw_val}"
                    )
                    return TickerValidationResult(
                        ticker=ticker,
                        filename=filename,
                        status="EXCLUDED_QUARANTINED",
                        role=role,
                        row_count=len(rows),
                        start_session=rows[0]["Date"],
                        end_session=raw_date,
                        byte_size=byte_size,
                        sha256=file_sha256,
                        missing_sessions=0,
                        quarantine_path=qpath,
                        exclusion_reason=f"Non-numeric price (NaN/Inf) for {col_name}",
                        discontinuity_class="Class 4 (Corruption)",
                    )
                try:
                    dec_val = Decimal(raw_val)
                except (InvalidOperation, ValueError) as e:
                    qpath = self._quarantine_file(
                        filepath, filename, f"Invalid price format for {col_name} at row {idx+2}: {e}"
                    )
                    return TickerValidationResult(
                        ticker=ticker,
                        filename=filename,
                        status="EXCLUDED_QUARANTINED",
                        role=role,
                        row_count=len(rows),
                        start_session=rows[0]["Date"],
                        end_session=raw_date,
                        byte_size=byte_size,
                        sha256=file_sha256,
                        missing_sessions=0,
                        quarantine_path=qpath,
                        exclusion_reason=f"Invalid price format for {col_name}: {e}",
                        discontinuity_class="Class 4 (Corruption)",
                    )
                if dec_val.is_nan() or dec_val.is_infinite():
                    qpath = self._quarantine_file(
                        filepath, filename, f"Non-numeric price (NaN/Inf) for {col_name} at row {idx+2}: {dec_val}"
                    )
                    return TickerValidationResult(
                        ticker=ticker,
                        filename=filename,
                        status="EXCLUDED_QUARANTINED",
                        role=role,
                        row_count=len(rows),
                        start_session=rows[0]["Date"],
                        end_session=raw_date,
                        byte_size=byte_size,
                        sha256=file_sha256,
                        missing_sessions=0,
                        quarantine_path=qpath,
                        exclusion_reason=f"Non-numeric price (NaN/Inf) for {col_name}",
                        discontinuity_class="Class 4 (Corruption)",
                    )
                if dec_val <= Decimal("0.00"):
                    qpath = self._quarantine_file(
                        filepath, filename, f"Non-positive price for {col_name} at row {idx+2}: {dec_val}"
                    )
                    return TickerValidationResult(
                        ticker=ticker,
                        filename=filename,
                        status="EXCLUDED_QUARANTINED",
                        role=role,
                        row_count=len(rows),
                        start_session=rows[0]["Date"],
                        end_session=raw_date,
                        byte_size=byte_size,
                        sha256=file_sha256,
                        missing_sessions=0,
                        quarantine_path=qpath,
                        exclusion_reason=f"Non-positive financial price for {col_name}",
                        discontinuity_class="Class 4 (Corruption)",
                    )
                prices_map[col_name] = dec_val

            op = prices_map["Open"]
            hp = prices_map["High"]
            lp = prices_map["Low"]
            cp = prices_map["Close"]

            # Parse & validate volume: reject NaN, Infinity, negative, and fractional volume (Section 6)
            raw_vol = str(row.get("Volume", "")).strip()
            if not raw_vol or "nan" in raw_vol.lower() or "inf" in raw_vol.lower():
                qpath = self._quarantine_file(filepath, filename, f"Non-numeric volume (NaN/Inf) at row {idx+2}: {raw_vol}")
                return TickerValidationResult(
                    ticker=ticker,
                    filename=filename,
                    status="EXCLUDED_QUARANTINED",
                    role=role,
                    row_count=len(rows),
                    start_session=rows[0]["Date"],
                    end_session=raw_date,
                    byte_size=byte_size,
                    sha256=file_sha256,
                    missing_sessions=0,
                    quarantine_path=qpath,
                    exclusion_reason="Non-numeric volume (NaN/Inf)",
                    discontinuity_class="Class 4 (Corruption)",
                )
            try:
                vol_dec = Decimal(raw_vol)
            except (InvalidOperation, ValueError) as e:
                qpath = self._quarantine_file(filepath, filename, f"Non-numeric volume format at row {idx+2}: {e}")
                return TickerValidationResult(
                    ticker=ticker,
                    filename=filename,
                    status="EXCLUDED_QUARANTINED",
                    role=role,
                    row_count=len(rows),
                    start_session=rows[0]["Date"],
                    end_session=raw_date,
                    byte_size=byte_size,
                    sha256=file_sha256,
                    missing_sessions=0,
                    quarantine_path=qpath,
                    exclusion_reason=f"Invalid volume format: {e}",
                    discontinuity_class="Class 4 (Corruption)",
                )
            if vol_dec.is_nan() or vol_dec.is_infinite():
                qpath = self._quarantine_file(filepath, filename, f"Non-numeric volume (NaN/Inf) at row {idx+2}: {vol_dec}")
                return TickerValidationResult(
                    ticker=ticker,
                    filename=filename,
                    status="EXCLUDED_QUARANTINED",
                    role=role,
                    row_count=len(rows),
                    start_session=rows[0]["Date"],
                    end_session=raw_date,
                    byte_size=byte_size,
                    sha256=file_sha256,
                    missing_sessions=0,
                    quarantine_path=qpath,
                    exclusion_reason="Non-numeric volume (NaN/Inf)",
                    discontinuity_class="Class 4 (Corruption)",
                )
            # Enforce integer volume: reject fractional volume values
            if vol_dec != vol_dec.to_integral_value():
                qpath = self._quarantine_file(
                    filepath, filename, f"Fractional volume rejected at row {idx+2}: {raw_vol}"
                )
                return TickerValidationResult(
                    ticker=ticker,
                    filename=filename,
                    status="EXCLUDED_QUARANTINED",
                    role=role,
                    row_count=len(rows),
                    start_session=rows[0]["Date"],
                    end_session=raw_date,
                    byte_size=byte_size,
                    sha256=file_sha256,
                    missing_sessions=0,
                    quarantine_path=qpath,
                    exclusion_reason=f"Fractional volume rejected: {raw_vol}",
                    discontinuity_class="Class 4 (Corruption)",
                )
            vol = int(vol_dec)
            if vol < 0:
                qpath = self._quarantine_file(filepath, filename, f"Negative volume at row {idx+2}: {vol}")
                return TickerValidationResult(
                    ticker=ticker,
                    filename=filename,
                    status="EXCLUDED_QUARANTINED",
                    role=role,
                    row_count=len(rows),
                    start_session=rows[0]["Date"],
                    end_session=raw_date,
                    byte_size=byte_size,
                    sha256=file_sha256,
                    missing_sessions=0,
                    quarantine_path=qpath,
                    exclusion_reason=f"Negative volume observation: {vol}",
                    discontinuity_class="Class 4 (Corruption)",
                )

            # OHLC geometric bounds
            if hp < lp or hp < op or hp < cp or lp > op or lp > cp:
                qpath = self._quarantine_file(
                    filepath, filename, f"Geometric OHLC violation at row {idx+2}: O={op}, H={hp}, L={lp}, C={cp}"
                )
                return TickerValidationResult(
                    ticker=ticker,
                    filename=filename,
                    status="EXCLUDED_QUARANTINED",
                    role=role,
                    row_count=len(rows),
                    start_session=rows[0]["Date"],
                    end_session=raw_date,
                    byte_size=byte_size,
                    sha256=file_sha256,
                    missing_sessions=0,
                    quarantine_path=qpath,
                    exclusion_reason="Geometric OHLC invariant violation (High/Low out of bounds)",
                    discontinuity_class="Class 4 (Corruption)",
                )

            prev_date = curr_date
            parsed_bars.append({
                "date": curr_date,
                "open": op,
                "high": hp,
                "low": lp,
                "close": cp,
                "volume": vol,
                "dollar_volume": cp * Decimal(vol),
            })

        # Gate 3: Mathematical Corporate-Action Split Verification
        date_to_idx = {b["date"]: i for i, b in enumerate(parsed_bars)}
        splits_for_ticker = KNOWN_SPLITS.get(ticker, [])

        for split in splits_for_ticker:
            split_date: date = split["date"]
            k_factor: Decimal = split["split_factor"]
            if split_date in date_to_idx:
                t_idx = date_to_idx[split_date]
                if t_idx > 0:
                    prev_close = parsed_bars[t_idx - 1]["close"]
                    curr_close = parsed_bars[t_idx]["close"]
                    observed_ratio = curr_close / prev_close
                    unadjusted_neighborhood = Decimal("1.0") / k_factor
                    tolerance_radius = Decimal("0.15") * unadjusted_neighborhood

                    # Rejection condition: |P_T / P_{T-1} - 1/K| < 0.15 * (1/K)
                    if abs(observed_ratio - unadjusted_neighborhood) < tolerance_radius:
                        qpath = self._quarantine_file(
                            filepath,
                            filename,
                            f"UNADJUSTED_SPLIT_ERROR on {split_date}: observed ratio {observed_ratio:.4f} "
                            f"in unadjusted neighborhood of 1/{k_factor} ({unadjusted_neighborhood:.4f} +/- {tolerance_radius:.4f})",
                        )
                        return TickerValidationResult(
                            ticker=ticker,
                            filename=filename,
                            status="EXCLUDED_QUARANTINED",
                            role=role,
                            row_count=len(parsed_bars),
                            start_session=str(parsed_bars[0]["date"]),
                            end_session=str(parsed_bars[-1]["date"]),
                            byte_size=byte_size,
                            sha256=file_sha256,
                            missing_sessions=0,
                            quarantine_path=qpath,
                            exclusion_reason=f"UNADJUSTED_SPLIT_ERROR on {split_date} (factor {k_factor})",
                            discontinuity_class="Class 1 (Unadjusted Split Corruption)",
                        )

                    # Gate 4: Dollar-Volume ADV Continuity Invariant
                    # Pre-split window: [T-20, T-1]
                    pre_window = parsed_bars[max(0, t_idx - 20) : t_idx]
                    # Post-split window: [T, T+20]
                    post_window = parsed_bars[t_idx : min(len(parsed_bars), t_idx + 21)]

                    pre_dv = [b["dollar_volume"] for b in pre_window if b["dollar_volume"] > Decimal("0.0")]
                    post_dv = [b["dollar_volume"] for b in post_window if b["dollar_volume"] > Decimal("0.0")]

                    if pre_dv and post_dv:
                        med_pre = compute_median(pre_dv)
                        med_post = compute_median(post_dv)
                        if med_pre > Decimal("0.0"):
                            dv_ratio = med_post / med_pre
                            # Invariant: 0.40 <= Median(Post) / Median(Pre) <= 2.50
                            if dv_ratio < Decimal("0.40") or dv_ratio > Decimal("2.50"):
                                # Check if ratio indicates volume failed to adjust (e.g. ratio ~ K)
                                qpath = self._quarantine_file(
                                    filepath,
                                    filename,
                                    f"UNADJUSTED_VOLUME_CORRUPTION on {split_date}: ADV dollar-volume ratio {dv_ratio:.2f} "
                                    f"outside [0.40, 2.50] continuity bound",
                                )
                                return TickerValidationResult(
                                    ticker=ticker,
                                    filename=filename,
                                    status="EXCLUDED_QUARANTINED",
                                    role=role,
                                    row_count=len(parsed_bars),
                                    start_session=str(parsed_bars[0]["date"]),
                                    end_session=str(parsed_bars[-1]["date"]),
                                    byte_size=byte_size,
                                    sha256=file_sha256,
                                    missing_sessions=0,
                                    quarantine_path=qpath,
                                    exclusion_reason=f"UNADJUSTED_VOLUME_CORRUPTION on {split_date} (ratio {dv_ratio:.2f})",
                                    discontinuity_class="Class 4 (Corruption)",
                                )

        # Gate 5: 4-Tier Discontinuity Taxonomy & Non-Split Anomalies
        # Check consecutive sessions for single-session spikes > 35% without news or high volume
        for i in range(1, len(parsed_bars)):
            prev_b = parsed_bars[i - 1]
            curr_b = parsed_bars[i]
            if curr_b["date"] in [s["date"] for s in splits_for_ticker]:
                continue  # already checked under split rules

            pct_change = abs(curr_b["close"] - prev_b["close"]) / prev_b["close"]
            if pct_change >= Decimal("0.35"):
                # Check recent 20-day median volume
                hist_window = parsed_bars[max(0, i - 20) : i]
                vol_window = [Decimal(b["volume"]) for b in hist_window if b["volume"] > 0]
                med_vol = compute_median(vol_window)
                curr_vol = Decimal(curr_b["volume"])

                # If single-day move >= 35% occurs with abnormally thin volume (< 0.5 * ADV), flag as unexplained corruption
                if med_vol > Decimal("0") and curr_vol < (Decimal("0.5") * med_vol):
                    qpath = self._quarantine_file(
                        filepath,
                        filename,
                        f"Unexplained single-session price spike {pct_change:.2%} on {curr_b['date']} with thin volume",
                    )
                    return TickerValidationResult(
                        ticker=ticker,
                        filename=filename,
                        status="EXCLUDED_QUARANTINED",
                        role=role,
                        row_count=len(parsed_bars),
                        start_session=str(parsed_bars[0]["date"]),
                        end_session=str(parsed_bars[-1]["date"]),
                        byte_size=byte_size,
                        sha256=file_sha256,
                        missing_sessions=0,
                        quarantine_path=qpath,
                        exclusion_reason=f"Class 4 Unexplained Corruption: {pct_change:.2%} move on thin volume on {curr_b['date']}",
                        discontinuity_class="Class 4 (Corruption)",
                    )

        # Check session deficit vs expected
        missing_count = 0
        if self.expected_sessions is not None:
            missing_count = max(0, self.expected_sessions - len(parsed_bars))
            if missing_count > 0:
                # Deficit exists: if candidate stock and missing sessions > 15, exclude due to deficit
                if role == "CANDIDATE_STOCK" and missing_count > 15:
                    return TickerValidationResult(
                        ticker=ticker,
                        filename=filename,
                        status="EXCLUDED_DEFICIT",
                        role=role,
                        row_count=len(parsed_bars),
                        start_session=str(parsed_bars[0]["date"]),
                        end_session=str(parsed_bars[-1]["date"]),
                        byte_size=byte_size,
                        sha256=file_sha256,
                        missing_sessions=missing_count,
                        exclusion_reason=f"Excessive missing sessions: {missing_count} sessions missing vs {self.expected_sessions} expected",
                        discontinuity_class="Class 3 (Missing Sessions)",
                    )

        return TickerValidationResult(
            ticker=ticker,
            filename=filename,
            status="VALIDATED",
            role=role,
            row_count=len(parsed_bars),
            start_session=str(parsed_bars[0]["date"]),
            end_session=str(parsed_bars[-1]["date"]),
            byte_size=byte_size,
            sha256=file_sha256,
            missing_sessions=missing_count,
            discontinuity_class="Class 1 / Class 2 Verified",
        )

    def verify_partition(self) -> Dict[str, Any]:
        """Verify entire partition directory, enforce universe thresholds, and write manifest & audit docs."""
        if not os.path.exists(self.partition_dir):
            raise FileNotFoundError(f"Partition directory does not exist: {self.partition_dir}")

        all_files = [f for f in os.listdir(self.partition_dir) if f.endswith(".csv")]
        results: Dict[str, TickerValidationResult] = {}

        # Collect tickers to verify
        # Map filenames to symbols: e.g. "SPY_daily.csv" -> "SPY"
        for fname in all_files:
            sym = fname.replace("_daily.csv", "").replace(".csv", "").upper().strip()
            fpath = os.path.join(self.partition_dir, fname)
            res = self.verify_single_csv(fpath, sym)
            results[sym] = res

        # Core proxies check (SPY, QQQ, IWM or required_proxies)
        missing_proxies = [p for p in self.required_proxies if p not in results or results[p].status != "VALIDATED"]
        proxies_passed = len(missing_proxies) == 0

        # Benchmark session count determination
        spy_res = results.get(BENCHMARK_TICKER)
        actual_benchmark_sessions = spy_res.row_count if spy_res and spy_res.status == "VALIDATED" else 0
        expected_sessions = self.expected_sessions or actual_benchmark_sessions or EXPECTED_NYSE_SESSIONS_2021_2023

        # Candidate equities check
        target_equities_set = set(self.target_equities)
        validated_equities = [sym for sym, r in results.items() if sym in target_equities_set and r.status == "VALIDATED"]
        excluded_equities = [sym for sym, r in results.items() if sym in target_equities_set and r.status != "VALIDATED"]

        equities_threshold_met = len(validated_equities) >= min(self.min_equities, len(target_equities_set))
        overall_passed = proxies_passed and equities_threshold_met

        manifest_status = "READY_FOR_BACKTEST" if overall_passed else "VALIDATION_FAILED"

        # Construct DATASET_MANIFEST.json
        manifest_data: Dict[str, Any] = {
            "manifest_version": "1.2.0",
            "partition_label": os.path.basename(self.partition_dir),
            "partition_status": manifest_status,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_provider": "Yahoo Finance via yfinance (auto_adjust=False, actions=True)",
            "cross_check_oracle": "Stooq CSV / SEC EDGAR",
            "date_bounds": {
                "start_date": spy_res.start_session if spy_res else "UNKNOWN",
                "end_date": spy_res.end_session if spy_res else "UNKNOWN",
                "expected_nyse_sessions": expected_sessions,
                "benchmark_sessions": actual_benchmark_sessions,
            },
            "universe_classification": "Surviving Large-Cap Proxy Universe (Retrospective)",
            "survivorship_bias_disclosure": SURVIVORSHIP_BIAS_DISCLOSURE,
            "ticker_universe_summary": {
                "total_target_equities": len(target_equities_set),
                "validated_equities_count": len(validated_equities),
                "excluded_equities_count": len(excluded_equities),
                "acceptance_threshold_met": equities_threshold_met,
                "proxies_intact": proxies_passed,
                "missing_proxies": missing_proxies,
            },
            "files": {},
            "quarantined_files": {},
            "delisted_probe": {
                "TWTR": {"status": "ACQUISITION_CASH_TERMINAL_2022-10-27", "cash_per_share": "54.20"},
                "SIVB": {"status": "BANKRUPTCY_CONSERVATIVE_ZERO_TERMINAL_2023-03-10", "cash_per_share": "0.00"},
                "SBNY": {"status": "BANKRUPTCY_CONSERVATIVE_ZERO_TERMINAL_2023-03-10", "cash_per_share": "0.00"},
                "FRC": {"status": "BANKRUPTCY_CONSERVATIVE_ZERO_TERMINAL_2023-04-28", "cash_per_share": "0.00"},
            },
        }

        for sym, r in sorted(results.items()):
            if r.status == "VALIDATED":
                manifest_data["files"][r.filename] = {
                    "role": r.role,
                    "price_adjustment": "SPLIT_ADJUSTED",
                    "row_count": r.row_count,
                    "start_session": r.start_session,
                    "end_session": r.end_session,
                    "byte_size": r.byte_size,
                    "sha256": r.sha256,
                    "status": r.status,
                    "missing_sessions": r.missing_sessions,
                }
            else:
                manifest_data["quarantined_files"][r.filename] = {
                    "role": r.role,
                    "status": r.status,
                    "exclusion_reason": r.exclusion_reason,
                    "quarantine_path": r.quarantine_path,
                    "discontinuity_class": r.discontinuity_class,
                    "row_count": r.row_count,
                    "sha256": r.sha256,
                }

        # Write manifest
        with open(self.manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest_data, f, indent=2)

        # Write DATASET_AUDIT.md
        self._write_audit_markdown(manifest_data, results)

        return {
            "overall_passed": overall_passed,
            "manifest_status": manifest_status,
            "manifest_path": self.manifest_path,
            "audit_path": self.audit_md_path,
            "proxies_passed": proxies_passed,
            "equities_passed_count": len(validated_equities),
            "equities_threshold_met": equities_threshold_met,
            "missing_proxies": missing_proxies,
            "total_validated_files": len(manifest_data["files"]),
            "quarantined_count": len(manifest_data["quarantined_files"]),
        }

    def _write_audit_markdown(
        self,
        manifest_data: Dict[str, Any],
        results: Dict[str, TickerValidationResult],
    ) -> None:
        """Write human-readable DATASET_AUDIT.md audit trail."""
        lines = [
            f"# Historical Dataset Audit Report: {manifest_data['partition_label']}",
            "",
            f"**Audit Status:** `{manifest_data['partition_status']}`  ",
            f"**Generated UTC:** {manifest_data['generated_at_utc']}  ",
            f"**Universe Classification:** {manifest_data['universe_classification']}  ",
            "",
            "## Mandatory Survivorship Bias Disclosure",
            "",
            "> [!WARNING]",
            f"> {manifest_data['survivorship_bias_disclosure']}",
            "",
            "## Universe Summary",
            "",
            f"- **Target Candidate Equities:** {manifest_data['ticker_universe_summary']['total_target_equities']}",
            f"- **Validated Equities:** {manifest_data['ticker_universe_summary']['validated_equities_count']}",
            f"- **Excluded Equities:** {manifest_data['ticker_universe_summary']['excluded_equities_count']}",
            f"- **Threshold Met (>= {self.min_equities}):** `{manifest_data['ticker_universe_summary']['acceptance_threshold_met']}`",
            f"- **Core Proxies Intact (SPY, QQQ, IWM):** `{manifest_data['ticker_universe_summary']['proxies_intact']}`",
            "",
            "## Validated Core Files",
            "",
            "| Ticker | Role | Sessions | Start | End | SHA-256 (Prefix) | Status |",
            "| :--- | :--- | :---: | :---: | :---: | :--- | :--- |",
        ]

        for fname, f_info in sorted(manifest_data["files"].items()):
            t_sym = fname.replace("_daily.csv", "").replace(".csv", "")
            sha_pref = f_info["sha256"][:12] + "..."
            lines.append(
                f"| **{t_sym}** | {f_info['role']} | {f_info['row_count']} | {f_info['start_session']} | {f_info['end_session']} | `{sha_pref}` | {f_info['status']} |"
            )

        if manifest_data["quarantined_files"]:
            lines.extend([
                "",
                "## Quarantined & Excluded Files (Zero Silent Repairs)",
                "",
                "| Ticker | Role | Status | Discontinuity Class | Reason |",
                "| :--- | :--- | :--- | :--- | :--- |",
            ])
            for fname, q_info in sorted(manifest_data["quarantined_files"].items()):
                t_sym = fname.replace("_daily.csv", "").replace(".csv", "")
                lines.append(
                    f"| **{t_sym}** | {q_info['role']} | `{q_info['status']}` | {q_info.get('discontinuity_class', 'N/A')} | {q_info.get('exclusion_reason', 'N/A')} |"
                )

        lines.extend([
            "",
            "## Delisted Equities Handling Protocol",
            "",
            "| Ticker | Resolution Date | Resolution Payout | Engine Status |",
            "| :--- | :--- | :--- | :--- |",
            "| **TWTR** | 2022-10-27 | $54.20 USD (Actual) | `CASH_ACQUISITION_PAYOUT` |",
            "| **SIVB** | 2023-03-10 | $0.00 USD (Conservative) | `CONSERVATIVE_STRESS_ASSUMPTION_ZERO_RECOVERY` |",
            "| **SBNY** | 2023-03-10 | $0.00 USD (Conservative) | `CONSERVATIVE_STRESS_ASSUMPTION_ZERO_RECOVERY` |",
            "| **FRC**  | 2023-04-28 | $0.00 USD (Conservative) | `CONSERVATIVE_STRESS_ASSUMPTION_ZERO_RECOVERY` |",
            "",
        ])

        with open(self.audit_md_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify historical data partitions and compile SHA-256 manifest.")
    parser.add_argument("--partition", required=True, help="Path to partition directory to verify.")
    parser.add_argument("--manifest", default=None, help="Path to output manifest JSON.")
    parser.add_argument("--quarantine", default=None, help="Path to quarantine directory.")
    parser.add_argument("--expected-sessions", type=int, default=EXPECTED_NYSE_SESSIONS_2021_2023, help="Expected session count.")
    parser.add_argument("--min-equities", type=int, default=MIN_EQUITIES_ACCEPTED, help="Minimum accepted equities count.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    verifier = PartitionVerifier(
        partition_dir=args.partition,
        manifest_path=args.manifest,
        quarantine_dir=args.quarantine,
        expected_sessions=args.expected_sessions,
        min_equities=args.min_equities,
    )

    result = verifier.verify_partition()
    print(json.dumps(result, indent=2))
    if not result["overall_passed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
