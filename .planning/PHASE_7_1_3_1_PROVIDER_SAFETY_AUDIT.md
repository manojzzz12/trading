# Phase 7.1.3.1 — Alpaca Provider Safety & Compatibility Audit

**Date:** 2026-11-17  
**Phase:** 7.1.3.1 (Code-Level Safety, Volume Isolation & Compatibility Audit)  
**Status:** COMPLETE  
**Audit Classification:** STRICTLY READ-ONLY & OFFLINE TEST AUDIT  
**Recommendation:** **GO FOR PHASE 7.1.4 (CONDITIONAL)**

---

## 1. Executive Summary

This code-level audit evaluates the security, data validation, and architectural safety of the newly implemented **Alpaca Market Data Provider** (`AlpacaMarketDataProvider` and `AlpacaCandle`). 

The audit focused on the **Critical Volume Isolation Boundary**: verifying whether unconsolidated IEX volume ($\approx 1.5\% - 3.0\%$ of market volume) could accidentally enter consolidated liquidity qualification routines ($ADV \ge \$10M$, $RVOL \ge 0.80\text{x}$) through existing strategy code paths.

### Key Audit Finding
> [!WARNING]
> **CRITICAL ARCHITECTURAL FINDING: DIRECT INGESTION BYPASS DETECTED**  
> While `AlpacaCandle` explicitly implements `is_consolidated_volume = False` and an anti-substitution guard `get_consolidated_volume()` raising `InvalidVolumeError`, existing engine routines in `indicators.py` (lines 756 and 870) and `scanner.py` (line 102) access the raw attribute `candle.volume` directly.  
> 
> If `AlpacaCandle` instances were fed directly into the existing `MarketDataFeedAdapter` and `StockScanner` without the **Phase 7.1.4 Dual-Source Aggregator**, the engine would compute ADV and RVOL using single-exchange IEX volume, causing widespread false liquidity rejections across the stock universe.  
> 
> Therefore, direct ingestion of raw `AlpacaCandle` objects into the strategy engine is **strictly unsafe** until the Dual-Source Aggregator replaces/supplements `candle.volume` with Yahoo consolidated tape volume.

---

## 2. Critical Volume Isolation Audit

### 2.1 Code-Level Inspection of Volume Access

We audited all files in `.agents/skills/trader-swing/` to trace how `.volume` is accessed:

| File | Line(s) | Code Construct | Access Type | Calls `get_consolidated_volume()`? |
| :--- | :---: | :--- | :---: | :---: |
| `alpaca_provider.py` | L48–L60 | `def get_consolidated_volume(self): raise InvalidVolumeError` | Guard | **N/A (Defines guard)** |
| `indicators.py` | L756 | `running_vol_sum += Decimal(candle.volume)` | Direct Attribute | **NO (Bypasses guard)** |
| `indicators.py` | L870 | `candle.volume / prior_avg_vol` | Direct Attribute | **NO (Bypasses guard)** |
| `scanner.py` | L102 | `avg_vol_20 = snap.average_volume_20.value` | Derivative | **NO (Uses L756 result)** |
| `feed_adapter.py` | L280–L300 | `validate_candle_series(candles)` | Sequence Check | **NO (Does not check volume provenance)** |
| `backtest_engine.py` | L475 | `sum(c.volume for c in trailing_20) // 20` | Direct Attribute | **NO (Bypasses guard)** |

### 2.2 Complete Execution Path Trace (Raw Alpaca $\rightarrow$ ADV/RVOL)

If raw `AlpacaCandle` objects were supplied to the current engine:

```
[AlpacaMarketDataProvider]
       │
       ▼ returns AlpacaCandle(volume=1,245,000 [IEX], is_consolidated_volume=False)
[MarketDataFeedAdapter]
       │ checks: SPLIT_ADJUSTED? Yes. COMPLETE status? Yes.
       ▼ (Passes without checking is_consolidated_volume)
[StockScanner.evaluate_candidate(candles)]
       │ calls IndicatorsEngine.compute_all(candles)
       ▼
[TechnicalIndicatorsEngine]
       │ calls calculate_average_volume_series(candles)
       ▼
[indicators.py L756]
       │ reads candle.volume (Direct attribute access: bypasses get_consolidated_volume())
       ▼ calculates rolling sum using IEX volume
[Average Volume Output]
       │ avg_vol_20 = 1,245,000 (Single-exchange IEX average, not Consolidated ~60M)
       ▼
[scanner.py L105]
       │ adv = avg_vol_20 * close = 1,245,000 * $352.78 = $439M
       ▼ (For mid/small caps, ADV would collapse to < $10M, triggering false rejection)
```

### 2.3 Empirical Test Proof (`test_17_volume_isolation_bypass_audit`)
A dedicated unit test was added to `tests/test_swing_alpaca_provider.py`:
- **Guard Verification:** Asserts `candle.get_consolidated_volume()` raises `InvalidVolumeError`.
- **Bypass Proof:** Asserts that calling legacy `calculate_average_volume([candle], period=1)` executes without invoking the guard and calculates an average volume equal to the raw IEX volume (`1,245,000`).
- **Audit Conclusion:** The provider itself has the correct protective attributes, but the legacy indicator and scanner engines have an unprotected direct attribute path.

### 2.4 Required Architectural Remedy for Phase 7.1.4
To close this bypass permanently:
1. **Mandatory Dual-Source Aggregator:** `DualSourceAggregator` (designed in Phase 7.1.2) must construct a `DualSourceCandle` where `volume` is strictly populated with Yahoo consolidated tape volume, while IEX volume is preserved in `auxiliary_iex_volume`.
2. **Adapter Provenance Enforcement:** In Phase 7.1.4, `MarketDataFeedAdapter.validate_candle_series()` must be updated with an explicit check:
   ```python
   for c in candles:
       if getattr(c, "is_consolidated_volume", True) is False:
           raise UnconsolidatedVolumeError(
               f"Candle for {ticker} holds unconsolidated volume ({c.source}). "
               "Direct strategy engine ingestion of unconsolidated volume is prohibited."
           )
   ```

---

## 3. Data Validation Audit

We audited all 9 validation requirements in `AlpacaMarketDataProvider` and `market_data_validation.py`:

| Validation Criterion | Implementation Mechanism | Handling / Exception | Test Coverage | Status |
| :--- | :--- | :--- | :---: | :---: |
| **Invalid OHLC Geometry** | `validate_single_candle` checks $Low \le Open \le High$ and $Low \le Close \le High$ | Raises `InvalidPriceError` | `test_08` | **PASS** |
| **Non-Positive Prices** | `validate_single_candle` checks $Price > 0$ (rejects 0 or negative) | Raises `InvalidPriceError` | `test_18` | **PASS** |
| **Negative Volume** | `parse_alpaca_bar` checks $vol < 0$; `validate_single_candle` checks $volume \ge 0$ | Raises `InvalidVolumeError` | `test_09` | **PASS** |
| **Duplicate Timestamps** | `validate_candle_sequence` tracks seen timestamps; rejects duplicates | Raises `SequenceValidationError` | `test_10` | **PASS** |
| **Missing Sessions** | Monotonicity verified by `validate_candle_sequence` ($t_i > t_{i-1}$); intermediate calendar gaps caught by `FeedAdapter` bar count rules | Rejects out-of-order; excludes deficient counts | `test_10` | **PASS** |
| **Timezone Normalization** | Daily bar UTC strings parsed and converted to Eastern date, then anchored at `09:30:00 ET` in UTC | Strict `MarketClock` alignment | `test_01` | **PASS** |
| **Incomplete Candles** | `resolve_candle_lifecycle()` evaluates bar end vs current mock time | Marks `FORMING` vs `COMPLETE` | `test_01` | **PASS** |
| **Split-Adjustment Metadata** | API query passes `adjustment="split"`; candle tags `SPLIT_ADJUSTED` | Disclosed in candle and metadata | `test_01`, `test_15` | **PASS** |
| **Malformed & Missing Fields** | `parse_alpaca_bar` validates mandatory keys `('t', 'o', 'h', 'l', 'c', 'v')` | Raises `InvalidResponseError` | `test_07` | **PASS** |
| **Future Skew Prevention** | `validate_single_candle` checks $Timestamp \le CurrentTime + Tolerance$ | Raises `TimestampValidationError` | `test_19` | **PASS** |

---

## 4. Retry and Offline Safety Audit

Inspection of `AlpacaMarketDataProvider._execute_request()` confirmed:

1. **Zero Unauthorized Network Calls:**
   - If `alpaca_request_fn` is `None` (default), any fetch call raises `ProviderUnavailableError("Live network execution is disabled in Phase 7.1.3...")` immediately.
   - Verified by unit test: `test_16_offline_safety_guard_blocks_unmocked_network`.
2. **Deterministic Mock Responses:**
   - All tests use static JSON files in `tests/fixtures/trader-swing/alpaca/provider_responses.json`.
   - Results are 100% reproducible and isolated in memory.
3. **Bounded Retries:**
   - Server errors (HTTP 500, 502, 503) retry up to `max_retries` (default 3, total 4 attempts).
   - Once retries are exhausted, raises `ProviderUnavailableError`.
   - Verified by unit test: `test_13_transient_http_500_retries_and_exhausts`.
4. **Rate-Limit Handling:**
   - HTTP 429 maps directly to `RateLimitError` and does not perform un-backoff blind retries.
   - Verified by unit test: `test_11_rate_limit_maps_to_rate_limit_error`.
5. **Clear Authentication Failure Separation:**
   - HTTP 401 / 403 maps directly to `ProviderUnavailableError("Authentication failed...")`.
   - It fails immediately without retrying and **cannot be mistaken for missing data (`NoDataError` / 404)**.
   - Verified by unit test: `test_12_authentication_errors_map_to_provider_unavailable`.

---

## 5. Compatibility & Regression Audit

| Component | Audit Check | Finding |
| :--- | :--- | :--- |
| **`YFinanceMarketDataProvider`** | Verified code and test execution | **100% Untouched.** All 26 yfinance provider tests pass cleanly. |
| **Strategy & Indicators** | Inspected `indicators.py`, `scanner.py`, `risk.py` | **100% Untouched.** Zero mathematical formulas or thresholds modified. |
| **Historical Partitions** | Inspected `.planning/data/historical_partitions/` | **100% Untouched.** No files modified or accessed. |
| **Paper Ledger & State** | Inspected `observation_log.jsonl`, `CURRENT_DAILY_MANIFEST.json` | **100% Untouched.** SHA-256 hashes match Phase 6.4.5 post-audit. |
| **Lockbox Isolation** | Checked for any references to 2026 lockbox | **Zero access.** 2026 lockbox remains sealed. |
| **Silent Replacement Guard** | Checked whether Alpaca could replace Yahoo data | **Impossible.** `scripts/acquire_current_daily.py` and `paper_cli.py` explicitly instantiate `YFinanceMarketDataProvider`. |

---

## 6. Test Suite Results Summary

A total of **19 dedicated unit tests** were executed in `tests/test_swing_alpaca_provider.py`:

```text
test_01_historical_bars_fetching_and_normalization:       PASSED
test_02_multi_ticker_batch_fetching:                      PASSED
test_03_latest_bar_retrieval:                             PASSED
test_04_anti_volume_substitution_guard:                   PASSED
test_05_empty_bar_series_raises_no_data_error:            PASSED
test_06_missing_symbol_raises_no_data_error:              PASSED
test_07_malformed_missing_close_raises_invalid_response:  PASSED
test_08_inverted_geometry_raises_invalid_price:           PASSED
test_09_negative_volume_raises_invalid_volume:            PASSED
test_10_duplicate_timestamps_raises_sequence_validation:  PASSED
test_11_rate_limit_maps_to_rate_limit_error:             PASSED
test_12_authentication_errors_map_to_provider_unavail:    PASSED
test_13_transient_http_500_retries_and_exhausts:          PASSED
test_14_transient_recovery_after_retry:                   PASSED
test_15_provider_metadata_disclosures:                    PASSED
test_16_offline_safety_guard_blocks_unmocked_network:     PASSED
test_17_volume_isolation_bypass_audit:                    PASSED
test_18_non_positive_price_rejection:                     PASSED
test_19_future_timestamp_rejection:                       PASSED
----------------------------------------------------------------------
Ran 19 tests in 0.067s — OK (100% Pass Rate)
```

Full repository regression suite results:
```text
tests/test_swing_alpaca_provider.py:        19 passed (100%)
tests/test_swing_dashboard.py:              11 passed (100%)
tests/test_swing_observation_workflow.py:    7 passed (100%)
tests/test_swing_observation_logging.py:    12 passed (100%)
tests/test_swing_market_data_provider.py:   18 passed (100%)
tests/test_swing_yfinance_provider.py:      26 passed (100%)
============================= 93 passed in 25.8s =============================
```

---

## 7. Recommendation: GO / NO-GO Verdict

### Verdict: **GO FOR PHASE 7.1.4 (CONDITIONAL)**

The mocked Alpaca provider foundation is **technically sound, fully offline-safe, and thoroughly tested**.

#### Mandatory Gating Conditions for Phase 7.1.4:
1. **Never Feed Raw `AlpacaCandle` Directly to Strategy Engine:** Raw `AlpacaCandle` instances must be consumed **exclusively** by the Phase 7.1.4 Dual-Source Aggregator and the Lightweight Charts visualizer.
2. **Dual-Source Binding:** The Phase 7.1.4 Aggregator must replace `candle.volume` with Yahoo consolidated volume before any candles reach `MarketDataFeedAdapter` or `StockScanner`.
3. **Adapter Guard Implementation:** Add an explicit provenance check in `MarketDataFeedAdapter` that rejects any candle sequence declaring `is_consolidated_volume == False`.
