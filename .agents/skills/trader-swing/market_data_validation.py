"""Strict candle, sequence, and provider validation for trader-swing market data.

Enforces:
1. Pure Decimal sanity on all prices (> 0, non-NaN, finite).
2. Non-negative volume (int >= 0).
3. Intrinsic candle geometry: low <= open <= high, low <= close <= high, high >= low.
4. Timezone-aware timestamps with chronological monotonicity.
5. Prevention of future-dated bars, duplicate timestamps, and mixed intervals.
6. Explicit verification of forming-candle preconditions.
7. Structured exception hierarchy distinguishing provider, network, and data errors.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
import sys
from typing import Any, Dict, List, Optional, Sequence

# Ensure local imports work regardless of execution context
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from accounting import AccountingError, InvalidFinancialValue
from market_data_models import (
    CandleStatus,
    DataFreshness,
    MarketClock,
    MarketSessionState,
    OHLCVCandle,
    interval_to_timedelta,
    to_utc,
    to_eastern,
    UTC,
    EASTERN,
    MarketDataError,
    CandleValidationError,
    InvalidPriceError,
    InvalidVolumeError,
    TimestampValidationError,
    SequenceValidationError,
    FormingCandleError,
    StaleDataError,
    ProviderError,
    ProviderUnavailableError,
    RateLimitError,
    TimeoutError,
    InvalidResponseError,
    NoDataError,
)


def validate_single_candle(
    candle: OHLCVCandle,
    current_time: Optional[datetime] = None,
    future_skew_tolerance: timedelta = timedelta(seconds=60),
) -> None:
    """Validate a single OHLCV candle against all financial and structural invariants.

    Raises specific CandleValidationError subclasses upon any invariant breach.
    """
    sym = candle.ticker
    ts = candle.timestamp

    # 1. Ticker and interval validation
    if not sym or not isinstance(sym, str) or not sym.strip():
        raise CandleValidationError("Candle ticker must be a non-empty string.", ticker=sym, timestamp=ts)
    if not candle.interval or not isinstance(candle.interval, str):
        raise CandleValidationError("Candle interval must be a non-empty string.", ticker=sym, timestamp=ts)

    # 2. Timezone-awareness checks
    if ts.tzinfo is None:
        raise TimestampValidationError(
            f"Candle timestamp for {sym} at {ts} is timezone-naive. Timezone-aware timestamp required.",
            ticker=sym,
            timestamp=ts,
        )
    if candle.received_timestamp.tzinfo is None:
        raise TimestampValidationError(
            f"Candle received_timestamp for {sym} is timezone-naive.",
            ticker=sym,
            timestamp=ts,
        )

    # 3. Future-timestamp prevention
    now_utc = to_utc(current_time) if current_time is not None else datetime.now(UTC)
    cand_utc = to_utc(ts)
    if cand_utc > now_utc + future_skew_tolerance:
        raise TimestampValidationError(
            f"Candle timestamp {cand_utc.isoformat()} is in the future relative to current time {now_utc.isoformat()} (tolerance: {future_skew_tolerance}).",
            ticker=sym,
            timestamp=ts,
        )

    # 4. Price positivity and finiteness
    for name, px in [("open", candle.open), ("high", candle.high), ("low", candle.low), ("close", candle.close)]:
        if px is None:
            raise InvalidPriceError(f"Candle {name} price for {sym} cannot be None.", ticker=sym, timestamp=ts)
        if not isinstance(px, Decimal):
            raise InvalidPriceError(f"Candle {name} price for {sym} must be Decimal, got {type(px).__name__}.", ticker=sym, timestamp=ts)
        if px.is_nan() or px.is_infinite():
            raise InvalidPriceError(f"Candle {name} price for {sym} must be finite, got {px}.", ticker=sym, timestamp=ts)
        if px <= Decimal("0.00"):
            raise InvalidPriceError(f"Candle {name} price for {sym} must be strictly positive, got {px}.", ticker=sym, timestamp=ts)

    # 5. Volume non-negativity
    if candle.volume is None or candle.volume < 0:
        raise InvalidVolumeError(f"Candle volume for {sym} must be non-negative integer, got {candle.volume}.", ticker=sym, timestamp=ts)

    # 6. Intrinsic price bounds
    if candle.high < candle.low:
        raise InvalidPriceError(
            f"Candle high ({candle.high}) is lower than low ({candle.low}) for {sym} at {ts}.",
            ticker=sym,
            timestamp=ts,
        )
    if candle.open < candle.low or candle.open > candle.high:
        raise InvalidPriceError(
            f"Candle open ({candle.open}) is outside [low: {candle.low}, high: {candle.high}] for {sym} at {ts}.",
            ticker=sym,
            timestamp=ts,
        )
    if candle.close < candle.low or candle.close > candle.high:
        raise InvalidPriceError(
            f"Candle close ({candle.close}) is outside [low: {candle.low}, high: {candle.high}] for {sym} at {ts}.",
            ticker=sym,
            timestamp=ts,
        )

    # 7. Lifecycle status validation
    if not isinstance(candle.status, CandleStatus):
        raise CandleValidationError(
            f"Candle status must be an instance of CandleStatus enum, got {candle.status}.",
            ticker=sym,
            timestamp=ts,
        )


def validate_candle_sequence(
    candles: Sequence[OHLCVCandle],
    expected_interval: Optional[str] = None,
    current_time: Optional[datetime] = None,
) -> None:
    """Validate a chronological sequence of candles for continuity, ordering, and consistency.

    Raises SequenceValidationError or CandleValidationError upon any defect.
    """
    if not candles:
        return

    first = candles[0]
    expected_ticker = first.ticker
    target_interval = (expected_interval or first.interval).lower().strip()

    seen_timestamps = set()
    prev_ts: Optional[datetime] = None

    for i, candle in enumerate(candles):
        # Validate individual candle integrity
        validate_single_candle(candle, current_time=current_time)

        # Consistent ticker check
        if candle.ticker != expected_ticker:
            raise SequenceValidationError(
                f"Mixed tickers in sequence: expected {expected_ticker}, found {candle.ticker} at index {i}."
            )

        # Consistent interval check
        if candle.interval.lower().strip() != target_interval:
            raise SequenceValidationError(
                f"Mixed intervals in sequence for {expected_ticker}: expected {target_interval}, found {candle.interval} at index {i}."
            )

        # Strict chronological ordering & duplicate prevention
        curr_ts = to_utc(candle.timestamp)
        if curr_ts in seen_timestamps:
            raise SequenceValidationError(
                f"Duplicate timestamp {curr_ts.isoformat()} found in candle sequence for {expected_ticker} at index {i}."
            )
        seen_timestamps.add(curr_ts)

        if prev_ts is not None and curr_ts <= prev_ts:
            raise SequenceValidationError(
                f"Non-chronological order for {expected_ticker}: bar at index {i} ({curr_ts.isoformat()}) "
                f"does not follow previous bar ({prev_ts.isoformat()})."
            )
        prev_ts = curr_ts

        # Forming candle position constraint: only the terminal bar can be FORMING
        if candle.status == CandleStatus.FORMING and i < len(candles) - 1:
            raise SequenceValidationError(
                f"Intermediary candle at index {i} ({curr_ts.isoformat()}) is marked FORMING; "
                f"only the final bar in a sequence may be FORMING."
            )


def resolve_candle_lifecycle(
    candidate: OHLCVCandle,
    current_time: Optional[datetime] = None,
) -> CandleStatus:
    """Resolve the structured lifecycle status (FORMING, COMPLETE, UNKNOWN) for a candidate candle.

    Requirements:
    1. Must pass all single-candle structural invariant checks.
    2. Daily candles (1d):
       - Before regular market open (now < 9:30 AM Eastern on session date): UNKNOWN.
         Pre-market indicative quotes must never be classified as COMPLETE or FORMING.
       - During active regular trading (9:30 AM to 4:00 PM Eastern, or 1:00 PM early close): FORMING.
       - At or after scheduled regular session close: COMPLETE, provided the bar matches a confirmed trading day.
       - On non-trading days (weekends, holidays, unsupported calendar years):
         * A candle from a past completed session remains COMPLETE.
         * A candle stamped for a non-trading day is UNKNOWN.
    3. Intraday intervals (1m, 5m, 15m, 30m, 1h):
       - If bar duration has fully elapsed (now >= cand_end): COMPLETE.
       - If currently active window (cand_start <= now < cand_end) and market/extended session active: FORMING.
       - If session closed or future: UNKNOWN.
    4. Weekly intervals (1wk):
       - If Friday regular session close of candidate week has elapsed: COMPLETE.
       - If currently active week before Friday close: FORMING.
       - Otherwise: UNKNOWN.
    """
    now_utc = to_utc(current_time) if current_time is not None else datetime.now(UTC)
    try:
        validate_single_candle(candidate, current_time=now_utc)
    except CandleValidationError:
        return CandleStatus.UNKNOWN

    cand_start = to_utc(candidate.timestamp)
    now_et = to_eastern(now_utc)
    cand_et = to_eastern(cand_start)

    # 1. Daily candle session-aware lifecycle logic (Fix A)
    if candidate.interval == "1d":
        # Candidate daily bar timestamp represents its session date
        if cand_et.date() > now_et.date():
            return CandleStatus.UNKNOWN

        # Case A: Historical daily candle (session date strictly before current date)
        if cand_et.date() < now_et.date():
            sess = MarketClock.get_historical_session(cand_et.date())
            if sess is not None:
                # The regular session for this past date has fully concluded
                return CandleStatus.COMPLETE
            else:
                # Non-trading day historical observation (weekend/holiday/unsupported year)
                return CandleStatus.UNKNOWN

        # Case B: Current calendar date (cand_et.date() == now_et.date())
        sess = MarketClock.get_historical_session(now_et.date()) or MarketClock.get_session(now_et.date())
        if sess is None:
            # Current date is a non-trading day (weekend, holiday, unsupported year)
            # A daily regular session bar cannot exist, form, or complete today
            return CandleStatus.UNKNOWN

        open_t, close_t = sess
        sess_open_dt = datetime.combine(now_et.date(), open_t, tzinfo=EASTERN)
        sess_close_dt = datetime.combine(now_et.date(), close_t, tzinfo=EASTERN)

        # Before regular session opens (pre-market): regular daily bar is UNKNOWN (not started)
        if now_et < sess_open_dt:
            return CandleStatus.UNKNOWN

        # During regular trading hours: regular daily bar is actively FORMING
        if sess_open_dt <= now_et < sess_close_dt:
            return CandleStatus.FORMING

        # At or after regular session close: regular daily bar is COMPLETE
        if now_et >= sess_close_dt:
            return CandleStatus.COMPLETE

        return CandleStatus.UNKNOWN

    # 2. Weekly interval logic
    if candidate.interval == "1wk":
        monday = cand_et.date() - timedelta(days=cand_et.weekday())
        friday = monday + timedelta(days=4)
        if cand_start > now_utc:
            return CandleStatus.UNKNOWN
        fri_sess = MarketClock.get_historical_session(friday) or MarketClock.get_session(friday)
        close_t = fri_sess[1] if fri_sess else MarketClock.REGULAR_CLOSE
        fri_close_dt = datetime.combine(friday, close_t, tzinfo=EASTERN)
        if now_et >= fri_close_dt:
            return CandleStatus.COMPLETE
        elif cand_start <= now_utc < fri_close_dt:
            return CandleStatus.FORMING
        return CandleStatus.UNKNOWN

    # 3. Intraday intervals (1m, 5m, 15m, 30m, 1h, etc.) - preserved lifecycle
    try:
        duration = interval_to_timedelta(candidate.interval)
    except ValueError:
        return CandleStatus.UNKNOWN

    cand_end = cand_start + duration

    if cand_start > now_utc:
        return CandleStatus.UNKNOWN

    session_state = MarketClock.get_session_state(now_utc)
    if cand_start <= now_utc < cand_end:
        if session_state in (MarketSessionState.REGULAR, MarketSessionState.PRE_MARKET, MarketSessionState.POST_MARKET):
            return CandleStatus.FORMING
        else:
            # When market session is closed, candidate falls back to complete
            return CandleStatus.COMPLETE

    if now_utc >= cand_end:
        return CandleStatus.COMPLETE

    return CandleStatus.UNKNOWN


def verify_forming_candle(
    candidate: OHLCVCandle,
    current_time: Optional[datetime] = None,
) -> bool:
    """Convenience predicate checking whether candidate resolves to CandleStatus.FORMING."""
    return resolve_candle_lifecycle(candidate, current_time=current_time) == CandleStatus.FORMING
