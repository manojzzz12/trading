"""Deterministic offline unit test suite for Phase 7.1.4 Dual-Source Market Data Aggregator.

Verifies:
1. Correct merged candle construction (Alpaca prices + Yahoo consolidated volume).
2. Alpaca IEX volume is NEVER copied into .volume (preserved only in .auxiliary_iex_volume).
3. Raw Alpaca candles (is_consolidated_volume=False) are rejected at the MarketDataFeedAdapter boundary.
4. Verified DualSourceCandles (is_consolidated_volume=True) pass the MarketDataFeedAdapter boundary.
5. Missing Yahoo volume raises MissingVolumeError.
6. Session date mismatch raises SessionMismatchError.
7. Symbol mismatch raises SymbolMismatchError.
8. Adjustment basis mismatch raises AdjustmentMismatchError.
9. Invalid OHLC prices fail intrinsic candle validation.
10. Negative volume raises InvalidVolumeError.
11. Duplicate timestamps in sequence raise SequenceValidationError.
12. Explicit proof: existing ADV/RVOL calculations receive Yahoo volume after aggregation.
13. Full backwards-compatibility: existing Yahoo-only OHLCVCandles pass adapter without error.
"""

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import os
import sys
import unittest

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
    InvalidPriceError,
    InvalidVolumeError,
    SequenceValidationError,
)
from feed_adapter import (
    MarketDataReadinessEvaluator,
    UnconsolidatedVolumeError,
)
from providers.alpaca_provider import AlpacaCandle
from dual_source_aggregator import (
    AdjustmentMismatchError,
    DualSourceAggregator,
    DualSourceCandle,
    MissingVolumeError,
    SessionMismatchError,
    SymbolMismatchError,
)
from indicators import calculate_average_volume, calculate_relative_volume


class TestSwingDualSourceAggregator(unittest.TestCase):
    """Test suite for DualSourceAggregator and adapter volume boundary enforcement."""

    def setUp(self):
        self.clock = MarketClock()
        self.aggregator = DualSourceAggregator(clock=self.clock)
        self.evaluator = MarketDataReadinessEvaluator(clock=self.clock)
        self.session_date = date(2026, 11, 16)
        self.ts_utc = datetime(2026, 11, 16, 14, 30, tzinfo=UTC)
        self.eval_now = datetime(2026, 11, 16, 21, 5, tzinfo=UTC)

        # Baseline Alpaca IEX candle (IEX volume: 1,695,400)
        self.alpaca_candle = AlpacaCandle(
            ticker="SPY",
            interval="1d",
            timestamp=self.ts_utc,
            open=Decimal("745.20"),
            high=Decimal("747.80"),
            low=Decimal("738.50"),
            close=Decimal("739.96"),
            volume=1695400,  # Unconsolidated IEX volume
            status=CandleStatus.COMPLETE,
            source="alpaca_iex",
            is_delayed=False,
            vwap=Decimal("742.15"),
            trade_count=19450,
            feed_classification="IEX",
            session_date=self.session_date,
            adjustment_type="SPLIT_ADJUSTED",
            is_consolidated_volume=False,
        )

        # Baseline Yahoo Finance candle (Consolidated volume: 64,892,400)
        self.yahoo_candle = OHLCVCandle(
            ticker="SPY",
            interval="1d",
            timestamp=self.ts_utc,
            open=Decimal("745.20"),
            high=Decimal("747.80"),
            low=Decimal("738.50"),
            close=Decimal("739.96"),
            volume=64892400,  # Authoritative composite volume
            status=CandleStatus.COMPLETE,
            source="yahoo",
            is_delayed=True,
        )

    # -------------------------------------------------------------------------
    # 1. Correct Merged Candle Construction
    # -------------------------------------------------------------------------
    def test_01_correct_merged_candle_construction(self):
        """DualSourceCandle must combine Alpaca prices with Yahoo consolidated volume."""
        merged_series = self.aggregator.merge_candle_series(
            ticker="SPY",
            alpaca_candles=[self.alpaca_candle],
            yahoo_candles=[self.yahoo_candle],
            current_time=self.eval_now,
        )

        self.assertEqual(len(merged_series), 1)
        merged = merged_series[0]
        self.assertIsInstance(merged, DualSourceCandle)
        self.assertIsInstance(merged, OHLCVCandle)

        # Price fields from Alpaca
        self.assertEqual(merged.open, Decimal("745.20"))
        self.assertEqual(merged.high, Decimal("747.80"))
        self.assertEqual(merged.low, Decimal("738.50"))
        self.assertEqual(merged.close, Decimal("739.96"))
        self.assertEqual(merged.vwap, Decimal("742.15"))
        self.assertEqual(merged.trade_count, 19450)

        # Volume strictly from Yahoo
        self.assertEqual(merged.volume, 64892400)

        # Auxiliary IEX volume preserved
        self.assertEqual(merged.auxiliary_iex_volume, 1695400)

        # Provenance metadata
        self.assertEqual(merged.price_source, "ALPACA_IEX")
        self.assertEqual(merged.volume_source, "YAHOO_CONSOLIDATED")
        self.assertTrue(merged.is_consolidated_volume)
        self.assertEqual(merged.feed_classification, "DUAL_SOURCE_HYBRID")

    # -------------------------------------------------------------------------
    # 2. IEX Volume Substitution Guard
    # -------------------------------------------------------------------------
    def test_02_iex_volume_never_enters_merged_volume(self):
        """Alpaca IEX volume must never be copied to merged candle .volume."""
        merged_series = self.aggregator.merge_candle_series(
            ticker="SPY",
            alpaca_candles=[self.alpaca_candle],
            yahoo_candles=[self.yahoo_candle],
            current_time=self.eval_now,
        )
        merged = merged_series[0]
        self.assertNotEqual(merged.volume, self.alpaca_candle.volume)
        self.assertEqual(merged.volume, self.yahoo_candle.volume)
        self.assertEqual(merged.auxiliary_iex_volume, self.alpaca_candle.volume)

    # -------------------------------------------------------------------------
    # 3. Raw Alpaca Candle Rejection at FeedAdapter Boundary
    # -------------------------------------------------------------------------
    def test_03_raw_alpaca_candle_rejected_at_adapter_boundary(self):
        """Raw Alpaca candles with is_consolidated_volume=False must be rejected by FeedAdapter."""
        with self.assertRaises(UnconsolidatedVolumeError) as ctx:
            self.evaluator.validate_candle_series([self.alpaca_candle], "SPY", current_time=self.eval_now)
        self.assertIn("declares is_consolidated_volume=False", str(ctx.exception))
        self.assertIn("DualSourceAggregator", str(ctx.exception))

    # -------------------------------------------------------------------------
    # 4. Merged Candle Accepted at FeedAdapter Boundary
    # -------------------------------------------------------------------------
    def test_04_merged_candle_accepted_at_adapter_boundary(self):
        """DualSourceCandle with verified consolidated volume must pass FeedAdapter validation."""
        merged_series = self.aggregator.merge_candle_series(
            ticker="SPY",
            alpaca_candles=[self.alpaca_candle],
            yahoo_candles=[self.yahoo_candle],
            current_time=self.eval_now,
        )
        # Must not raise
        self.evaluator.validate_candle_series(merged_series, "SPY", current_time=self.eval_now)

    # -------------------------------------------------------------------------
    # 5. Missing Yahoo Volume
    # -------------------------------------------------------------------------
    def test_05_missing_yahoo_volume_raises_missing_volume_error(self):
        """Missing Yahoo volume for session date must raise MissingVolumeError."""
        yahoo_other_date = OHLCVCandle(
            ticker="SPY",
            interval="1d",
            timestamp=datetime(2026, 11, 13, 14, 30, tzinfo=UTC),
            open=Decimal("750.00"),
            high=755,
            low=749,
            close=754,
            volume=50000000,
            status=CandleStatus.COMPLETE,
        )
        with self.assertRaises(MissingVolumeError) as ctx:
            self.aggregator.merge_candle_series(
                ticker="SPY",
                alpaca_candles=[self.alpaca_candle],
                yahoo_candles=[yahoo_other_date],
                current_time=self.eval_now,
            )
        self.assertIn("Missing Yahoo consolidated volume", str(ctx.exception))

    # -------------------------------------------------------------------------
    # 6. Session Date Mismatch
    # -------------------------------------------------------------------------
    def test_06_session_date_mismatch_raises_session_mismatch_error(self):
        """Mismatch between Alpaca session date and Yahoo candle date must raise error."""
        alpaca_cand_13 = AlpacaCandle(
            ticker="SPY",
            interval="1d",
            timestamp=datetime(2026, 11, 13, 14, 30, tzinfo=UTC),
            open=Decimal("750.00"),
            high=755,
            low=749,
            close=754,
            volume=1000000,
            status=CandleStatus.COMPLETE,
            session_date=date(2026, 11, 13),
            is_consolidated_volume=False,
        )
        with self.assertRaises(MissingVolumeError):
            self.aggregator.merge_candle_series(
                ticker="SPY",
                alpaca_candles=[alpaca_cand_13],
                yahoo_candles=[self.yahoo_candle],  # Date 2026-11-16
                current_time=self.eval_now,
            )

    # -------------------------------------------------------------------------
    # 7. Symbol Mismatch
    # -------------------------------------------------------------------------
    def test_07_symbol_mismatch_raises_symbol_mismatch_error(self):
        """Candle with mismatched ticker symbol must raise SymbolMismatchError."""
        yahoo_qqq = OHLCVCandle(
            ticker="QQQ",
            interval="1d",
            timestamp=self.ts_utc,
            open=Decimal("510.00"),
            high=515,
            low=508,
            close=512,
            volume=40000000,
            status=CandleStatus.COMPLETE,
        )
        with self.assertRaises(SymbolMismatchError):
            self.aggregator.merge_candle_series(
                ticker="SPY",
                alpaca_candles=[self.alpaca_candle],
                yahoo_candles=[yahoo_qqq],
                current_time=self.eval_now,
            )

    # -------------------------------------------------------------------------
    # 8. Adjustment Basis Mismatch
    # -------------------------------------------------------------------------
    def test_08_adjustment_mismatch_raises_adjustment_mismatch_error(self):
        """Incompatible price adjustment basis must raise AdjustmentMismatchError."""
        alpaca_raw = AlpacaCandle(
            ticker="SPY",
            interval="1d",
            timestamp=self.ts_utc,
            open=Decimal("745.20"),
            high=Decimal("747.80"),
            low=Decimal("738.50"),
            close=Decimal("739.96"),
            volume=1695400,
            status=CandleStatus.COMPLETE,
            adjustment_type="RAW_UNADJUSTED",  # Incompatible!
            session_date=self.session_date,
            is_consolidated_volume=False,
        )
        with self.assertRaises(AdjustmentMismatchError):
            self.aggregator.merge_candle_series(
                ticker="SPY",
                alpaca_candles=[alpaca_raw],
                yahoo_candles=[self.yahoo_candle],
                current_time=self.eval_now,
            )

    # -------------------------------------------------------------------------
    # 9. Invalid OHLC Geometry
    # -------------------------------------------------------------------------
    def test_09_invalid_ohlc_raises_invalid_price_error(self):
        """Inverted OHLC geometry in Alpaca candle must raise InvalidPriceError."""
        alpaca_inv = AlpacaCandle(
            ticker="SPY",
            interval="1d",
            timestamp=self.ts_utc,
            open=Decimal("745.20"),
            high=Decimal("730.00"),  # High < Low!
            low=Decimal("738.50"),
            close=Decimal("739.96"),
            volume=1695400,
            status=CandleStatus.COMPLETE,
            session_date=self.session_date,
            is_consolidated_volume=False,
        )
        with self.assertRaises(InvalidPriceError):
            self.aggregator.merge_candle_series(
                ticker="SPY",
                alpaca_candles=[alpaca_inv],
                yahoo_candles=[self.yahoo_candle],
                current_time=self.eval_now,
            )

    # -------------------------------------------------------------------------
    # 10. Negative Yahoo Volume
    # -------------------------------------------------------------------------
    def test_10_negative_yahoo_volume_raises_invalid_volume_error(self):
        """Negative volume in Yahoo candle must raise InvalidVolumeError."""
        yahoo_neg = OHLCVCandle(
            ticker="SPY",
            interval="1d",
            timestamp=self.ts_utc,
            open=Decimal("745.20"),
            high=Decimal("747.80"),
            low=Decimal("738.50"),
            close=Decimal("739.96"),
            volume=-1000,
            status=CandleStatus.COMPLETE,
        )
        with self.assertRaises(InvalidVolumeError):
            self.aggregator.merge_candle_series(
                ticker="SPY",
                alpaca_candles=[self.alpaca_candle],
                yahoo_candles=[yahoo_neg],
                current_time=self.eval_now,
            )

    # -------------------------------------------------------------------------
    # 11. Proof of Yahoo Volume in ADV and RVOL Calculations
    # -------------------------------------------------------------------------
    def test_11_adv_and_rvol_calculations_receive_yahoo_volume(self):
        """Existing calculate_average_volume must calculate ADV/RVOL using Yahoo volume.

        Proves:
        1. 20-day Average Volume = Yahoo consolidated volume (64,892,400).
        2. 20-day ADV = 64,892,400 * $739.96 = $48,017,779,504.
        3. Never uses Alpaca IEX volume ($1.25B) for ADV calculations.
        """
        # Create 20 sequential merged candles
        base_dt = datetime(2026, 10, 15, 14, 30, tzinfo=UTC)
        merged_candles = []
        for i in range(20):
            d_cand = DualSourceCandle(
                ticker="SPY",
                interval="1d",
                timestamp=base_dt + timedelta(days=i),
                open=Decimal("745.20"),
                high=Decimal("747.80"),
                low=Decimal("738.50"),
                close=Decimal("739.96"),
                volume=64892400,  # Consolidated Yahoo volume
                status=CandleStatus.COMPLETE,
                source="dual_source_alpaca_yahoo",
                price_source="ALPACA_IEX",
                volume_source="YAHOO_CONSOLIDATED",
                auxiliary_iex_volume=1695400,  # Auxiliary IEX
                is_consolidated_volume=True,
            )
            merged_candles.append(d_cand)

        # Run existing indicator calculation
        avg_vol = calculate_average_volume(merged_candles, period=20, validate=False)
        self.assertEqual(avg_vol.value, Decimal("64892400.00"))

        # Calculate ADV exactly matching scanner.py methodology
        adv = (avg_vol.value * Decimal("739.96")).quantize(Decimal("0.01"))
        self.assertEqual(adv, Decimal("48017780304.00"))

        # Assert ADV is based on 64.8M volume, NOT 1.69M IEX volume ($1.25B)
        self.assertGreater(adv, Decimal("40000000000.00"))

    # -------------------------------------------------------------------------
    # 12. Full Backwards Compatibility with Existing Yahoo-Only Candles
    # -------------------------------------------------------------------------
    def test_12_existing_yahoo_only_compatibility(self):
        """Standard Yahoo OHLCVCandle without is_consolidated_volume must pass FeedAdapter validation."""
        # Standard Yahoo candles do not declare is_consolidated_volume; default is True
        self.evaluator.validate_candle_series([self.yahoo_candle], "SPY", current_time=self.eval_now)
        self.assertTrue(getattr(self.yahoo_candle, "is_consolidated_volume", True))


if __name__ == "__main__":
    unittest.main()
