"""Validation routines for market regime detection inputs in trader-swing.

Enforces:
1. Strict candle sequence validation for market index proxies.
2. Incomplete candle exclusion (FORMING and UNKNOWN bars rejected for finalized regime analysis).
3. Structured error handling for missing proxies, insufficient history, and timestamp skew.
"""

from datetime import datetime, timedelta
import os
import sys
from typing import Dict, List, Optional, Sequence

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from market_data_models import CandleStatus, MarketDataError, OHLCVCandle, to_utc, UTC
from market_data_validation import resolve_candle_lifecycle
from indicator_validation import IncompleteCandleError, InsufficientDataError, validate_indicator_candles


class RegimeError(MarketDataError):
    """Base exception for market regime detection errors."""
    pass


class InsufficientRegimeDataError(RegimeError):
    """Raised when available proxy candle data is inadequate to determine a regime."""
    pass


class StaleRegimeDataError(RegimeError):
    """Raised when proxy data is older than acceptable freshness limits."""
    pass


def validate_regime_candles(
    candles: Sequence[OHLCVCandle],
    min_count: int = 20,
    require_complete: bool = True,
    expected_interval: str = "1d",
    current_time: Optional[datetime] = None,
) -> None:
    """Validate a single proxy's candle sequence for regime evaluation.

    Args:
        candles: Sequence of OHLCVCandle objects.
        min_count: Minimum bars required (default: 20 bars).
        require_complete: Must be True to ensure finalized regime determination.
        expected_interval: Expected interval (default: "1d").
        current_time: Optional current/simulation timestamp for lifecycle validation.
    """
    if not candles:
        raise InsufficientRegimeDataError("Proxy candle sequence is empty.")
    if len(candles) < min_count:
        raise InsufficientRegimeDataError(
            f"Insufficient proxy candles for {candles[0].ticker}: required {min_count}, got {len(candles)}."
        )

    # Reuse core indicator validation
    validate_indicator_candles(
        candles,
        min_count=min_count,
        require_complete=require_complete,
        expected_interval=expected_interval,
        current_time=current_time,
    )

    # Independent daily candle lifecycle verification (Fix for NYSE session schedule defect)
    if require_complete and candles:
        for idx, candle in enumerate(candles):
            if candle.interval == "1d":
                resolved = resolve_candle_lifecycle(candle, current_time=current_time)
                if resolved != CandleStatus.COMPLETE:
                    raise IncompleteCandleError(
                        f"Daily candle at index {idx} ({candle.timestamp.isoformat()}) for {candle.ticker} "
                        f"resolves to lifecycle status {resolved.value} as of {current_time or 'current time'}; "
                        f"cannot be accepted as COMPLETE for finalized regime analysis.",
                        ticker=candle.ticker,
                        timestamp=candle.timestamp,
                        status=resolved,
                    )


def validate_proxy_map(
    proxy_map: Dict[str, Sequence[OHLCVCandle]],
    primary_proxy: str = "SPY",
    min_bars: int = 20,
    require_complete: bool = True,
    current_time: Optional[datetime] = None,
) -> List[str]:
    """Validate a collection of market proxies and return any warnings.

    Args:
        proxy_map: Dict mapping ticker (e.g. 'SPY', 'QQQ', 'IWM') to candle sequences.
        primary_proxy: The essential baseline proxy (default: 'SPY').
        min_bars: Minimum bars required per proxy.
        require_complete: Must be True for finalized regime determination.
        current_time: Optional current/simulation timestamp for lifecycle validation.

    Returns:
        List of non-fatal warning strings.
    """
    warnings: List[str] = []

    if not proxy_map:
        raise InsufficientRegimeDataError("No proxy candle data provided.")

    if primary_proxy not in proxy_map or not proxy_map[primary_proxy]:
        raise InsufficientRegimeDataError(
            f"Primary market proxy '{primary_proxy}' is missing from provided proxy data."
        )

    # Validate primary proxy
    validate_regime_candles(
        proxy_map[primary_proxy],
        min_count=min_bars,
        require_complete=require_complete,
        current_time=current_time,
    )

    # Validate secondary proxies
    primary_ts = to_utc(proxy_map[primary_proxy][-1].timestamp)
    for ticker, candles in proxy_map.items():
        if ticker == primary_proxy:
            continue
        try:
            validate_regime_candles(
                candles,
                min_count=min_bars,
                require_complete=require_complete,
                current_time=current_time,
            )
            cand_ts = to_utc(candles[-1].timestamp)
            if abs(cand_ts - primary_ts) > timedelta(days=3):
                warnings.append(
                    f"Timestamp skew between {primary_proxy} ({primary_ts.date()}) and {ticker} ({cand_ts.date()})."
                )
        except (InsufficientRegimeDataError, InsufficientDataError, IncompleteCandleError) as exc:
            warnings.append(f"Secondary proxy {ticker} skipped: {str(exc)}")

    return warnings
