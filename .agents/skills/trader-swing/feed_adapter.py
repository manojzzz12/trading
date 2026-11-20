"""Isolated market data feed adapter and readiness evaluator for trader-swing.

Phase 6.2B — Step 3: Feed Adapter & Readiness Evaluator

Responsibilities:
1. Proxy data:
   - Evaluates SPY, QQQ, and IWM.
   - SPY is mandatory: requires at least 200 valid completed daily bars.
   - Missing or insufficient SPY produces a HARD_FAIL.
   - QQQ and IWM are optional secondaries: excluded with explicit warnings if missing or insufficient (<20 bars).
2. Stock universe:
   - Evaluates requested candidate stocks.
   - Requires at least 50 valid completed bars.
   - Excludes stocks with missing latest completed-session data.
   - Reports exclusions and reasons per ticker.
3. Candle validation:
   - Rejects forming, incomplete, unknown, stale, or invalid daily candles.
   - Validates exchange sessions, holidays, early closes, and timestamps via existing calendar/lifecycle logic.
   - Never fabricates, interpolates, silently repairs, or forward-fills missing candles.
   - Enforces SPLIT_ADJUSTED price basis; rejects mixed adjustment basis.
4. Structured readiness output:
   - FeedReadinessResult with status, validated proxy/stock candle maps, included/excluded tickers,
     bar counts, warnings, failure reasons, and provider disclosures.
5. Cache isolation:
   - Operates in-memory; never reads, opens, modifies, or writes to default market_data.db.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from enum import Enum
import logging
import os
import sys
import time as time_mod
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

# Ensure local imports work regardless of execution context
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from market_data_models import (
    CandleStatus,
    DataFreshness,
    EASTERN,
    MarketClock,
    MarketDataError,
    OHLCVCandle,
    parse_iso_timestamp,
    to_eastern,
    to_utc,
    UTC,
)
from market_data_validation import (
    CandleValidationError,
    InvalidPriceError,
    InvalidResponseError,
    InvalidVolumeError,
    NoDataError,
    ProviderError,
    ProviderUnavailableError,
    RateLimitError,
    SequenceValidationError,
    StaleDataError,
    TimeoutError,
    resolve_candle_lifecycle,
    validate_candle_sequence,
    validate_single_candle,
)
from indicator_validation import IncompleteCandleError
from providers.base import MarketDataProvider

logger = logging.getLogger(__name__)


__all__ = [
    "ReadinessStatus",
    "FeedAdapterError",
    "HardReadinessFailureError",
    "IncompatibleAdjustmentBasisError",
    "UnconsolidatedVolumeError",
    "StockExclusion",
    "TickerSummary",
    "FeedReadinessResult",
    "MarketDataReadinessEvaluator",
    "MarketDataFeedAdapter",
]


class ReadinessStatus(Enum):
    """Overall market data readiness status for strategy execution."""
    READY = "READY"
    HARD_FAIL = "HARD_FAIL"
    NOT_READY = "NOT_READY"


class FeedAdapterError(MarketDataError):
    """Base exception for feed adapter and readiness evaluation errors."""
    pass


class HardReadinessFailureError(FeedAdapterError):
    """Raised when hard readiness preconditions fail (e.g. missing primary proxy SPY)."""
    pass


class IncompatibleAdjustmentBasisError(FeedAdapterError):
    """Raised when market data has an incompatible or mixed price adjustment basis."""
    pass


class UnconsolidatedVolumeError(FeedAdapterError):
    """Raised when candles contain unconsolidated volume (e.g. single-exchange IEX) without verified composite tape volume."""
    pass


@dataclass
class StockExclusion:
    """Details of a candidate stock excluded from the active universe."""
    ticker: str
    reason: str
    bar_count: int = 0
    latest_candle_date: Optional[date] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "reason": self.reason,
            "bar_count": self.bar_count,
            "latest_candle_date": self.latest_candle_date.isoformat() if self.latest_candle_date else None,
        }


@dataclass
class TickerSummary:
    """Per-ticker summary of validated market data coverage."""
    ticker: str
    bar_count: int
    first_candle_date: Optional[date]
    latest_candle_date: Optional[date]
    is_proxy: bool
    status: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "bar_count": self.bar_count,
            "first_candle_date": self.first_candle_date.isoformat() if self.first_candle_date else None,
            "latest_candle_date": self.latest_candle_date.isoformat() if self.latest_candle_date else None,
            "is_proxy": self.is_proxy,
            "status": self.status,
        }


@dataclass
class FeedReadinessResult:
    """Comprehensive, structured evaluation result of market data feed readiness."""
    is_ready: bool
    status: ReadinessStatus
    latest_completed_session: Optional[date]
    proxy_candles: Dict[str, List[OHLCVCandle]] = field(default_factory=dict)
    stock_candles: Dict[str, List[OHLCVCandle]] = field(default_factory=dict)
    included_tickers: List[str] = field(default_factory=list)
    excluded_tickers: List[StockExclusion] = field(default_factory=list)
    ticker_summaries: Dict[str, TickerSummary] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    failure_reasons: List[str] = field(default_factory=list)
    provider_metadata: Dict[str, Any] = field(default_factory=dict)
    data_freshness: str = "UNKNOWN"
    evaluation_time: Optional[datetime] = None

    def __getitem__(self, key: str) -> Any:
        """Provide dictionary subscription compatibility for existing test assertions."""
        if key == "is_ready":
            return self.is_ready
        elif key == "status":
            return self.status.value
        elif key == "reason":
            return "; ".join(self.failure_reasons) if self.failure_reasons else ""
        elif key == "warnings":
            return self.warnings
        elif key in ("excluded_stocks", "excluded_tickers"):
            return [{"ticker": e.ticker, "reason": e.reason, "bar_count": e.bar_count} for e in self.excluded_tickers]
        elif key in ("proxy_candles", "proxy_map"):
            return self.proxy_candles
        elif key in ("stock_candles", "universe_map"):
            return self.stock_candles
        elif key == "latest_completed_session":
            return self.latest_completed_session
        elif key == "included_tickers":
            return self.included_tickers
        elif key == "failure_reasons":
            return self.failure_reasons
        elif key == "provider_metadata":
            return self.provider_metadata
        elif key == "data_freshness":
            return self.data_freshness
        elif key == "ticker_summaries":
            return self.ticker_summaries
        raise KeyError(f"FeedReadinessResult has no key '{key}'.")

    def __contains__(self, key: str) -> bool:
        return key in {
            "is_ready", "status", "reason", "warnings", "excluded_stocks",
            "excluded_tickers", "proxy_candles", "proxy_map", "stock_candles",
            "universe_map", "latest_completed_session", "included_tickers",
            "failure_reasons", "provider_metadata", "data_freshness", "ticker_summaries"
        }

    def to_dict(self) -> Dict[str, Any]:
        """Serialize evaluation result to dictionary representation."""
        return {
            "is_ready": self.is_ready,
            "status": self.status.value,
            "latest_completed_session": self.latest_completed_session.isoformat() if self.latest_completed_session else None,
            "included_tickers": list(self.included_tickers),
            "excluded_tickers": [e.to_dict() for e in self.excluded_tickers],
            "proxy_counts": {k: len(v) for k, v in self.proxy_candles.items()},
            "stock_counts": {k: len(v) for k, v in self.stock_candles.items()},
            "ticker_summaries": {k: v.to_dict() for k, v in self.ticker_summaries.items()},
            "warnings": list(self.warnings),
            "failure_reasons": list(self.failure_reasons),
            "data_freshness": self.data_freshness,
            "provider_metadata": self.provider_metadata,
            "evaluation_time": self.evaluation_time.isoformat() if self.evaluation_time else None,
        }


class MarketDataReadinessEvaluator:
    """Evaluates the readiness and integrity of market data inputs for trading execution."""

    MANDATORY_PRIMARY_PROXY: str = "SPY"
    SECONDARY_PROXIES: Tuple[str, ...] = ("QQQ", "IWM")
    MIN_SPY_BARS: int = 200
    MIN_SECONDARY_PROXY_BARS: int = 20
    MIN_STOCK_BARS: int = 50
    EXPECTED_INTERVAL: str = "1d"
    EXPECTED_ADJUSTMENT_TYPE: str = "SPLIT_ADJUSTED"

    def __init__(self, clock: Optional[MarketClock] = None):
        self.clock = clock or MarketClock()

    def get_latest_completed_session(self, as_of: datetime) -> Optional[date]:
        """Find the most recent regular completed trading session as of as_of datetime."""
        et = to_eastern(as_of)
        curr_date = et.date()

        # Check today's regular session
        sess = self.clock.get_session(curr_date)
        if sess is None and self.clock.is_historical_year_supported(curr_date.year):
            sess = self.clock.get_historical_session(curr_date)

        if sess is not None:
            open_t, close_t = sess
            # If Eastern time is at or after scheduled regular session close, today is completed
            if et.time() >= close_t:
                return curr_date

        # Otherwise, step backward day-by-day for the latest completed trading day
        check_date = curr_date - timedelta(days=1)
        for _ in range(30):
            s = self.clock.get_session(check_date)
            if s is None and self.clock.is_historical_year_supported(check_date.year):
                s = self.clock.get_historical_session(check_date)
            if s is not None:
                return check_date
            check_date -= timedelta(days=1)

        return None

    def validate_candle_series(
        self,
        candles: Sequence[OHLCVCandle],
        ticker: str,
        current_time: Optional[datetime] = None,
        expected_adjustment: str = EXPECTED_ADJUSTMENT_TYPE,
    ) -> None:
        """Validate candle sequence integrity, chronological ordering, and complete lifecycle.

        Raises:
            CandleValidationError / SequenceValidationError / IncompleteCandleError on defects.
        """
        if not candles:
            raise SequenceValidationError(f"Candle sequence for {ticker} is empty.")

        now_utc = to_utc(current_time) if current_time is not None else datetime.now(UTC)

        # 1. Structural sequence validation
        validate_candle_sequence(candles, expected_interval=self.EXPECTED_INTERVAL, current_time=now_utc)

        # 2. Check for mixed adjustment types or unadjusted series
        for i, c in enumerate(candles):
            adj_type = getattr(c, "price_adjustment_type", None)
            if adj_type is not None and adj_type != expected_adjustment:
                raise IncompatibleAdjustmentBasisError(
                    f"Candle at index {i} for {ticker} declares adjustment '{adj_type}'; "
                    f"expected '{expected_adjustment}'. Mixed price adjustments forbidden."
                )

        # 3. Independent daily candle lifecycle verification (reject forming/unknown/weekend/holiday)
        for idx, candle in enumerate(candles):
            if candle.status != CandleStatus.COMPLETE:
                raise IncompleteCandleError(
                    f"Daily candle at index {idx} ({candle.timestamp.isoformat()}) for {ticker} "
                    f"has candle status '{candle.status.value}'; only COMPLETE candles are accepted for swing strategy execution.",
                    ticker=ticker,
                    timestamp=candle.timestamp,
                    status=candle.status,
                )
            if candle.interval == "1d":
                resolved = resolve_candle_lifecycle(candle, current_time=now_utc)
                if resolved != CandleStatus.COMPLETE:
                    raise IncompleteCandleError(
                        f"Daily candle at index {idx} ({candle.timestamp.isoformat()}) for {ticker} "
                        f"resolves to lifecycle status {resolved.value} as of {now_utc.isoformat()}; "
                        f"only COMPLETE candles are accepted for swing strategy execution.",
                        ticker=ticker,
                        timestamp=candle.timestamp,
                        status=resolved,
                    )

        # 4. Mandatory Volume Provenance & Consolidated Volume Guard
        for i, c in enumerate(candles):
            if getattr(c, "is_consolidated_volume", True) is False:
                raise UnconsolidatedVolumeError(
                    f"Candle at index {i} for {ticker} declares is_consolidated_volume=False (source: '{getattr(c, 'source', 'unknown')}'). "
                    "Unconsolidated volume (e.g. single-exchange IEX) cannot be ingested into the strategy engine. "
                    "Data must be merged through DualSourceAggregator with verified consolidated volume."
                )

    def evaluate_readiness(
        self,
        proxy_candles_map: Dict[str, Sequence[OHLCVCandle]],
        stock_candles_map: Optional[Dict[str, Sequence[OHLCVCandle]]] = None,
        current_time: Optional[datetime] = None,
        provider_metadata: Optional[Dict[str, Any]] = None,
        initial_warnings: Optional[Sequence[str]] = None,
    ) -> FeedReadinessResult:
        """Perform comprehensive readiness evaluation on proxy and stock candle inputs."""
        now_utc = to_utc(current_time) if current_time is not None else datetime.now(UTC)
        stock_map = stock_candles_map or {}
        provider_meta = provider_metadata or {}

        warnings: List[str] = list(initial_warnings) if initial_warnings else []
        failure_reasons: List[str] = []
        excluded_tickers: List[StockExclusion] = []
        included_tickers: List[str] = []
        validated_proxy_map: Dict[str, List[OHLCVCandle]] = {}
        validated_stock_map: Dict[str, List[OHLCVCandle]] = {}
        ticker_summaries: Dict[str, TickerSummary] = {}

        # 1. Determine latest completed trading session date
        latest_completed_sess = self.get_latest_completed_session(now_utc)
        if latest_completed_sess is None:
            failure_reasons.append("UNABLE_TO_DETERMINE_LATEST_COMPLETED_SESSION: calendar coverage error.")

        # 2. Check provider metadata and adjustment basis
        if provider_meta:
            native_adj = provider_meta.get("native_adjustment_type")
            if native_adj and native_adj != self.EXPECTED_ADJUSTMENT_TYPE:
                failure_reasons.append(
                    f"INCOMPATIBLE_ADJUSTMENT_BASIS: Provider declares '{native_adj}'; "
                    f"system requires '{self.EXPECTED_ADJUSTMENT_TYPE}'."
                )

        data_freshness = "DELAYED" if provider_meta.get("is_delayed", True) else "REAL_TIME"

        # 3. Validate Mandatory Primary Proxy (SPY)
        if self.MANDATORY_PRIMARY_PROXY not in proxy_candles_map or not proxy_candles_map[self.MANDATORY_PRIMARY_PROXY]:
            failure_reasons.append("MISSING_PRIMARY_PROXY_SPY: Primary market proxy SPY is not available.")
        else:
            spy_candles = list(proxy_candles_map[self.MANDATORY_PRIMARY_PROXY])
            try:
                self.validate_candle_series(spy_candles, self.MANDATORY_PRIMARY_PROXY, current_time=now_utc)

                if len(spy_candles) < self.MIN_SPY_BARS:
                    failure_reasons.append(
                        f"INSUFFICIENT_SPY_BARS: {len(spy_candles)} < {self.MIN_SPY_BARS} required."
                    )
                else:
                    last_spy_d = to_eastern(spy_candles[-1].timestamp).date()
                    if latest_completed_sess is not None and last_spy_d < latest_completed_sess:
                        failure_reasons.append(
                            f"STALE_SPY_DATA: latest candle date {last_spy_d} < expected completed session {latest_completed_sess}."
                        )
                    else:
                        validated_proxy_map[self.MANDATORY_PRIMARY_PROXY] = spy_candles
                        ticker_summaries[self.MANDATORY_PRIMARY_PROXY] = TickerSummary(
                            ticker=self.MANDATORY_PRIMARY_PROXY,
                            bar_count=len(spy_candles),
                            first_candle_date=to_eastern(spy_candles[0].timestamp).date(),
                            latest_candle_date=last_spy_d,
                            is_proxy=True,
                            status="VALIDATED",
                        )
            except Exception as e:
                failure_reasons.append(f"INVALID_SPY_CANDLES: {e}")

        # 4. Validate Secondary Proxies (QQQ, IWM)
        for sec_proxy in self.SECONDARY_PROXIES:
            if sec_proxy not in proxy_candles_map or not proxy_candles_map[sec_proxy]:
                warnings.append(f"SECONDARY_PROXY_EXCLUDED: {sec_proxy} missing from inputs.")
                continue

            sec_candles = list(proxy_candles_map[sec_proxy])
            try:
                self.validate_candle_series(sec_candles, sec_proxy, current_time=now_utc)
                if len(sec_candles) < self.MIN_SECONDARY_PROXY_BARS:
                    warnings.append(
                        f"SECONDARY_PROXY_EXCLUDED: {sec_proxy} has insufficient bars "
                        f"({len(sec_candles)} < {self.MIN_SECONDARY_PROXY_BARS})."
                    )
                else:
                    sec_d = to_eastern(sec_candles[-1].timestamp).date()
                    if latest_completed_sess is not None and sec_d < latest_completed_sess:
                        warnings.append(
                            f"SECONDARY_PROXY_EXCLUDED: {sec_proxy} is stale "
                            f"(latest {sec_d} < expected {latest_completed_sess})."
                        )
                    else:
                        validated_proxy_map[sec_proxy] = sec_candles
                        ticker_summaries[sec_proxy] = TickerSummary(
                            ticker=sec_proxy,
                            bar_count=len(sec_candles),
                            first_candle_date=to_eastern(sec_candles[0].timestamp).date(),
                            latest_candle_date=sec_d,
                            is_proxy=True,
                            status="VALIDATED",
                        )
            except Exception as e:
                warnings.append(f"SECONDARY_PROXY_EXCLUDED: {sec_proxy} failed validation: {e}")

        # 5. Validate Candidate Stock Universe
        for sym, candles in stock_map.items():
            sym_clean = sym.upper().strip()
            c_list = list(candles) if candles else []

            if not c_list:
                excluded_tickers.append(
                    StockExclusion(ticker=sym_clean, reason="EMPTY_BAR_SERIES", bar_count=0)
                )
                ticker_summaries[sym_clean] = TickerSummary(
                    ticker=sym_clean,
                    bar_count=0,
                    first_candle_date=None,
                    latest_candle_date=None,
                    is_proxy=False,
                    status="EXCLUDED_EMPTY",
                )
                continue

            try:
                self.validate_candle_series(c_list, sym_clean, current_time=now_utc)

                last_d = to_eastern(c_list[-1].timestamp).date()
                first_d = to_eastern(c_list[0].timestamp).date()

                if len(c_list) < self.MIN_STOCK_BARS:
                    reason = f"INSUFFICIENT_BARS: {len(c_list)} < {self.MIN_STOCK_BARS}"
                    excluded_tickers.append(
                        StockExclusion(ticker=sym_clean, reason=reason, bar_count=len(c_list), latest_candle_date=last_d)
                    )
                    ticker_summaries[sym_clean] = TickerSummary(
                        ticker=sym_clean,
                        bar_count=len(c_list),
                        first_candle_date=first_d,
                        latest_candle_date=last_d,
                        is_proxy=False,
                        status="EXCLUDED_INSUFFICIENT_BARS",
                    )
                elif latest_completed_sess is not None and last_d < latest_completed_sess:
                    reason = f"STALE_STOCK_DATA: latest {last_d} < expected {latest_completed_sess}"
                    excluded_tickers.append(
                        StockExclusion(ticker=sym_clean, reason=reason, bar_count=len(c_list), latest_candle_date=last_d)
                    )
                    ticker_summaries[sym_clean] = TickerSummary(
                        ticker=sym_clean,
                        bar_count=len(c_list),
                        first_candle_date=first_d,
                        latest_candle_date=last_d,
                        is_proxy=False,
                        status="EXCLUDED_STALE",
                    )
                else:
                    validated_stock_map[sym_clean] = c_list
                    included_tickers.append(sym_clean)
                    ticker_summaries[sym_clean] = TickerSummary(
                        ticker=sym_clean,
                        bar_count=len(c_list),
                        first_candle_date=first_d,
                        latest_candle_date=last_d,
                        is_proxy=False,
                        status="VALIDATED",
                    )
            except Exception as e:
                reason = f"INVALID_CANDLES: {e}"
                excluded_tickers.append(
                    StockExclusion(ticker=sym_clean, reason=reason, bar_count=len(c_list))
                )
                ticker_summaries[sym_clean] = TickerSummary(
                    ticker=sym_clean,
                    bar_count=len(c_list),
                    first_candle_date=to_eastern(c_list[0].timestamp).date() if c_list else None,
                    latest_candle_date=to_eastern(c_list[-1].timestamp).date() if c_list else None,
                    is_proxy=False,
                    status="EXCLUDED_INVALID",
                )

        # 6. Overall Readiness Verdict
        is_ready = len(failure_reasons) == 0
        overall_status = ReadinessStatus.READY if is_ready else ReadinessStatus.HARD_FAIL

        return FeedReadinessResult(
            is_ready=is_ready,
            status=overall_status,
            latest_completed_session=latest_completed_sess,
            proxy_candles=validated_proxy_map,
            stock_candles=validated_stock_map,
            included_tickers=included_tickers,
            excluded_tickers=excluded_tickers,
            ticker_summaries=ticker_summaries,
            warnings=warnings,
            failure_reasons=failure_reasons,
            provider_metadata=provider_meta,
            data_freshness=data_freshness,
            evaluation_time=now_utc,
        )


class MarketDataFeedAdapter:
    """Isolated market data feed adapter orchestrating provider data retrieval and readiness evaluation."""

    def __init__(
        self,
        provider: Optional[MarketDataProvider] = None,
        clock: Optional[MarketClock] = None,
        evaluator: Optional[MarketDataReadinessEvaluator] = None,
    ):
        self.clock = clock or MarketClock()
        self.evaluator = evaluator or MarketDataReadinessEvaluator(clock=self.clock)
        self.provider = provider
        if self.provider is None:
            from providers.yfinance_provider import YFinanceMarketDataProvider
            self.provider = YFinanceMarketDataProvider(clock=self.clock)

    def fetch_and_evaluate(
        self,
        stock_universe: Optional[Sequence[str]] = None,
        current_time: Optional[datetime] = None,
        lookback_days: int = 400,
    ) -> FeedReadinessResult:
        """Fetch market data from provider and evaluate readiness across proxies and universe."""
        now_utc = to_utc(current_time) if current_time is not None else datetime.now(UTC)
        start_dt = now_utc - timedelta(days=lookback_days)
        end_dt = now_utc

        proxy_candles_map: Dict[str, List[OHLCVCandle]] = {}
        stock_candles_map: Dict[str, List[OHLCVCandle]] = {}
        initial_warnings: List[str] = []
        provider_meta = self.provider.get_provider_metadata()

        # 1. Fetch Proxies (SPY, QQQ, IWM)
        for proxy in ("SPY", "QQQ", "IWM"):
            try:
                bars = self.provider.fetch_historical_bars(
                    proxy, "1d", start=start_dt, end=end_dt, current_time=now_utc
                )
                if bars:
                    proxy_candles_map[proxy] = bars
            except ProviderError as e:
                logger.warning(f"Provider failed to fetch proxy {proxy}: {e}")
                initial_warnings.append(f"Provider failed for proxy {proxy}: {e}")

        # 2. Fetch Stock Universe
        universe = stock_universe or []
        for ticker in universe:
            sym = ticker.upper().strip()
            if sym in proxy_candles_map:
                stock_candles_map[sym] = proxy_candles_map[sym]
                continue

            try:
                bars = self.provider.fetch_historical_bars(
                    sym, "1d", start=start_dt, end=end_dt, current_time=now_utc
                )
                if bars:
                    stock_candles_map[sym] = bars
            except ProviderError as e:
                logger.warning(f"Provider failed to fetch ticker {sym}: {e}")
                stock_candles_map[sym] = []
                initial_warnings.append(f"Provider failed for ticker {sym}: {e}")

        return self.evaluator.evaluate_readiness(
            proxy_candles_map=proxy_candles_map,
            stock_candles_map=stock_candles_map,
            current_time=now_utc,
            provider_metadata=provider_meta,
            initial_warnings=initial_warnings,
        )

    def evaluate_readiness(
        self,
        proxy_candles_map: Dict[str, Sequence[OHLCVCandle]],
        stock_candles_map: Optional[Dict[str, Sequence[OHLCVCandle]]] = None,
        current_time: Optional[datetime] = None,
        provider_metadata: Optional[Dict[str, Any]] = None,
    ) -> FeedReadinessResult:
        """Evaluate readiness directly on provided candle maps."""
        meta = provider_metadata or (self.provider.get_provider_metadata() if self.provider else {})
        return self.evaluator.evaluate_readiness(
            proxy_candles_map=proxy_candles_map,
            stock_candles_map=stock_candles_map,
            current_time=current_time,
            provider_metadata=meta,
        )


# -----------------------------------------------------------------------------
# Dual-Source Feed Adapter Bridge (Phase 7.3.4)
# -----------------------------------------------------------------------------
class DualSourceFeedAdapter(MarketDataFeedAdapter):
    """Feed adapter backed by DualSourceSyncGateway for live research workflows.

    Guarantees:
    1. 100% interface compatibility with MarketDataFeedAdapter.
    2. Strict Volume Provenance:
       - Every completed candle must declare is_consolidated_volume=True.
       - Rejects raw unconsolidated Alpaca IEX candles (is_consolidated_volume=False)
         via UnconsolidatedVolumeError.
       - Raw single-exchange IEX volume is preserved strictly in .auxiliary_iex_volume.
    3. Exact Session Alignment:
       - Enforces D_alpaca == D_yahoo via gateway synchronization.
       - Rejects session date desync, missing volume, or upstream defects (fails closed).
    4. Completed-Session Filtering:
       - Strictly filters out CandleStatus.FORMING intraday candles.
       - Only CandleStatus.COMPLETE bars are eligible for screening and historical analysis.
    5. Synchronous Bounded Pacing:
       - Injects bounded delay (default 0.2s) between ticker volume queries
         without background threads, daemons, or persistent processes.
    6. No Silent Failover:
       - Alpaca is authoritative for price action; Yahoo prices are never substituted.
       - No silent fallback to historical disk fixtures while in live mode.
    """

    def __init__(
        self,
        gateway: Optional[Any] = None,
        clock: Optional[MarketClock] = None,
        evaluator: Optional[MarketDataReadinessEvaluator] = None,
        pacing_delay_seconds: float = 0.2,
        sleep_fn: Optional[Callable[[float], None]] = None,
        time_fn: Optional[Callable[[], float]] = None,
    ):
        self.clock = clock or MarketClock()
        self.evaluator = evaluator or MarketDataReadinessEvaluator(clock=self.clock)
        self.pacing_delay_seconds = max(0.0, float(pacing_delay_seconds))
        self.sleep_fn = sleep_fn if sleep_fn is not None else time_mod.sleep
        self.time_fn = time_fn if time_fn is not None else time_mod.time
        self.pacing_calls: List[float] = []

        if gateway is not None:
            self.gateway = gateway
        else:
            # Deferred import to prevent top-level circular dependency
            from dual_source_sync_gateway import DualSourceSyncGateway
            self.gateway = DualSourceSyncGateway(clock=self.clock)

        self.provider = getattr(self.gateway, "alpaca_provider", None)

    def _apply_pacing(self) -> None:
        """Apply bounded inter-request delay without background threads or daemons."""
        if self.pacing_delay_seconds > 0:
            self.pacing_calls.append(self.pacing_delay_seconds)
            self.sleep_fn(self.pacing_delay_seconds)

    def fetch_and_evaluate(
        self,
        stock_universe: Optional[Sequence[str]] = None,
        current_time: Optional[datetime] = None,
        lookback_days: int = 400,
    ) -> FeedReadinessResult:
        """Synchronize dual-source market data and evaluate readiness across proxies and universe."""
        now_utc = to_utc(current_time) if current_time is not None else datetime.now(UTC)
        start_dt = now_utc - timedelta(days=lookback_days)
        end_dt = now_utc

        # Deferred imports
        from dual_source_sync_gateway import FeedState

        proxy_candles_map: Dict[str, List[OHLCVCandle]] = {}
        stock_candles_map: Dict[str, List[OHLCVCandle]] = {}
        initial_warnings: List[str] = []

        # 1. Synchronize Proxies (SPY, QQQ, IWM)
        for idx, proxy in enumerate(("SPY", "QQQ", "IWM")):
            if idx > 0:
                self._apply_pacing()

            try:
                sync_res = self.gateway.synchronize_completed_daily(
                    symbol=proxy,
                    start=start_dt,
                    end=end_dt,
                    current_time=now_utc,
                )
            except Exception as e:
                logger.warning(f"Gateway exception for proxy {proxy}: {e}")
                initial_warnings.append(f"Gateway exception for proxy {proxy}: {e}")
                proxy_candles_map[proxy] = []
                continue

            if not sync_res.success:
                err = sync_res.error_message or f"Gateway synchronization failed for proxy {proxy}"
                logger.warning(err)
                initial_warnings.append(err)
                proxy_candles_map[proxy] = []
                continue

            # Only completed bars qualify for historical analysis and screening
            candles = sync_res.candles or []
            completed_bars = [c for c in candles if c.status == CandleStatus.COMPLETE]

            # Strict Provenance Verification: Ensure every bar declares consolidated volume
            for c in completed_bars:
                if not getattr(c, "is_consolidated_volume", False):
                    raise UnconsolidatedVolumeError(
                        f"Candle for proxy {proxy} at {c.timestamp} holds unconsolidated volume. "
                        "DualSourceFeedAdapter strictly requires verified consolidated volume."
                    )

            if sync_res.state in (FeedState.LIVE_IEX_HYBRID, FeedState.COMPLETE_BAR):
                proxy_candles_map[proxy] = completed_bars
            else:
                initial_warnings.append(
                    f"Proxy {proxy} in unapproved state '{sync_res.state.value}': {sync_res.error_message}"
                )
                proxy_candles_map[proxy] = []

        # 2. Synchronize Stock Universe
        universe = stock_universe or []
        for ticker in universe:
            sym = ticker.upper().strip()
            if sym in proxy_candles_map:
                stock_candles_map[sym] = proxy_candles_map[sym]
                continue

            self._apply_pacing()

            try:
                sync_res = self.gateway.synchronize_completed_daily(
                    symbol=sym,
                    start=start_dt,
                    end=end_dt,
                    current_time=now_utc,
                )
            except Exception as e:
                logger.warning(f"Gateway exception for ticker {sym}: {e}")
                initial_warnings.append(f"Gateway exception for ticker {sym}: {e}")
                stock_candles_map[sym] = []
                continue

            if not sync_res.success:
                err = sync_res.error_message or f"Gateway synchronization failed for ticker {sym}"
                logger.warning(err)
                initial_warnings.append(err)
                stock_candles_map[sym] = []
                continue

            candles = sync_res.candles or []
            completed_bars = [c for c in candles if c.status == CandleStatus.COMPLETE]

            # Strict Provenance Verification: Ensure every bar declares consolidated volume
            for c in completed_bars:
                if not getattr(c, "is_consolidated_volume", False):
                    raise UnconsolidatedVolumeError(
                        f"Candle for ticker {sym} at {c.timestamp} holds unconsolidated volume. "
                        "DualSourceFeedAdapter strictly requires verified consolidated volume."
                    )

            if sync_res.state in (FeedState.LIVE_IEX_HYBRID, FeedState.COMPLETE_BAR):
                stock_candles_map[sym] = completed_bars
            else:
                initial_warnings.append(
                    f"Ticker {sym} in unapproved state '{sync_res.state.value}': {sync_res.error_message}"
                )
                stock_candles_map[sym] = []

        provider_meta = {
            "name": "dual_source_hybrid",
            "price_provider": "alpaca_iex",
            "volume_provider": "yahoo_consolidated",
            "feed_classification": "LIVE_IEX_HYBRID",
            "adjustment_type": "SPLIT_ADJUSTED",
            "is_consolidated_volume": True,
            "is_delayed": False,
        }

        return self.evaluator.evaluate_readiness(
            proxy_candles_map=proxy_candles_map,
            stock_candles_map=stock_candles_map,
            current_time=now_utc,
            provider_metadata=provider_meta,
            initial_warnings=initial_warnings,
        )

    def evaluate_readiness(
        self,
        proxy_candles_map: Dict[str, Sequence[OHLCVCandle]],
        stock_candles_map: Optional[Dict[str, Sequence[OHLCVCandle]]] = None,
        current_time: Optional[datetime] = None,
        provider_metadata: Optional[Dict[str, Any]] = None,
        initial_warnings: Optional[Sequence[str]] = None,
    ) -> FeedReadinessResult:
        """Evaluate readiness directly on provided candle maps with provenance enforcement."""
        # Enforce consolidated volume provenance across all supplied candles
        all_maps = [proxy_candles_map]
        if stock_candles_map:
            all_maps.append(stock_candles_map)

        for m in all_maps:
            for sym, candles in m.items():
                for c in candles:
                    if not getattr(c, "is_consolidated_volume", True):
                        raise UnconsolidatedVolumeError(
                            f"Candle for {sym} at {c.timestamp} holds unconsolidated volume. "
                            "DualSourceFeedAdapter strictly requires verified consolidated volume."
                        )

        meta = provider_metadata or {
            "name": "dual_source_hybrid",
            "price_provider": "alpaca_iex",
            "volume_provider": "yahoo_consolidated",
            "feed_classification": "LIVE_IEX_HYBRID",
            "adjustment_type": "SPLIT_ADJUSTED",
            "is_consolidated_volume": True,
            "is_delayed": False,
        }

        return self.evaluator.evaluate_readiness(
            proxy_candles_map=proxy_candles_map,
            stock_candles_map=stock_candles_map,
            current_time=current_time,
            provider_metadata=meta,
            initial_warnings=initial_warnings,
        )

