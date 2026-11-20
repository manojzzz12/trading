# Phase 7.3.2 — Live Data Reliability Observation Report

**Project:** Trader-Swing Live Data Reliability Observation  
**Phase:** 7.3.2  
**Execution Timestamps:**
- **UTC:** `2026-11-17T18:58:42Z` to `2026-11-17T19:01:22Z`
- **America/New_York (ET):** `2026-11-17T13:58:42-05:00` to `2026-11-17T14:01:22-05:00` (NYSE Regular Market Session Active)  
**Target Symbols:** `SPY`, `QQQ`, `IWM`, `AAPL`, `MSFT` (5 Authorized US Equity Symbols)  
**Configured Feeds:**
- **Authoritative Price Action:** Alpaca IEX (`https://data.alpaca.markets`, `wss://stream.data.alpaca.markets/v2/iex`)
- **Authoritative Consolidated Volume:** Yahoo Finance Composite Tape  
**Observation Window:** `60.08 seconds` (bounded &le; 15 minutes)  
**Overall Phase Verdict:** **PROCEED (ALL COMPONENTS PASS)**  
**Subsequent Phase Gate:** **STOPPED — AWAITING FRESH EXPLICIT AUTHORIZATION**

---

## 1. Executive Summary

Phase 7.3.2 was initiated under explicit user authorization to evaluate the operational reliability of the live dual-source market data architecture across five representative US equity tickers (`SPY`, `QQQ`, `IWM`, `AAPL`, `MSFT`).

The evaluation tested all four architectural tiers under live exchange market conditions (NYSE regular trading hours active at ~14:00 ET):
1. **Alpaca REST Reliability:** 100% request success rate across all 5 symbols (mean latency: 1,128 ms). All retrieved daily bars demonstrated valid OHLC geometry, positive prices, and native split-adjusted pricing.
2. **Yahoo Consolidated Volume Matching:** 100% match rate on the exact completed NYSE session date (`2026-11-16`). Verified that IEX single-exchange volume represents between 1.35% and 3.30% of composite volume, proving the critical need for the Anti-Substitution Guard.
3. **Alpaca WebSocket Streaming Reliability:** 100% connection and authentication success on `wss://stream.data.alpaca.markets/v2/iex`. Ingested 18,203 streaming messages (17,842 quotes, 356 trades, 5 minute bars) across the 5 symbols over a 60.08-second window. The average message gap was 3.20 ms with a maximum gap of 451.11 ms (zero stale-feed events). Disconnected cleanly to `DISCONNECTED`.
4. **Dual-Source Gateway Synchronization:** Merged all completed daily bars into `DualSourceCandle` instances carrying `price_source="ALPACA_IEX"` and `volume_source="YAHOO_CONSOLIDATED"`. Completed daily bars accurately resolved to `LIVE_IEX_HYBRID` (permitting screening), while forming intraday bars resolved to `FORMING_BAR` (strictly blocking new-long screening).
5. **Safety Invariance:** Pre-test and post-test SHA-256 hashes of all protected datasets, journals, and manifests matched identically. Zero portfolio mutation, zero order placement, zero code modifications, and zero Git operations were executed.

```mermaid
flowchart TD
    start["User Authorizes Phase 7.3.2 (5 Symbols)"] --> preflight["Step 1: Preflight Audit"]
    
    subgraph Preflight_Checks [Preflight Checks (PASS)]
        c1["Credential Presence Recheck (Non-Exposing)"]
        c2["Feed Verification: IEX REST & WS"]
        c3["Phase 7.3.1 Prior Pass Confirmed"]
        c4["Market Session Check: NYSE Regular Hours Active"]
    end
    preflight --> c1 --> c2 --> c3 --> c4
    
    subgraph Live_Execution [Live Observation Pipeline]
        rest["Step 2: Alpaca REST (5 Symbols)<br/>Completed Daily Bars (Mean Latency 1128ms)"]
        yahoo["Step 3: Yahoo Volume (5 Symbols)<br/>Exact Session Match on 2026-11-16 (100% Match)"]
        ws["Step 4: Alpaca WebSocket (5 Symbols)<br/>60.08s Window, 18,203 msgs, Clean Disconnect"]
        gateway["Step 5: Dual-Source Sync Gateway<br/>8-State Readiness Matrix & Permission Guard"]
    end
    c4 --> rest --> yahoo --> ws --> gateway
    
    gateway --> audit["Step 6: Protected State & Ledger Audit<br/>Pre/Post SHA-256 Hashes Identical"]
    audit --> stop["STOP: Phase 7.3.2 Complete<br/>Await Fresh Authorization for Phase 7.3.3+"]
```

---

## 2. Component-by-Component Validation Status

| Component | Target Invariant / Protocol | Status | Metric / Evidence Summary |
| :--- | :--- | :---: | :--- |
| **1. Preflight Audit** | Non-exposing credential check, IEX config, Market session open | **PASS** | Credentials verified; feed confirmed IEX; NYSE regular session active at 13:58 ET. |
| **2. Alpaca REST** | Daily bar retrieval across 5 symbols (SPY, QQQ, IWM, AAPL, MSFT) | **PASS** | 5/5 symbols succeeded (100% success rate). Mean latency: 1,128.06 ms. |
| **3. Yahoo Volume** | Match exact completed NYSE session date (`2026-11-16`) | **PASS** | 5/5 matched (100% success rate). Volume > 0 on all symbols. Mean latency: 464.97 ms. |
| **4. Alpaca WebSocket** | Auth, 5-symbol subscribe, streaming &le; 15 min, clean disconnect | **PASS** | 18,203 messages received in 60.08s. Max gap: 451.11 ms. Zero stale streams. Clean disconnect. |
| **5. Dual-Source Gateway** | Sync prices + volume, verify 8-state readiness and permissions | **PASS** | Completed session: `LIVE_IEX_HYBRID`. Active forming session: `FORMING_BAR`. |
| **6. Strategy / Risk** | Prohibit execution of scanner or sizing | **NOT INVOKED (PASS)** | Scanner and Risk Engine untouched; zero orders or paper fills generated. |
| **7. State Invariance** | Zero ledger mutation, order generation, or code modifications | **INVARIANT (PASS)** | All protected file hashes matched identically. Account cash frozen at $5,000.00. |

---

## 3. Preflight & Environmental Audit

### 3.1 Credential & Endpoint Verification
- **Credential Check:** Automated non-disclosing routines confirmed `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` are populated in the local environment. Zero secret characters were exposed or logged.
- **REST Feed:** Confirmed `https://data.alpaca.markets/v2/stocks/bars` with parameter `feed=iex`.
- **WebSocket Feed:** Confirmed `wss://stream.data.alpaca.markets/v2/iex`.
- **Phase 7.3.1 Gate:** Confirmed Phase 7.3.1 passed successfully with report [PHASE_7_3_1_LIVE_CONNECTION_VALIDATION_REPORT.md](file:///C:/Users/mmano/OneDrive/Desktop/trading/PHASE_7_3_1_LIVE_CONNECTION_VALIDATION_REPORT.md).
- **Phase 7.3.2 Prior Check:** Confirmed no prior Phase 7.3.2 report was recorded.

### 3.2 Market Session Verification
- **Execution Timestamp:** `2026-11-17T18:58:42Z` (`2026-11-17 13:58:42-05:00` ET).
- **Session State:** `MarketSessionState.REGULAR` (active regular trading hours 09:30:00 to 16:00:00 ET).
- **Session Classification:**
  - `2026-11-16` is the latest **completed** NYSE exchange session.
  - `2026-11-17` is the currently **forming** intraday session.
- **Result:** Preflight verified that the market is open; execution proceeded without simulating streaming data.

---

## 4. REST API Reliability Analysis

Each of the five symbols was queried independently against Alpaca Market Data v2 REST API for split-adjusted daily bars:

| Symbol | HTTP Status | Request Latency (ms) | Bars Count | Latest Completed Session | Completed OHLC Prices (Alpaca IEX) | IEX Daily Volume (Shares) | Geometry & Validity |
| :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **SPY** | 200 OK | 1,406.51 ms | 5 | `2026-11-16` | O: $749.68 \| H: $749.79 \| L: $738.10 \| C: $740.04 | 1,719,427 | VALID (COMPLETE) |
| **QQQ** | 200 OK | 1,056.55 ms | 5 | `2026-11-16` | O: $671.05 \| H: $672.02 \| L: $660.50 \| C: $661.80 | 578,823 | VALID (COMPLETE) |
| **IWM** | 200 OK | 1,055.75 ms | 5 | `2026-11-16` | O: $276.12 \| H: $276.54 \| L: $271.99 \| C: $273.00 | 850,141 | VALID (COMPLETE) |
| **AAPL** | 200 OK | 1,177.67 ms | 5 | `2026-11-16` | O: $356.98 \| H: $357.01 \| L: $351.34 \| C: $352.78 | 969,009 | VALID (COMPLETE) |
| **MSFT** | 200 OK | 943.82 ms | 5 | `2026-11-16` | O: $524.50 \| H: $527.55 \| L: $516.85 \| C: $518.23 | 799,502 | VALID (COMPLETE) |

### REST Reliability Observations
- **Success Rate:** 100% (5 of 5 symbols returned HTTP 200).
- **Latency Distribution:** Min: `943.82 ms` (MSFT) | Max: `1,406.51 ms` (SPY) | Mean: `1,128.06 ms`.
- **Candle Integrity:** All candles satisfied structural invariants (`low <= open <= high`, `low <= close <= high`, positive non-zero prices).
- **Split Basis:** Confirmed natively `SPLIT_ADJUSTED`.
- **Failover Protection:** Zero failover to Yahoo price action occurred.

---

## 5. Yahoo Consolidated-Volume Validation

For each symbol, Yahoo Finance was queried for daily composite volume, matching strictly on the latest completed NYSE session date identified by Alpaca (`2026-11-16`):

| Symbol | Query Latency (ms) | Matched Session Date | Yahoo Consolidated Volume | Alpaca IEX Volume | IEX Coverage Ratio | Matching Outcome | Gateway Alignment |
| :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **SPY** | 334.06 ms | `2026-11-16` | **69,044,100** | 1,719,427 | 2.49% | EXACT_MATCH | **PASS** |
| **QQQ** | 296.69 ms | `2026-11-16` | **42,801,200** | 578,823 | 1.35% | EXACT_MATCH | **PASS** |
| **IWM** | 867.89 ms | `2026-11-16` | **25,795,200** | 850,141 | 3.30% | EXACT_MATCH | **PASS** |
| **AAPL** | 570.54 ms | `2026-11-16` | **45,945,500** | 969,009 | 2.11% | EXACT_MATCH | **PASS** |
| **MSFT** | 255.69 ms | `2026-11-16` | **30,198,800** | 799,502 | 2.65% | EXACT_MATCH | **PASS** |

### Volume Invariant Validation
- **Matching Success Rate:** 100% (5 of 5 symbols had identical session dates `D_alpaca == D_yahoo == 2026-11-16`).
- **Volume Positivity:** 100% of composite volume values were valid positive integers.
- **IEX Tape Share:** Single-exchange IEX volume represented between **1.35%** (QQQ) and **3.30%** (IWM) of total composite SIP tape volume.
- **Anti-Substitution Proof:** Confirmed that raw IEX volume cannot be used for ADV/RVOL liquidity filtering; the gateway strictly isolates IEX volume in `.auxiliary_iex_volume` and populates `.volume` strictly with Yahoo composite volume.

---

## 6. WebSocket Streaming Reliability Observation

A live, state-managed streaming connection was established to `wss://stream.data.alpaca.markets/v2/iex`, subscribing simultaneously to all five authorized symbols across `bars`, `trades`, and `quotes`.

### 6.1 Observation Timing & Lifecycle
- **Start Timestamp (UTC):** `2026-11-17T19:00:21.397513Z`
- **End Timestamp (UTC):** `2026-11-17T19:01:21.475770Z`
- **Actual Observation Duration:** `60.08 seconds` (strictly bounded &le; 15 minutes)
- **Final Client State:** `WebSocketState.DISCONNECTED`

```
Connection Lifecycle Event Sequence:
[19:00:21.397] DISCONNECTED  -> CONNECTING     (WebSocket open initiated)
[19:00:22.516] CONNECTING    -> AUTHENTICATING (Connected frame received, auth dispatched)
[19:00:22.869] AUTHENTICATING-> AUTHENTICATED  (Auth ACK received from server)
[19:00:23.151] AUTHENTICATED -> SUBSCRIBED     (Subscription ACK received for 5 symbols)
[19:01:21.475] SUBSCRIBED    -> DISCONNECTING  (Client disconnect initiated after 60s)
[19:01:21.743] DISCONNECTING -> DISCONNECTED   (Socket cleanly closed)
```

### 6.2 Subscription Confirmation
Alpaca streaming server acknowledged the subscription payload with zero errors:
- **Bars Subscribed:** `["AAPL", "IWM", "MSFT", "QQQ", "SPY"]`
- **Quotes Subscribed:** `["AAPL", "IWM", "MSFT", "QQQ", "SPY"]`
- **Trades Subscribed:** `["AAPL", "IWM", "MSFT", "QQQ", "SPY"]`

### 6.3 Inbound Message Distribution (60.08-Second Window)

| Symbol | Top-of-Book Quotes | Real-Time Trades | 1-Minute Bars | Total Messages | Messages / Second |
| :---: | :---: | :---: | :---: | :---: | :---: |
| **SPY** | 7,248 | 43 | 1 | 7,292 | 121.37 msg/s |
| **QQQ** | 948 | 9 | 1 | 958 | 15.95 msg/s |
| **IWM** | 7,316 | 73 | 1 | 7,390 | 123.00 msg/s |
| **AAPL** | 1,078 | 97 | 1 | 1,176 | 19.57 msg/s |
| **MSFT** | 1,252 | 134 | 1 | 1,387 | 23.09 msg/s |
| **TOTAL** | **17,842** | **356** | **5** | **18,203** | **302.98 msg/s** |

### 6.4 Message Timing & Latency Gaps
- **Total Ingested Messages:** `18,203`
- **Minimum Message Gap:** `0.00 ms`
- **Maximum Message Gap:** `451.11 ms` (well below the 30,000 ms stale-stream threshold)
- **Mean Message Gap:** `3.20 ms`
- **Stale Stream Events:** `0` (Zero occurrences of stream inactivity)
- **Disconnects / Drops:** `0` (Zero mid-stream drops; client maintained uninterrupted stream)

### 6.5 Intraday Forming Candle Lifecycle Invariance
The `FormingCandleManager` processed incoming minute bars ('b') and bar updates ('u') for all 5 symbols:
- **Lifecycle Status:** Strictly `CandleStatus.FORMING` across all 5 symbols.
- **Consolidated Volume Flag:** Strictly `is_consolidated_volume = False`.
- **Anti-Promotion Guard:** Confirmed that zero forming intraday bars were promoted to completed daily candles.
- **Intraday Volume Snapshot:**
  - `SPY`: 2,079 shares (Last Close: $730.74)
  - `QQQ`: 402 shares (Last Close: $654.02)
  - `IWM`: 10,776 shares (Last Close: $269.54)
  - `AAPL`: 3,661 shares (Last Close: $349.95)
  - `MSFT`: 6,978 shares (Last Close: $504.66)

---

## 7. Dual-Source Gateway Verification

The existing `DualSourceAggregator` and `DualSourceSyncGateway` were executed without modification to synchronize price candles and consolidated volume across all 5 symbols.

### 7.1 Provenance Verification
Every synchronized daily candle produced by the gateway was verified against strict data contracts:
1. `price_source == "ALPACA_IEX"`: Prices derived strictly from Alpaca IEX.
2. `volume_source == "YAHOO_CONSOLIDATED"`: Volume derived strictly from Yahoo composite tape.
3. `is_consolidated_volume == True`: Authoritative volume flag active.
4. `auxiliary_iex_volume`: Raw IEX volume preserved for diagnostics without contaminating `.volume`.
5. `adjustment_type == "SPLIT_ADJUSTED"`: Both providers confirmed identical adjustment basis.

### 7.2 8-State Data Readiness Matrix Verification

| Symbol | Query Scope | Resolved Feed State | Permits Display | Permits Historical Analysis | Permits New Long Screening | Verdict |
| :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **SPY** | Completed (`2026-11-16`) | `LIVE_IEX_HYBRID` | Yes | Yes | **Yes** | **PASS** |
| **QQQ** | Completed (`2026-11-16`) | `LIVE_IEX_HYBRID` | Yes | Yes | **Yes** | **PASS** |
| **IWM** | Completed (`2026-11-16`) | `LIVE_IEX_HYBRID` | Yes | Yes | **Yes** | **PASS** |
| **AAPL** | Completed (`2026-11-16`) | `LIVE_IEX_HYBRID` | Yes | Yes | **Yes** | **PASS** |
| **MSFT** | Completed (`2026-11-16`) | `LIVE_IEX_HYBRID` | Yes | Yes | **Yes** | **PASS** |
| **All 5** | Active Intraday (`2026-11-17`) | `FORMING_BAR` | Yes | No | **No (Strictly Blocked)** | **PASS** |

---

## 8. Protected State & Ledger Audit

Pre-observation and post-observation SHA-256 cryptographic hashes were audited across all protected repository files:

| Protected File Path | Baseline SHA-256 Hash | Post-Test SHA-256 Hash | Verification Status |
| :--- | :--- | :--- | :---: |
| `.planning/data/observations/observation_log.jsonl` | `A8F5603652062A8D1E25DEC309CA046293D0733E02DAD5FA177C3F1727E75E34` | `A8F5603652062A8D1E25DEC309CA046293D0733E02DAD5FA177C3F1727E75E34` | **IDENTICAL (PASS)** |
| `.planning/data/current_daily/CURRENT_DAILY_MANIFEST.json` | `C31A6AE4DE4F52FB55288D766C980D4BFB26D039281443E5A7005CBFABF35B51` | `C31A6AE4DE4F52FB55288D766C980D4BFB26D039281443E5A7005CBFABF35B51` | **IDENTICAL (PASS)** |
| `.planning/data/current_daily/SPY_daily.csv` | `03F55CEA4EDCBB985723688E60224E812E67A2C0DCB32FE99728671F79835626` | `03F55CEA4EDCBB985723688E60224E812E67A2C0DCB32FE99728671F79835626` | **IDENTICAL (PASS)** |
| `paper_journal.json` | `04116B7BDA4326880FF4D56899B170CA7AA9C69719E804148FCC5B684735656B` | `04116B7BDA4326880FF4D56899B170CA7AA9C69719E804148FCC5B684735656B` | **IDENTICAL (PASS)** |

- **Paper Trading Ledger:** `paper_journal.json` was not mutated; paper cash remains frozen at **$5,000.00**.
- **Execution Paths:** Zero signals, orders, paper fills, or position adjustments occurred.
- **Git State:** Zero code modifications, zero Git commits, branches, or staging operations.

---

## 9. Limitations & Edge Cases

1. **Intraday Bar Frequency:** In a 60-second observation window during active trading, exactly one 1-minute bar completed across each symbol. Longer multi-hour observations will be necessary in forward monitoring to evaluate 1-minute bar aggregation across thousands of cycles.
2. **Delayed Yahoo Free-Tier Volume:** While completed daily volume matched with 100% precision for completed sessions, intraday forming volume on Yahoo's free chart endpoint exhibits a ~15-minute reporting lag, confirming that intraday volume screening must remain strictly blocked until regular session conclusion.

---

## 10. Per-Component Verdicts & Overall Recommendation

### Component Scorecard

| Component | Verdict | Justification |
| :--- | :---: | :--- |
| **Alpaca REST Connector** | **PASS** | 100% success rate across 5 symbols; mean latency 1,128 ms; valid OHLC geometry. |
| **Yahoo Volume Integration** | **PASS** | 100% session date matching; volume positivity validated; Anti-Substitution Guard verified. |
| **Alpaca WebSocket Streaming** | **PASS** | 18,203 messages received in 60.08s; zero drops; max gap 451 ms; clean disconnect. |
| **Dual-Source Gateway** | **PASS** | Full provenance attribution; exact 8-state readiness matrix compliance. |
| **Safety & State Invariance** | **PASS** | Zero mutations to protected datasets, ledger, strategy, or git history. |

### Overall Recommendation: **PROCEED**

All live connection, latency, volume matching, streaming throughput, and architectural safety criteria defined for Phase 7.3.2 passed without exception. The dual-source market data architecture is verified to be robust, performant, and fully operational under live exchange market conditions.

---

## 11. Governance Gate

> [!IMPORTANT]
> **Phase 7.3.2 is complete.**
> 
> In strict accordance with the user instructions:
> *"This authorization applies only to this bounded observation. Do not begin Phase 7.3.3 or automated paper trading."*
> 
> Execution is halted immediately. Awaiting fresh explicit user authorization before initiating any subsequent phase.
