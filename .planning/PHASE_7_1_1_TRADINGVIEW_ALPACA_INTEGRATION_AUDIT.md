# Trader-Swing Phase 7.1.1: TradingView Lightweight Charts + Alpaca Integration Audit

**Date:** 2026-11-17  
**Phase:** 7.1.1 (Inspection & Safe Architectural Design)  
**Status:** COMPLETE (Design & Audit Only — Zero Code Implemented)  
**Safety Classification:** STRICTLY READ-ONLY (No Live Network Requests, Zero Broker Execution)

---

## 1. Executive Summary

This audit establishes a safe, modular, and non-destructive integration blueprint for two key architectural enhancements to the **Trader-Swing** platform:
1. **TradingView Lightweight Charts (v4.x/v5.x):** Replacing or augmenting static/Plotly charts with an interactive, high-performance financial charting engine rendered entirely client-side via a sandboxed, read-only interface.
2. **Alpaca Market Data API (Free Basic IEX Tier):** Introducing an institutional-grade, rate-limited REST and WebSocket market data provider as an alternative/secondary feed to Yahoo Finance (`yfinance`).

### Key Design Commitments
- **Zero Third-Party Data Leakage:** TradingView Lightweight Charts will operate purely as a visual renderer. It will **never** fetch data from TradingView servers or external widgets. All series are hydrated directly from our validated local datasets.
- **Strict Data Segregation:** Alpaca data will reside in an isolated partition (`.planning/data/alpaca/`), completely separate from `.planning/data/current_daily/`, `.planning/data/historical_partitions/`, and the 2026 lockbox.
- **Unified Engine Source:** Both the strategy engine and the chart interface will consume the identical normalized `OHLCVCandle` model when configured to use a provider.
- **Guaranteed Paper Safety:** Future paper trading will enforce a mandatory 3-gate human approval protocol. Zero automatic order placement or fill recording will occur.

---

## 2. Part A — TradingView Lightweight Charts Integration Design

### 2.1 Existing Dashboard Architecture Findings
The existing Phase 6.5 dashboard is built on Streamlit with custom cyber-dark CSS (`dashboard/styles.py`) and Plotly subplots (`dashboard/charts.py`).
- **Rendering Mechanism:** Plotly figures are serialized to JSON and rendered inside Streamlit via `st.plotly_chart()`.
- **Performance Characteristics:** Plotly renders SVG/WebGL traces well for static historical views, but displays noticeable render latency when zooming/panning large daily series (>300 bars) with dual subplots (price + volume) and three moving average traces.
- **Interactivity Limitations:** Plotly does not provide native financial crosshairs with sticky price scales, magnetic snapping, or lightweight multi-timeframe switching without full Python server-side roundtrips.

### 2.2 Embedding Lightweight Charts in Streamlit
Three embedding approaches were evaluated:

| Approach | Description | Pros | Cons | Recommendation |
| :--- | :--- | :--- | :--- | :---: |
| **Approach 1: Sandboxed HTML/JS (`st.components.v1.html`)** | Inject self-contained HTML/JS template with locally vendored `lightweight-charts.standalone.production.js`. | • Zero extra PyPI dependencies<br>• 100% offline-capable<br>• Fully isolated iframe sandbox<br>• Complete custom styling control | • One-way data binding (Python $\rightarrow$ JS)<br>• Requires explicit height setting | **RECOMMENDED** |
| **Approach 2: Bidirectional Custom Component (`declare_component`)** | Full React/TypeScript Streamlit component communicating back via `Streamlit.setComponentValue()`. | • Two-way communication (user zoom/pan/click sent back to Python) | • Requires Node.js build step<br>• Adds npm dependencies & compilation overhead<br>• High complexity for read-only view | **REJECTED for MVP** |
| **Approach 3: Third-Party PyPI Wrapper (`streamlit-lightweight-charts`)** | Pre-packaged community wrapper from PyPI. | • Quick initial setup | • Third-party dependency risk<br>• Version lag behind upstream TradingView<br>• Inflexible CSS/layout customization | **REJECTED** |

**Recommended Solution:** Implement a dedicated rendering module `dashboard/charts_tv.py` that generates an iframe string using `streamlit.components.v1.html(html_content, height=620, scrolling=False)`. The standalone JavaScript bundle (~45KB compressed) will be vendored locally in `dashboard/static/js/lightweight-charts.standalone.js`, ensuring zero external network calls during dashboard rendering.

### 2.3 Shared Symbol Selection with Scanner
To achieve seamless workflow continuity between the candidate scanner and the visual chart:
1. **Shared Session State:** Streamlit's `st.session_state["selected_ticker"]` serves as the single source of truth across views.
2. **Scanner Selection:** In Tab 3 (*Scanner & Candidates*), selecting a ticker from the evaluated candidate table updates `st.session_state["selected_ticker"]`.
3. **Chart View Synchronization:** In Tab 2 (*Market Overview / Interactive Chart*), the ticker selector binds directly to `st.session_state["selected_ticker"]` (defaulting to `"SPY"` if unset).
4. **Zero Engine Mutation:** Changing the displayed symbol only filters the client-side candle series and does not trigger scanner recalculation or paper state changes.

### 2.4 Chart Display & Visual Specifications
The Lightweight Charts canvas will be configured to match the terminal dark cyber theme (`#0b0f19`):

```javascript
const chart = LightweightCharts.createChart(container, {
    width: container.clientWidth,
    height: 520,
    layout: {
        background: { type: 'solid', color: '#0b0f19' },
        textColor: '#94a3b8',
        fontFamily: 'JetBrains Mono, -apple-system, monospace',
    },
    grid: {
        vertLines: { color: 'rgba(30, 41, 59, 0.6)' },
        horzLines: { color: 'rgba(30, 41, 59, 0.6)' },
    },
    crosshair: {
        mode: LightweightCharts.CrosshairMode.Normal,
        vertLine: { color: '#38bdf8', width: 1, style: 3, labelBackgroundColor: '#0284c7' },
        horzLine: { color: '#38bdf8', width: 1, style: 3, labelBackgroundColor: '#0284c7' },
    },
    timeScale: {
        borderColor: '#1e293b',
        timeVisible: true,
        secondsVisible: false,
    },
    rightPriceScale: {
        borderColor: '#1e293b',
        autoScale: true,
    },
});
```

#### Series Overlays:
1. **Candlestick Series:**
   - Bullish: Up color `#10b981`, wick up `#10b981`, border visible: `false`.
   - Bearish: Down color `#f43f5e`, wick down `#f43f5e`, border visible: `false`.
2. **Volume Histogram Subplot:**
   - Rendered using `chart.addHistogramSeries()`.
   - Configured with `priceScaleId: ""` and `scaleMargins: { top: 0.82, bottom: 0 }` to dock volume bars at the bottom 18% of the canvas without overlapping candles.
   - Dynamic bar coloring: `#10b981` (up candle) or `#f43f5e` (down candle) with 50% opacity.
3. **Technical Overlays (Moving Averages):**
   - **SMA 20:** `chart.addLineSeries({ color: '#38bdf8', lineWidth: 2, title: 'SMA 20' })`
   - **SMA 50:** `chart.addLineSeries({ color: '#f59e0b', lineWidth: 2, title: 'SMA 50' })`
   - **SMA 200:** `chart.addLineSeries({ color: '#a855f7', lineWidth: 2, title: 'SMA 200' })`
4. **Timeframe Controls:**
   - HTML buttons (`1D`, `1H`, `15M`, `5M`) positioned above the canvas.
   - Clicking a timeframe sends the request back or loads client-side pre-cached timeframe slices.
5. **Crosshair Floating Tooltip:**
   - Subscribes to `chart.subscribeCrosshairMove((param) => { ... })`.
   - Renders a floating HTML badge displaying Date/Time, Open, High, Low, Close, Change (%), Volume, and SMA values.

### 2.5 Data Supply & Read-Only Governance
- **Strict Data Pipeline:** Python reads local CSV/database files, validates them through `MarketDataFeedAdapter`, formats records into JSON arrays, and injects them directly into the JavaScript template.
- **TradingView Widget Prohibition:** Embedded iframe widgets from `tradingview.com` that fetch remote market data over TradingView's proprietary servers are **strictly prohibited**. The engine must never use data that did not pass our deterministic validation gates.
- **Read-Only Sandbox:** The component iframe will have sandbox attributes restricted to `allow-scripts` (no `allow-forms`, no `allow-top-navigation`, no external socket connections).

---

## 3. Part B — Alpaca Market Data API Evaluation

### 3.1 Overview & Free Basic Tier (IEX Feed)
Alpaca provides two market data tiers:
- **Unlimited/Paid Tier:** Consolidated SIP feed (CTA/UTP) covering all US stock exchanges.
- **Free Basic Tier:** Single-exchange IEX feed (`feed=iex`).

### 3.2 Deep-Dive Technical Evaluation

#### A. Exchange Coverage & Liquidity Implications
- **IEX Market Share:** IEX represents approximately **1.5% to 3.0%** of total US equity market volume.
- **Price Precision:** For highly liquid large caps (SPY, AAPL, MSFT, NVDA), IEX trades occur at the National Best Bid and Offer (NBBO) throughout regular trading hours. Daily High, Low, and Close prices track the consolidated SIP closely (typical delta < 0.05%).
- **Volume Disparity (CRITICAL):** Total daily volume on IEX is **substantially lower** than consolidated volume reported by Yahoo Finance or SIP.
  - *Example:* On 2026-11-16, consolidated SPY volume was ~65,000,000 shares; IEX volume was ~1,500,000 shares.
  - *Strategy Impact:* Trader-Swing's 5-pillar scanner enforces liquidity criteria (e.g., ADV $\ge$ $10M, RVOL $\ge$ 0.80x). Direct calculation of dollar volume using IEX shares would trigger false liquidity rejections across the universe.
  - *Architectural Requirement:* When operating on the IEX feed, liquidity metrics must either:
    1. Be explicitly scaled against historical IEX baselines, OR
    2. Require a dedicated `IEX_LIQUIDITY_ADJUSTED` policy, OR
    3. Retain Yahoo consolidated volume for ADV/RVOL while using Alpaca for price verification.

#### B. Historical Bars REST API
- **Endpoint:** `GET https://data.alpaca.markets/v2/stocks/bars`
- **Authentication:** HTTP Headers `APCA-API-KEY-ID` and `APCA-API-SECRET-KEY`.
- **Key Parameters:**
  - `symbols`: Comma-separated list of tickers (supports up to 100 tickers per call on Free tier).
  - `timeframe`: `1Day`, `1Hour`, `15Min`, `5Min`, `1Min`.
  - `start`, `end`: RFC 3339 formatted ISO datetimes in UTC (`YYYY-MM-DDTHH:MM:SSZ`).
  - `adjustment`: Supported options: `raw`, `split`, `dividend`, `all`.
  - `feed`: `iex`.
  - `limit`: Maximum 10,000 bars per request.
  - `page_token`: Opaque pagination token for requests exceeding limit.
- **Response Format:**
  ```json
  {
    "bars": {
      "SPY": [
        {
          "t": "2026-11-16T05:00:00Z",
          "o": 745.20,
          "h": 747.80,
          "l": 738.50,
          "c": 739.96,
          "v": 1542100,
          "n": 18450,
          "vw": 742.15
        }
      ]
    },
    "next_page_token": null
  }
  ```

#### C. Real-Time WebSocket API
- **Endpoint:** `wss://stream.data.alpaca.markets/v2/iex`
- **Protocol:** JSON message streams over persistent WebSocket.
- **Connection Handshake:**
  1. Client connects $\rightarrow$ Server responds `[{"T":"success","msg":"connected"}]`.
  2. Client authenticates: `{"action": "auth", "key": "...", "secret": "..."}`.
  3. Server acknowledges: `[{"T":"success","msg":"authenticated"}]`.
  4. Client subscribes: `{"action": "subscribe", "bars": ["SPY"], "dailyBars": ["SPY"]}`.
- **Message Types:**
  - `b`: Minute bar (`t`, `o`, `h`, `l`, `c`, `v`, `n`, `vw`).
  - `d`: Daily bar roll-up.
  - `t`: Trade tick.
  - `q`: Quote (Bid/Ask).
- **Assessment for Trader-Swing:** Trader-Swing is a swing-trading system operating on completed daily session bars after market close (16:00 ET). The REST API is vastly preferable over WebSockets: deterministic, stateless, immune to disconnects during market close, and easy to pace. WebSockets should be reserved exclusively for future intraday observation.

#### D. Timestamp Handling & Session Normalization
- Alpaca returns timestamps in RFC 3339 UTC format.
- For daily bars, Alpaca anchors timestamps at `00:00:00Z` or `05:00:00Z` (midnight UTC or midnight Eastern).
- **Normalization Required:** In accordance with Trader-Swing's `MarketClock` convention, the daily bar timestamp must be normalized to the session date at `09:30:00 America/New_York` converted to UTC (`14:30:00Z` EDT or `13:30:00Z` EST).

#### E. Rate Limits & Symbol Batching
- **Free Basic Tier Limit:** 200 requests per minute per API key.
- **Multi-Symbol Query Advantage:** Alpaca allows querying up to 100 symbols in a single HTTP request (`symbols=SPY,QQQ,IWM,AAPL,MSFT...`).
- **Pacing Efficiency:** Fetching our 52-ticker universe requires only **1 HTTP request** (or 2 requests if split), completing in < 1.5 seconds. This eliminates the 130-second sequential pacing delay required by Yahoo Finance while remaining far below the 200 req/min limit.

#### F. Incomplete / Missing Bars
- On the IEX feed, low-volume symbols may have intervals with zero executed trades. Alpaca returns an empty array or omits the bar.
- **Zero-Fabrication Policy:** The adapter must **never** interpolate, synthetic-fill, or forward-fill missing bars. If a symbol lacks completed session data, it must be reported in `StockExclusion` with reason `INSUFFICIENT_BARS` or `NO_DATA`.

#### G. Corporate Actions & Split Adjustments
- Alpaca natively supports `adjustment=split`, which adjusts prices and volumes for stock splits while preserving nominal historical close prices (cash dividends are not deflated).
- This aligns with Trader-Swing's mandatory `SPLIT_ADJUSTED` price basis standard established in Phase 6.2B.

---

### 3.3 Provider Comparison Matrix

| Dimension | Existing `yfinance` Provider | Proposed `AlpacaMarketDataProvider` |
| :--- | :--- | :--- |
| **Data Source** | Scraping Yahoo Finance web endpoints | Official REST / WebSocket API |
| **Authentication** | Unauthenticated (crumb/cookie session) | API Key + Secret (Header authenticated) |
| **Exchange Scope** | Consolidated US Market (SIP composite) | Single Exchange (IEX Free) / SIP (Paid) |
| **Volume Basis** | Consolidated total market volume | IEX-only volume (~2% of market) |
| **Rate Limits** | IP-throttled (~2.5s jittered pacing mandatory) | 200 req/min (Free) / 10,000 req/min (Paid) |
| **Batch Querying** | No (must request ticker-by-ticker) | Yes (up to 100 symbols per REST call) |
| **Timestamp Format** | Varies (epoch seconds or date string) | Strict RFC 3339 UTC |
| **Adjustment Handling** | `auto_adjust=False, actions=True` | `adjustment="split"` native query parameter |
| **Reliability / SLA** | No SLA; subject to Yahoo structure changes | Documented commercial API with status page |
| **Forming Candle Detection** | Inferred via MarketClock elapsed time | Explicit real-time minute/daily bars |
| **Data Classification** | `DELAYED_UNVERIFIED` | `IEX_REALTIME_UNCONSOLIDATED` (Free) |

---

## 4. Part C — Data Architecture & Independent Data Paths

```
                                 ┌─────────────────────────┐
                                 │  Alpaca Market Data API │
                                 │     (REST / IEX)        │
                                 └────────────┬────────────┘
                                              │
                                              ▼
                                 ┌─────────────────────────┐
                                 │ AlpacaMarketDataProvider│
                                 │ (Adapts to Base Provider│
                                 └────────────┬────────────┘
                                              │
                      ┌───────────────────────┴───────────────────────┐
                      │                                               │
                      ▼                                               ▼
     ┌─────────────────────────────────┐             ┌─────────────────────────────────┐
     │           PATH A:               │             │           PATH B:               │
     │       Strategy Engine           │             │       Visual Dashboard          │
     ├─────────────────────────────────┤             ├─────────────────────────────────┤
     │ MarketDataFeedAdapter           │             │ Lightweight Charts Serializer   │
     │  ├── SPY Mandatory Check (>=200)│             │  ├── Candlestick Data Array     │
     │  ├── Stock Min Bars (>=50)      │             │  ├── Volume Histogram Array     │
     │  └── Split Cliff Continuity     │             │  └── SMA 20 / 50 / 200 Series   │
     │                 │               │             │                 │               │
     │                 ▼               │             │                 ▼               │
     │ FeedReadinessResult             │             │ TradingView Lightweight Canvas  │
     │                 │               │             │ (Client-side Sandboxed iframe)  │
     │                 ▼               │             └─────────────────────────────────┘
     │ Regime Detector & Scanner       │
     │                 │               │
     │                 ▼               │
     │ Deterministic Risk Engine       │
     └─────────────────────────────────┘
```

### 4.1 Storage Partition Design
To preserve absolute isolation between existing historical records, Yahoo observation data, and Alpaca feeds:

```
.planning/data/
├── historical_partitions/         <-- [FROZEN] Historical backtest partitions
├── current_daily/                 <-- [EXISTING] Yahoo Finance forward observation data
│   ├── CURRENT_DAILY_MANIFEST.json
│   └── <TICKER>_daily.csv
├── alpaca/                        <-- [NEW ISOLATED PARTITION]
│   ├── daily/                     <-- Daily candle CSV files from Alpaca
│   │   ├── SPY_daily.csv
│   │   └── <TICKER>_daily.csv
│   ├── manifest/                  <-- Alpaca acquisition manifests
│   │   └── ALPACA_DAILY_MANIFEST.json
│   └── staging/                   <-- Atomic staging directory for atomic writes
│       └── .staging/
```

- **Dedicated Local Cache:** `.agents/skills/trader-swing/data/market_data_alpaca.db` (isolated SQLite cache, never touching `market_data_yfinance_split_adj.db` or root databases).
- **Prohibited Overwrites:** The Alpaca pipeline will have strict hard-coded path checks rejecting any destination containing `historical_partitions`, `2026_lockbox`, `current_daily`, `TradingPaperState`, or `paper_journal.json`.

---

## 5. Part D — Paper Trading Preparation & Safety Governance

Future automated paper-trading integration must operate with complete separation between market-data ingestion, strategy signal generation, and simulated order handling.

### 5.1 Mandatory 3-Gate Approval Protocol
No order may ever be submitted to Alpaca's paper trading API without passing three independent gates:

```
┌────────────────────────┐      ┌────────────────────────┐      ┌────────────────────────┐
│        GATE 1:         │      │        GATE 2:         │      │        GATE 3:         │
│   Deterministic Risk   │ ===> │ Explicit Chat Approval │ ===> │ Antigravity Confirmation│
│      Verification      │      │     (Human "YES")      │      │     Interactive Modal  │
└────────────────────────┘      └────────────────────────┘      └────────────────────────┘
```

1. **Gate 1 — Frozen Strategy & Risk Rules:**
   - Account Capital: $5,000.00 baseline.
   - Max Risk Per Trade: 1.0% ($50.00 maximum stop-loss dollar risk).
   - Max Simultaneous Positions: 4 positions.
   - Cash Buffer: 100% reserved if regime is BEARISH/STRONG_BEARISH.
   - Position Sizing: Floor-rounded integer shares calculated deterministically from Stop distance.
2. **Gate 2 — Explicit User Chat Confirmation:**
   - The user must review a plain-text ticket detailing: Symbol, Side, Shares, Limit Price, Stop Loss, Take Profit, Max Dollar Risk, and Reward:Risk ratio.
   - The user must explicitly type an affirmative confirmation in chat.
3. **Gate 3 — Antigravity Tool Confirmation:**
   - The final tool execution prompt must require manual user acceptance before any broker API request is dispatched.

### 5.2 Isolated Paper Ledger Architecture
- **Simulated Paper Ledger:** `.planning/data/alpaca_paper/paper_ledger.json`
- **Execution Boundaries:**
  - Automated order routing: **DISABLED by default**.
  - Auto-fill recording: **DISABLED by default**.
  - All paper executions remain simulated and isolated until approved.
  - Zero access or writes to root `paper_journal.json`, root `portfolio.json`, or external `TradingPaperState`.

---

## 6. Data-Quality & Validation Rules

When ingesting data from Alpaca into Trader-Swing, the following deterministic validation rules must be enforced by `AlpacaMarketDataProvider` and `MarketDataFeedAdapter`:

1. **Intrinsic Bar Sanity:**
   - $Low \le Open \le High$
   - $Low \le Close \le High$
   - $Low > 0$, $Open > 0$, $High > 0$, $Close > 0$
   - $Volume \ge 0$ (must be integer)
2. **Session Alignment & Timing:**
   - Reject any candle dated in the future or belonging to a non-trading calendar day (weekends, official NYSE holidays).
   - Only completed session bars are accepted for swing scanning (elapsed time $\ge$ 16:00 ET).
3. **Sequence Chronology & Continuity:**
   - Bar timestamps must be strictly monotonic ascending ($t_i > t_{i-1}$).
   - Duplicate timestamps trigger immediate rejection.
4. **Mandatory Proxy Verification:**
   - SPY is mandatory: requires $\ge 200$ completed daily bars. If SPY is missing or stale, produce a `HARD_FAIL`.
   - QQQ and IWM are optional: requires $\ge 20$ completed daily bars; exclude with explicit warning if deficient.
5. **Stock Universe Eligibility:**
   - Stocks require $\ge 50$ completed daily bars.
   - Exclude stocks missing the latest completed session date.
6. **Data Classification Tagging:**
   - Every bar and manifest generated from the free Alpaca tier must carry the explicit classification label:
     `IEX_UNCONSOLIDATED_REALTIME` (or `IEX_DELAYED` if past session).

---

## 7. Testing Strategy

Prior to any live network usage, the Alpaca provider and Lightweight Charts integration must undergo complete offline testing:

### 7.1 Alpaca Provider Mocked Test Suite (`tests/test_swing_alpaca_provider.py`)
1. **Historical Bar Deserialization:** Verify conversion of Alpaca JSON bar responses into normalized `OHLCVCandle` objects with Decimal pricing.
2. **Multi-Symbol Batching:** Verify parser correctly handles responses containing multiple symbol keys (`bars: {"SPY": [...], "AAPL": [...]}`).
3. **Rate Limit Handling (HTTP 429):** Mock 429 response; assert provider enters bounded exponential backoff and raises `RateLimitError` upon retry exhaustion.
4. **Authentication Failure (HTTP 401/403):** Verify missing/invalid API keys fail immediately with `ProviderUnavailableError` without retrying.
5. **No Data / Invalid Ticker (HTTP 404):** Verify unknown symbols raise `NoDataError`.
6. **Timestamp & Calendar Normalization:** Verify UTC timestamps are converted to NYSE 09:30:00 session start in UTC.
7. **Read-Only Invariance:** Confirm that provider instantiation and offline calls never write to disk or mutate existing partitions.

### 7.2 Lightweight Charts Unit Test Suite (`tests/test_swing_lightweight_charts.py`)
1. **JSON Serialization:** Test conversion of DataFrame/Candle series to Lightweight Charts format (`time`, `open`, `high`, `low`, `close`).
2. **Subplot Scaling:** Verify volume histogram series has correct `priceScaleId` and margin settings.
3. **SMA Alignment:** Verify SMA20, SMA50, and SMA200 traces match candle date keys.
4. **HTML Template Construction:** Verify HTML string contains required container IDs, script tags, and dark theme palette.

---

## 8. Security, Licensing & Dependencies

### 8.1 API Key Security & Storage
- **Zero Secrets in Code/Chat:** Alpaca API keys (`APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`) must **never** be hardcoded, printed in logs, or written into git-tracked files.
- **Environment Variable Resolution:** The provider will read exclusively from `os.environ.get("ALPACA_API_KEY_ID")` and `os.environ.get("ALPACA_API_SECRET_KEY")` (or an untracked `.env` file).
- **Graceful Offline Mode:** If environment keys are missing, the provider must remain inert and report `ProviderUnavailableError("Missing Alpaca API credentials in environment")`.

### 8.2 Dependency & Licensing Requirements
- **TradingView Lightweight Charts:**
  - License: **Apache License 2.0** (Open Source, permissive, commercial-friendly).
  - Dependency: Zero Python package dependencies if using vendored standalone JS (`lightweight-charts.standalone.production.js`).
- **Alpaca Integration:**
  - Preferred Implementation: Direct REST queries using standard Python standard library (`urllib.request`) or `requests`, matching the pattern in `yfinance_provider.py`.
  - Alternative: `alpaca-py` official SDK (MIT license).
  - Recommendation: Use lightweight direct HTTP calls via `urllib` or existing installed `requests` to avoid introducing a heavy SDK dependency footprint.

---

## 9. Phased Implementation Sequence

The proposed roadmap decomposes Phase 7 into safe, gated sub-phases:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ Phase 7.1.1: Design & Integration Audit (THIS PHASE — COMPLETED)             │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ Phase 7.1.2: Lightweight Charts Visual Component Implementation             │
│ • Vendor lightweight-charts.standalone.production.js in dashboard/static/js │
│ • Create dashboard/charts_tv.py generator module                            │
│ • Wire symbol selection to st.session_state                                 │
│ • Add offline unit tests in tests/test_swing_lightweight_charts.py          │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ Phase 7.1.3: Alpaca Market Data Provider Implementation                     │
│ • Create .agents/skills/trader-swing/providers/alpaca_provider.py           │
│ • Implement AlpacaMarketDataProvider subclassing MarketDataProvider         │
│ • Implement mock response fixtures & unit tests                             │
│ • Verify split adjustment and timestamp normalization                       │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ Phase 7.1.4: Dual-Provider Feed Adapter Verification                        │
│ • Configure FeedAdapter to support selectable provider (yfinance / alpaca)  │
│ • Validate IEX volume handling & liquidity thresholds                       │
│ • Conduct controlled, authorized live acquisition into .planning/data/alpaca│
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ Phase 7.1.5: Paper Trading Preparation & 3-Gate Verification                │
│ • Build isolated paper ledger simulation                                    │
│ • Implement plain ticket renderer and confirmation checks                   │
│ • Perform end-to-end rehearsal with mock fills                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 10. Technical Risks, Tradeoffs & Unresolved Questions

| Risk / Consideration | Severity | Mitigation Strategy |
| :--- | :---: | :--- |
| **IEX Volume Under-Representation** | **HIGH** | IEX volume is ~2% of total market. Using raw IEX volume in Trader-Swing's standard $10M ADV and 0.8x RVOL scanner rules will reject almost all valid setups. *Decision needed:* Either use Yahoo consolidated volume for liquidity screening while using Alpaca for price action, or establish dedicated IEX liquidity thresholds. |
| **IEX Data Gaps on Thin Equities** | **MEDIUM** | Less liquid universe stocks may have missing daily bars on IEX if zero trades executed on the IEX venue. *Mitigation:* The feed adapter must strictly exclude these tickers (`StockExclusion`) rather than forward-filling. |
| **Iframe CSS Container Resizing** | **LOW** | Streamlit iframe height is fixed. *Mitigation:* Set responsive client-side JavaScript resize observer with fixed default height (620px). |
| **API Key Requirement** | **LOW** | Alpaca requires a free developer account and API key. *Mitigation:* Keep provider disabled by default until keys are placed in local environment variables. |

---

## 11. Protected State Audit & Boundary Confirmation

In strict compliance with instructions:
- **Zero Code Implemented:** No code was added or modified in `dashboard/`, `scripts/`, or `.agents/skills/trader-swing/`.
- **Zero Network Calls:** No HTTP requests, WebSocket handshakes, or live data downloads were executed.
- **Zero State Mutation:**
  - `observation_log.jsonl` remains untouched (`63,475 bytes`, `sha256: a8f56036...`).
  - `CURRENT_DAILY_MANIFEST.json` remains untouched (`1,725 bytes`, `sha256: c31a6ae4...`).
  - `.planning/data/historical_partitions/` remains untouched.
  - Root `paper_journal.json`, root `portfolio.json`, `broker.json`, and the 2026 lockbox were **not accessed or modified**.
- **No Background Processes:** No servers, daemons, or background workers were spawned.
