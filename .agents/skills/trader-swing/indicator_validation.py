"""Validation routines for technical indicator candle sequences in trader-swing.

Enforces:
1. Non-empty, chronological candle sequences.
2. Incomplete candle exclusion: calculations strictly reject FORMING and UNKNOWN bars when require_complete=True.
3. Warm-up and minimum data count verification.
4. Consistent ticker and interval validation.
"""

from datetime import datetime
from decimal import Decimal
import os
import sys
from typing import Any, Optional, Sequence

# Ensure local imports work regardless of execution context
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from market_data_models import (
    CandleStatus,
    MarketDataError,
    OHLCVCandle,
    SequenceValidationError,
)
from market_data_validation import validate_candle_sequence


class IndicatorError(MarketDataError):
    """Base exception for all technical indicator calculation and validation errors."""
    pass


class InsufficientDataError(IndicatorError):
    """Raised when the candle sequence does not have enough bars for the requested indicator."""
    def __init__(self, message: str, required_count: int = 0, actual_count: int = 0):
        super().__init__(message)
        self.required_count = required_count
        self.actual_count = actual_count


class IncompleteCandleError(IndicatorError):
    """Raised when an indicator calculation receives FORMING or UNKNOWN candles."""
    def __init__(
        self,
        message: str,
        ticker: Optional[str] = None,
        timestamp: Optional[datetime] = None,
        status: Optional[CandleStatus] = None,
    ):
        super().__init__(message)
        self.ticker = ticker
        self.timestamp = timestamp
        self.status = status


class IndicatorCalculationError(IndicatorError):
    """Raised when an indicator calculation encounters an unrecoverable mathematical error."""
    pass


def validate_indicator_candles(
    candles: Sequence[OHLCVCandle],
    min_count: int = 1,
    require_complete: bool = True,
    expected_interval: Optional[str] = None,
    current_time: Optional[datetime] = None,
) -> None:
    """Validate a sequence of candles for technical indicator calculation.

    Args:
        candles: Chronological sequence of OHLCVCandle objects.
        min_count: Minimum number of bars required.
        require_complete: If True, all candles must have CandleStatus.COMPLETE.
        expected_interval: If specified, all candles must match this interval.
        current_time: Optional current/simulation timestamp for future-skew and lifecycle verification.

    Raises:
        InsufficientDataError: If len(candles) < min_count or candles is empty.
        IncompleteCandleError: If require_complete is True and any candle is not COMPLETE.
        SequenceValidationError: If sequence has duplicate timestamps, out-of-order bars, or mixed tickers/intervals.
        CandleValidationError: If any candle violates price/volume invariants.
    """
    if candles is None or len(candles) == 0:
        raise InsufficientDataError(
            f"Candle sequence is empty. Required at least {min_count} bars.",
            required_count=min_count,
            actual_count=0,
        )

    actual_len = len(candles)
    if actual_len < min_count:
        raise InsufficientDataError(
            f"Insufficient candle data: required at least {min_count} bars, but got {actual_len}.",
            required_count=min_count,
            actual_count=actual_len,
        )

    # Incomplete candle lifecycle check
    if require_complete:
        for idx, candle in enumerate(candles):
            if candle.status != CandleStatus.COMPLETE:
                raise IncompleteCandleError(
                    f"Candle at index {idx} ({candle.timestamp.isoformat()}) for {candle.ticker} "
                    f"is not COMPLETE (status: {candle.status.value}). Finalized indicator calculations "
                    f"strictly require COMPLETE bars.",
                    ticker=candle.ticker,
                    timestamp=candle.timestamp,
                    status=candle.status,
                )

    # Validate sequence continuity and invariants
    validate_candle_sequence(candles, expected_interval=expected_interval, current_time=current_time)
