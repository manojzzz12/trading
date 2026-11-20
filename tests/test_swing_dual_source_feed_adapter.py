"""Deterministic offline unit test suite for DualSourceFeedAdapter (Phase 7.3.4).

Verifies:
1. Adapter interface compatibility with MarketDataFeedAdapter.
2. Identical indicator outputs for identical completed candles.
3. Correct Yahoo consolidated-volume provenance.
4. Rejection of raw unconsolidated Alpaca IEX candles (UnconsolidatedVolumeError).
5. Missing volume and session-date mismatch fail-closed behavior.
6. Filtering of active forming bars (only completed bars enter screening).
7. Synchronous bounded request pacing without background threads or daemons.
8. Complete offline operation with zero network calls.
9. Strict preservation of frozen strategy parameters and risk sizing limits.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import os
import sys
import unittest
from typing import Any, Dict, List, Optional, Sequence

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SWING_DIR = os.path.join(ROOT_DIR, ".agents", "skills", "trader-swing")
for path in (ROOT_DIR, SWING_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

from market_data_models import (
    CandleStatus,
    EASTERN,
    MarketClock,
    OHLCVCandle,
    to_eastern,
    to_utc,
    UTC,
)
from market_data_validation import (
    CandleValidationError,
    InvalidVolumeError,
)
from feed_adapter import (
    DualSourceFeedAdapter,
    FeedReadinessResult,
    MarketDataFeedAdapter,
    MarketDataReadinessEvaluator,
    ReadinessStatus,
    UnconsolidatedVolumeError,
)
from dual_source_aggregator import DualSourceCandle
from dual_source_sync_gateway import DailySyncResult, DualSourceSyncGateway, FeedState
from providers.alpaca_provider import AlpacaCandle
from indicators import (
    calculate_atr,
    calculate_average_volume,
    calculate_relative_volume,
    calculate_rsi,
    calculate_sma,
)
from risk import RiskConfig
from scanner_models import ScreeningCriteria


class MockSyncGateway:
    """Deterministic offline mock for DualSourceSyncGateway."""

    def __init__(self, responses: Optional[Dict[str, DailySyncResult]] = None):
        self.responses: Dict[str, DailySyncResult] = responses or {}
        self.calls: List[Dict[str, Any]] = []

    def synchronize_completed_daily(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        current_time: Optional[datetime] = None,
    ) -> DailySyncResult:
        sym = symbol.upper().strip()
        self.calls.append({
            "symbol": sym,
            "start": start,
            "end": end,
            "current_time": current_time,
        })
        if sym in self.responses:
            return self.responses[sym]

        # Default fallback: return FEED_UNAVAILABLE
        return DailySyncResult(
            symbol=sym,
            state=FeedState.FEED_UNAVAILABLE,
            success=False,
            error_message=f"No mock response configured for {sym}",
        )


def generate_completed_dual_source_candles(
    ticker: str,
    count: int = 200,
    end_date: Optional[date] = None,
    base_price: float = 100.0,
    consolidated_volume: int = 50_000_000,
    auxiliary_iex_volume: int = 1_250_000,
) -> List[DualSourceCandle]:
    """Generate a series of valid completed DualSourceCandles for offline testing."""
    clock = MarketClock()
    target_end = end_date or date(2026, 11, 16)
    d = target_end
    session_dates = []
    while len(session_dates) < count:
        sess = clock.get_historical_session(d) or clock.get_session(d)
        if sess is not None:
            session_dates.append((d, sess))
        d -= timedelta(days=1)
    session_dates.reverse()

    candles: List[DualSourceCandle] = []
    for idx, (curr_d, sess) in enumerate(session_dates):
        sess_open_dt = datetime.combine(curr_d, sess[0], tzinfo=EASTERN)
        ts_utc = to_utc(sess_open_dt)
        px = Decimal(str(round(base_price + idx * 0.1, 2)))
        candles.append(
            DualSourceCandle(
                ticker=ticker.upper().strip(),
                interval="1d",
                timestamp=ts_utc,
                open=px,
                high=px + Decimal("2.00"),
                low=px - Decimal("1.00"),
                close=px + Decimal("1.00"),
                volume=consolidated_volume,  # Authoritative Yahoo volume
                status=CandleStatus.COMPLETE,
                session_date=curr_d,
                adjustment_type="SPLIT_ADJUSTED",
                is_consolidated_volume=True,
                price_source="ALPACA_IEX",
                volume_source="YAHOO_CONSOLIDATED",
                auxiliary_iex_volume=auxiliary_iex_volume,
                feed_classification="DUAL_SOURCE_HYBRID",
            )
        )

    return candles


class TestDualSourceFeedAdapter(unittest.TestCase):
    """Test suite for DualSourceFeedAdapter."""

    def setUp(self):
        self.clock = MarketClock()
        self.pacing_calls: List[float] = []

    def mock_sleep(self, seconds: float):
        self.pacing_calls.append(seconds)

    # -------------------------------------------------------------------------
    # 1. Interface Compatibility & Inheritance
    # -------------------------------------------------------------------------
    def test_interface_compatibility(self):
        """Verify DualSourceFeedAdapter is a compliant subclass of MarketDataFeedAdapter."""
        gateway = MockSyncGateway()
        adapter = DualSourceFeedAdapter(gateway=gateway, clock=self.clock)
        self.assertIsInstance(adapter, MarketDataFeedAdapter)
        self.assertTrue(hasattr(adapter, "fetch_and_evaluate"))
        self.assertTrue(hasattr(adapter, "evaluate_readiness"))

    # -------------------------------------------------------------------------
    # 2. Offline Zero-Network Operation
    # -------------------------------------------------------------------------
    def test_offline_operation_zero_network(self):
        """Verify adapter operates 100% offline with zero network requests."""
        spy_candles = generate_completed_dual_source_candles("SPY", count=200)
        gateway = MockSyncGateway({
            "SPY": DailySyncResult(
                symbol="SPY",
                state=FeedState.LIVE_IEX_HYBRID,
                success=True,
                candles=spy_candles,
            )
        })
        adapter = DualSourceFeedAdapter(
            gateway=gateway,
            clock=self.clock,
            sleep_fn=self.mock_sleep,
        )
        res = adapter.fetch_and_evaluate(stock_universe=["SPY"], current_time=datetime(2026, 11, 16, 21, 0, tzinfo=UTC))
        self.assertIsInstance(res, FeedReadinessResult)
        self.assertTrue(res.is_ready)
        self.assertEqual(res.status, ReadinessStatus.READY)
        self.assertIn("SPY", res.proxy_candles)
        self.assertEqual(len(res.proxy_candles["SPY"]), 200)

    # -------------------------------------------------------------------------
    # 3. Identical Indicator Outputs
    # -------------------------------------------------------------------------
    def test_identical_indicator_outputs(self):
        """Verify DualSourceCandles produce mathematically identical indicators to OHLCVCandles."""
        dual_candles = generate_completed_dual_source_candles("SPY", count=50, base_price=150.0)
        # Create identical raw OHLCVCandles
        standard_candles = [
            OHLCVCandle(
                ticker=c.ticker,
                interval=c.interval,
                timestamp=c.timestamp,
                open=c.open,
                high=c.high,
                low=c.low,
                close=c.close,
                volume=c.volume,
                status=c.status,
            )
            for c in dual_candles
        ]

        # SMA
        sma_dual = calculate_sma(dual_candles, period=20)
        sma_std = calculate_sma(standard_candles, period=20)
        self.assertEqual(sma_dual, sma_std)

        # ATR
        atr_dual = calculate_atr(dual_candles, period=14)
        atr_std = calculate_atr(standard_candles, period=14)
        self.assertEqual(atr_dual, atr_std)

        # RSI
        rsi_dual = calculate_rsi(dual_candles, period=14)
        rsi_std = calculate_rsi(standard_candles, period=14)
        self.assertEqual(rsi_dual, rsi_std)

        # ADV & RVOL
        adv_dual = calculate_average_volume(dual_candles, period=20)
        adv_std = calculate_average_volume(standard_candles, period=20)
        self.assertEqual(adv_dual.value, adv_std.value)

        rvol_dual = calculate_relative_volume(dual_candles, period=20)
        rvol_std = calculate_relative_volume(standard_candles, period=20)
        self.assertEqual(rvol_dual.value, rvol_std.value)

    # -------------------------------------------------------------------------
    # 4. Correct Yahoo Consolidated Volume Provenance
    # -------------------------------------------------------------------------
    def test_correct_yahoo_volume_provenance(self):
        """Verify volume is strictly Yahoo composite volume and IEX volume is auxiliary."""
        dual_candles = generate_completed_dual_source_candles(
            "AAPL",
            count=60,
            consolidated_volume=65_000_000,
            auxiliary_iex_volume=1_500_000,
        )
        c = dual_candles[-1]
        self.assertEqual(c.volume, 65_000_000)
        self.assertEqual(c.auxiliary_iex_volume, 1_500_000)
        self.assertTrue(c.is_consolidated_volume)
        self.assertEqual(c.price_source, "ALPACA_IEX")
        self.assertEqual(c.volume_source, "YAHOO_CONSOLIDATED")

        # ADV must reflect consolidated volume (65M), not IEX volume (1.5M)
        adv = calculate_average_volume(dual_candles, period=20)
        self.assertEqual(adv.value, Decimal("65000000.00"))

    # -------------------------------------------------------------------------
    # 5. Rejection of Raw Alpaca IEX Candles
    # -------------------------------------------------------------------------
    def test_rejection_of_raw_alpaca_iex_candles(self):
        """Verify raw unconsolidated AlpacaCandles raise UnconsolidatedVolumeError."""
        now_utc = datetime(2026, 11, 16, 21, 0, tzinfo=UTC)
        raw_alpaca_candle = AlpacaCandle(
            ticker="SPY",
            interval="1d",
            timestamp=now_utc,
            open=Decimal("100.0"),
            high=Decimal("102.0"),
            low=Decimal("99.0"),
            close=Decimal("101.0"),
            volume=1_200_000,
            status=CandleStatus.COMPLETE,
            is_consolidated_volume=False,  # Strictly False
            feed_classification="IEX",
        )

        adapter = DualSourceFeedAdapter(
            gateway=MockSyncGateway(),
            clock=self.clock,
            sleep_fn=self.mock_sleep,
        )

        with self.assertRaises(UnconsolidatedVolumeError):
            adapter.evaluate_readiness(
                proxy_candles_map={"SPY": [raw_alpaca_candle]},
                current_time=now_utc,
            )

    # -------------------------------------------------------------------------
    # 6. Missing Volume & Session-Date Mismatch Fail-Closed
    # -------------------------------------------------------------------------
    def test_missing_volume_fails_closed(self):
        """Verify VOLUME_UNVERIFIABLE excludes stock and fails closed."""
        spy_candles = generate_completed_dual_source_candles("SPY", count=200)
        gateway = MockSyncGateway({
            "SPY": DailySyncResult(
                symbol="SPY",
                state=FeedState.LIVE_IEX_HYBRID,
                success=True,
                candles=spy_candles,
            ),
            "AAPL": DailySyncResult(
                symbol="AAPL",
                state=FeedState.VOLUME_UNVERIFIABLE,
                success=False,
                error_message="Yahoo consolidated volume missing",
            ),
        })

        adapter = DualSourceFeedAdapter(
            gateway=gateway,
            clock=self.clock,
            sleep_fn=self.mock_sleep,
        )
        res = adapter.fetch_and_evaluate(
            stock_universe=["AAPL"],
            current_time=datetime(2026, 11, 16, 21, 0, tzinfo=UTC),
        )
        self.assertTrue(res.is_ready)  # SPY passed
        self.assertNotIn("AAPL", res.included_tickers)
        self.assertTrue(any(e.ticker == "AAPL" for e in res.excluded_tickers))

    def test_session_date_mismatch_fails_closed(self):
        """Verify FEED_DEGRADED from date mismatch excludes symbol."""
        spy_candles = generate_completed_dual_source_candles("SPY", count=200)
        gateway = MockSyncGateway({
            "SPY": DailySyncResult(
                symbol="SPY",
                state=FeedState.LIVE_IEX_HYBRID,
                success=True,
                candles=spy_candles,
            ),
            "MSFT": DailySyncResult(
                symbol="MSFT",
                state=FeedState.FEED_DEGRADED,
                success=False,
                error_message="Session date mismatch: Alpaca=2026-11-16 != Yahoo=2026-11-15",
            ),
        })

        adapter = DualSourceFeedAdapter(
            gateway=gateway,
            clock=self.clock,
            sleep_fn=self.mock_sleep,
        )
        res = adapter.fetch_and_evaluate(
            stock_universe=["MSFT"],
            current_time=datetime(2026, 11, 16, 21, 0, tzinfo=UTC),
        )
        self.assertNotIn("MSFT", res.included_tickers)
        self.assertTrue(any(e.ticker == "MSFT" for e in res.excluded_tickers))

    # -------------------------------------------------------------------------
    # 7. Forming Bar Isolation
    # -------------------------------------------------------------------------
    def test_forming_bars_strictly_isolated(self):
        """Verify forming intraday bars are filtered out and not included in completed screening series."""
        candles = generate_completed_dual_source_candles("SPY", count=200)
        # Append an active forming bar
        forming_bar = DualSourceCandle(
            ticker="SPY",
            interval="1d",
            timestamp=datetime(2026, 11, 17, 14, 30, tzinfo=UTC),
            open=Decimal("150.0"),
            high=Decimal("152.0"),
            low=Decimal("149.0"),
            close=Decimal("151.0"),
            volume=15_000_000,
            status=CandleStatus.FORMING,  # Actively forming!
            session_date=date(2026, 11, 17),
            is_consolidated_volume=True,
            price_source="ALPACA_IEX",
            volume_source="YAHOO_CONSOLIDATED",
            auxiliary_iex_volume=300_000,
        )
        candles_with_forming = candles + [forming_bar]

        gateway = MockSyncGateway({
            "SPY": DailySyncResult(
                symbol="SPY",
                state=FeedState.LIVE_IEX_HYBRID,
                success=True,
                candles=candles_with_forming,
            )
        })

        adapter = DualSourceFeedAdapter(
            gateway=gateway,
            clock=self.clock,
            sleep_fn=self.mock_sleep,
        )
        res = adapter.fetch_and_evaluate(
            stock_universe=["SPY"],
            current_time=datetime(2026, 11, 17, 15, 0, tzinfo=UTC),
        )
        # Verify proxy candles contains exactly the 200 completed bars, zero forming bars
        spy_res = res.proxy_candles.get("SPY", [])
        self.assertEqual(len(spy_res), 200)
        self.assertTrue(all(c.status == CandleStatus.COMPLETE for c in spy_res))

    # -------------------------------------------------------------------------
    # 8. Synchronous Bounded Pacing
    # -------------------------------------------------------------------------
    def test_synchronous_bounded_pacing(self):
        """Verify 0.2s pacing delay is invoked synchronously between requests."""
        spy_candles = generate_completed_dual_source_candles("SPY", count=200)
        gateway = MockSyncGateway({
            "SPY": DailySyncResult(symbol="SPY", state=FeedState.LIVE_IEX_HYBRID, success=True, candles=spy_candles),
            "QQQ": DailySyncResult(symbol="QQQ", state=FeedState.LIVE_IEX_HYBRID, success=True, candles=spy_candles),
            "IWM": DailySyncResult(symbol="IWM", state=FeedState.LIVE_IEX_HYBRID, success=True, candles=spy_candles),
            "AAPL": DailySyncResult(symbol="AAPL", state=FeedState.LIVE_IEX_HYBRID, success=True, candles=spy_candles),
        })

        adapter = DualSourceFeedAdapter(
            gateway=gateway,
            clock=self.clock,
            pacing_delay_seconds=0.25,
            sleep_fn=self.mock_sleep,
        )
        adapter.fetch_and_evaluate(
            stock_universe=["AAPL"],
            current_time=datetime(2026, 11, 16, 21, 0, tzinfo=UTC),
        )

        # 3 proxies (SPY, QQQ, IWM) + 1 stock (AAPL):
        # SPY (no delay), QQQ (0.25), IWM (0.25), AAPL (0.25) -> exactly 3 pacing calls
        self.assertEqual(len(self.pacing_calls), 3)
        self.assertTrue(all(d == 0.25 for d in self.pacing_calls))

    # -------------------------------------------------------------------------
    # 9. Strategy Parameters and Risk Rules Invariance
    # -------------------------------------------------------------------------
    def test_strategy_and_risk_parameters_invariance(self):
        """Verify frozen strategy parameters (RVOL threshold, 0.50% risk-per-trade) remain intact."""
        risk_cfg = RiskConfig()
        self.assertEqual(risk_cfg.risk_per_trade_pct, Decimal("0.005"))  # Exactly 0.50% rule preserved!
        self.assertEqual(risk_cfg.starting_virtual_capital_usd, Decimal("5000.00"))
        self.assertEqual(risk_cfg.max_simultaneous_positions, 4)

        criteria = ScreeningCriteria()
        self.assertEqual(criteria.min_rvol, Decimal("0.8000"))  # RVOL threshold preserved!
        self.assertEqual(criteria.min_price, Decimal("10.00"))
        self.assertEqual(criteria.max_price, Decimal("2000.00"))
        self.assertEqual(criteria.min_history_bars, 25)


if __name__ == "__main__":
    unittest.main()
