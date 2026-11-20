# Phase 7.1.2 — Dual-Source Market Data Contract Design

**Date:** 2026-11-17  
**Phase:** 7.1.2 (Dual-Source Market Data Contract Specification)  
**Status:** COMPLETE (Design & Specification Only — Zero Code Implemented)  
**Safety Classification:** STRICTLY READ-ONLY (No Live Data Downloads, Zero Paper Ledger Mutation)

---

## 1. Executive Summary & Core Architectural Decision

This document defines the formal data contract for a **Dual-Source Hybrid Market Data Architecture** for the **Trader-Swing** platform.

### Architectural Decision
- **Alpaca Market Data API (Free Basic IEX Tier):** Designated as the authoritative feed for **price action** (Open, High, Low, Close, Intraday Candles, Moving Averages, RSI, MACD, and ATR).
- **Yahoo Finance (`yfinance`):** Designated as the authoritative feed for **consolidated tape volume** (Composite US market volume across all exchanges and FINRA TRF) used strictly for **liquidity qualification** (20-day Average Dollar Volume `ADV` and Relative Volume `RVOL`).
- **TradingView Lightweight Charts:** Acts strictly as an offline, client-side visual renderer for the combined dataset inside the dashboard.
- **Strategy & Risk Engine Invariance:** The frozen trading strategy, 5-pillar screening rules, regime qualification thresholds, and deterministic risk engine ($5,000 equity, 1.0% / $50 max risk, 4 positions) remain **100% frozen and unmodified**.

```
┌──────────────────────────────────────────────┐    ┌──────────────────────────────────────────────┐
│           Alpaca Market Data (IEX)           │    │           Yahoo Finance (Composite)          │
│   • Open, High, Low, Close (Price Action)    │    │   • Composite 20-Day Volume (Tape A/B/C)     │
│   • Technical Indicators (SMA, RSI, MACD)    │    │   • Consolidated Volume Screening            │
└──────────────────────┬───────────────────────┘    └──────────────────────┬───────────────────────┘
                       │                                                   │
                       └─────────────────────┬─────────────────────────────┘
                                             │
                                             ▼
                       ┌───────────────────────────────────────────┐
                       │      Dual-Source Feed Aggregator          │
                       │   • Session Date Alignment (D_Alpaca==D_Y)│
                       │   • Intrinsic Geometry Validation         │
                       │   • IEX Volume Substitution Guard         │
                       │   • Full Provenance Metadata Tracking     │
                       └─────────────────────┬─────────────────────┘
                                             │
                      ┌──────────────────────┴──────────────────────┐
                      │                                             │
                      ▼                                             ▼
       ┌───────────────────────────────┐             ┌───────────────────────────────┐
       │   Strategy & Scanner Engine   │             │ TradingView Lightweight Canvas│
       │   • Alpaca: Price & Trends    │             │   • Interactive Candlesticks  │
       │   • Yahoo: ADV & RVOL Checks  │             │   • Volume Histogram Dock     │
       │   • Frozen Risk Constraints   │             │   • Technical Overlays (SMAs) │
       └───────────────────────────────┘             └───────────────────────────────┘
```

---

## 2. Separate Schemas & Provenance Metadata

To prevent data contamination and ensure full auditability, data from each provider is captured with distinct schemas and explicit provenance tracking before merging into a unified execution candle.

### 2.1 Alpaca Price Data Schema (`AlpacaPriceRecord`)
Captures price action and exchange-specific execution details:

| Field Name | Type | Description | Example |
| :--- | :--- | :--- | :--- |
| `ticker` | `str` | Normalized uppercase ticker symbol | `"SPY"` |
| `session_date` | `date` | Trading day (YYYY-MM-DD) | `2026-11-16` |
| `timestamp` | `datetime` | Timezone-aware UTC bar timestamp (NYSE 09:30:00 ET) | `2026-11-16T14:30:00Z` |
| `open` | `Decimal` | Opening price on IEX | `745.2000` |
| `high` | `Decimal` | Highest traded price on IEX | `747.8000` |
| `low` | `Decimal` | Lowest traded price on IEX | `738.5000` |
| `close` | `Decimal` | Final closing price on IEX | `739.9600` |
| `iex_volume` | `int` | Single-exchange volume executed on IEX | `1542100` |
| `trade_count` | `int` | Number of executed trades on IEX | `18450` |
| `vwap` | `Decimal` | Volume-weighted average price on IEX | `742.1500` |
| `provenance_source` | `str` | Fixed provider identifier | `"ALPACA_IEX"` |
| `adjustment_type` | `str` | Corporate action adjustment type | `"SPLIT_ADJUSTED"` |
| `received_timestamp` | `datetime` | UTC timestamp when response was received | `2026-11-16T21:05:02Z` |

### 2.2 Yahoo Consolidated Volume Schema (`YahooVolumeRecord`)
Captures composite US equity market volume across all exchanges:

| Field Name | Type | Description | Example |
| :--- | :--- | :--- | :--- |
| `ticker` | `str` | Normalized uppercase ticker symbol | `"SPY"` |
| `session_date` | `date` | Trading day (YYYY-MM-DD) | `2026-11-16` |
| `timestamp` | `datetime` | Timezone-aware UTC bar timestamp | `2026-11-16T14:30:00Z` |
| `consolidated_volume` | `int` | Total composite market volume (SIP composite) | `64892400` |
| `raw_nominal_close` | `Decimal` | Reference nominal close reported by Yahoo | `739.9600` |
| `provenance_source` | `str` | Fixed provider identifier | `"YAHOO_CONSOLIDATED"` |
| `delay_classification`| `str` | Regulatory delay classification | `"DELAYED_UNVERIFIED"` |
| `received_timestamp` | `datetime` | UTC timestamp when scrape completed | `2026-11-16T21:05:05Z` |

### 2.3 Merged Dual-Source Execution Candle (`DualSourceCandle`)
The unified model passed into the technical indicator engine and scanner:

```python
@dataclass(frozen=True)
class DualSourceCandle:
    ticker: str
    session_date: date
    timestamp: datetime  # 09:30:00 ET converted to UTC
    
    # Authoritative Price Action (Alpaca IEX)
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    
    # Authoritative Liquidity Volume (Yahoo Consolidated Tape)
    volume: int  # Must ALWAYS be consolidated volume!
    
    # Auxiliary Venue Data (Retained for execution audit only)
    iex_volume: int
    trade_count: Optional[int]
    vwap: Optional[Decimal]
    
    # Provenance Tracking Metadata
    price_source: str = "ALPACA_IEX"
    volume_source: str = "YAHOO_CONSOLIDATED"
    price_status: str = "COMPLETE"
    volume_status: str = "DELAYED_UNVERIFIED"
    merged_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
```

---

## 3. Timestamp Alignment & Maximum Acceptable Data-Age Rules

### 3.1 Session Date Alignment Rule ($D_{Alpaca} == D_{Yahoo}$)
Both feeds must strictly represent the **identical trading session date**:
1. The aggregator extracts `session_date` from both `AlpacaPriceRecord` and `YahooVolumeRecord`.
2. If `record_alpaca.session_date != record_yahoo.session_date`:
   - The candle is rejected immediately with error `SESSION_DATE_MISMATCH`.
   - The symbol is excluded from candidate evaluation (`StockExclusion`).
   - Zero forward-filling or borrowing of previous session volume is permitted.

### 3.2 Maximum Acceptable Data-Age Rules
1. **Regular Session Completion Guard:**
   - For swing trading, observation scans evaluate only completed daily sessions.
   - Earliest permissible acquisition time: **16:15 ET** on regular trading days (allowing 15 minutes post-close for NYSE/NASDAQ closing auction settlement and composite tape consolidation).
   - Ingesting prior to 16:15 ET triggers `FORMING_SESSION_REJECTED`.
2. **Session Staleness Threshold:**
   - The expected session date $D_{expected}$ is determined deterministically via `MarketClock.get_latest_completed_session()`.
   - If either feed returns a date $D < D_{expected}$:
     - For SPY: Triggers `STALE_MANDATORY_PROXY_HARD_FAIL` (entire scan halts).
     - For individual universe stocks: Symbol is excluded with reason `STALE_FEED_DATA` ($D_{actual} < D_{expected}$).
3. **Feed Desynchronization Limit:**
   - In real-time monitoring or future intraday evaluations, maximum permissible timestamp skew between Alpaca and Yahoo updates for the same bar is **300 seconds (5 minutes)**.

---

## 4. Provider Degradation & Failure Protocols

The system must handle partial provider outages deterministically without compromising capital safety:

| Alpaca Status | Yahoo Status | System State | Strategy & Scanner Behavior | Dashboard Classification |
| :---: | :---: | :---: | :--- | :--- |
| **HEALTHY** | **HEALTHY** | `OPTIMAL_DUAL_SOURCE` | Full 5-pillar scanning enabled. Real price action + consolidated ADV/RVOL. | `DUAL_SOURCE_HEALTHY` |
| **HEALTHY** | **UNAVAILABLE / STALE** | `DEGRADED_PRICE_ONLY` | **BLOCKED:** Scanner cannot verify liquidity. All new candidate generation is suppressed (`allow_new_longs = False`). Technical indicators and chart display remain active. | `DEGRADED_VOLUME_OFFLINE` (Warning: Liquidity Unverifiable) |
| **UNAVAILABLE / STALE** | **HEALTHY** | `DEGRADED_VOLUME_ONLY` | **BLOCKED:** Primary price action missing. Scanner suppressed. Zero order generation. | `DEGRADED_PRICE_OFFLINE` (Warning: Primary Price Missing) |
| **UNAVAILABLE** | **UNAVAILABLE** | `HARD_FAIL_OFFLINE` | Complete halt. No scan executed. Error logged to `observation_log.jsonl`. | `FEEDS_OFFLINE_CRITICAL` |

### Explicit Rejection Reason on Degraded Volume:
When Yahoo is unavailable but Alpaca is healthy, the scanner records:
```text
REJECTED: Liquidity unverifiable — Yahoo consolidated volume feed is unavailable or stale. 
IEX volume cannot be substituted for ADV/RVOL screening under dual-source governance.
```

---

## 5. Strict IEX Volume Substitution Guard

### 5.1 The Danger of Substitution
IEX represents $\approx 1.5\% - 3.0\%$ of total US equity market volume.
- If raw IEX volume were accidentally passed into the liquidity engine:
  - An institutional stock with \$50M consolidated ADV would display an apparent IEX ADV of only $\approx \$1M$.
  - It would be falsely rejected by the \$10M minimum ADV threshold.
  - RVOL calculations would suffer severe spikes or collapse depending on intermittent IEX routing.

### 5.2 Architectural & Code-Level Guards
1. **Strict Type Separation:** `AlpacaPriceRecord.iex_volume` cannot be passed to the constructor parameter `volume` of `DualSourceCandle`. The constructor enforces `volume_provenance == "YAHOO_CONSOLIDATED"`.
2. **Defensive Value Check:**
   If `DualSourceCandle.volume == DualSourceCandle.iex_volume` (indicating a potential copy-paste bug or accidental substitution), a runtime warning is raised:
   `PotentialSubstitutionWarning: Consolidated volume exactly matches IEX volume.`
3. **Hard Assertion in Indicator Engine:**
   ```python
   def calculate_average_dollar_volume(candles: Sequence[DualSourceCandle]) -> Decimal:
       for c in candles:
           if getattr(c, "volume_source", None) == "ALPACA_IEX":
               raise InvalidVolumeSourceError(
                   f"FATAL: Attempted to calculate ADV using unconsolidated IEX volume for {c.ticker}."
               )
   ```

---

## 6. Mathematical Methodology for ADV and RVOL

The existing frozen formulas in `indicators.py` and `scanner.py` are preserved with 100% mathematical fidelity:

### 6.1 20-Day Average Daily Volume ($\overline{V}_{20}$)
Calculated using strictly prior completed sessions of **Yahoo consolidated volume**:
$$\overline{V}_{20, t} = \frac{1}{20} \sum_{k=0}^{19} V_{t-k}^{consolidated}$$

### 6.2 Average Dollar Volume ($ADV_{20}$)
Calculated by multiplying the **Yahoo 20-day consolidated average volume** by the **Alpaca validated Close price**:
$$ADV_{20, t} = \overline{V}_{20, t} \times Close_t^{Alpaca}$$
* **Threshold Rule:** Must be $\ge \$10,000,000.00$ (quantized to 2 decimal places).
* *Rationale:* The Alpaca Close price provides the most accurate, institutional settlement reference, while the Yahoo volume provides the accurate total market liquidity pool.

### 6.3 Relative Volume ($RVOL_{20}$)
Calculated as the ratio of current session **Yahoo consolidated volume** to the 20-day trailing baseline:
$$RVOL_{20, t} = \frac{V_t^{consolidated}}{\frac{1}{20} \sum_{k=1}^{20} V_{t-k}^{consolidated}}$$
* **Threshold Rule:** Must be $\ge 0.80\text{x}$ (quantized to 4 decimal places).

---

## 7. Feed-Status Labels & Dashboard Visual Warnings

Every dashboard view and generated observation report must clearly articulate feed status:

### 7.1 Status Labels
- `DUAL_FEED_SYNCED`: Both Alpaca (Price) and Yahoo (Volume) are fresh, aligned, and validated.
- `IEX_PRICE_AUTHORITATIVE`: Confirms Open/High/Low/Close originate from Alpaca IEX API.
- `CONSOLIDATED_VOLUME_APPLIED`: Confirms ADV/RVOL derive from composite tape volume.
- `DELAYED_UNVERIFIED`: Retained as long as Yahoo volume feed reflects end-of-day delayed scraping.

### 7.2 UI Banner Specification in Streamlit
```html
<div class="banner-dual-source">
    <b>DUAL-SOURCE MARKET FEED:</b> Price action & indicators sourced from <b>Alpaca IEX API</b>; 
    Liquidity metrics (ADV & RVOL) sourced from <b>Yahoo Finance Consolidated Tape</b>. 
    Session: 2026-11-16 | Status: OPTIMAL | Read-Only Research Terminal.
</div>
```

---

## 8. Comparative Tolerance Analysis (Alpaca vs Yahoo)

To monitor data feed health without falsely flagging legitimate exchange-coverage differences as errors, the system applies calibrated tolerance bands:

| Metric | Normal Expected Variation | Investigation Threshold | Action on Breach |
| :--- | :---: | :---: | :--- |
| **Close Price Delta** | $\le 0.10\%$ | $> 0.50\%$ | Flag `PRICE_DIVERGENCE_WARNING`. Possible split desync or corporate action. |
| **High / Low Delta** | $\le 0.25\%$ | $> 1.00\%$ | Flag `RANGE_DIVERGENCE_WARNING`. Regional print or bad tick. |
| **Volume Ratio** $\left(\frac{V_{IEX}}{V_{Consolidated}}\right)$ | **$1.0\%$ to $4.0\%$** | $> 15.0\%$ or $< 0.1\%$ | Flag `VOLUME_ANOMALY_INVESTIGATION`. Verify exchange status. |
| **Session Date Delta** | **0 days (Exact match)** | $\ne 0$ days | **HARD REJECTION:** `SESSION_MISMATCH_ERROR`. |

*Crucial Rule:* A volume delta where $V_{IEX} \ll V_{Consolidated}$ is **normal, expected market microstructure behavior** and must **NEVER** trigger a data rejection or error.

---

## 9. Comprehensive Offline Testing Strategy

A dedicated test suite `tests/test_swing_dual_source_contract.py` must verify all boundary behaviors using 100% offline mocked fixtures:

| Test Case | Scenario Verified | Expected Outcome |
| :--- | :--- | :---: |
| `test_dual_source_successful_merge` | Clean Alpaca JSON + Clean Yahoo CSV | Returns `DualSourceCandle` with price from Alpaca, volume from Yahoo. |
| `test_session_date_mismatch_rejection`| Alpaca date = 2026-11-16, Yahoo date = 2026-11-15 | Raises `SessionMismatchError`; candidate excluded. |
| `test_iex_volume_substitution_guard` | Attempting to pass IEX volume to ADV calculation | Raises `InvalidVolumeSourceError`; execution halts safely. |
| `test_yahoo_offline_degradation` | Alpaca healthy, Yahoo returns HTTP 429/500 | System enters `DEGRADED_PRICE_ONLY`; scanner suppresses candidates. |
| `test_alpaca_offline_degradation`| Alpaca returns 401/403/500, Yahoo healthy | System enters `DEGRADED_VOLUME_ONLY`; scan aborted. |
| `test_adv_and_rvol_formula_precision` | Known price (\$100) and volume series | Verifies exact Decimal precision matching `scanner.py` rules. |
| `test_price_divergence_flagging` | Close price delta = 2.5% | Logs warning, attaches flag to manifest without silent alteration. |
| `test_read_only_invariance` | Ingest operations executed | All source files on disk retain identical SHA-256 hashes. |

---

## 10. Implementation Prerequisites & Unresolved Risks

### 10.1 Implementation Prerequisites
1. **Alpaca API Credentials:** Free developer API Key and Secret stored securely in local environment variables (`ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY`).
2. **Isolated Storage Directories:** Creation of `.planning/data/dual_source/` with dedicated subdirectories for daily candles, manifests, and atomic staging.
3. **Vendored Lightweight Charts Bundle:** `lightweight-charts.standalone.production.js` placed in `dashboard/static/js/` for zero-network charting.
4. **Frozen Dataset Preservation:** Verification that `.planning/data/current_daily/`, `.planning/data/historical_partitions/`, and the 2026 lockbox remain write-protected.

### 10.2 Unresolved Technical Risks
1. **Yahoo Scraper Fragility for Volume:** While Alpaca provides an institutional REST endpoint for price, Yahoo Finance remains an unauthenticated scraping endpoint for volume. If Yahoo alters its HTML or crumb mechanism, volume acquisition will fail, driving the system into `DEGRADED_PRICE_ONLY` (blocking all new trades).
   - *Future Solution:* Evaluate Polygon.io Starter or Alpaca Paid SIP feed for consolidated volume.
2. **Thin Universe Equities on IEX:** Smaller cap stocks within our 52-ticker universe may have zero trades executed on IEX on low-volatility days, resulting in missing Alpaca bars.
   - *Mitigation:* The dual-source contract mandates strict exclusion (`StockExclusion`) rather than synthetic fabrication.

---

## 11. Final Recommendation: GO / NO-GO Verdict

### Recommendation: **GO (CONDITIONAL)**

The Dual-Source Hybrid architecture is **architecturally sound, mathematically robust, and strictly protects trading capital**.

#### Conditions for Proceeding to Implementation:
1. **Phase Gating:** Implement in isolated sub-phases:
   - Phase 7.1.3: Offline mocked `AlpacaMarketDataProvider` and unit tests.
   - Phase 7.1.4: Dual-source aggregator module with volume substitution guards.
   - Phase 7.1.5: Lightweight Charts frontend component inside the dashboard.
2. **Environment Variable Isolation:** Credentials must never be written to chat, code, or tracked configuration files.
3. **Zero Strategy Drift:** Scanner rules, indicator code, and risk sizing must remain strictly frozen.
