"""Historical market data management, validation, and corporate action readiness for trader-swing.

Enforces:
1. Strict chronological ordering and rejection of duplicate or out-of-order timestamps.
2. Invariant validation for OHLC relationships (High >= Open/Close/Low, Low <= Open/Close/High).
3. Strictly excludes FORMING or UNKNOWN candles from strategy decisions.
4. Corporate action price-basis awareness: detects overnight price discontinuities and unadjusted splits.
5. Backward split adjustment and corporate event tracking (splits and cash dividends).
6. Calendar coverage and missing session detection against NYSE trading sessions.
7. Strict temporal slicing ensuring ZERO LOOK-AHEAD BIAS.
8. Explicit documentation and disclosure of survivorship bias in curated universes.
"""

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
import os
import sys
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from backtest_models import (
    DatasetIntegrityReport,
    DividendEvent,
    PriceAdjustmentType,
    StockSplitEvent,
)
from market_data_models import CandleStatus, MarketClock, OHLCVCandle, UTC
from universe import validate_ticker

TWO_DECIMALS = Decimal("0.01")

SURVIVORSHIP_BIAS_DISCLOSURE = (
    "SURVIVORSHIP BIAS NOTICE: The default curated universe (DEFAULT_CURATED_WATCHLIST) "
    "consists of large-cap US equities surviving as liquid index components as of 2026. "
    "Executing historical backtests across prior periods (e.g., 2015-2025) using this fixed list "
    "inherently introduces survivorship bias, as companies that went bankrupt, were acquired, "
    "or suffered delisting during that period are omitted. For academic or production backtesting, "
    "callers should supply point-in-time constituent lists."
)


class HistoricalDataValidationError(ValueError):
    """Raised when historical candle data fails validation rules."""
    pass


class HistoricalDataManager:
    """Stores, validates, and slices historical candle datasets for event-driven simulation."""

    def __init__(self, universe_manager: Optional[Any] = None):
        self.universe_manager = universe_manager
        # Ticker -> List[OHLCVCandle] sorted chronologically
        self._candles_by_ticker: Dict[str, List[OHLCVCandle]] = defaultdict(list)
        # Ticker -> Date -> OHLCVCandle for fast session lookup
        self._candles_by_date: Dict[str, Dict[date, OHLCVCandle]] = defaultdict(dict)
        # Ticker -> PriceAdjustmentType
        self._adjustment_type_by_ticker: Dict[str, PriceAdjustmentType] = defaultdict(
            lambda: PriceAdjustmentType.SPLIT_ADJUSTED
        )
        # All unique trading dates present across loaded universe, sorted ascending
        self._all_session_dates: List[date] = []
        # Ticker -> List of data quality warnings
        self._warnings_by_ticker: Dict[str, List[str]] = defaultdict(list)

        # Corporate Action Registries
        # Ticker -> List[StockSplitEvent]
        self._splits_by_ticker: Dict[str, List[StockSplitEvent]] = defaultdict(list)
        # Date -> List[StockSplitEvent]
        self._splits_by_date: Dict[date, List[StockSplitEvent]] = defaultdict(list)
        # Ticker -> List[DividendEvent]
        self._dividends_by_ticker: Dict[str, List[DividendEvent]] = defaultdict(list)
        # Date -> List[DividendEvent]
        self._dividends_by_date: Dict[date, List[DividendEvent]] = defaultdict(list)

    def load_candles(
        self,
        ticker: str,
        candles: Sequence[OHLCVCandle],
        require_complete: bool = True,
        adjustment_type: PriceAdjustmentType = PriceAdjustmentType.SPLIT_ADJUSTED,
    ) -> int:
        """Validate, sort, and register historical daily candles for a ticker.

        Returns:
            Count of successfully validated and stored candles.

        Raises:
            HistoricalDataValidationError: If data has invalid OHLC math, duplicates, or interval mismatch.
        """
        norm_ticker = validate_ticker(ticker)
        if not candles:
            raise HistoricalDataValidationError(f"Cannot load empty candle series for '{norm_ticker}'.")

        validated: List[OHLCVCandle] = []
        seen_dates: Set[date] = set()

        for idx, candle in enumerate(candles):
            # 1. Ticker consistency
            if candle.ticker.upper() != norm_ticker:
                raise HistoricalDataValidationError(
                    f"Candle ticker mismatch at index {idx}: expected '{norm_ticker}', got '{candle.ticker}'."
                )

            # 2. Daily interval enforcement
            if candle.interval != "1d":
                raise HistoricalDataValidationError(
                    f"Invalid interval '{candle.interval}' for {norm_ticker} at index {idx}; backtesting requires '1d'."
                )

            # 3. Status check: Only COMPLETE candles allowed for backtesting
            if require_complete and candle.status != CandleStatus.COMPLETE:
                raise HistoricalDataValidationError(
                    f"Incomplete candle ({candle.status.value}) for {norm_ticker} on {candle.timestamp.date()}; "
                    "backtest strategy cannot use forming or unverified bars."
                )

            # 4. Strictly positive financial prices
            if candle.open <= Decimal("0.00") or candle.high <= Decimal("0.00") or \
               candle.low <= Decimal("0.00") or candle.close <= Decimal("0.00"):
                raise HistoricalDataValidationError(
                    f"Non-positive price in candle for {norm_ticker} on {candle.timestamp.date()}."
                )

            # 5. Non-negative volume
            if candle.volume < 0:
                raise HistoricalDataValidationError(
                    f"Negative volume ({candle.volume}) for {norm_ticker} on {candle.timestamp.date()}."
                )

            # 6. OHLC relationship invariants
            if candle.high < candle.low:
                raise HistoricalDataValidationError(
                    f"High ({candle.high}) < Low ({candle.low}) for {norm_ticker} on {candle.timestamp.date()}."
                )
            if candle.high < candle.open or candle.high < candle.close:
                raise HistoricalDataValidationError(
                    f"High ({candle.high}) must be >= Open ({candle.open}) and Close ({candle.close}) for {norm_ticker} on {candle.timestamp.date()}."
                )
            if candle.low > candle.open or candle.low > candle.close:
                raise HistoricalDataValidationError(
                    f"Low ({candle.low}) must be <= Open ({candle.open}) and Close ({candle.close}) for {norm_ticker} on {candle.timestamp.date()}."
                )

            # 7. Duplicate date detection
            c_date = candle.timestamp.date()
            if c_date in seen_dates:
                raise HistoricalDataValidationError(
                    f"Duplicate candle timestamp date '{c_date}' for ticker {norm_ticker} at index {idx}."
                )
            seen_dates.add(c_date)
            validated.append(candle)

        # Sort strictly ascending by timestamp
        validated.sort(key=lambda c: c.timestamp)

        # 8. Check for corporate action overnight gaps (> 35% jump/drop with normal volume)
        for i in range(1, len(validated)):
            prev = validated[i - 1]
            curr = validated[i]
            pct_change = abs(curr.close - prev.close) / prev.close
            if pct_change >= Decimal("0.35"):
                self._warnings_by_ticker[norm_ticker].append(
                    f"Possible unadjusted stock split or extreme corporate event between {prev.timestamp.date()} "
                    f"and {curr.timestamp.date()}: price changed {pct_change * 100:.1f}%."
                )

        # Store in registries
        self._candles_by_ticker[norm_ticker] = validated
        self._candles_by_date[norm_ticker] = {c.timestamp.date(): c for c in validated}
        self._adjustment_type_by_ticker[norm_ticker] = adjustment_type

        # Update global distinct sessions
        all_dates_set = set(self._all_session_dates)
        all_dates_set.update(seen_dates)
        self._all_session_dates = sorted(list(all_dates_set))

        return len(validated)

    def load_from_service(
        self,
        service: Any,  # MarketDataService
        ticker: str,
        start: datetime,
        end: datetime,
        interval: str = "1d",
        adjustment_type: PriceAdjustmentType = PriceAdjustmentType.SPLIT_ADJUSTED,
        require_complete: bool = True,
    ) -> int:
        """Fetch candles via MarketDataService coordinator and load them into manager."""
        norm_ticker = validate_ticker(ticker)

        # Enforce provider native adjustment compatibility
        if hasattr(service, "provider") and hasattr(service.provider, "native_adjustment_type"):
            native = service.provider.native_adjustment_type
            native_val = native.value if hasattr(native, "value") else str(native)
            if native_val != adjustment_type.value:
                raise HistoricalDataValidationError(
                    f"Provider '{service.provider.name}' natively outputs {native_val} candles; "
                    f"cannot label as {adjustment_type.value} without explicit conversion."
                )

        bars = service.get_historical_bars(
            ticker=norm_ticker,
            interval=interval,
            start=start,
            end=end,
            use_cache=True,
        )
        return self.load_candles(
            ticker=norm_ticker,
            candles=bars,
            require_complete=require_complete,
            adjustment_type=adjustment_type,
        )

    def load_splits(self, ticker: str, splits: Sequence[StockSplitEvent]) -> int:
        """Register stock split events for a ticker with strict deduplication and validation."""
        norm_ticker = validate_ticker(ticker)
        loaded_count = 0
        for s in splits:
            if s.ticker.upper() != norm_ticker:
                raise HistoricalDataValidationError(f"Split event ticker '{s.ticker}' does not match '{norm_ticker}'.")
            if s.ratio <= Decimal("0.0"):
                raise HistoricalDataValidationError(f"Split ratio must be positive, got {s.ratio} for {norm_ticker}.")
            if s.ratio == Decimal("1.0"):
                raise HistoricalDataValidationError(f"Invalid 1:1 split ratio for {norm_ticker}.")

            # Deduplication: check if split for same ticker on same date already exists
            existing = [x for x in self._splits_by_ticker[norm_ticker] if x.split_date == s.split_date]
            if existing:
                if any(x.ratio == s.ratio for x in existing):
                    # Duplicate identical split - skip
                    continue
                else:
                    raise HistoricalDataValidationError(
                        f"Conflicting split event on {s.split_date} for {norm_ticker}: "
                        f"already registered with ratio {existing[0].ratio}, cannot add ratio {s.ratio}."
                    )

            self._splits_by_ticker[norm_ticker].append(s)
            self._splits_by_date[s.split_date].append(s)
            loaded_count += 1
        return loaded_count

    def load_dividends(self, ticker: str, dividends: Sequence[DividendEvent]) -> int:
        """Register cash dividend events for a ticker with validation and settlement lag enforcement."""
        norm_ticker = validate_ticker(ticker)

        # Conflict rejection: Cannot load cash dividends if dataset is SPLIT_AND_DIVIDEND_ADJUSTED
        if self._adjustment_type_by_ticker.get(norm_ticker) == PriceAdjustmentType.SPLIT_AND_DIVIDEND_ADJUSTED:
            raise HistoricalDataValidationError(
                f"Cannot load cash dividends for '{norm_ticker}': dataset is registered as "
                f"SPLIT_AND_DIVIDEND_ADJUSTED. Dividend returns are already in the price series."
            )

        loaded_count = 0
        for d in dividends:
            if d.ticker.upper() != norm_ticker:
                raise HistoricalDataValidationError(f"Dividend event ticker '{d.ticker}' does not match '{norm_ticker}'.")
            if d.amount_per_share <= Decimal("0.0"):
                raise HistoricalDataValidationError(f"Dividend amount must be positive, got {d.amount_per_share} for {norm_ticker}.")

            # Validate payable date ordering if provided
            if d.payable_date is not None and d.payable_date < d.ex_date:
                raise HistoricalDataValidationError(
                    f"Dividend payable_date {d.payable_date} cannot precede ex_date {d.ex_date} for {norm_ticker}."
                )

            # Assign 14-day estimated settlement lag if payable_date is omitted
            resolved_event = d
            if d.payable_date is None:
                resolved_event = DividendEvent(
                    ticker=norm_ticker,
                    ex_date=d.ex_date,
                    amount_per_share=d.amount_per_share,
                    payable_date=d.ex_date + timedelta(days=14),
                    is_estimated_payable_date=True,
                )

            # Deduplication: check if dividend on same ex_date already registered
            existing = [x for x in self._dividends_by_ticker[norm_ticker] if x.ex_date == d.ex_date]
            if existing:
                raise HistoricalDataValidationError(
                    f"Duplicate dividend event for {norm_ticker} on ex-date {d.ex_date} already registered."
                )

            self._dividends_by_ticker[norm_ticker].append(resolved_event)
            self._dividends_by_date[resolved_event.ex_date].append(resolved_event)
            loaded_count += 1
        return loaded_count

    def get_splits_for_session(self, session_date: date) -> List[StockSplitEvent]:
        """Return all splits occurring on the specified date."""
        return list(self._splits_by_date.get(session_date, []))

    def get_dividends_for_session(self, session_date: date) -> List[DividendEvent]:
        """Return all cash dividend ex-dates occurring on the specified date."""
        return list(self._dividends_by_date.get(session_date, []))

    def get_dividends_for_ticker(self, ticker: str) -> List[DividendEvent]:
        """Return all cash dividends registered for a ticker."""
        return list(self._dividends_by_ticker.get(ticker.upper(), []))

    def detect_unadjusted_splits(
        self,
        ticker: str,
        threshold: Decimal = Decimal("0.35"),
    ) -> List[StockSplitEvent]:
        """Detect potential unadjusted stock splits from overnight price cliff jumps."""
        norm_ticker = ticker.upper()
        bars = self._candles_by_ticker.get(norm_ticker, [])
        if len(bars) < 2:
            return []

        detected: List[StockSplitEvent] = []
        for i in range(1, len(bars)):
            prev = bars[i - 1]
            curr = bars[i]
            ratio = prev.close / curr.close

            # Forward split: price dropped drastically (e.g. 2:1 ratio ~= 2.0, 3:1 ~= 3.0, 4:1 ~= 4.0)
            if ratio >= Decimal("1.70"):
                est_ratio = Decimal(str(round(float(ratio))))
                detected.append(
                    StockSplitEvent(
                        ticker=norm_ticker,
                        split_date=curr.timestamp.date(),
                        ratio=est_ratio,
                        description=f"Estimated {int(est_ratio)}:1 forward split (cliff: ${prev.close} -> ${curr.close}).",
                    )
                )
            # Reverse split: price jumped drastically (e.g. 1:2 ratio ~= 0.5, 1:10 ~= 0.1)
            elif ratio <= Decimal("0.60"):
                est_ratio = (Decimal("1.0") / Decimal(str(round(float(Decimal('1.0') / ratio))))).quantize(Decimal("0.0001"))
                detected.append(
                    StockSplitEvent(
                        ticker=norm_ticker,
                        split_date=curr.timestamp.date(),
                        ratio=est_ratio,
                        description=f"Estimated reverse split (cliff: ${prev.close} -> ${curr.close}).",
                    )
                )

        return detected

    def apply_split_adjustment(
        self,
        ticker: str,
        splits: Sequence[StockSplitEvent],
    ) -> List[OHLCVCandle]:
        """Apply backward split adjustment to historical raw candles before the split date.

        Price_adj = Price_raw / Split_Ratio
        Volume_adj = Volume_raw * Split_Ratio
        """
        norm_ticker = ticker.upper()
        bars = self._candles_by_ticker.get(norm_ticker, [])
        if not bars or not splits:
            return bars

        sorted_splits = sorted(splits, key=lambda s: s.split_date, reverse=True)
        adjusted: List[OHLCVCandle] = []

        for c in bars:
            c_date = c.timestamp.date()
            cum_ratio = Decimal("1.0")
            for s in sorted_splits:
                if c_date < s.split_date:
                    cum_ratio *= s.ratio

            if cum_ratio != Decimal("1.0"):
                adj_open = (c.open / cum_ratio).quantize(TWO_DECIMALS, ROUND_HALF_UP)
                adj_high = (c.high / cum_ratio).quantize(TWO_DECIMALS, ROUND_HALF_UP)
                adj_low = (c.low / cum_ratio).quantize(TWO_DECIMALS, ROUND_HALF_UP)
                adj_close = (c.close / cum_ratio).quantize(TWO_DECIMALS, ROUND_HALF_UP)
                adj_vol = int(Decimal(c.volume) * cum_ratio)

                adjusted.append(
                    OHLCVCandle(
                        ticker=norm_ticker,
                        interval=c.interval,
                        timestamp=c.timestamp,
                        open=adj_open,
                        high=adj_high,
                        low=adj_low,
                        close=adj_close,
                        volume=adj_vol,
                        status=c.status,
                        source=c.source,
                        provider_event_timestamp=c.provider_event_timestamp,
                        received_timestamp=c.received_timestamp,
                        is_delayed=c.is_delayed,
                    )
                )
            else:
                adjusted.append(c)

        # Update manager state
        self._candles_by_ticker[norm_ticker] = adjusted
        self._candles_by_date[norm_ticker] = {c.timestamp.date(): c for c in adjusted}
        self._adjustment_type_by_ticker[norm_ticker] = PriceAdjustmentType.SPLIT_ADJUSTED

        # Clear unadjusted split warnings for this ticker
        self._warnings_by_ticker[norm_ticker] = [
            w for w in self._warnings_by_ticker[norm_ticker] if "unadjusted stock split" not in w
        ]

        return adjusted

    def validate_dataset_integrity(self, ticker: str) -> DatasetIntegrityReport:
        """Audit candle coverage against expected NYSE trading calendar and check for anomalies."""
        norm_ticker = ticker.upper()
        bars = self._candles_by_ticker.get(norm_ticker, [])
        warnings: List[str] = list(self._warnings_by_ticker.get(norm_ticker, []))

        if not bars:
            return DatasetIntegrityReport(
                ticker=norm_ticker,
                total_candles=0,
                start_date=None,
                end_date=None,
                expected_calendar_sessions=0,
                missing_sessions=[],
                duplicate_dates_count=0,
                detected_unadjusted_splits=[],
                adjustment_type=self._adjustment_type_by_ticker[norm_ticker],
                quality_status="CORRUPTED",
                warnings=["No candles loaded for ticker."],
            )

        start_date = bars[0].timestamp.date()
        end_date = bars[-1].timestamp.date()
        actual_dates = set(b.timestamp.date() for b in bars)

        # Check calendar coverage using MarketClock if years supported
        expected_sessions: List[date] = []
        unsupported_years: Set[int] = set()
        curr = start_date
        while curr <= end_date:
            if MarketClock.is_historical_year_supported(curr.year):
                is_trading, _ = MarketClock.is_historical_trading_day(curr)
                if is_trading:
                    expected_sessions.append(curr)
            else:
                unsupported_years.add(curr.year)
            curr += timedelta(days=1)

        missing = [d for d in expected_sessions if d not in actual_dates]
        if missing:
            warnings.append(f"Detected {len(missing)} missing trading sessions between {start_date} and {end_date}.")

        if unsupported_years:
            warnings.append(
                f"Dataset covers unsupported calendar years {sorted(list(unsupported_years))}. "
                f"Historical calendar confirmed coverage is limited to {MarketClock.HISTORICAL_SUPPORTED_YEARS[0]}-{MarketClock.HISTORICAL_SUPPORTED_YEARS[-1]}; "
                f"holiday coverage cannot be verified."
            )

        # Check for unhandled splits
        splits = self.detect_unadjusted_splits(norm_ticker)
        if splits and self._adjustment_type_by_ticker[norm_ticker] == PriceAdjustmentType.RAW_UNADJUSTED:
            warnings.append(f"Detected {len(splits)} unadjusted stock splits in raw dataset.")

        if unsupported_years:
            quality_status = "UNSUPPORTED_CALENDAR"
        elif len(missing) > (len(bars) * 0.5):
            quality_status = "CORRUPTED"
        elif warnings:
            quality_status = "WARNINGS"
        else:
            quality_status = "CLEAN"

        return DatasetIntegrityReport(
            ticker=norm_ticker,
            total_candles=len(bars),
            start_date=start_date,
            end_date=end_date,
            expected_calendar_sessions=len(expected_sessions),
            missing_sessions=missing,
            duplicate_dates_count=0,
            detected_unadjusted_splits=splits,
            adjustment_type=self._adjustment_type_by_ticker[norm_ticker],
            quality_status=quality_status,
            warnings=warnings,
        )

    def get_all_session_dates(
        self,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> List[date]:
        """Return all distinct trading dates in the loaded dataset, optionally filtered by range."""
        dates = self._all_session_dates
        if start_date:
            dates = [d for d in dates if d >= start_date]
        if end_date:
            dates = [d for d in dates if d <= end_date]
        return dates

    def get_price_adjustment_type(self, ticker: str) -> PriceAdjustmentType:
        """Return the registered price adjustment type for a ticker."""
        return self._adjustment_type_by_ticker.get(
            ticker.upper(), PriceAdjustmentType.SPLIT_ADJUSTED
        )

    def get_execution_candle(self, ticker: str, session_date: date) -> Optional[OHLCVCandle]:
        """Get the single execution candle for a ticker on a specific trading date.

        Preserves actual historical OHLCV prices as traded on the exchange.
        Used for order fills, stops, targets, slippage, mark-to-market, and cash accounting.
        """
        norm_ticker = ticker.upper()
        candle = self._candles_by_date.get(norm_ticker, {}).get(session_date)
        if candle is not None:
            return candle
        if self.universe_manager:
            try:
                sec = self.universe_manager.resolve_security_at_date(norm_ticker, session_date)
                if sec:
                    active_sym = self.universe_manager.resolve_ticker_at_date(sec.security_id, session_date)
                    if active_sym and active_sym.upper() != norm_ticker:
                        candle = self._candles_by_date.get(active_sym.upper(), {}).get(session_date)
                        if candle:
                            return candle
                    candle = self._candles_by_date.get(sec.security_id, {}).get(session_date)
                    if candle:
                        return candle
            except Exception:
                pass
        return None

    def get_session_candle(self, ticker: str, session_date: date) -> Optional[OHLCVCandle]:
        """Get the single execution candle for a ticker on a specific trading date (alias)."""
        return self.get_execution_candle(ticker, session_date)

    def get_execution_candles(self, ticker: str) -> List[OHLCVCandle]:
        """Return the actual execution candle series as traded for a ticker."""
        return list(self._candles_by_ticker.get(ticker.upper(), []))

    def get_candles(self, ticker: str) -> List[OHLCVCandle]:
        """Return validated execution candles for a ticker (alias)."""
        return self.get_execution_candles(ticker)

    def get_execution_candles_slice(
        self,
        ticker: str,
        as_of_date: date,
        max_bars: Optional[int] = None,
    ) -> List[OHLCVCandle]:
        """Return raw execution candle series for ticker strictly up to and including as_of_date.

        CRITICAL: Never returns candles with date > as_of_date (strictly eliminates look-ahead).
        """
        norm_ticker = ticker.upper()
        all_bars = self._candles_by_ticker.get(norm_ticker, [])
        if not all_bars and self.universe_manager:
            try:
                sec = self.universe_manager.resolve_security_at_date(norm_ticker, as_of_date)
                if sec:
                    active_sym = self.universe_manager.resolve_ticker_at_date(sec.security_id, as_of_date)
                    if active_sym and active_sym.upper() in self._candles_by_ticker:
                        all_bars = self._candles_by_ticker[active_sym.upper()]
                    elif sec.security_id in self._candles_by_ticker:
                        all_bars = self._candles_by_ticker[sec.security_id]
            except Exception:
                pass

        if not all_bars:
            return []

        sliced = [c for c in all_bars if c.timestamp.date() <= as_of_date]
        if max_bars is not None and len(sliced) > max_bars:
            sliced = sliced[-max_bars:]
        return sliced

    def get_analytical_candles_slice(
        self,
        ticker: str,
        as_of_date: date,
        max_bars: Optional[int] = None,
    ) -> List[OHLCVCandle]:
        """Return continuous split-adjusted analytical candles strictly up to as_of_date.

        For RAW_UNADJUSTED mode:
          Applies backward split adjustments for all splits effective on or before as_of_date.
          Bars prior to each split are scaled by the cumulative split ratio to align with
          the current trading price scale as of as_of_date.
          ZERO LOOK-AHEAD BIAS: splits with split_date > as_of_date are strictly ignored.
          NO DIVIDEND ADJUSTMENTS: preserves standard price scale without total-return distortion.

        For SPLIT_ADJUSTED or SPLIT_AND_DIVIDEND_ADJUSTED mode:
          Historical prices are already split-adjusted; returns the pre-adjusted series directly
          without duplicate adjustment.
        """
        norm_ticker = ticker.upper()
        raw_slice = self.get_execution_candles_slice(norm_ticker, as_of_date, max_bars=None)
        if not raw_slice:
            return []

        # If data is already split-adjusted or no splits exist, return sliced bars
        adj_type = self._adjustment_type_by_ticker.get(norm_ticker, PriceAdjustmentType.SPLIT_ADJUSTED)
        if adj_type != PriceAdjustmentType.RAW_UNADJUSTED:
            if max_bars is not None and len(raw_slice) > max_bars:
                return raw_slice[-max_bars:]
            return raw_slice

        splits = [s for s in self._splits_by_ticker.get(norm_ticker, []) if s.split_date <= as_of_date]
        if not splits:
            if max_bars is not None and len(raw_slice) > max_bars:
                return raw_slice[-max_bars:]
            return raw_slice

        sorted_splits = sorted(splits, key=lambda s: s.split_date, reverse=True)
        adjusted: List[OHLCVCandle] = []

        for c in raw_slice:
            c_date = c.timestamp.date()
            cum_ratio = Decimal("1.0")
            for s in sorted_splits:
                if c_date < s.split_date:
                    cum_ratio *= s.ratio

            if cum_ratio != Decimal("1.0"):
                adj_open = (c.open / cum_ratio).quantize(TWO_DECIMALS, ROUND_HALF_UP)
                adj_high = (c.high / cum_ratio).quantize(TWO_DECIMALS, ROUND_HALF_UP)
                adj_low = (c.low / cum_ratio).quantize(TWO_DECIMALS, ROUND_HALF_UP)
                adj_close = (c.close / cum_ratio).quantize(TWO_DECIMALS, ROUND_HALF_UP)
                adj_vol = int(Decimal(c.volume) * cum_ratio)

                adjusted.append(
                    OHLCVCandle(
                        ticker=norm_ticker,
                        interval=c.interval,
                        timestamp=c.timestamp,
                        open=adj_open,
                        high=adj_high,
                        low=adj_low,
                        close=adj_close,
                        volume=adj_vol,
                        status=c.status,
                        source=c.source,
                        provider_event_timestamp=c.provider_event_timestamp,
                        received_timestamp=c.received_timestamp,
                        is_delayed=c.is_delayed,
                        bar_end_timestamp=c.bar_end_timestamp,
                    )
                )
            else:
                adjusted.append(c)

        if max_bars is not None and len(adjusted) > max_bars:
            adjusted = adjusted[-max_bars:]
        return adjusted

    def get_analytical_universe_candles_slice(
        self,
        universe: Sequence[str],
        as_of_date: date,
        max_bars: Optional[int] = 250,
    ) -> Dict[str, List[OHLCVCandle]]:
        """Return analytical candles map for multiple tickers sliced strictly up to as_of_date."""
        result: Dict[str, List[OHLCVCandle]] = {}
        for sym in universe:
            bars = self.get_analytical_candles_slice(sym, as_of_date, max_bars=max_bars)
            if bars:
                result[sym.upper()] = bars
        return result

    def get_candles_slice(
        self,
        ticker: str,
        as_of_date: date,
        max_bars: Optional[int] = None,
        analytical: bool = True,
    ) -> List[OHLCVCandle]:
        """Return candle series for ticker strictly up to and including as_of_date.

        If analytical=True (default), returns continuous split-adjusted analytical candles.
        If analytical=False, returns raw execution candles.
        """
        if analytical:
            return self.get_analytical_candles_slice(ticker, as_of_date, max_bars=max_bars)
        return self.get_execution_candles_slice(ticker, as_of_date, max_bars=max_bars)

    def get_universe_candles_slice(
        self,
        universe: Sequence[str],
        as_of_date: date,
        max_bars: Optional[int] = 250,
        analytical: bool = True,
    ) -> Dict[str, List[OHLCVCandle]]:
        """Return candles map for multiple tickers sliced strictly up to as_of_date."""
        if analytical:
            return self.get_analytical_universe_candles_slice(universe, as_of_date, max_bars=max_bars)
        result: Dict[str, List[OHLCVCandle]] = {}
        for sym in universe:
            bars = self.get_execution_candles_slice(sym, as_of_date, max_bars=max_bars)
            if bars:
                result[sym.upper()] = bars
        return result

    def get_quality_warnings(self) -> Dict[str, List[str]]:
        """Return all detected data quality warnings by ticker."""
        return {k: list(v) for k, v in self._warnings_by_ticker.items() if v}

    @staticmethod
    def get_survivorship_bias_notice() -> str:
        """Return official survivorship bias notice for curated universes."""
        return SURVIVORSHIP_BIAS_DISCLOSURE
