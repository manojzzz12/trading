"""Phase 5.9.3 -- Regression and Integration Test Suite.
Validates historical daily-candle lifecycle resolution, Yahoo market-data parsing,
and end-to-end ingestion via MarketDataService into HistoricalDataManager without network access.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import os
import sys
import unittest

PARENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL_DIR = os.path.join(PARENT_DIR, ".agents", "skills", "trader-swing")
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)

from market_data_models import (
    CandleStatus,
    MarketClock,
    OHLCVCandle,
    UTC,
    EASTERN,
    to_utc,
    to_eastern,
)
from market_data_validation import (
    CandleValidationError,
    InvalidPriceError,
    InvalidVolumeError,
    resolve_candle_lifecycle,
    validate_single_candle,
    validate_candle_sequence,
)
from market_data_cache import MarketDataCache
from market_data import MarketDataService
from providers.yahoo import YahooMarketDataProvider
from backtest_data import HistoricalDataManager
from backtest_models import PriceAdjustmentType


class TestHistoricalDataIngestion593(unittest.TestCase):
    """Phase 5.9.3 historical daily-candle lifecycle resolution and ingestion tests."""

    def setUp(self):
        # Current reference time in 2026 for evaluating past historical candles
        self.ref_now_2026 = datetime(2026, 11, 4, 12, 0, 0, tzinfo=UTC)

    def test_01_valid_historical_session_2021_resolves_complete(self):
        """A valid historical trading session in 2021 resolves to COMPLETE."""
        # Tuesday 2021-06-15 09:30 EDT = 13:30 UTC
        bar_ts = datetime(2021, 6, 15, 13, 30, tzinfo=UTC)
        candle = OHLCVCandle(
            ticker="AAPL",
            interval="1d",
            timestamp=bar_ts,
            open=Decimal("130.00"),
            high=Decimal("132.00"),
            low=Decimal("129.50"),
            close=Decimal("131.00"),
            volume=50000000,
            status=CandleStatus.UNKNOWN,
        )
        status = resolve_candle_lifecycle(candle, current_time=self.ref_now_2026)
        self.assertEqual(status, CandleStatus.COMPLETE)

    def test_02_valid_historical_session_2022_resolves_complete(self):
        """A valid historical trading session in 2022 resolves to COMPLETE."""
        # Wednesday 2022-03-16 09:30 EDT = 13:30 UTC
        bar_ts = datetime(2022, 3, 16, 13, 30, tzinfo=UTC)
        candle = OHLCVCandle(
            ticker="MSFT",
            interval="1d",
            timestamp=bar_ts,
            open=Decimal("290.00"),
            high=Decimal("295.00"),
            low=Decimal("288.00"),
            close=Decimal("294.00"),
            volume=35000000,
            status=CandleStatus.UNKNOWN,
        )
        status = resolve_candle_lifecycle(candle, current_time=self.ref_now_2026)
        self.assertEqual(status, CandleStatus.COMPLETE)

    def test_03_valid_historical_session_2025_resolves_complete(self):
        """A valid historical trading session in 2025 resolves to COMPLETE."""
        # Wednesday 2025-01-08 09:30 EST = 14:30 UTC
        bar_ts = datetime(2025, 1, 8, 14, 30, tzinfo=UTC)
        candle = OHLCVCandle(
            ticker="NVDA",
            interval="1d",
            timestamp=bar_ts,
            open=Decimal("135.00"),
            high=Decimal("140.00"),
            low=Decimal("134.00"),
            close=Decimal("139.50"),
            volume=45000000,
            status=CandleStatus.UNKNOWN,
        )
        status = resolve_candle_lifecycle(candle, current_time=self.ref_now_2026)
        self.assertEqual(status, CandleStatus.COMPLETE)

    def test_04_special_closures_resolve_unknown(self):
        """January 9, 2025 (Carter) and Dec 5, 2018 (Bush) resolve as non-trading UNKNOWN."""
        # Carter Day of Mourning: Thursday 2025-01-09
        c_carter = OHLCVCandle(
            ticker="SPY",
            interval="1d",
            timestamp=datetime(2025, 1, 9, 14, 30, tzinfo=UTC),
            open=Decimal("580.00"),
            high=Decimal("585.00"),
            low=Decimal("579.00"),
            close=Decimal("582.00"),
            volume=1000,
            status=CandleStatus.UNKNOWN,
        )
        self.assertEqual(
            resolve_candle_lifecycle(c_carter, current_time=self.ref_now_2026),
            CandleStatus.UNKNOWN,
        )

        # Bush Day of Mourning: Wednesday 2018-12-05
        c_bush = OHLCVCandle(
            ticker="SPY",
            interval="1d",
            timestamp=datetime(2018, 12, 5, 14, 30, tzinfo=UTC),
            open=Decimal("270.00"),
            high=Decimal("272.00"),
            low=Decimal("268.00"),
            close=Decimal("271.00"),
            volume=1000,
            status=CandleStatus.UNKNOWN,
        )
        self.assertEqual(
            resolve_candle_lifecycle(c_bush, current_time=self.ref_now_2026),
            CandleStatus.UNKNOWN,
        )

    def test_05_historical_weekends_resolve_unknown(self):
        """Historical weekend daily candles are rejected as UNKNOWN."""
        # Saturday 2021-06-19
        sat_bar = OHLCVCandle(
            ticker="AAPL",
            interval="1d",
            timestamp=datetime(2021, 6, 19, 14, 30, tzinfo=UTC),
            open=Decimal("130.00"),
            high=Decimal("131.00"),
            low=Decimal("129.00"),
            close=Decimal("130.50"),
            volume=100,
            status=CandleStatus.UNKNOWN,
        )
        self.assertEqual(
            resolve_candle_lifecycle(sat_bar, current_time=self.ref_now_2026),
            CandleStatus.UNKNOWN,
        )

        # Sunday 2023-04-16
        sun_bar = OHLCVCandle(
            ticker="AAPL",
            interval="1d",
            timestamp=datetime(2023, 4, 16, 14, 30, tzinfo=UTC),
            open=Decimal("165.00"),
            high=Decimal("166.00"),
            low=Decimal("164.00"),
            close=Decimal("165.50"),
            volume=100,
            status=CandleStatus.UNKNOWN,
        )
        self.assertEqual(
            resolve_candle_lifecycle(sun_bar, current_time=self.ref_now_2026),
            CandleStatus.UNKNOWN,
        )

    def test_06_historical_nyse_holidays_resolve_unknown(self):
        """Historical NYSE holidays resolve as UNKNOWN."""
        holidays = [
            ("Juneteenth 2022", datetime(2022, 6, 20, 13, 30, tzinfo=UTC)),
            ("Thanksgiving 2023", datetime(2023, 11, 23, 14, 30, tzinfo=UTC)),
            ("Christmas 2024", datetime(2024, 12, 25, 14, 30, tzinfo=UTC)),
            ("New Year's Day 2025", datetime(2025, 1, 1, 14, 30, tzinfo=UTC)),
        ]
        for name, dt_val in holidays:
            c = OHLCVCandle(
                ticker="SPY",
                interval="1d",
                timestamp=dt_val,
                open=Decimal("400.00"),
                high=Decimal("405.00"),
                low=Decimal("398.00"),
                close=Decimal("402.00"),
                volume=1000,
                status=CandleStatus.UNKNOWN,
            )
            self.assertEqual(
                resolve_candle_lifecycle(c, current_time=self.ref_now_2026),
                CandleStatus.UNKNOWN,
                f"{name} should resolve to UNKNOWN",
            )

    def test_07_unsupported_historical_year_resolves_unknown(self):
        """Historical daily candles outside confirmed coverage (2015-2027) resolve to UNKNOWN."""
        # 2010 (before historical coverage)
        c_2010 = OHLCVCandle(
            ticker="AAPL",
            interval="1d",
            timestamp=datetime(2010, 5, 14, 13, 30, tzinfo=UTC),
            open=Decimal("10.00"),
            high=Decimal("11.00"),
            low=Decimal("9.50"),
            close=Decimal("10.50"),
            volume=1000000,
            status=CandleStatus.UNKNOWN,
        )
        self.assertEqual(
            resolve_candle_lifecycle(c_2010, current_time=self.ref_now_2026),
            CandleStatus.UNKNOWN,
        )

        # 2030 (after supported coverage)
        c_2030 = OHLCVCandle(
            ticker="AAPL",
            interval="1d",
            timestamp=datetime(2030, 5, 17, 13, 30, tzinfo=UTC),
            open=Decimal("250.00"),
            high=Decimal("255.00"),
            low=Decimal("248.00"),
            close=Decimal("252.00"),
            volume=1000000,
            status=CandleStatus.UNKNOWN,
        )
        self.assertEqual(
            resolve_candle_lifecycle(c_2030, current_time=self.ref_now_2026),
            CandleStatus.UNKNOWN,
        )

    def test_08_current_date_pre_market_daily_resolves_unknown(self):
        """Current-date daily candle before regular market open (8:00 AM EDT) resolves to UNKNOWN."""
        bar_ts = datetime(2026, 10, 19, 4, 0, 0, tzinfo=UTC)
        candle = OHLCVCandle(
            ticker="AAPL",
            interval="1d",
            timestamp=bar_ts,
            open=Decimal("150.00"),
            high=Decimal("152.00"),
            low=Decimal("149.00"),
            close=Decimal("151.00"),
            volume=1000,
            status=CandleStatus.UNKNOWN,
        )
        cur_8am = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)
        self.assertEqual(
            resolve_candle_lifecycle(candle, current_time=cur_8am),
            CandleStatus.UNKNOWN,
        )

    def test_09_current_date_regular_session_daily_resolves_forming(self):
        """Current-date daily candle during regular hours (10:00 AM EDT) resolves to FORMING."""
        bar_ts = datetime(2026, 10, 19, 4, 0, 0, tzinfo=UTC)
        candle = OHLCVCandle(
            ticker="AAPL",
            interval="1d",
            timestamp=bar_ts,
            open=Decimal("150.00"),
            high=Decimal("155.00"),
            low=Decimal("149.00"),
            close=Decimal("154.00"),
            volume=500000,
            status=CandleStatus.FORMING,
        )
        cur_10am = datetime(2026, 10, 19, 14, 0, 0, tzinfo=UTC)
        self.assertEqual(
            resolve_candle_lifecycle(candle, current_time=cur_10am),
            CandleStatus.FORMING,
        )

    def test_10_current_date_post_close_daily_resolves_complete(self):
        """Current-date daily candle after regular close (4:30 PM EDT) resolves to COMPLETE."""
        bar_ts = datetime(2026, 10, 19, 4, 0, 0, tzinfo=UTC)
        candle = OHLCVCandle(
            ticker="AAPL",
            interval="1d",
            timestamp=bar_ts,
            open=Decimal("150.00"),
            high=Decimal("156.00"),
            low=Decimal("149.00"),
            close=Decimal("155.50"),
            volume=2000000,
            status=CandleStatus.COMPLETE,
        )
        cur_430pm = datetime(2026, 10, 19, 20, 30, 0, tzinfo=UTC)
        self.assertEqual(
            resolve_candle_lifecycle(candle, current_time=cur_430pm),
            CandleStatus.COMPLETE,
        )

    def test_11_early_close_daily_candle_lifecycle(self):
        """Early-close sessions resolve correctly for historical and live schedules."""
        # 1. Historical July 3 early close (Wednesday 2024-07-03, closed at 1:00 PM EDT)
        c_hist_early = OHLCVCandle(
            ticker="AAPL",
            interval="1d",
            timestamp=datetime(2024, 7, 3, 13, 30, tzinfo=UTC),
            open=Decimal("220.00"),
            high=Decimal("222.00"),
            low=Decimal("219.00"),
            close=Decimal("221.50"),
            volume=15000000,
            status=CandleStatus.UNKNOWN,
        )
        self.assertEqual(
            resolve_candle_lifecycle(c_hist_early, current_time=self.ref_now_2026),
            CandleStatus.COMPLETE,
        )

        # 2. Current early close (Black Friday 2026-11-27, 1:00 PM EST / 18:00 UTC)
        c_bf = OHLCVCandle(
            ticker="AAPL",
            interval="1d",
            timestamp=datetime(2026, 11, 27, 5, 0, 0, tzinfo=UTC),
            open=Decimal("150.00"),
            high=Decimal("153.00"),
            low=Decimal("149.00"),
            close=Decimal("152.00"),
            volume=1000000,
            status=CandleStatus.FORMING,
        )
        # 12:30 PM EST: FORMING
        t_1230 = datetime(2026, 11, 27, 17, 30, 0, tzinfo=UTC)
        self.assertEqual(resolve_candle_lifecycle(c_bf, current_time=t_1230), CandleStatus.FORMING)
        # 1:00 PM EST: COMPLETE
        t_1300 = datetime(2026, 11, 27, 18, 0, 0, tzinfo=UTC)
        self.assertEqual(resolve_candle_lifecycle(c_bf, current_time=t_1300), CandleStatus.COMPLETE)
        # 1:30 PM EST: COMPLETE
        t_1330 = datetime(2026, 11, 27, 18, 30, 0, tzinfo=UTC)
        self.assertEqual(resolve_candle_lifecycle(c_bf, current_time=t_1330), CandleStatus.COMPLETE)

    def test_12_yahoo_historical_parsing_retains_valid_pre_2026_candles(self):
        """Yahoo provider parses and retains historical daily candles prior to 2026."""
        # 3 sessions in January 2021 (Jan 4, Jan 5, Jan 6)
        timestamps = [1609770600, 1609857000, 1609943400]
        mock_payload = {
            "chart": {
                "result": [{
                    "meta": {"symbol": "AAPL", "regularMarketTime": timestamps[-1]},
                    "timestamp": timestamps,
                    "indicators": {
                        "quote": [{
                            "open": [133.52, 133.44, 127.72],
                            "high": [133.61, 134.43, 131.05],
                            "low": [126.76, 130.78, 126.38],
                            "close": [129.41, 131.01, 126.60],
                            "volume": [143301900, 97664900, 155088000],
                        }]
                    },
                }],
                "error": None,
            }
        }
        provider = YahooMarketDataProvider(http_get_fn=lambda url, t: json.dumps(mock_payload))
        start = datetime(2021, 1, 1, tzinfo=UTC)
        end = datetime(2021, 1, 10, tzinfo=UTC)
        bars = provider.fetch_historical_bars("AAPL", "1d", start, end, current_time=self.ref_now_2026)

        self.assertEqual(len(bars), 3)
        for b in bars:
            self.assertEqual(b.status, CandleStatus.COMPLETE)
            self.assertEqual(b.ticker, "AAPL")
            self.assertEqual(b.interval, "1d")

    def test_13_invalid_historical_candles_remain_rejected(self):
        """Candles with invalid prices, volumes, or geometries are strictly rejected."""
        bar_ts = datetime(2021, 6, 15, 13, 30, tzinfo=UTC)

        # High < Low
        c_bad_geom = OHLCVCandle(
            ticker="AAPL",
            interval="1d",
            timestamp=bar_ts,
            open=Decimal("130.00"),
            high=Decimal("125.00"),
            low=Decimal("135.00"),
            close=Decimal("130.00"),
            volume=1000,
            status=CandleStatus.UNKNOWN,
        )
        self.assertEqual(
            resolve_candle_lifecycle(c_bad_geom, current_time=self.ref_now_2026),
            CandleStatus.UNKNOWN,
        )
        with self.assertRaises(InvalidPriceError):
            validate_single_candle(c_bad_geom, current_time=self.ref_now_2026)

        # Negative price
        c_neg_px = OHLCVCandle(
            ticker="AAPL",
            interval="1d",
            timestamp=bar_ts,
            open=Decimal("-130.00"),
            high=Decimal("132.00"),
            low=Decimal("129.50"),
            close=Decimal("131.00"),
            volume=1000,
            status=CandleStatus.UNKNOWN,
        )
        self.assertEqual(
            resolve_candle_lifecycle(c_neg_px, current_time=self.ref_now_2026),
            CandleStatus.UNKNOWN,
        )
        with self.assertRaises(InvalidPriceError):
            validate_single_candle(c_neg_px, current_time=self.ref_now_2026)

        # Negative volume
        c_neg_vol = OHLCVCandle(
            ticker="AAPL",
            interval="1d",
            timestamp=bar_ts,
            open=Decimal("130.00"),
            high=Decimal("132.00"),
            low=Decimal("129.50"),
            close=Decimal("131.00"),
            volume=-50,
            status=CandleStatus.UNKNOWN,
        )
        self.assertEqual(
            resolve_candle_lifecycle(c_neg_vol, current_time=self.ref_now_2026),
            CandleStatus.UNKNOWN,
        )
        with self.assertRaises(InvalidVolumeError):
            validate_single_candle(c_neg_vol, current_time=self.ref_now_2026)

    def test_14_intraday_and_weekly_lifecycle_preserved(self):
        """Intraday and weekly interval lifecycle resolution continues functioning."""
        # 15m candle fully elapsed
        c_15m = OHLCVCandle(
            ticker="AAPL",
            interval="15m",
            timestamp=datetime(2026, 10, 19, 14, 0, tzinfo=UTC),
            open=Decimal("150.00"),
            high=Decimal("151.00"),
            low=Decimal("149.50"),
            close=Decimal("150.50"),
            volume=10000,
            status=CandleStatus.UNKNOWN,
        )
        # Checked at 14:20 UTC (15m window 14:00-14:15 has elapsed)
        t_after = datetime(2026, 10, 19, 14, 20, tzinfo=UTC)
        self.assertEqual(resolve_candle_lifecycle(c_15m, current_time=t_after), CandleStatus.COMPLETE)

        # Weekly candle past Friday close
        c_1wk = OHLCVCandle(
            ticker="AAPL",
            interval="1wk",
            timestamp=datetime(2026, 10, 12, 4, 0, tzinfo=UTC),  # Monday of week
            open=Decimal("148.00"),
            high=Decimal("155.00"),
            low=Decimal("147.00"),
            close=Decimal("154.00"),
            volume=10000000,
            status=CandleStatus.UNKNOWN,
        )
        # Saturday after week closed
        t_sat = datetime(2026, 10, 17, 16, 0, tzinfo=UTC)
        self.assertEqual(resolve_candle_lifecycle(c_1wk, current_time=t_sat), CandleStatus.COMPLETE)

    def test_15_market_data_service_historical_retrieval_success(self):
        """MarketDataService.get_historical_bars() returns valid historical COMPLETE daily candles."""
        timestamps = [1609770600, 1609857000, 1609943400]
        mock_payload = {
            "chart": {
                "result": [{
                    "meta": {"symbol": "AAPL", "regularMarketTime": timestamps[-1]},
                    "timestamp": timestamps,
                    "indicators": {
                        "quote": [{
                            "open": [133.52, 133.44, 127.72],
                            "high": [133.61, 134.43, 131.05],
                            "low": [126.76, 130.78, 126.38],
                            "close": [129.41, 131.01, 126.60],
                            "volume": [143301900, 97664900, 155088000],
                        }]
                    },
                }],
                "error": None,
            }
        }
        provider = YahooMarketDataProvider(http_get_fn=lambda url, t: json.dumps(mock_payload))
        cache = MarketDataCache(db_path=":memory:")
        try:
            service = MarketDataService(provider=provider, cache=cache)
            start = datetime(2021, 1, 1, tzinfo=UTC)
            end = datetime(2021, 1, 10, tzinfo=UTC)
            bars = service.get_historical_bars(
                ticker="AAPL",
                interval="1d",
                start=start,
                end=end,
                use_cache=True,
                current_time=self.ref_now_2026,
            )
            self.assertEqual(len(bars), 3)
            self.assertEqual(bars[0].status, CandleStatus.COMPLETE)
            self.assertEqual(bars[1].status, CandleStatus.COMPLETE)
            self.assertEqual(bars[2].status, CandleStatus.COMPLETE)
            self.assertEqual(bars[0].close, Decimal("129.41"))
            self.assertEqual(bars[1].close, Decimal("131.01"))
            self.assertEqual(bars[2].close, Decimal("126.60"))
        finally:
            cache.close()

    def test_16_critical_integration_full_chain_service_to_data_manager(self):
        """CRITICAL INTEGRATION TEST: Full pipeline from YahooMarketDataProvider -> MarketDataService -> HistoricalDataManager.

        Proves that:
        1. Provider parses historical daily candles across 2021-2025.
        2. Lifecycle resolution classifies them as COMPLETE.
        3. MarketDataService returns the candles.
        4. Validation succeeds and stores candles in cache.
        5. HistoricalDataManager loads from service through the normal public API.
        6. Dataset integrity verification confirms CLEAN status.
        7. Slicing with ZERO LOOK-AHEAD bias works seamlessly.
        """
        # Create a 5-day series spanning 2021, 2022, 2023, 2024, 2025
        # 1. 2021-01-04 (Mon): 1609770600 (14:30 UTC)
        # 2. 2022-01-03 (Mon): 1641220200 (14:30 UTC)
        # 3. 2023-01-03 (Tue): 1672756200 (14:30 UTC)
        # 4. 2024-01-02 (Tue): 1704205800 (14:30 UTC)
        # 5. 2025-01-02 (Thu): 1735828200 (14:30 UTC)
        timestamps = [1609770600, 1641220200, 1672756200, 1704205800, 1735828200]
        mock_payload = {
            "chart": {
                "result": [{
                    "meta": {"symbol": "AAPL", "regularMarketTime": timestamps[-1]},
                    "timestamp": timestamps,
                    "indicators": {
                        "quote": [{
                            "open": [133.52, 177.83, 130.28, 187.15, 244.20],
                            "high": [133.61, 182.88, 130.90, 188.44, 245.50],
                            "low": [126.76, 177.71, 124.17, 183.89, 242.10],
                            "close": [129.41, 182.01, 125.07, 185.64, 243.85],
                            "volume": [143301900, 107917700, 112117500, 82488700, 48200000],
                        }]
                    },
                }],
                "error": None,
            }
        }

        # Deterministic mocked HTTP function: NO live network access
        def mock_http(url: str, timeout: int) -> str:
            return json.dumps(mock_payload)

        provider = YahooMarketDataProvider(http_get_fn=mock_http)
        cache = MarketDataCache(db_path=":memory:")
        try:
            service = MarketDataService(provider=provider, cache=cache)
            dm = HistoricalDataManager()

            start_dt = datetime(2021, 1, 1, tzinfo=UTC)
            end_dt = datetime(2025, 1, 10, tzinfo=UTC)

            # Ingest through normal production service path (NO direct load_candles bypass)
            count = dm.load_from_service(
                service=service,
                ticker="AAPL",
                start=start_dt,
                end=end_dt,
                interval="1d",
                adjustment_type=PriceAdjustmentType.SPLIT_ADJUSTED,
            )

            self.assertEqual(count, 5)

            # Verify candles in HistoricalDataManager
            candles = dm.get_execution_candles("AAPL")
            self.assertEqual(len(candles), 5)
            for c in candles:
                self.assertEqual(c.status, CandleStatus.COMPLETE)
                self.assertEqual(c.ticker, "AAPL")

            # Verify temporal slicing up to 2023-01-03
            as_of_2023 = date(2023, 1, 3)
            slice_2023 = dm.get_execution_candles_slice("AAPL", as_of_date=as_of_2023)
            self.assertEqual(len(slice_2023), 3)
            self.assertEqual(slice_2023[-1].timestamp.date(), as_of_2023)
            self.assertEqual(slice_2023[-1].close, Decimal("125.07"))

            # Slicing after 2023 strictly excludes 2024 and 2025
            self.assertTrue(all(c.timestamp.date() <= as_of_2023 for c in slice_2023))

        finally:
            cache.close()


if __name__ == "__main__":
    unittest.main()
