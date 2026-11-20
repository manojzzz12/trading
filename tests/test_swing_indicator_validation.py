"""Unit test suite for trader-swing technical indicator validation.

Verifies:
1. Rejection of empty candle sequence with InsufficientDataError.
2. Rejection of sequence with fewer bars than min_count.
3. Strict rejection of FORMING candles when require_complete=True.
4. Strict rejection of UNKNOWN candles when require_complete=True.
5. Incomplete candle exception carries ticker, timestamp, and status metadata.
6. Acceptance of terminal FORMING candle when require_complete=False.
7. Sequence continuity enforcement (duplicate timestamps rejected).
8. Chronological ordering enforcement (out-of-order bars rejected).
9. Consistent ticker enforcement (mixed tickers rejected).
10. Consistent interval enforcement (mixed intervals rejected).
11. Expected interval enforcement when specified.
12. Single candle price invariant enforcement during sequence validation.
"""

from datetime import datetime, timedelta
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

from market_data_models import (
    CandleStatus,
    CandleValidationError,
    OHLCVCandle,
    SequenceValidationError,
    UTC,
)
from indicator_validation import (
    IncompleteCandleError,
    IndicatorError,
    InsufficientDataError,
    validate_indicator_candles,
)


def make_candle(
    ticker: str = "AAPL",
    interval: str = "1d",
    timestamp: datetime = None,
    open_p: str = "150.00",
    high_p: str = None,
    low_p: str = None,
    close_p: str = "150.00",
    volume: int = 1000000,
    status: CandleStatus = CandleStatus.COMPLETE,
) -> OHLCVCandle:
    if timestamp is None:
        timestamp = datetime(2026, 10, 14, 20, 0, 0, tzinfo=UTC)
    c_dec = Decimal(close_p)
    o_dec = Decimal(open_p)
    h_dec = Decimal(high_p) if high_p is not None else max(c_dec, o_dec) + Decimal("2.00")
    l_dec = Decimal(low_p) if low_p is not None else min(c_dec, o_dec) - Decimal("2.00")
    return OHLCVCandle(
        ticker=ticker,
        interval=interval,
        timestamp=timestamp,
        open=o_dec,
        high=h_dec,
        low=l_dec,
        close=c_dec,
        volume=volume,
        status=status,
        source="yahoo",
        received_timestamp=timestamp,
        is_delayed=True,
    )


class TestSwingIndicatorValidation(unittest.TestCase):
    """Test suite covering indicator candle sequence validation and incomplete bar exclusion."""

    def setUp(self):
        self.base_time = datetime(2026, 10, 1, 20, 0, 0, tzinfo=UTC)
        self.candles = [
            make_candle(timestamp=self.base_time + timedelta(days=i), close_p=str(150 + i))
            for i in range(25)
        ]

    def test_01_empty_sequence_rejected(self):
        with self.assertRaises(InsufficientDataError) as ctx:
            validate_indicator_candles([], min_count=1)
        self.assertEqual(ctx.exception.actual_count, 0)
        self.assertEqual(ctx.exception.required_count, 1)

    def test_02_insufficient_bars_rejected(self):
        with self.assertRaises(InsufficientDataError) as ctx:
            validate_indicator_candles(self.candles[:10], min_count=20)
        self.assertEqual(ctx.exception.actual_count, 10)
        self.assertEqual(ctx.exception.required_count, 20)

    def test_03_valid_complete_sequence_passes(self):
        # Should execute cleanly without raising any exception
        validate_indicator_candles(self.candles, min_count=20, require_complete=True)

    def test_04_terminal_forming_candle_rejected_by_default(self):
        forming_series = list(self.candles[:19]) + [
            make_candle(
                timestamp=self.base_time + timedelta(days=19),
                status=CandleStatus.FORMING,
            )
        ]
        with self.assertRaises(IncompleteCandleError) as ctx:
            validate_indicator_candles(forming_series, min_count=10, require_complete=True)
        self.assertEqual(ctx.exception.status, CandleStatus.FORMING)
        self.assertEqual(ctx.exception.ticker, "AAPL")

    def test_05_intermediate_forming_candle_rejected_by_default(self):
        bad_series = list(self.candles[:20])
        bad_series[10] = make_candle(
            timestamp=self.candles[10].timestamp,
            status=CandleStatus.FORMING,
        )
        with self.assertRaises(IncompleteCandleError) as ctx:
            validate_indicator_candles(bad_series, min_count=10, require_complete=True)
        self.assertEqual(ctx.exception.status, CandleStatus.FORMING)

    def test_06_initial_forming_candle_rejected(self):
        bad_series = list(self.candles[:20])
        bad_series[0] = make_candle(
            timestamp=self.candles[0].timestamp,
            status=CandleStatus.FORMING,
        )
        with self.assertRaises(IncompleteCandleError):
            validate_indicator_candles(bad_series, min_count=10, require_complete=True)

    def test_07_unknown_candle_rejected_when_complete_required(self):
        bad_series = list(self.candles[:20])
        bad_series[-1] = make_candle(
            timestamp=self.candles[19].timestamp,
            status=CandleStatus.UNKNOWN,
        )
        with self.assertRaises(IncompleteCandleError) as ctx:
            validate_indicator_candles(bad_series, min_count=10, require_complete=True)
        self.assertEqual(ctx.exception.status, CandleStatus.UNKNOWN)

    def test_08_terminal_forming_candle_accepted_when_require_complete_false(self):
        forming_series = list(self.candles[:19]) + [
            make_candle(
                timestamp=self.base_time + timedelta(days=19),
                status=CandleStatus.FORMING,
            )
        ]
        # require_complete=False allows valid terminal forming bar through sequence validation
        validate_indicator_candles(forming_series, min_count=10, require_complete=False)

    def test_09_duplicate_timestamp_rejected(self):
        bad_series = list(self.candles[:10])
        # Inject duplicate timestamp at index 5
        bad_series[5] = make_candle(
            timestamp=bad_series[4].timestamp,
            close_p="155.00",
        )
        with self.assertRaises(SequenceValidationError):
            validate_indicator_candles(bad_series, min_count=5)

    def test_10_out_of_order_sequence_rejected(self):
        bad_series = list(self.candles[:10])
        # Swap indices 4 and 5
        bad_series[4], bad_series[5] = bad_series[5], bad_series[4]
        with self.assertRaises(SequenceValidationError):
            validate_indicator_candles(bad_series, min_count=5)

    def test_11_mixed_tickers_rejected(self):
        bad_series = list(self.candles[:10])
        bad_series[7] = make_candle(
            ticker="MSFT",
            timestamp=bad_series[7].timestamp,
        )
        with self.assertRaises(SequenceValidationError):
            validate_indicator_candles(bad_series, min_count=5)

    def test_12_mixed_intervals_rejected(self):
        bad_series = list(self.candles[:10])
        bad_series[3] = make_candle(
            interval="1h",
            timestamp=bad_series[3].timestamp,
        )
        with self.assertRaises(SequenceValidationError):
            validate_indicator_candles(bad_series, min_count=5)

    def test_13_expected_interval_mismatch_rejected(self):
        with self.assertRaises(SequenceValidationError):
            validate_indicator_candles(self.candles[:10], expected_interval="1h")

    def test_14_candle_price_invariant_failure_rejected(self):
        bad_series = list(self.candles[:10])
        # High lower than low
        bad_series[2] = make_candle(
            timestamp=bad_series[2].timestamp,
            high_p="140.00",
            low_p="150.00",
        )
        with self.assertRaises(CandleValidationError):
            validate_indicator_candles(bad_series, min_count=5)


if __name__ == "__main__":
    unittest.main()
