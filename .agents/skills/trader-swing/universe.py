"""Universe management and ticker validation for trader-swing.

Enforces:
1. Strict US-equity ticker validation (1 to 5 uppercase alphabetical characters with optional 1-2 letter dot-separated share class suffix).
2. Deduplication preserving original input order.
3. Clean error and warning reporting for malformed or unsupported symbols.
4. Curated default watchlist of liquid US equities.
5. Explicit survivorship bias disclosure for historical backtesting.
"""

import os
import re
import sys
from typing import List, Optional, Sequence, Set, Tuple

TICKER_REGEX = re.compile(r"^[A-Z]{1,5}(?:\.[A-Z]{1,2})?$")

SURVIVORSHIP_BIAS_DISCLOSURE = (
    "SURVIVORSHIP BIAS NOTICE: The default 50-ticker curated watchlist represents large-cap "
    "US equities active and liquid as of 2026. Backtesting across multi-year historical periods "
    "using a current survivor universe introduces survivorship bias, as companies that went bankrupt, "
    "were acquired, or were delisted during the backtest window are not represented. For production "
    "point-in-time backtesting, use survivorship-free historical universe snapshots."
)

DEFAULT_CURATED_WATCHLIST = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD", "JPM", "UNH",
    "LLY", "XOM", "V", "PG", "JNJ", "HD", "AVGO", "MRK", "COST", "ABBV",
    "CVX", "CRM", "BAC", "WMT", "NFLX", "ADBE", "QCOM", "MCD", "CSCO", "INTC",
    "TXN", "AMAT", "INTU", "IBM", "GE", "CAT", "NOW", "ISRG", "ACN", "AMGN",
    "GS", "MS", "BKNG", "SPGI", "BLK", "MDT", "SYK", "TJX", "VRTX", "REGN"
]


class UniverseError(ValueError):
    """Raised when ticker universe is structurally invalid or empty."""
    pass


def validate_ticker(ticker: str) -> str:
    """Validate and normalize a single ticker symbol.

    Args:
        ticker: Raw ticker string.

    Returns:
        Normalized uppercase ticker string.

    Raises:
        ValueError: If ticker is not a valid 1-5 letter US equity symbol or dot-separated share class.
    """
    if not ticker or not isinstance(ticker, str):
        raise ValueError(f"Ticker must be a non-empty string, got {type(ticker).__name__}.")

    normalized = ticker.strip().upper()
    if not TICKER_REGEX.match(normalized):
        raise ValueError(
            f"Invalid US equity ticker '{ticker}'. Tickers must consist of 1 to 5 alphabetical characters with optional 1-2 letter dot-separated share class suffix."
        )

    return normalized


def normalize_universe(
    tickers: Sequence[str],
    max_size: Optional[int] = None,
    allow_empty: bool = False,
) -> Tuple[List[str], List[str]]:
    """Clean, validate, and deduplicate a sequence of ticker symbols.

    Args:
        tickers: Iterable of ticker strings.
        max_size: Optional upper limit on universe size.
        allow_empty: If True, returns empty list without raising UniverseError.

    Returns:
        Tuple of (valid_deduplicated_tickers, dropped_invalid_tickers_with_reasons).
    """
    if not tickers:
        if allow_empty:
            return [], []
        raise UniverseError("Ticker universe is empty.")

    valid: List[str] = []
    seen: Set[str] = set()
    dropped: List[str] = []

    for raw in tickers:
        try:
            norm = validate_ticker(raw)
            if norm not in seen:
                seen.add(norm)
                valid.append(norm)
                if max_size is not None and len(valid) >= max_size:
                    break
        except ValueError as exc:
            dropped.append(f"Dropped '{raw}': {str(exc)}")

    if not valid and not allow_empty:
        raise UniverseError("No valid US equity tickers remained after normalization.")

    return valid, dropped
