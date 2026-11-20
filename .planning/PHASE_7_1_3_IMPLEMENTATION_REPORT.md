# Phase 7.1.3 — Mocked Alpaca Market Data Provider Implementation Report

**Date:** 2026-11-17  
**Phase:** 7.1.3 (Mocked Alpaca Market Data Provider Foundation)  
**Status:** COMPLETE & FULLY VERIFIED  
**Safety Classification:** STRICTLY OFFLINE (Mock Fixtures Only — Zero Live Network Requests)

---

## 1. Executive Summary

Phase 7.1.3 successfully implemented the foundational **Alpaca Market Data Provider** for Trader-Swing in strict compliance with the project's modular provider architecture. The provider is fully operational in **100% offline mode**, hydrated exclusively via deterministic JSON mock fixtures with zero live network calls, zero API credentials, and zero external package installations.

### Core Deliverables Completed:
1. **alpaca_provider.py:** Implemented `AlpacaMarketDataProvider` subclassing `MarketDataProvider` and `AlpacaCandle` subclassing `OHLCVCandle`.
2. **provider_responses.json:** Created comprehensive deterministic offline test fixtures covering single-ticker series, multi-ticker batches, malformed payloads, rate limits, and network errors.
3. **test_swing_alpaca_provider.py:** Implemented 16 deterministic unit tests validating deserialization, normalization, error classification, retry backoff, and volume guards.
4. **__init__.py:** Exported `AlpacaMarketDataProvider` and `AlpacaCandle`.

---

## 2. Architecture & Data Model Implementation

### 2.1 The `AlpacaCandle` Data Model
`AlpacaCandle` inherits directly from `OHLCVCandle`, providing complete backwards-compatibility and polymorphism with all existing indicators, feed adapters, and scanner routines while carrying Alpaca-specific provenance:

```python
@dataclass
class AlpacaCandle(OHLCVCandle):
    vwap: Optional[Decimal] = None
    trade_count: Optional[int] = None
    feed_classification: str = "IEX"
    session_date: Optional[date] = None
    adjustment_type: str = "SPLIT_ADJUSTED"
    is_consolidated_volume: bool = False  # Strictly False: IEX volume is single-exchange!
    source: str = "alpaca_iex"
    is_delayed: bool = False
```

### 2.2 Strict Anti-Volume Substitution Guard
To guarantee that unconsolidated IEX volume ($\approx 1.5\% - 3.0\%$ of market volume) can **never** silently pollute consolidated liquidity calculations:
- `is_consolidated_volume: bool = False` is hardcoded.
- Calling `candle.get_consolidated_volume()` raises an explicit `InvalidVolumeError`:
  ```python
  def get_consolidated_volume(self) -> int:
      raise InvalidVolumeError(
          f"AlpacaCandle for {self.ticker} holds unconsolidated IEX volume ({self.volume:,} shares). "
          "IEX volume cannot be used as consolidated volume for ADV/RVOL liquidity screening.",
          ticker=self.ticker,
          timestamp=self.timestamp,
      )
  ```

### 2.3 Timestamp & Calendar Normalization
Alpaca daily bar timestamps (`00:00:00Z` or `05:00:00Z`) are automatically normalized to the NYSE regular session opening time at `09:30:00 America/New_York` converted to UTC (`14:30:00Z` EDT or `13:30:00Z` EST), perfectly aligning with `MarketClock` conventions.

### 2.4 Deterministic Error & Retry Handling
- **HTTP 429:** Mapped directly to `RateLimitError`.
- **HTTP 401 / 403:** Mapped to `ProviderUnavailableError` (authentication/forbidden; fails fast without retrying).
- **HTTP 404 / Empty bars:** Mapped to `NoDataError`.
- **HTTP 500 / 502 / 503:** Bounded retries (default 3 attempts with exponential backoff: 1s, 2s, 4s). If retries exhaust, raises `ProviderUnavailableError`.
- **Offline Safety Guard:** If `alpaca_request_fn` is omitted and credentials are unset, any fetch attempt raises `ProviderUnavailableError("...Live network calls are disabled in offline mode.")`, preventing accidental network access.

---

## 3. Test Suite Verification

### 3.1 Unit Test Execution (`tests/test_swing_alpaca_provider.py`)
All 16 unit tests passed in **0.047 seconds**:

| Test Method | Functionality Verified | Result |
| :--- | :--- | :---: |
| `test_01_historical_bars_fetching_and_normalization` | SPY 3-bar series parsing, Decimal prices, VWAP, session time | **PASS** |
| `test_02_multi_ticker_batch_fetching` | Batch querying (up to 100 tickers) mapping SPY, AAPL, QQQ | **PASS** |
| `test_03_latest_bar_retrieval` | Single most recent bar retrieval | **PASS** |
| `test_04_anti_volume_substitution_guard` | `get_consolidated_volume()` raises `InvalidVolumeError` | **PASS** |
| `test_05_empty_bar_series_raises_no_data_error` | Empty bars list maps to `NoDataError` | **PASS** |
| `test_06_missing_symbol_raises_no_data_error` | Missing ticker in response maps to `NoDataError` | **PASS** |
| `test_07_malformed_missing_close_raises_invalid_response` | Null close price raises `InvalidResponseError` | **PASS** |
| `test_08_inverted_geometry_raises_invalid_price` | High < Low raises `InvalidPriceError` | **PASS** |
| `test_09_negative_volume_raises_invalid_volume` | Negative volume raises `InvalidVolumeError` | **PASS** |
| `test_10_duplicate_timestamps_raises_sequence_validation_error` | Duplicate timestamps raise `SequenceValidationError` | **PASS** |
| `test_11_rate_limit_maps_to_rate_limit_error` | HTTP 429 maps to `RateLimitError` | **PASS** |
| `test_12_authentication_errors_map_to_provider_unavailable` | HTTP 401 and 403 map to `ProviderUnavailableError` | **PASS** |
| `test_13_transient_http_500_retries_and_exhausts` | HTTP 500 retries 3 times with backoff then fails cleanly | **PASS** |
| `test_14_transient_recovery_after_retry` | Transient HTTP 500 recovering on attempt 2 succeeds | **PASS** |
| `test_15_provider_metadata_disclosures` | Verifies metadata, IEX feed disclosure, unconsolidated flag | **PASS** |
| `test_16_offline_safety_guard_blocks_unmocked_network` | Uninjected call blocks network calls safely | **PASS** |

### 3.2 Regression Suite Verification
The entire test suite across all subsystems was executed:

```text
tests/test_swing_alpaca_provider.py:        16 passed (100%)
tests/test_swing_dashboard.py:              11 passed (100%)
tests/test_swing_observation_workflow.py:    7 passed (100%)
tests/test_swing_observation_logging.py:    12 passed (100%)
tests/test_swing_market_data_provider.py:   18 passed (100%)
tests/test_swing_yfinance_provider.py:      26 passed (100%)
============================= 90 passed in 25.5s =============================
```

---

## 4. Compatibility Findings & Boundary Preservation

* **Zero Yahoo Impact:** `YFinanceMarketDataProvider` and existing Yahoo scrapers/parsers remain 100% untouched and functional.
* **Zero Strategy / Indicator Mutation:** No parameters, thresholds, or mathematical routines in `indicators.py`, `scanner.py`, `risk.py`, or `regime_detector.py` were altered.
* **Decoupled Architecture:** The dual-source aggregator (Phase 7.1.4) and live WebSocket streaming are not yet integrated, preserving clean phased boundaries.
* **Dataset Immutability:** SHA-256 hashes confirm `observation_log.jsonl`, `CURRENT_DAILY_MANIFEST.json`, `SPY_daily.csv`, and historical partitions are byte-for-byte identical.

---

## 5. Unresolved Risks & Next Phase Prerequisites

1. **Unconsolidated Volume Isolation:** Because Alpaca Free tier volume is IEX-only, the provider must remain isolated from candidate liquidity screening until the **Dual-Source Aggregator (Phase 7.1.4)** binds it with Yahoo consolidated volume.
2. **Credential Management:** When live testing is authorized in future phases, API credentials must be delivered via local environment variables (`ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY`), never stored in files or chat.
