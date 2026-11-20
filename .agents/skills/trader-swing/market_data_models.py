"""Normalized market data models, exchange clock, and timezone definitions for trader-swing.

Enforces:
1. Strict timezone-aware datetimes normalized to US/Eastern or UTC.
2. Distinction between market event time, received/knowledge time, and processing time.
3. Explicit candle lifecycle states: COMPLETE, FORMING, and UNKNOWN.
4. Pure Decimal financial prices and integer volumes.
5. NYSE trading calendar with holiday and early-close session awareness.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from enum import Enum
import os
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union
import sys

# Ensure accounting imports work regardless of execution context
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from accounting import to_decimal, quantize_cent, AccountingError, InvalidFinancialValue


class MarketDataError(AccountingError):
    """Base exception for all market data errors in trader-swing."""
    pass


class CandleValidationError(MarketDataError):
    """Raised when an individual candle fails intrinsic sanity checks."""
    def __init__(self, message: str, ticker: Optional[str] = None, timestamp: Optional[Any] = None):
        super().__init__(message)
        self.ticker = ticker
        self.timestamp = timestamp


class InvalidPriceError(CandleValidationError, InvalidFinancialValue):
    """Raised when open, high, low, or close prices violate financial invariants."""
    pass


class InvalidVolumeError(CandleValidationError):
    """Raised when volume is negative or non-integer."""
    pass


class TimestampValidationError(CandleValidationError):
    """Raised when timestamps are naive, non-chronological, or future-dated."""
    pass


class SequenceValidationError(MarketDataError):
    """Raised when a time-series of candles contains duplicates, gaps, or out-of-order bars."""
    pass


class FormingCandleError(CandleValidationError):
    """Raised when a forming candle is invalid, stale, or lacks session plausibility."""
    pass


class StaleDataError(MarketDataError):
    """Raised when market data exceeds permissible latency or session staleness thresholds."""
    pass


class ProviderError(MarketDataError):
    """Base exception for external market data provider communication errors."""
    pass


class ProviderUnavailableError(ProviderError):
    """Raised when the provider cannot be reached (DNS, connection refused, 503)."""
    pass


class RateLimitError(ProviderError):
    """Raised when the provider returns HTTP 429 Too Many Requests."""
    pass


class TimeoutError(ProviderError):
    """Raised when a provider request times out."""
    pass


class InvalidResponseError(ProviderError):
    """Raised when provider returns malformed, unparseable, or schema-violating JSON."""
    pass


class NoDataError(ProviderError):
    """Raised when provider returns an empty result for a known or unknown ticker."""
    pass


class CandleStatus(str, Enum):
    """Lifecycle status of an OHLCV candle."""
    COMPLETE = "COMPLETE"
    FORMING = "FORMING"
    UNKNOWN = "UNKNOWN"


class DataFreshness(str, Enum):
    """Data currency classification considering exchange session and provider delay."""
    FRESH = "FRESH"
    DELAYED = "DELAYED"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


class MarketSessionState(str, Enum):
    """Exchange session phase for US equities."""
    PRE_MARKET = "PRE_MARKET"
    REGULAR = "REGULAR"
    POST_MARKET = "POST_MARKET"
    CLOSED = "CLOSED"
    UNKNOWN = "UNKNOWN"


class USEastern(tzinfo):
    """Timezone implementation for America/New_York supporting US Daylight Saving Time.

    DST rules (US Energy Policy Act of 2005):
    - Starts at 2:00 a.m. local time on the second Sunday of March (-4h EDT).
    - Ends at 2:00 a.m. local time on the first Sunday of November (-5h EST).
    """

    def utcoffset(self, dt: Optional[datetime]) -> timedelta:
        return timedelta(hours=-4 if self._is_dst(dt) else -5)

    def dst(self, dt: Optional[datetime]) -> timedelta:
        return timedelta(hours=1 if self._is_dst(dt) else 0)

    def tzname(self, dt: Optional[datetime]) -> str:
        return "EDT" if self._is_dst(dt) else "EST"

    def _is_dst(self, dt: Optional[datetime]) -> bool:
        if dt is None:
            return False
        # Derive year and boundaries
        y = dt.year
        # Second Sunday in March
        d_mar = date(y, 3, 1)
        d_mar += timedelta(days=(6 - d_mar.weekday()) % 7 + 7)
        dst_start = datetime(y, 3, d_mar.day, 2, 0)

        # First Sunday in November
        d_nov = date(y, 11, 1)
        d_nov += timedelta(days=(6 - d_nov.weekday()) % 7)
        dst_end = datetime(y, 11, d_nov.day, 2, 0)

        # Compare naive components
        naive = dt.replace(tzinfo=None) if dt.tzinfo else dt
        return dst_start <= naive < dst_end


EASTERN = USEastern()
UTC = timezone.utc


def to_utc(dt: datetime) -> datetime:
    """Convert any timezone-aware datetime to UTC, or assume Eastern if naive."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=EASTERN)
    return dt.astimezone(UTC)


def to_eastern(dt: datetime) -> datetime:
    """Convert any timezone-aware datetime to US Eastern time."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(EASTERN)


def parse_iso_timestamp(text: str) -> datetime:
    """Parse an ISO 8601 string into a timezone-aware UTC datetime."""
    t = text.strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    dt = datetime.fromisoformat(t)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=EASTERN)
    return dt.astimezone(UTC)


def interval_to_timedelta(interval: str) -> timedelta:
    """Convert standard bar interval notation (1m, 5m, 15m, 1h, 1d) into timedelta."""
    inv = interval.lower().strip()
    if inv == "1m":
        return timedelta(minutes=1)
    elif inv == "5m":
        return timedelta(minutes=5)
    elif inv == "15m":
        return timedelta(minutes=15)
    elif inv == "30m":
        return timedelta(minutes=30)
    elif inv in ("1h", "60m"):
        return timedelta(hours=1)
    elif inv == "1d":
        return timedelta(days=1)
    elif inv == "1wk":
        return timedelta(weeks=1)
    raise ValueError(f"Unsupported market data interval: '{interval}'")


# Standard NYSE Exchange Holidays (confirmed for 2026-2027 for live, 2015-2027 for historical)
# Note: Live market data calendar coverage is confirmed for SUPPORTED_CALENDAR_YEARS (2026-2027).
# Historical backtesting coverage is confirmed for HISTORICAL_SUPPORTED_YEARS (2015-2027).
SUPPORTED_CALENDAR_YEARS = (2026, 2027)
HISTORICAL_SUPPORTED_YEARS = tuple(range(2015, 2028))


def _calculate_easter(year: int) -> date:
    """Meeus/Butcher algorithm to compute Gregorian Easter Sunday."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _get_nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """Return the n-th occurrence of a weekday in a given month (weekday: 0=Mon..6=Sun)."""
    d = date(year, month, 1)
    while d.weekday() != weekday:
        d += timedelta(days=1)
    return d + timedelta(weeks=n - 1)


def _get_last_weekday(year: int, month: int, weekday: int) -> date:
    """Return the last occurrence of a weekday in a given month."""
    if month == 12:
        d = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        d = date(year, month + 1, 1) - timedelta(days=1)
    while d.weekday() != weekday:
        d -= timedelta(days=1)
    return d


def _generate_nyse_calendar(years: Sequence[int]) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Generate official NYSE regular holidays and early closes for given years."""
    h_dict: Dict[str, str] = {}
    ec_dict: Dict[str, str] = {}

    for year in years:
        # 1. New Year's Day (Jan 1)
        nyd = date(year, 1, 1)
        if nyd.weekday() == 6:
            h_dict[date(year, 1, 2).isoformat()] = "New Year's Day (observed)"
        elif nyd.weekday() < 5:
            h_dict[nyd.isoformat()] = "New Year's Day"

        # 2. Martin Luther King, Jr. Day (3rd Mon in Jan)
        h_dict[_get_nth_weekday(year, 1, 0, 3).isoformat()] = "Martin Luther King, Jr. Day"

        # 3. Washington's Birthday (3rd Mon in Feb)
        h_dict[_get_nth_weekday(year, 2, 0, 3).isoformat()] = "Washington's Birthday"

        # 4. Good Friday (Friday before Easter)
        gf = _calculate_easter(year) - timedelta(days=2)
        h_dict[gf.isoformat()] = "Good Friday"

        # 5. Memorial Day (last Mon in May)
        h_dict[_get_last_weekday(year, 5, 0).isoformat()] = "Memorial Day"

        # 6. Juneteenth National Independence Day (June 19, observed since 2022)
        if year >= 2022:
            j19 = date(year, 6, 19)
            if j19.weekday() == 5:
                h_dict[date(year, 6, 18).isoformat()] = "Juneteenth National Independence Day (observed)"
            elif j19.weekday() == 6:
                h_dict[date(year, 6, 20).isoformat()] = "Juneteenth National Independence Day (observed)"
            else:
                h_dict[j19.isoformat()] = "Juneteenth National Independence Day"

        # 7. Independence Day (July 4)
        j4 = date(year, 7, 4)
        if j4.weekday() == 5:
            h_dict[date(year, 7, 3).isoformat()] = "Independence Day (observed)"
        elif j4.weekday() == 6:
            h_dict[date(year, 7, 5).isoformat()] = "Independence Day (observed)"
        else:
            h_dict[j4.isoformat()] = "Independence Day"

        # Early close: Day before Independence Day (July 3) if weekday and not holiday
        j3 = date(year, 7, 3)
        if j3.weekday() < 5 and j3.isoformat() not in h_dict:
            ec_dict[j3.isoformat()] = "Day before Independence Day (1:00 PM close)"

        # 8. Labor Day (1st Mon in Sep)
        h_dict[_get_nth_weekday(year, 9, 0, 1).isoformat()] = "Labor Day"

        # 9. Thanksgiving Day (4th Thu in Nov)
        tg = _get_nth_weekday(year, 11, 3, 4)
        h_dict[tg.isoformat()] = "Thanksgiving Day"

        # Early close: Day after Thanksgiving (Black Friday 1:00 PM close)
        ec_dict[(tg + timedelta(days=1)).isoformat()] = "Day after Thanksgiving (1:00 PM close)"

        # 10. Christmas Day (Dec 25)
        xmas = date(year, 12, 25)
        if xmas.weekday() == 5:
            h_dict[date(year, 12, 24).isoformat()] = "Christmas Day (observed)"
        elif xmas.weekday() == 6:
            h_dict[date(year, 12, 26).isoformat()] = "Christmas Day (observed)"
        else:
            h_dict[xmas.isoformat()] = "Christmas Day"

        # Early close: Christmas Eve (Dec 24) if weekday and not holiday
        xmas_eve = date(year, 12, 24)
        if xmas_eve.weekday() < 5 and xmas_eve.isoformat() not in h_dict:
            ec_dict[xmas_eve.isoformat()] = "Christmas Eve (1:00 PM close)"

        # Special Historical Closures / Days of Mourning
        if year == 2018:
            h_dict["2018-12-05"] = "National Day of Mourning for President George H.W. Bush"
        if year == 2025:
            h_dict["2025-01-09"] = "National Day of Mourning for President Jimmy Carter"

    return h_dict, ec_dict


NYSE_HOLIDAYS, NYSE_EARLY_CLOSE = _generate_nyse_calendar(SUPPORTED_CALENDAR_YEARS)
HISTORICAL_NYSE_HOLIDAYS, HISTORICAL_NYSE_EARLY_CLOSE = _generate_nyse_calendar(HISTORICAL_SUPPORTED_YEARS)


class MarketClock:
    """NYSE market hours, calendar, and trading session resolver."""

    REGULAR_OPEN = time(9, 30)
    REGULAR_CLOSE = time(16, 0)
    EARLY_CLOSE = time(13, 0)
    PRE_MARKET_OPEN = time(4, 0)
    POST_MARKET_CLOSE = time(20, 0)
    SUPPORTED_YEARS = SUPPORTED_CALENDAR_YEARS
    HISTORICAL_SUPPORTED_YEARS = HISTORICAL_SUPPORTED_YEARS

    @classmethod
    def is_year_supported(cls, year: int) -> bool:
        """Check whether the calendar year is within confirmed live-trading NYSE holiday coverage (2026-2027)."""
        return year in cls.SUPPORTED_YEARS

    @classmethod
    def is_historical_year_supported(cls, year: int) -> bool:
        """Check whether the calendar year is within confirmed historical NYSE holiday coverage (2015-2027)."""
        return year in cls.HISTORICAL_SUPPORTED_YEARS

    @classmethod
    def is_trading_day(cls, d: Union[date, datetime]) -> Tuple[bool, str]:
        """Check if date is a regular NYSE trading day (excludes weekends and holidays).

        Note:
        1. Unscheduled emergency market closures are not guaranteed to be covered.
        2. Dates outside SUPPORTED_YEARS (2026-2027) cannot be confirmed and return False (unverified).
        """
        dt_date = d.date() if isinstance(d, datetime) else d
        if dt_date.weekday() >= 5:
            return False, "Weekend"
        if not cls.is_year_supported(dt_date.year):
            return False, f"Unsupported calendar year: {dt_date.year} outside confirmed coverage (2026-2027)"
        iso_str = dt_date.isoformat()
        if iso_str in NYSE_HOLIDAYS:
            return False, f"Holiday: {NYSE_HOLIDAYS[iso_str]}"
        return True, "Trading Day"

    @classmethod
    def is_historical_trading_day(cls, d: Union[date, datetime]) -> Tuple[bool, str]:
        """Check if date is a regular NYSE trading day within confirmed historical coverage (2015-2027)."""
        dt_date = d.date() if isinstance(d, datetime) else d
        if dt_date.weekday() >= 5:
            return False, "Weekend"
        if not cls.is_historical_year_supported(dt_date.year):
            return False, f"Unsupported calendar year: {dt_date.year} outside confirmed coverage (2015-2027)"
        iso_str = dt_date.isoformat()
        if iso_str in HISTORICAL_NYSE_HOLIDAYS:
            return False, f"Holiday: {HISTORICAL_NYSE_HOLIDAYS[iso_str]}"
        return True, "Trading Day"

    @classmethod
    def get_session(cls, d: Union[date, datetime]) -> Optional[Tuple[time, time]]:
        """Return (open_time, close_time) in Eastern time, or None if market closed or unverified."""
        dt_date = d.date() if isinstance(d, datetime) else d
        if not cls.is_year_supported(dt_date.year):
            return None
        is_open, _ = cls.is_trading_day(dt_date)
        if not is_open:
            return None
        iso_str = dt_date.isoformat()
        close_t = cls.EARLY_CLOSE if iso_str in NYSE_EARLY_CLOSE else cls.REGULAR_CLOSE
        return cls.REGULAR_OPEN, close_t

    @classmethod
    def get_historical_session(cls, d: Union[date, datetime]) -> Optional[Tuple[time, time]]:
        """Return (open_time, close_time) in Eastern time for historical date (2015-2027), or None if market closed or unverified."""
        dt_date = d.date() if isinstance(d, datetime) else d
        if not cls.is_historical_year_supported(dt_date.year):
            return None
        is_open, _ = cls.is_historical_trading_day(dt_date)
        if not is_open:
            return None
        iso_str = dt_date.isoformat()
        close_t = cls.EARLY_CLOSE if iso_str in HISTORICAL_NYSE_EARLY_CLOSE else cls.REGULAR_CLOSE
        return cls.REGULAR_OPEN, close_t

    @classmethod
    def get_session_state(cls, dt: datetime) -> MarketSessionState:
        """Determine whether the specified timestamp falls in PRE_MARKET, REGULAR, POST_MARKET, CLOSED, or UNKNOWN."""
        et = to_eastern(dt)
        if not cls.is_year_supported(et.year):
            return MarketSessionState.UNKNOWN
        sess = cls.get_session(et)
        if not sess:
            return MarketSessionState.CLOSED
        t = et.time()
        open_t, close_t = sess
        if t < cls.PRE_MARKET_OPEN or t >= cls.POST_MARKET_CLOSE:
            return MarketSessionState.CLOSED
        elif t < open_t:
            return MarketSessionState.PRE_MARKET
        elif open_t <= t < close_t:
            return MarketSessionState.REGULAR
        else:
            return MarketSessionState.POST_MARKET

    @classmethod
    def is_market_open(cls, dt: datetime) -> bool:
        """Check if regular US market trading is in session."""
        return cls.get_session_state(dt) == MarketSessionState.REGULAR

    @classmethod
    def get_last_completed_session(cls, as_of: datetime) -> Optional[Tuple[date, time, time]]:
        """Find the most recent completed regular trading session as of a given timestamp.

        Returns (session_date, open_time, close_time) in Eastern time, or None if
        the calendar cannot determine it reliably (e.g. outside supported calendar years).
        """
        et = to_eastern(as_of)
        if not cls.is_year_supported(et.year):
            return None

        curr_date = et.date()

        # If today is a trading day and current Eastern time is at or after scheduled regular close,
        # today's regular session has completed.
        sess = cls.get_session(curr_date)
        if sess is not None:
            open_t, close_t = sess
            if et.time() >= close_t:
                return curr_date, open_t, close_t

        # Otherwise, step backward day-by-day for the latest completed trading session
        check_date = curr_date - timedelta(days=1)
        for _ in range(30):
            if not cls.is_year_supported(check_date.year):
                return None
            sess = cls.get_session(check_date)
            if sess is not None:
                return check_date, sess[0], sess[1]
            check_date -= timedelta(days=1)

        return None


@dataclass
class OHLCVCandle:
    """Normalized, validated, immutable candle representation."""
    ticker: str
    interval: str
    timestamp: datetime  # Timezone-aware bar start event time
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    status: CandleStatus = CandleStatus.COMPLETE
    source: str = "yahoo"
    provider_event_timestamp: Optional[datetime] = None
    received_timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    processing_timestamp: Optional[datetime] = None
    is_delayed: bool = True
    bar_end_timestamp: Optional[datetime] = None

    def __post_init__(self):
        self.ticker = self.ticker.upper().strip()
        self.interval = self.interval.lower().strip()
        if self.timestamp.tzinfo is None:
            self.timestamp = self.timestamp.replace(tzinfo=UTC)
        if self.received_timestamp.tzinfo is None:
            self.received_timestamp = self.received_timestamp.replace(tzinfo=UTC)
        if self.provider_event_timestamp is not None and self.provider_event_timestamp.tzinfo is None:
            self.provider_event_timestamp = self.provider_event_timestamp.replace(tzinfo=UTC)
        if self.processing_timestamp is not None and self.processing_timestamp.tzinfo is None:
            self.processing_timestamp = self.processing_timestamp.replace(tzinfo=UTC)

        try:
            self.open = to_decimal(self.open)
            self.high = to_decimal(self.high)
            self.low = to_decimal(self.low)
            self.close = to_decimal(self.close)
        except InvalidFinancialValue as e:
            raise InvalidPriceError(str(e), ticker=self.ticker, timestamp=self.timestamp) from e

        if not isinstance(self.volume, int):
            self.volume = int(self.volume)

        if not isinstance(self.status, CandleStatus):
            self.status = CandleStatus(self.status)

        if self.bar_end_timestamp is None:
            if self.interval == "1d":
                sess = MarketClock.get_session(to_eastern(self.timestamp))
                if sess is None and MarketClock.is_historical_year_supported(to_eastern(self.timestamp).year):
                    sess = MarketClock.get_historical_session(to_eastern(self.timestamp))
                if sess is not None:
                    close_t = sess[1]
                    self.bar_end_timestamp = datetime.combine(
                        to_eastern(self.timestamp).date(), close_t, tzinfo=EASTERN
                    ).astimezone(UTC)
                else:
                    self.bar_end_timestamp = self.timestamp + timedelta(days=1)
            else:
                try:
                    self.bar_end_timestamp = self.timestamp + interval_to_timedelta(self.interval)
                except ValueError:
                    self.bar_end_timestamp = self.timestamp

    def to_dict(self) -> Dict[str, Any]:
        """Convert candle to dictionary with ISO-formatted timestamps and string Decimals."""
        return {
            "ticker": self.ticker,
            "interval": self.interval,
            "timestamp": self.timestamp.isoformat(),
            "open": str(self.open),
            "high": str(self.high),
            "low": str(self.low),
            "close": str(self.close),
            "volume": self.volume,
            "status": self.status.value,
            "source": self.source,
            "provider_event_timestamp": self.provider_event_timestamp.isoformat() if self.provider_event_timestamp else None,
            "received_timestamp": self.received_timestamp.isoformat(),
            "processing_timestamp": self.processing_timestamp.isoformat() if self.processing_timestamp else None,
            "is_delayed": self.is_delayed,
            "bar_end_timestamp": self.bar_end_timestamp.isoformat() if self.bar_end_timestamp else None,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OHLCVCandle":
        """Reconstruct candle from serialized dictionary."""
        return cls(
            ticker=data["ticker"],
            interval=data["interval"],
            timestamp=parse_iso_timestamp(data["timestamp"]),
            open=Decimal(str(data["open"])),
            high=Decimal(str(data["high"])),
            low=Decimal(str(data["low"])),
            close=Decimal(str(data["close"])),
            volume=int(data["volume"]),
            status=CandleStatus(data.get("status", "COMPLETE")),
            source=data.get("source", "yahoo"),
            provider_event_timestamp=parse_iso_timestamp(data["provider_event_timestamp"]) if data.get("provider_event_timestamp") else None,
            received_timestamp=parse_iso_timestamp(data["received_timestamp"]) if data.get("received_timestamp") else datetime.now(UTC),
            processing_timestamp=parse_iso_timestamp(data["processing_timestamp"]) if data.get("processing_timestamp") else None,
            is_delayed=bool(data.get("is_delayed", True)),
            bar_end_timestamp=parse_iso_timestamp(data["bar_end_timestamp"]) if data.get("bar_end_timestamp") else None,
        )


@dataclass
class MarketDataStatus:
    """Health, latency, and freshness metadata for a ticker and interval."""
    ticker: str
    interval: str
    freshness: DataFreshness
    latest_bar_timestamp: Optional[datetime]
    latest_bar_status: Optional[CandleStatus]
    source: str
    is_delayed: bool
    as_of: datetime = field(default_factory=lambda: datetime.now(UTC))
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "interval": self.interval,
            "freshness": self.freshness.value,
            "latest_bar_timestamp": self.latest_bar_timestamp.isoformat() if self.latest_bar_timestamp else None,
            "latest_bar_status": self.latest_bar_status.value if self.latest_bar_status else None,
            "source": self.source,
            "is_delayed": self.is_delayed,
            "as_of": self.as_of.isoformat(),
            "message": self.message,
        }
