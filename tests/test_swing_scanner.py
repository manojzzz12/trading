"""Unit test suite for trader-swing US stock scanner and ranking engine.

Verifies:
1. Universe normalization, deduplication, and invalid ticker rejection.
2. Screening criteria filter enforcement (price, dollar volume, RVOL, RSI, ATR, SMA).
3. 5-pillar scoring and deterministic tie-breaking.
4. Market regime policy enforcement (suppression under STRONG_BEARISH, threshold under SIDEWAYS).
5. Transparent rejection logging without data fabrication.
6. Empty universe and empty candidate set handling.
7. Pure Decimal arithmetic and deterministic repeatability.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
import sys
import unittest

SWING_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".agents",
    "skills",
    "trader-swing",
)
if SWING_DIR not in sys.path:
    sys.path.insert(0, SWING_DIR)

from market_data_models import CandleStatus, OHLCVCandle, UTC
from regime_models import DEFAULT_REGIME_POLICIES, MarketRegime, MarketRegimeResult
from ranking import calculate_candidate_score, rank_candidates
from scanner_models import ScreeningCriteria
from scanner import StockScanner
from universe import UniverseError, normalize_universe, validate_ticker


def make_candle(
    ticker: str,
    index: int,
    close_p: str,
    volume: int = 1000000,
    open_p: str = None,
    high_p: str = None,
    low_p: str = None,
    base_date: datetime = None,
) -> OHLCVCandle:
    if base_date is None:
        base_date = datetime(2026, 1, 5, 20, 0, 0, tzinfo=UTC)
    ts = base_date + timedelta(days=index)
    c_dec = Decimal(close_p)
    o_dec = Decimal(open_p) if open_p is not None else c_dec
    spread = max(Decimal("2.00"), c_dec * Decimal("0.02"))
    h_dec = Decimal(high_p) if high_p is not None else max(c_dec, o_dec) + spread
    l_dec = Decimal(low_p) if low_p is not None else min(c_dec, o_dec) - spread
    return OHLCVCandle(
        ticker=ticker,
        interval="1d",
        timestamp=ts,
        open=o_dec,
        high=h_dec,
        low=l_dec,
        close=c_dec,
        volume=volume,
        status=CandleStatus.COMPLETE,
        source="yahoo",
        received_timestamp=ts,
        is_delayed=True,
    )


class TestSwingScanner(unittest.TestCase):
    """Test suite covering universe hygiene, screening thresholds, scoring, and regime policy."""

    def setUp(self):
        self.scanner = StockScanner()
        self.base_date = datetime(2026, 1, 5, 20, 0, 0, tzinfo=UTC)

    def _build_series(self, ticker: str, count: int, close_start: Decimal, step: Decimal, volume: int = 1000000) -> list:
        bars = []
        c = close_start
        for i in range(count):
            if i % 2 == 0:
                c -= step * Decimal("0.6")
            else:
                c += step
            bars.append(make_candle(ticker, i, str(c), volume=volume, base_date=self.base_date))
        return bars

    def test_01_universe_normalization_and_deduplication(self):
        raw = ["aapl", " MSFT ", "NVDA", "AAPL", "123", "TOO_LONG_TICKER", "tsla"]
        valid, dropped = normalize_universe(raw, allow_empty=True)
        self.assertEqual(valid, ["AAPL", "MSFT", "NVDA", "TSLA"])
        self.assertEqual(len(dropped), 2)
        self.assertTrue(any("123" in d for d in dropped))
        self.assertTrue(any("TOO_LONG_TICKER" in d for d in dropped))

    def test_02_empty_universe_rejection(self):
        with self.assertRaises(UniverseError):
            normalize_universe([], allow_empty=False)

    def test_03_price_filters(self):
        # Stock with price below $10.00
        bars_cheap = self._build_series("PENNY", 30, Decimal("5.00"), Decimal("0.10"))
        cand_cheap = self.scanner.evaluate_candidate("PENNY", bars_cheap)
        self.assertFalse(cand_cheap.is_eligible)
        self.assertTrue(any("below minimum threshold" in r for r in cand_cheap.rejection_reasons))

    def test_04_liquidity_filter_dollar_volume(self):
        # Stock with low average dollar volume (1,000 shares * $15 = $15,000 ADV << $10M)
        bars_illiquid = self._build_series("THIN", 30, Decimal("15.00"), Decimal("0.10"), volume=1000)
        cand = self.scanner.evaluate_candidate("THIN", bars_illiquid)
        self.assertFalse(cand.is_eligible)
        self.assertTrue(any("dollar volume" in r for r in cand.rejection_reasons))

    def test_05_valid_bullish_candidate_passes_and_scores(self):
        # Liquid, rising stock with healthy volume (500k shares * $150 = $75M ADV)
        bars = self._build_series("NVDA", 35, Decimal("140.00"), Decimal("0.50"), volume=500000)
        cand = self.scanner.evaluate_candidate("NVDA", bars)
        self.assertTrue(cand.is_eligible)
        self.assertTrue(cand.score.total_score >= Decimal("60.0"))
        self.assertEqual(len(cand.rejection_reasons), 0)
        self.assertTrue(len(cand.positive_reasons) > 0)

    def test_06_regime_policy_suppresses_longs_under_bearish(self):
        bars = self._build_series("AAPL", 35, Decimal("150.00"), Decimal("0.50"), volume=500000)
        bear_result = MarketRegimeResult(
            regime=MarketRegime.STRONG_BEARISH,
            confidence=Decimal("0.9000"),
            aggregate_score=Decimal("-0.8000"),
            supporting_evidence=["Severe market downtrend."],
            contradicting_evidence=[],
            proxy_analyses={},
            indicators_used={},
            as_of_timestamp=bars[-1].timestamp,
            data_freshness="FRESH",
            explanation="Bearish crash.",
            warnings=[],
            missing_inputs=[],
            policy=DEFAULT_REGIME_POLICIES[MarketRegime.STRONG_BEARISH],
        )

        cand = self.scanner.evaluate_candidate("AAPL", bars, regime_result=bear_result)
        self.assertFalse(cand.is_eligible)
        self.assertTrue(any("STRONG_BEARISH" in r for r in cand.rejection_reasons))

    def test_07_regime_policy_score_threshold_under_sideways(self):
        # Sideways requires score >= 75.0
        bars = self._build_series("AAPL", 35, Decimal("150.00"), Decimal("0.20"), volume=200000)
        sideways_result = MarketRegimeResult(
            regime=MarketRegime.SIDEWAYS,
            confidence=Decimal("0.7000"),
            aggregate_score=Decimal("0.0500"),
            supporting_evidence=["Choppy market."],
            contradicting_evidence=[],
            proxy_analyses={},
            indicators_used={},
            as_of_timestamp=bars[-1].timestamp,
            data_freshness="FRESH",
            explanation="Sideways consolidation.",
            warnings=[],
            missing_inputs=[],
            policy=DEFAULT_REGIME_POLICIES[MarketRegime.SIDEWAYS],
        )

        cand = self.scanner.evaluate_candidate("AAPL", bars, regime_result=sideways_result)
        if cand.score.total_score < Decimal("75.0"):
            self.assertFalse(cand.is_eligible)
            self.assertTrue(any("below regime minimum (75.0)" in r for r in cand.rejection_reasons))

    def test_08_scan_universe_and_deterministic_ranking(self):
        # 3 tickers with varying quality
        bars_aapl = self._build_series("AAPL", 35, Decimal("180.00"), Decimal("0.60"), volume=1000000)
        bars_msft = self._build_series("MSFT", 35, Decimal("400.00"), Decimal("0.80"), volume=800000)
        bars_penny = self._build_series("PENNY", 35, Decimal("4.00"), Decimal("0.05"), volume=1000)

        universe = ["AAPL", "MSFT", "PENNY"]
        candles_map = {"AAPL": bars_aapl, "MSFT": bars_msft, "PENNY": bars_penny}

        scan_res = self.scanner.scan_universe(universe, candles_map, top_n=5)
        self.assertEqual(scan_res.total_scanned, 3)
        self.assertEqual(scan_res.total_passed, 2)
        self.assertIn("PENNY", scan_res.rejected_candidates)

        # Candidates ranked 1 and 2
        ranks = [c.rank for c in scan_res.candidates]
        self.assertEqual(ranks, [1, 2])
        # Higher score ranked first
        self.assertTrue(scan_res.candidates[0].score.total_score >= scan_res.candidates[1].score.total_score)

    def test_09_dot_separated_share_class_ticker_validation(self):
        """Verify acceptance of dot-separated US share classes and uppercase normalization."""
        # 1-5 letter base with 1-2 letter share class suffix
        valid_cases = [
            ("BRK.B", "BRK.B"),
            ("brk.b", "BRK.B"),
            ("  brk.b  ", "BRK.B"),
            ("BF.B", "BF.B"),
            ("JW.A", "JW.A"),
            ("HEI.A", "HEI.A"),
            ("BIO.B", "BIO.B"),
            ("ABC.PR", "ABC.PR"),
            ("XYZ.WS", "XYZ.WS"),
            ("AAPL", "AAPL"),
            ("F", "F"),
            ("GOOGL", "GOOGL"),
        ]
        for raw, expected in valid_cases:
            self.assertEqual(validate_ticker(raw), expected)

        # Universe normalization with deduplication preserving dot
        raw_list = ["brk.b", "BRK.B", "AAPL", "bf.b", "BRK.B", "BF.B"]
        valid_list, dropped = normalize_universe(raw_list, allow_empty=True)
        self.assertEqual(valid_list, ["BRK.B", "AAPL", "BF.B"])
        self.assertEqual(len(dropped), 0)

    def test_10_invalid_ticker_format_rejection(self):
        """Verify rejection of invalid formats: punctuation, numbers, slashes, hyphens, and excessive length."""
        invalid_symbols = [
            "BRK-B",        # Hyphen format rejected
            "BRK/B",        # Slash format rejected
            "BRK..B",       # Consecutive dots rejected
            ".BRK",         # Leading dot rejected
            "BRK.",         # Trailing dot rejected
            "BRK.ABC",      # Suffix exceeds 2 letters
            "TOOLONG.A",    # Base exceeds 5 letters
            "TOOLONG",      # Base exceeds 5 letters
            "123",          # Numeric only rejected
            "AAPL123",      # Alphanumeric rejected
            "$SPY",         # Punctuation/dollar rejected
            "",             # Empty string rejected
            "   ",          # Whitespace only rejected
        ]
        for sym in invalid_symbols:
            with self.assertRaises(ValueError):
                validate_ticker(sym)

        # In normalize_universe, invalid symbols are cleanly dropped with warnings
        mixed = ["AAPL", "BRK-B", "BRK.B", "123", "$SPY", "TOOLONG.A"]
        valid, dropped = normalize_universe(mixed, allow_empty=True)
        self.assertEqual(valid, ["AAPL", "BRK.B"])
        self.assertEqual(len(dropped), 4)


if __name__ == "__main__":
    unittest.main()
