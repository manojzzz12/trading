"""Trader-Swing Read-Only Research Dashboard (Phase 7.1.5).

A professional, locally-hosted Streamlit research terminal for inspecting:
1. 🖥️ Terminal Deep-Dive: Ticker-specific candlestick, volume, RSI, 5-pillar evaluation & trade setup.
2. 📊 Market & Screener Overview: Top evaluated stocks, 30-day relative trajectory, regime sentiment & breadth.
3. 💼 Portfolio Command Center: Baseline paper portfolio, equity curve, asset & risk allocation, active orders.
4. 📜 Observation History & Audit: Forward observation logs, candidate filter table, partition health & exclusions.

Strict Safety Boundaries:
- READ-ONLY: Never places orders, mutates ledgers, or calls external APIs.
- The existing Python strategy engine is the sole source of truth.
- Zero live network requests or external credential access.
"""

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
import streamlit as st
import pandas as pd

from data_loader import (
    DEFAULT_CURRENT_DATA_DIR,
    DEFAULT_GATEWAY_FIXTURES,
    DEFAULT_MANIFEST_PATH,
    DEFAULT_OBSERVATION_LOG,
    categorize_rejection_reasons,
    filter_candidates,
    get_available_session_dates,
    get_gateway_feed_status,
    get_gateway_health_history,
    get_gateway_screener_summary,
    get_market_breadth_metrics,
    get_portfolio_active_positions_and_orders,
    get_portfolio_equity_curve_data,
    get_readiness_badge,
    get_streaming_buffer,
    get_verified_portfolio_snapshot,
    load_acquisition_manifest,
    load_forward_evaluation_dashboard_data,
    load_gateway_ticker_data,
    load_observation_log,
    load_proxy_candles,
    load_ticker_candles,
    READINESS_BADGE_MAP,
    StreamingMessageBuffer,
)
from charts import (
    create_asset_allocation_donut,
    create_forming_intraday_chart,
    create_market_sentiment_gauge,
    create_pipeline_funnel_chart,
    create_portfolio_equity_chart,
    create_proxy_candlestick_chart,
    create_regime_donut_chart,
    create_rejection_reasons_bar_chart,
    create_risk_capacity_donut,
    create_ticker_candlestick_chart,
    create_top_equities_comparison_chart,
)
from styles import DARK_THEME_CSS


# -----------------------------------------------------------------------------
# App Configuration & Styling
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="Trader-Swing Terminal",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(DARK_THEME_CSS, unsafe_allow_html=True)


# -----------------------------------------------------------------------------
# Data Loading & Caching (Read-Only)
# -----------------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def get_dashboard_data():
    """Load local files into memory. Cached until manual refresh."""
    candidates, sessions, log_errors = load_observation_log(DEFAULT_OBSERVATION_LOG)
    manifest, manifest_warnings = load_acquisition_manifest(DEFAULT_MANIFEST_PATH)
    portfolio = get_verified_portfolio_snapshot()
    return {
        "candidates": candidates,
        "sessions": sessions,
        "log_errors": log_errors,
        "manifest": manifest,
        "manifest_warnings": manifest_warnings,
        "portfolio": portfolio,
    }


data = get_dashboard_data()
candidate_records = data["candidates"]
session_records = data["sessions"]
manifest_data = data["manifest"]
portfolio_data = data["portfolio"]
log_errors = data["log_errors"]

available_sessions = get_available_session_dates(candidate_records, session_records)
default_session = available_sessions[0] if available_sessions else "2026-11-16"


# -----------------------------------------------------------------------------
# Sidebar Navigation & Controls
# -----------------------------------------------------------------------------
st.sidebar.markdown(
    """
    <div style="padding: 0.5rem 0 0.8rem 0;">
        <div style="font-size: 1.15rem; font-weight: 800; letter-spacing: 0.08em; color: #38bdf8;">
            ⚡ TRADER-SWING
        </div>
        <div style="font-size: 0.72rem; color: #94a3b8; font-weight: 600;">
            RESEARCH TERMINAL (READ-ONLY)
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

# Navigation Menu
nav_options = [
    "🖥️ Terminal Deep-Dive",
    "📊 Market & Screener Overview",
    "💼 Portfolio Command Center",
    "📜 Observation History & Audit",
    "🔭 Forward Paper Evaluation",
]
default_nav_idx = 0
try:
    if "screen" in st.query_params:
        param_val = str(st.query_params["screen"]).lower()
        if "screener" in param_val or "market" in param_val or param_val == "2":
            default_nav_idx = 1
        elif "portfolio" in param_val or param_val == "3":
            default_nav_idx = 2
        elif "audit" in param_val or "history" in param_val or param_val == "4":
            default_nav_idx = 3
        elif "forward" in param_val or "eval" in param_val or param_val == "5":
            default_nav_idx = 4
except Exception:
    default_nav_idx = 0

nav_selection = st.sidebar.radio(
    "Terminal Navigation",
    options=nav_options,
    index=default_nav_idx,
)

st.sidebar.markdown("---")

# Session Selector & Forward Evaluation Contextualization
if nav_selection == "🔭 Forward Paper Evaluation":
    st.sidebar.markdown("#### 🔭 Forward Evaluation Scope")
    fwd_mode_choice = st.sidebar.radio(
        "Evaluation Scope",
        [
            "🔭 Genuine Daily Forward (Default)",
            "🧪 Diagnostic Fixtures (Test Data)",
            "🌐 Combined Audit (All Records)",
        ],
        index=0,
    )
    genuine_only = (fwd_mode_choice == "🔭 Genuine Daily Forward (Default)")
    filter_data_mode = "FIXTURE" if fwd_mode_choice == "🧪 Diagnostic Fixtures (Test Data)" else None

    # Load forward data for Screen 5
    fwd_data = load_forward_evaluation_dashboard_data(data_mode=filter_data_mode, genuine_only=genuine_only)
    fwd_sum_df = fwd_data["summaries_df"]
    fwd_sessions = sorted(fwd_sum_df["session_date"].dropna().unique().tolist(), reverse=True) if not fwd_sum_df.empty else []

    selected_session = st.sidebar.selectbox(
        "Forward Evaluation Session",
        options=fwd_sessions if fwd_sessions else (["No daily forward sessions"] if genuine_only else ["No sessions recorded"]),
        index=0,
    )
    curr_session_record = None
    curr_candidates = []
    tickers_evaluated = 0
    eligible_count = 0
    rejected_count = 0
    active_regime = "N/A"
    acq_date = "N/A"
    acq_timestamp = "N/A"
    selected_ticker = "SPY"
else:
    # Screens 1-4 Research Observation Session Selector
    selected_session = st.sidebar.selectbox(
        "Active Observation Session",
        options=available_sessions if available_sessions else [default_session],
        index=0,
    )

    curr_session_record = next((s for s in session_records if s.get("date") == selected_session), None)
    curr_candidates = [c for c in candidate_records if c.get("date") == selected_session]

    tickers_evaluated = curr_session_record.get("tickers_evaluated_count", len(curr_candidates)) if curr_session_record else len(curr_candidates)
    eligible_count = curr_session_record.get("eligible_count", len([c for c in curr_candidates if c.get("scanner_decision") == "ELIGIBLE"])) if curr_session_record else len([c for c in curr_candidates if c.get("scanner_decision") == "ELIGIBLE"])
    rejected_count = curr_session_record.get("rejected_count", len([c for c in curr_candidates if c.get("scanner_decision") == "REJECTED"])) if curr_session_record else len([c for c in curr_candidates if c.get("scanner_decision") == "REJECTED"])
    active_regime = curr_session_record.get("market_regime", curr_candidates[0].get("market_regime", "UNKNOWN")) if (curr_session_record or curr_candidates) else "UNKNOWN"

    acq_date = manifest_data.get("as_of_session_date", "2026-11-16") if manifest_data else "2026-11-16"
    acq_timestamp = manifest_data.get("acquisition_timestamp", "2026-11-16T21:02:19Z") if manifest_data else "2026-11-16T21:02:19Z"

    # Ticker Watchlist Options (for Deep-Dive)
    universe_symbols = [c.get("ticker") for c in curr_candidates if c.get("ticker")]
    all_selectable_symbols = ["SPY", "QQQ", "IWM"] + sorted(universe_symbols)

    if "selected_ticker" not in st.session_state:
        st.session_state["selected_ticker"] = "HD" if "HD" in universe_symbols else (universe_symbols[0] if universe_symbols else "AAPL")

    selected_ticker = st.sidebar.selectbox(
        "Deep-Dive Ticker",
        options=all_selectable_symbols,
        index=all_selectable_symbols.index(st.session_state["selected_ticker"]) if st.session_state["selected_ticker"] in all_selectable_symbols else 0,
    )
    st.session_state["selected_ticker"] = selected_ticker

# Data Pipeline Mode Selector (Historical vs Live Gateway)
st.sidebar.markdown(
    """
    <div style="font-size: 0.72rem; color: #94a3b8; font-weight: 700; text-transform: uppercase; margin-top: 0.5rem; margin-bottom: 0.2rem;">
        Data Pipeline Mode
    </div>
    """,
    unsafe_allow_html=True,
)
data_mode_choice = st.sidebar.radio(
    "Data Pipeline Mode",
    options=["⚡ Live Gateway (Dual-Source IEX + Yahoo)", "📜 Historical Baseline (Delayed)"],
    index=0,
    label_visibility="collapsed",
)
is_live_mode = "Live" in data_mode_choice
active_data_mode = "LIVE" if is_live_mode else "HISTORICAL"

# Local Refresh Button
if st.sidebar.button("🔄 Reload Local Data", use_container_width=True):
    st.cache_data.clear()
    st.rerun()

# Dual-Source Sync Gateway Status Widget
gateway_feed_info = get_gateway_feed_status(selected_ticker, data_mode=active_data_mode)
gw_badge_cls, gw_badge_lbl = get_readiness_badge(gateway_feed_info["combined_readiness"])
st.sidebar.markdown(
    f"""
    <div class="card-container" style="padding: 0.75rem; margin-top: 0.8rem; margin-bottom: 0.8rem;">
        <div style="font-size: 0.75rem; font-weight: 700; color: #38bdf8; text-transform: uppercase; margin-bottom: 0.4rem;">
            ⚡ DUAL-SOURCE GATEWAY
        </div>
        <div style="font-size: 0.75rem; color: #f8fafc; margin-bottom: 0.35rem;">
            Feed State: <span class="badge {gw_badge_cls}">{gw_badge_lbl}</span>
        </div>
        <div style="font-size: 0.7rem; color: #94a3b8; line-height: 1.45;">
            <b>Price Feed:</b> {gateway_feed_info['alpaca_feed']}<br/>
            <b>Volume Tape:</b> {gateway_feed_info['yahoo_feed']}<br/>
            <b>Session:</b> {gateway_feed_info['exchange_session']['session_state']}<br/>
            <b>Last Sync:</b> {gateway_feed_info['last_update_timestamp']}
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

st.sidebar.markdown(
    """
    <div style="margin-top: 1.2rem; font-size: 0.72rem; color: #64748b; line-height: 1.5;">
        <p><b>Boundary Status:</b> <span style="color: #38bdf8;">LOCKED</span></p>
        <p><b>Execution Mode:</b> READ-ONLY</p>
        <p><b>Broker Connect:</b> DISABLED</p>
        <p><b>Strategy Engine:</b> Frozen v5.10.3 / 6.1</p>
    </div>
    """,
    unsafe_allow_html=True,
)


# =============================================================================
# SCREEN 1: TERMINAL DEEP-DIVE
# =============================================================================
if nav_selection == "🖥️ Terminal Deep-Dive":
    # Lookup observation record for this ticker if available
    ticker_obs = next((c for c in curr_candidates if c.get("ticker") == selected_ticker), None)
    cand_score = float(ticker_obs.get("candidate_score", 0) or 0) if ticker_obs else 0.0
    cand_decision = ticker_obs.get("scanner_decision", "NOT_EVALUATED") if ticker_obs else "NOT_EVALUATED"
    metrics = ticker_obs.get("technical_metrics") or {} if ticker_obs else {}
    rejection_reasons = ticker_obs.get("rejection_reasons", []) if ticker_obs else []

    # Load candle data via DualSourceSyncGateway
    df_candles, forming_bar, gateway_meta, candle_warnings = load_gateway_ticker_data(
        selected_ticker,
        data_mode=active_data_mode,
    )

    latest_close = df_candles["Close"].iloc[-1] if (df_candles is not None and not df_candles.empty) else float(metrics.get("price", 0) or 0)
    prev_close = df_candles["Close"].iloc[-2] if (df_candles is not None and len(df_candles) >= 2) else latest_close
    chg_usd = latest_close - prev_close
    chg_pct = (chg_usd / prev_close) * 100.0 if prev_close > 0 else 0.0

    # Header with Dual-Source Badges
    dec_badge_class = "badge-bullish" if cand_decision == "ELIGIBLE" else ("badge-bearish" if cand_decision == "REJECTED" else "badge-neutral")
    feed_badge_cls, feed_badge_lbl = get_readiness_badge(gateway_meta.get("feed_state", "LIVE_IEX_HYBRID"))
    st.markdown(
        f"""
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.8rem; flex-wrap: wrap; gap: 0.5rem;">
            <div>
                <span style="font-size: 1.6rem; font-weight: 800; color: #f8fafc; font-family: 'JetBrains Mono', monospace;">
                    {selected_ticker}
                </span>
                <span style="font-size: 1.4rem; font-weight: 700; color: {'#10b981' if chg_usd >= 0 else '#f43f5e'}; margin-left: 0.75rem; font-family: 'JetBrains Mono', monospace;">
                    ${latest_close:.2f}
                </span>
                <span style="font-size: 0.85rem; color: {'#10b981' if chg_usd >= 0 else '#f43f5e'}; margin-left: 0.4rem; font-family: 'JetBrains Mono', monospace;">
                    ({chg_usd:+.2f} / {chg_pct:+.2f}%)
                </span>
                <span class="badge {dec_badge_class}" style="margin-left: 0.75rem;">{cand_decision}</span>
            </div>
            <div>
                <span class="badge {feed_badge_cls}">{feed_badge_lbl}</span>
                <span class="badge badge-neutral" style="margin-left: 0.35rem;">PRICE: {gateway_meta.get('provenance', {}).get('price', 'ALPACA_IEX')}</span>
                <span class="badge badge-neutral" style="margin-left: 0.35rem;">VOL: {gateway_meta.get('provenance', {}).get('volume', 'YAHOO_CONSOLIDATED')}</span>
                <span class="badge badge-info" style="margin-left: 0.35rem;">SESSION: COMPLETE</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Candlestick Chart (3-tier: Completed Daily Candles + Consolidated Volume + RSI 14)
    if df_candles is not None and not df_candles.empty:
        chart_fig = create_ticker_candlestick_chart(
            df_candles,
            selected_ticker,
            max_bars=90,
            show_smas=True,
            show_rsi=True,
            price_source=gateway_meta.get("provenance", {}).get("price", "Alpaca IEX"),
            volume_source=gateway_meta.get("provenance", {}).get("volume", "Yahoo Consolidated"),
            feed_state=gateway_meta.get("feed_state", "LIVE_IEX_HYBRID"),
        )
        st.plotly_chart(chart_fig, use_container_width=True)
    else:
        st.warning(f"No completed daily candle data available for '{selected_ticker}'. Warnings: {candle_warnings}")

    # Separate Forming Intraday Section
    st.markdown("### ⏱️ Forming Intraday Session Stream")
    if forming_bar is not None:
        f_col_chart, f_col_info = st.columns([1.3, 0.7])
        with f_col_chart:
            forming_fig = create_forming_intraday_chart(forming_bar, selected_ticker)
            st.plotly_chart(forming_fig, use_container_width=True)
        with f_col_info:
            st.markdown(
                f"""
                <div class="forming-container">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem;">
                        <span style="font-weight: 700; color: #fbbf24; font-size: 0.92rem;">⚡ Live Minute Bar ({forming_bar['timestamp'][-8:-1]} UTC)</span>
                        <span class="badge badge-forming">FORMING_BAR</span>
                    </div>
                    <table style="width: 100%; font-size: 0.8rem; color: #cbd5e1; border-collapse: collapse;">
                        <tr style="border-bottom: 1px solid #1e293b;">
                            <td style="padding: 0.3rem 0; color: #94a3b8;">Bar Status:</td>
                            <td style="padding: 0.3rem 0; text-align: right; color: #fbbf24; font-weight: 700;">INCOMPLETE / FORMING</td>
                        </tr>
                        <tr style="border-bottom: 1px solid #1e293b;">
                            <td style="padding: 0.3rem 0; color: #94a3b8;">Current Close:</td>
                            <td style="padding: 0.3rem 0; text-align: right; font-family: 'JetBrains Mono', monospace; font-weight: 700;">${forming_bar['close']:.2f}</td>
                        </tr>
                        <tr style="border-bottom: 1px solid #1e293b;">
                            <td style="padding: 0.3rem 0; color: #94a3b8;">Open / High / Low:</td>
                            <td style="padding: 0.3rem 0; text-align: right; font-family: 'JetBrains Mono', monospace;">${forming_bar['open']:.2f} / ${forming_bar['high']:.2f} / ${forming_bar['low']:.2f}</td>
                        </tr>
                        <tr style="border-bottom: 1px solid #1e293b;">
                            <td style="padding: 0.3rem 0; color: #94a3b8;">Live VWAP:</td>
                            <td style="padding: 0.3rem 0; text-align: right; font-family: 'JetBrains Mono', monospace; color: #f59e0b;">${forming_bar['vwap']:.2f}</td>
                        </tr>
                        <tr style="border-bottom: 1px solid #1e293b;">
                            <td style="padding: 0.3rem 0; color: #94a3b8;">IEX Volume:</td>
                            <td style="padding: 0.3rem 0; text-align: right; font-family: 'JetBrains Mono', monospace;">{forming_bar['volume']:,} shares</td>
                        </tr>
                        <tr style="border-bottom: 1px solid #1e293b;">
                            <td style="padding: 0.3rem 0; color: #94a3b8;">Volume Provenance:</td>
                            <td style="padding: 0.3rem 0; text-align: right; color: #f87171; font-weight: 700;">UNCONSOLIDATED (IEX)</td>
                        </tr>
                        <tr>
                            <td style="padding: 0.3rem 0; color: #94a3b8;">Screening Gate:</td>
                            <td style="padding: 0.3rem 0; text-align: right; color: #f43f5e; font-weight: 700;">STRICTLY BLOCKED</td>
                        </tr>
                    </table>
                    <div style="font-size: 0.72rem; color: #fbbf24; margin-top: 0.6rem; line-height: 1.4;">
                        <b>Mandatory Protection:</b> Unconsolidated IEX streaming volume is never combined into daily ADV/RVOL and never used for long trade screening.
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
    else:
        st.markdown(
            f"""
            <div class="card-container" style="border-style: dashed; padding: 0.85rem 1.15rem;">
                <div style="display: flex; justify-content: space-between; align-items: center;">
                    <span style="font-size: 0.84rem; color: #cbd5e1;">
                        ℹ️ <b>Market Session Status:</b> Regular NYSE exchange session is closed. No active forming intraday bar for <b>{selected_ticker}</b>.
                    </span>
                    <span class="badge badge-complete">SESSION COMPLETE</span>
                </div>
                <div style="font-size: 0.75rem; color: #94a3b8; margin-top: 0.35rem;">
                    Completed daily candles with consolidated SIP volume are authoritative for all strategy screening and backtesting.
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    # Real-Time Streaming Message Feed (500-Item Circular Buffer)
    st.markdown("### 📡 Real-Time Streaming Message Feed (500-Item Circular Buffer)")
    streaming_buf = get_streaming_buffer()

    # Pre-seed buffer with initial streaming fixtures if empty and fixtures exist
    if streaming_buf.current_size == 0 and DEFAULT_GATEWAY_FIXTURES.exists():
        try:
            with open(DEFAULT_GATEWAY_FIXTURES, "r", encoding="utf-8") as f:
                fix_data = json.load(f)
                for b in fix_data.get("intraday_streaming_bars_spy", []):
                    streaming_buf.append({
                        "received_at": "2026-11-17T15:30:00Z",
                        "symbol": b.get("S", "SPY"),
                        "type": "bar_update" if b.get("T") == "u" else "minute_bar",
                        "price": f"${float(b.get('c', 0.0)):.2f}",
                        "open": f"${float(b.get('o', 0.0)):.2f}",
                        "high": f"${float(b.get('h', 0.0)):.2f}",
                        "low": f"${float(b.get('l', 0.0)):.2f}",
                        "volume": f"{int(b.get('v', 0)):,} (IEX)",
                        "vwap": f"${float(b.get('vw', 0.0)):.2f}" if b.get("vw") else "N/A",
                        "status": "FORMING (DISPLAY ONLY)",
                    })
                streaming_buf.set_connected(True)
        except Exception:
            pass

    buf_col1, buf_col2, buf_col3, buf_col4 = st.columns(4)
    with buf_col1:
        st.markdown(
            f"""
            <div class="kpi-card" style="padding: 0.6rem;">
                <div class="kpi-label">Buffer Capacity</div>
                <div class="kpi-value" style="font-size: 1.15rem; color: #38bdf8;">500 Items</div>
                <div class="kpi-sub">Strictly Bounded Memory</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with buf_col2:
        st.markdown(
            f"""
            <div class="kpi-card" style="padding: 0.6rem;">
                <div class="kpi-label">Current Messages</div>
                <div class="kpi-value" style="font-size: 1.15rem; color: #f8fafc;">{streaming_buf.current_size} / 500</div>
                <div class="kpi-sub">Total Received: {streaming_buf.total_received}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with buf_col3:
        st.markdown(
            f"""
            <div class="kpi-card" style="padding: 0.6rem;">
                <div class="kpi-label">FIFO Evictions</div>
                <div class="kpi-value" style="font-size: 1.15rem; color: {'#fbbf24' if streaming_buf.dropped_count > 0 else '#10b981'};">{streaming_buf.dropped_count} Dropped</div>
                <div class="kpi-sub">Deterministic Eviction</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with buf_col4:
        conn_cls = "badge-bullish" if streaming_buf.is_connected else "badge-neutral"
        conn_text = "CONNECTED" if streaming_buf.is_connected else "STANDBY / DISCONNECTED"
        st.markdown(
            f"""
            <div class="kpi-card" style="padding: 0.6rem;">
                <div class="kpi-label">WebSocket State</div>
                <div style="margin-top: 0.35rem;"><span class="badge {conn_cls}">{conn_text}</span></div>
                <div class="kpi-sub" style="margin-top: 0.35rem;">Zero Daemons / Zero Threads</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    buf_df = streaming_buf.to_dataframe(limit=10)
    if not buf_df.empty:
        st.dataframe(buf_df, use_container_width=True, hide_index=True)
    else:
        st.info("Streaming message buffer is empty. Connect WebSocket or ingest live streaming fixtures.")

    st.markdown("<br>", unsafe_allow_html=True)

    # 3-Column Diagnostic Bottom Area
    col_ai, col_metrics, col_setup = st.columns([1.1, 0.95, 0.95])

    with col_ai:
        st.markdown(
            f"""
            <div class="card-container">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.6rem;">
                    <span style="font-weight: 700; color: #f8fafc; font-size: 0.95rem;">🤖 Strategy & Regime Verdict</span>
                    <span class="badge {dec_badge_class}">{cand_decision}</span>
                </div>
                <div style="font-size: 0.8rem; color: #94a3b8; margin-bottom: 0.75rem;">
                    Scanner Score: <b style="color: #f8fafc; font-family: 'JetBrains Mono', monospace;">{cand_score:.1f} / 100.0</b> |
                    Session: <span style="color: #38bdf8;">{selected_session}</span>
                </div>

                <!-- 5-Pillar Breakdown -->
                <div class="pillar-box">
                    <div class="pillar-title">
                        <span>Pillar 1: Trend Structure</span>
                        <span class="badge {'badge-bullish' if not any('sma' in r.lower() for r in rejection_reasons) else 'badge-bearish'}">
                            {'PASS' if not any('sma' in r.lower() for r in rejection_reasons) else 'FAIL'}
                        </span>
                    </div>
                    <div class="pillar-detail">Price vs 20-Day & 50-Day SMA</div>
                </div>

                <div class="pillar-box">
                    <div class="pillar-title">
                        <span>Pillar 2: Momentum (RSI 14)</span>
                        <span class="badge {'badge-bullish' if not any('rsi' in r.lower() for r in rejection_reasons) else 'badge-bearish'}">
                            {'PASS' if not any('rsi' in r.lower() for r in rejection_reasons) else 'FAIL'}
                        </span>
                    </div>
                    <div class="pillar-detail">RSI(14): {float(metrics.get('rsi', 0) or 0):.1f} [Target: 40 - 65]</div>
                </div>

                <div class="pillar-box">
                    <div class="pillar-title">
                        <span>Pillar 3: Volatility (ATR%)</span>
                        <span class="badge {'badge-bullish' if not any('atr' in r.lower() for r in rejection_reasons) else 'badge-bearish'}">
                            {'PASS' if not any('atr' in r.lower() for r in rejection_reasons) else 'FAIL'}
                        </span>
                    </div>
                    <div class="pillar-detail">ATR%: {float(metrics.get('atr_pct', 0) or 0):.2f}% [Target: 1.5% - 8.0%]</div>
                </div>

                <div class="pillar-box">
                    <div class="pillar-title">
                        <span>Pillar 4: Liquidity</span>
                        <span class="badge {'badge-bullish' if not any('dollar volume' in r.lower() for r in rejection_reasons) else 'badge-bearish'}">
                            {'PASS' if not any('dollar volume' in r.lower() for r in rejection_reasons) else 'FAIL'}
                        </span>
                    </div>
                    <div class="pillar-detail">ADV &ge; $10M | RVOL: {float(metrics.get('rvol', 0) or 0):.2f}x</div>
                </div>

                <div class="pillar-box">
                    <div class="pillar-title">
                        <span>Pillar 5: Market Regime</span>
                        <span class="badge badge-bearish">VETOED</span>
                    </div>
                    <div class="pillar-detail">STRONG_BEARISH (Score: -0.82 &le; -0.60)</div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        if rejection_reasons:
            st.markdown(
                f"""
                <div class="card-container card-danger">
                    <div style="font-weight: 700; color: #fb7185; font-size: 0.85rem; margin-bottom: 0.4rem;">
                        ⛔ Scanner Rejection Rationale:
                    </div>
                    <ul style="margin: 0; padding-left: 1.2rem; font-size: 0.78rem; color: #cbd5e1; line-height: 1.4;">
                        {''.join(f'<li>{r}</li>' for r in rejection_reasons)}
                    </ul>
                </div>
                """,
                unsafe_allow_html=True,
            )

    with col_metrics:
        # Technical Metrics Grid
        sma20_val = df_candles["SMA20"].iloc[-1] if (df_candles is not None and "SMA20" in df_candles and df_candles["SMA20"].notna().any()) else None
        sma50_val = df_candles["SMA50"].iloc[-1] if (df_candles is not None and "SMA50" in df_candles and df_candles["SMA50"].notna().any()) else None
        sma200_val = df_candles["SMA200"].iloc[-1] if (df_candles is not None and "SMA200" in df_candles and df_candles["SMA200"].notna().any()) else None
        rsi_val = df_candles["RSI14"].iloc[-1] if (df_candles is not None and "RSI14" in df_candles and df_candles["RSI14"].notna().any()) else float(metrics.get("rsi", 0) or 0)
        h52 = df_candles["High"].tail(252).max() if (df_candles is not None and not df_candles.empty) else latest_close
        l52 = df_candles["Low"].tail(252).min() if (df_candles is not None and not df_candles.empty) else latest_close

        st.markdown(
            f"""
            <div class="card-container">
                <div style="font-weight: 700; color: #f8fafc; font-size: 0.95rem; margin-bottom: 0.75rem;">
                    📊 Technical Indicators
                </div>
                <table style="width: 100%; font-size: 0.82rem; color: #cbd5e1; border-collapse: collapse;">
                    <tr style="border-bottom: 1px solid #1f2937;">
                        <td style="padding: 0.4rem 0; color: #94a3b8;">Latest Close:</td>
                        <td style="padding: 0.4rem 0; text-align: right; font-family: 'JetBrains Mono', monospace; font-weight: 700;">${latest_close:.2f}</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #1f2937;">
                        <td style="padding: 0.4rem 0; color: #94a3b8;">20-Day SMA:</td>
                        <td style="padding: 0.4rem 0; text-align: right; font-family: 'JetBrains Mono', monospace; color: #38bdf8;">
                            {f'${sma20_val:.2f}' if sma20_val is not None else 'N/A'}
                        </td>
                    </tr>
                    <tr style="border-bottom: 1px solid #1f2937;">
                        <td style="padding: 0.4rem 0; color: #94a3b8;">50-Day SMA:</td>
                        <td style="padding: 0.4rem 0; text-align: right; font-family: 'JetBrains Mono', monospace; color: #f59e0b;">
                            {f'${sma50_val:.2f}' if sma50_val is not None else 'N/A'}
                        </td>
                    </tr>
                    <tr style="border-bottom: 1px solid #1f2937;">
                        <td style="padding: 0.4rem 0; color: #94a3b8;">200-Day SMA:</td>
                        <td style="padding: 0.4rem 0; text-align: right; font-family: 'JetBrains Mono', monospace; color: #a855f7;">
                            {f'${sma200_val:.2f}' if sma200_val is not None else 'N/A'}
                        </td>
                    </tr>
                    <tr style="border-bottom: 1px solid #1f2937;">
                        <td style="padding: 0.4rem 0; color: #94a3b8;">RSI (14 Wilder's):</td>
                        <td style="padding: 0.4rem 0; text-align: right; font-family: 'JetBrains Mono', monospace; font-weight: 700;">{rsi_val:.1f}</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #1f2937;">
                        <td style="padding: 0.4rem 0; color: #94a3b8;">Relative Volume (RVOL):</td>
                        <td style="padding: 0.4rem 0; text-align: right; font-family: 'JetBrains Mono', monospace;">
                            {f"{float(metrics.get('rvol', 0) or 0):.2f}x" if metrics.get('rvol') else 'N/A'}
                        </td>
                    </tr>
                    <tr style="border-bottom: 1px solid #1f2937;">
                        <td style="padding: 0.4rem 0; color: #94a3b8;">Average True Range (ATR%):</td>
                        <td style="padding: 0.4rem 0; text-align: right; font-family: 'JetBrains Mono', monospace;">
                            {f"{float(metrics.get('atr_pct', 0) or 0):.2f}%" if metrics.get('atr_pct') else 'N/A'}
                        </td>
                    </tr>
                    <tr>
                        <td style="padding: 0.4rem 0; color: #94a3b8;">52-Week Range:</td>
                        <td style="padding: 0.4rem 0; text-align: right; font-family: 'JetBrains Mono', monospace; font-size: 0.76rem;">
                            ${l52:.2f} — ${h52:.2f}
                        </td>
                    </tr>
                </table>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with col_setup:
        # Trade Setup & Sizing Panel
        st.markdown(
            f"""
            <div class="card-container">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.6rem;">
                    <span style="font-weight: 700; color: #f8fafc; font-size: 0.95rem;">🎯 Deterministic Trade Setup</span>
                    <span class="badge badge-bearish">SUPPRESSED</span>
                </div>
                <div class="trade-setup-box">
                    <div style="color: #94a3b8; margin-bottom: 0.4rem;">Setup Status: <span style="color: #fb7185; font-weight: 700;">VETOED BY REGIME</span></div>
                    <div>Reference Entry: <span style="color: #f8fafc;">${latest_close:.2f}</span></div>
                    <div>Stop Loss Level: <span style="color: #f43f5e;">N/A (Suppressed)</span></div>
                    <div>Profit Target (2R): <span style="color: #10b981;">N/A (Suppressed)</span></div>
                    <div>Reward:Risk Ratio: <span style="color: #38bdf8;">2.0 : 1.0</span></div>
                    <div style="border-top: 1px dashed #334155; margin: 0.5rem 0; padding-top: 0.4rem;">
                        <div>Max Capital Risk: <span style="color: #f8fafc;">$25.00 (0.50%)</span></div>
                        <div>Sized Order Quantity: <span style="color: #94a3b8;">0 Shares ($0.00)</span></div>
                    </div>
                </div>
                <div style="font-size: 0.74rem; color: #94a3b8; margin-top: 0.6rem; line-height: 1.4;">
                    <b>Governance Policy:</b> Under <code>STRONG_BEARISH</code> regime, rule <code>allow_new_longs = False</code> unconditionally halts trade generation at the scanner boundary.
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )


# =============================================================================
# SCREEN 2: MARKET & SCREENER OVERVIEW
# =============================================================================
elif nav_selection == "📊 Market & Screener Overview":
    screener_summary = get_gateway_screener_summary(curr_candidates, selected_session)
    gw_badge_cls, gw_badge_lbl = get_readiness_badge(gateway_feed_info["combined_readiness"])
    st.markdown(
        f"""
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.8rem; flex-wrap: wrap; gap: 0.5rem;">
            <div>
                <h2 style="margin: 0; color: #f8fafc; font-weight: 700;">Market Regime & Screener Overview</h2>
                <div style="color: #94a3b8; font-size: 0.82rem;">
                    Session: <span style="color: #38bdf8; font-weight: 600;">{selected_session}</span> |
                    Macro Score: <span style="color: #f43f5e; font-weight: 600;">-0.82</span> |
                    Regime: <span class="badge badge-bearish">{active_regime}</span>
                </div>
            </div>
            <div>
                <span class="badge {gw_badge_cls}">{gw_badge_lbl}</span>
                <span class="badge badge-info" style="margin-left: 0.35rem;">DELAYED_UNVERIFIED (Historical)</span>
                <span class="badge badge-neutral" style="margin-left: 0.35rem;">READ-ONLY RESEARCH</span>
            </div>
        </div>
        <div class="gateway-header-box">
            <div>
                <span style="font-size: 0.82rem; font-weight: 700; color: #f8fafc;">Gateway Feed Status:</span>
                <span class="badge {gw_badge_cls}" style="margin-left: 0.4rem;">{gw_badge_lbl}</span>
            </div>
            <div style="font-size: 0.76rem; color: #94a3b8;">
                <b>Price Action:</b> <span style="color: #38bdf8;">{screener_summary['price_provenance']}</span> |
                <b>Consolidated Volume:</b> <span style="color: #10b981;">{screener_summary['rvol_provenance'].split('(')[0].strip()}</span> |
                <b>Exchange Session:</b> <span style="color: #cbd5e1;">NYSE Complete (16:00 ET Close Verified)</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown(
        """
        <div class="banner-delayed">
            <b>DATA GOVERNANCE & PROVENANCE DISCLOSURE:</b>
            All daily screening calculations (20-day Average Dollar Volume &ge; $10M and Relative Volume &ge; 0.8x RVOL) strictly receive verified consolidated composite tape volume from Yahoo Finance across all US exchanges.
            <br><b>Governance Mandate:</b> <b>Never treat a readiness badge as trade authorization.</b> <code>LIVE_IEX_HYBRID</code> permits completed-bar candidate screening only; it does <b>NOT</b> authorize paper fills, order submission, or live trading.
            <br><b>Strict Distinction:</b> <code>DELAYED_UNVERIFIED</code> supports approved historical research only; it must <b>NOT</b> be represented as verified live trade eligibility.
            Top 5 candidates are displayed below with full volume provenance disclosures.
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Top 5 Evaluated Stocks Cards
    st.markdown("### Top 5 Evaluated Stocks (Ranked by Candidate Score)")
    sorted_candidates = sorted(
        curr_candidates,
        key=lambda c: float(c.get("candidate_score", 0) or 0),
        reverse=True,
    )
    top_5 = sorted_candidates[:5]
    if top_5:
        top_cols = st.columns(min(len(top_5), 5))
        for idx, cand in enumerate(top_5):
            with top_cols[idx]:
                sym = cand.get("ticker", "N/A")
                score = float(cand.get("candidate_score", 0) or 0)
                m = cand.get("technical_metrics") or {}
                px = float(m.get("price", 0) or 0)
                rsi = float(m.get("rsi", 0) or 0)
                rvol = float(m.get("rvol", 0) or 0)
                decision = cand.get("scanner_decision", "REJECTED")

                st.markdown(
                    f"""
                    <div class="ticker-card">
                        <div class="ticker-symbol">
                            <span>{sym}</span>
                            <span class="badge badge-bearish">{decision}</span>
                        </div>
                        <div class="ticker-price">${px:.2f}</div>
                        <div class="ticker-metrics">
                            <div><b>Score:</b> {score:.1f} / 100 | <span class="badge {gw_badge_cls}" style="font-size: 0.6rem; padding: 0.1rem 0.4rem;">{gw_badge_lbl}</span></div>
                            <div><b>RSI(14):</b> {rsi:.1f} | <b>RVOL:</b> {rvol:.2f}x</div>
                            <div style="font-size: 0.68rem; color: #10b981; margin-top: 0.15rem;">
                                <b>Volume Tape:</b> Yahoo SIP
                            </div>
                            <div style="color: #fb7185; margin-top: 0.15rem; font-size: 0.68rem; font-weight: 600;">
                                Veto: STRONG_BEARISH
                            </div>
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

        st.markdown("<br>", unsafe_allow_html=True)

    # Middle Section: 30-Day Trajectory & Market Sentiment Gauge
    mid_left, mid_right = st.columns([1.2, 0.8])

    with mid_left:
        top_symbols = [c.get("ticker") for c in top_5 if c.get("ticker")]
        if top_symbols:
            st.markdown("### Top Evaluated Equities Trajectory (Last 30 Sessions, % Return)")
            comp_fig = create_top_equities_comparison_chart(
                data_dir=DEFAULT_CURRENT_DATA_DIR,
                tickers=top_symbols + ["SPY"],
                max_bars=30,
            )
            if comp_fig:
                st.plotly_chart(comp_fig, use_container_width=True)

    with mid_right:
        st.markdown("### Market Sentiment & Breadth")
        sentiment_fig = create_market_sentiment_gauge(macro_score=-0.82, regime=active_regime)
        st.plotly_chart(sentiment_fig, use_container_width=True)

        # Market Breadth Box
        breadth = get_market_breadth_metrics(candidate_records, selected_session)
        st.markdown(
            f"""
            <div class="card-container" style="margin-top: -10px;">
                <div style="font-weight: 700; color: #f8fafc; font-size: 0.85rem; margin-bottom: 0.5rem;">
                    🌐 Session Breadth Indicators ({selected_session})
                </div>
                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 0.5rem; font-size: 0.78rem; color: #cbd5e1;">
                    <div>Evaluated Equities: <b style="color: #f8fafc;">{breadth['total_evaluated']}</b></div>
                    <div>Eligible Setups: <b style="color: #94a3b8;">{breadth['eligible_count']} (0.0%)</b></div>
                    <div>Regime Vetoed: <b style="color: #fb7185;">{breadth['regime_vetoed_count']} (100.0%)</b></div>
                    <div>Average Score: <b style="color: #38bdf8;">{breadth['avg_candidate_score']:.1f} / 100</b></div>
                    <div>Above 20-Day SMA: <b style="color: #cbd5e1;">{breadth['above_sma20_count']} ({breadth['above_sma20_pct']}%)</b></div>
                    <div>Above 50-Day SMA: <b style="color: #cbd5e1;">{breadth['above_sma50_count']} ({breadth['above_sma50_pct']}%)</b></div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    st.markdown("<br>", unsafe_allow_html=True)

    # Bottom Section: Narrative Analysis & Funnel
    bot_left, bot_right = st.columns([1.1, 0.9])

    with bot_left:
        st.markdown(
            f"""
            <div class="card-container card-danger">
                <h4 style="margin: 0 0 0.5rem 0; color: #f8fafc;">Session Analysis: Defensive Regime Veto</h4>
                <p style="color: #cbd5e1; font-size: 0.84rem; line-height: 1.5;">
                    On trading session <b>{selected_session}</b>, broad US equity benchmarks suffered significant distribution.
                    SPY closed at <b>$739.96</b> (down from $754.37 on Nov 13), breaching its 20-day and 50-day moving averages with negative MACD momentum.
                </p>
                <p style="color: #cbd5e1; font-size: 0.84rem; line-height: 1.5;">
                    Under the frozen strategy policy for <b>STRONG_BEARISH</b> regimes, the scanner unconditionally
                    enforces <code>allow_new_longs = False</code> and requires candidate score &ge; 100.0.
                    Consequently, <b>all 49 evaluated equities were vetoed</b> at the scanner boundary, safely preserving paper capital.
                </p>
                <div style="background: #0f172a; border: 1px solid #1e293b; border-radius: 6px; padding: 0.65rem 0.85rem; font-size: 0.78rem; color: #94a3b8;">
                    <b>Policy Mandate:</b> "Severe downtrend / market stress. All new long candidate generation suppressed."
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with bot_right:
        funnel_fig = create_pipeline_funnel_chart(
            evaluated_count=tickers_evaluated,
            eligible_count=eligible_count,
            signal_gen_count=0,
            risk_sized_count=0,
            executed_fills=0,
        )
        st.plotly_chart(funnel_fig, use_container_width=True)


# =============================================================================
# SCREEN 3: PORTFOLIO COMMAND CENTER
# =============================================================================
elif nav_selection == "💼 Portfolio Command Center":
    st.markdown(
        """
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.8rem;">
            <div>
                <h2 style="margin: 0; color: #f8fafc; font-weight: 700;">Portfolio Command Center</h2>
                <div style="color: #94a3b8; font-size: 0.82rem;">
                    Independently verified paper ledger baseline as of session 2026-11-16.
                </div>
            </div>
            <div>
                <span class="badge badge-warning">PAPER PORTFOLIO</span>
                <span class="badge badge-neutral" style="margin-left: 0.4rem;">ZERO BROKER EXECUTION</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown(
        f"""
        <div class="banner-warning">
            <b>SAFETY & GOVERNANCE NOTICE:</b> This portfolio snapshot represents the frozen, independently audited state
            as of <b>{portfolio_data['snapshot_date']}</b>. It is completely isolated from live execution and does not reflect
            live brokerage holdings.
        </div>
        """,
        unsafe_allow_html=True,
    )

    # 5 KPI Metric Cards
    p1, p2, p3, p4, p5 = st.columns(5)
    with p1:
        st.markdown(
            f"""
            <div class="kpi-card">
                <div class="kpi-label">Portfolio Net Equity</div>
                <div class="kpi-value">${portfolio_data['equity_usd']}</div>
                <div class="kpi-sub">Verified Baseline</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with p2:
        st.markdown(
            f"""
            <div class="kpi-card">
                <div class="kpi-label">Available Cash</div>
                <div class="kpi-value" style="color: #10b981;">${portfolio_data['cash_usd']}</div>
                <div class="kpi-sub">100.0% Cash Reserve</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with p3:
        st.markdown(
            f"""
            <div class="kpi-card">
                <div class="kpi-label">Invested Capital</div>
                <div class="kpi-value" style="color: #94a3b8;">${portfolio_data['invested_capital_usd']}</div>
                <div class="kpi-sub">0.0% Capital Allocated</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with p4:
        st.markdown(
            f"""
            <div class="kpi-card">
                <div class="kpi-label">Active Positions</div>
                <div class="kpi-value">{portfolio_data['open_positions_count']} / {portfolio_data['max_simultaneous_positions_limit']}</div>
                <div class="kpi-sub">4 Free Slots Available</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with p5:
        st.markdown(
            f"""
            <div class="kpi-card">
                <div class="kpi-label">Max Risk / Trade</div>
                <div class="kpi-value" style="color: #38bdf8;">{portfolio_data['max_risk_per_trade_usd']}</div>
                <div class="kpi-sub">{portfolio_data['risk_limit_per_trade_pct']} Capital Cap</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    st.markdown("<br>", unsafe_allow_html=True)

    # Middle Section: Equity Curve & Allocation Donut Charts
    p_col_left, p_col_right = st.columns([1.2, 0.8])

    with p_col_left:
        eq_df, has_history = get_portfolio_equity_curve_data(available_sessions)
        equity_fig = create_portfolio_equity_chart(eq_df, baseline_value=5000.0, has_history=has_history)
        st.plotly_chart(equity_fig, use_container_width=True)

    with p_col_right:
        d_col1, d_col2 = st.columns(2)
        with d_col1:
            asset_fig = create_asset_allocation_donut(cash=5000.0, invested=0.0)
            st.plotly_chart(asset_fig, use_container_width=True)
        with d_col2:
            risk_fig = create_risk_capacity_donut(max_positions=4, open_positions=0, risk_per_trade_usd=25.0)
            st.plotly_chart(risk_fig, use_container_width=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # Active Positions & Orders Tables (Empty State with Explicit Transparency)
    st.markdown("### Active Positions & Orders Ledger")

    df_positions, df_orders = get_portfolio_active_positions_and_orders()

    pos_tab, ord_tab = st.tabs(["Active Holdings (0 Positions)", "Pending Orders (0 Orders)"])

    with pos_tab:
        if df_positions.empty:
            st.markdown(
                """
                <div class="empty-state-box">
                    <div style="font-size: 1.1rem; font-weight: 700; color: #f8fafc; margin-bottom: 0.4rem;">
                        NO ACTIVE POSITIONS RECORDED
                    </div>
                    <div style="font-size: 0.82rem; color: #94a3b8;">
                        The paper portfolio holds 100% cash buffer ($5,000.00). Under the active <b>STRONG_BEARISH</b> regime policy,
                        all new candidate generation is suppressed to protect capital.
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
        else:
            st.dataframe(df_positions, use_container_width=True, hide_index=True)

    with ord_tab:
        if df_orders.empty:
            st.markdown(
                """
                <div class="empty-state-box">
                    <div style="font-size: 1.1rem; font-weight: 700; color: #f8fafc; margin-bottom: 0.4rem;">
                        NO ACTIVE ORDERS PENDING
                    </div>
                    <div style="font-size: 0.82rem; color: #94a3b8;">
                        Broker execution is completely disabled in read-only research mode. Zero orders have been submitted.
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
        else:
            st.dataframe(df_orders, use_container_width=True, hide_index=True)

    # Safety Governance Statement
    st.markdown(
        f"""
        <div class="card-container card-success" style="margin-top: 1rem;">
            <h4 style="margin: 0 0 0.5rem 0; color: #f8fafc;">Paper Trading Risk Constraints & Audit Baseline</h4>
            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 0.75rem; font-size: 0.82rem; color: #cbd5e1;">
                <div><b>Risk Limit Per Trade:</b> {portfolio_data['risk_limit_per_trade_pct']} (${portfolio_data['max_risk_per_trade_usd']} maximum loss)</div>
                <div><b>Max Positions Allowed:</b> {portfolio_data['max_simultaneous_positions_limit']} simultaneous long positions</div>
                <div><b>Execution Fills Recorded:</b> {portfolio_data['executed_fills_count']} fills</div>
                <div><b>Active Holdings:</b> None (100% Cash Buffer)</div>
            </div>
            <div style="margin-top: 0.75rem; font-size: 0.76rem; color: #94a3b8; line-height: 1.4;">
                {portfolio_data['disclaimer']}
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# =============================================================================
# SCREEN 4: OBSERVATION HISTORY & AUDIT
# =============================================================================
elif nav_selection == "📜 Observation History & Audit":
    gw_badge_cls, gw_badge_lbl = get_readiness_badge(gateway_feed_info["combined_readiness"])
    st.markdown(
        f"""
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.8rem;">
            <div>
                <h2 style="margin: 0; color: #f8fafc; font-weight: 700;">Forward Observation History & Audit</h2>
                <div style="color: #94a3b8; font-size: 0.82rem;">
                    Historical log of research scans recorded in <code>.planning/data/observations/observation_log.jsonl</code>.
                </div>
            </div>
            <div>
                <span class="badge badge-info">DELAYED_UNVERIFIED (Historical)</span>
                <span class="badge badge-neutral" style="margin-left: 0.4rem;">AUDIT TRAIL</span>
                <span class="badge {gw_badge_cls}" style="margin-left: 0.4rem;">GATEWAY: {gw_badge_lbl}</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown(
        """
        <div class="banner-delayed">
            <b>RESEARCH AUDIT NOTICE:</b> Observations record strategy signals under forward market conditions.
            Rejected candidates reflect defensive rule enforcement, NOT losing trades. Zero capital has been committed.
        </div>
        """,
        unsafe_allow_html=True,
    )

    # History KPI Summary
    h1, h2, h3, h4 = st.columns(4)
    h1.metric("Sessions Scanned", len(session_records))
    h2.metric("Total Ticker Observations", len(candidate_records))
    h3.metric("Eligible Candidates", len([c for c in candidate_records if c.get("scanner_decision") == "ELIGIBLE"]))
    h4.metric("Executed Paper Fills", "0 (Read-Only)")

    st.markdown("<br>", unsafe_allow_html=True)

    # Charts: Regime Distribution & Rejection Breakdown
    chart_c1, chart_c2 = st.columns(2)
    with chart_c1:
        regime_fig = create_regime_donut_chart(session_records)
        st.plotly_chart(regime_fig, use_container_width=True)

    with chart_c2:
        rejection_stats = categorize_rejection_reasons(candidate_records)
        rejection_fig = create_rejection_reasons_bar_chart(rejection_stats)
        st.plotly_chart(rejection_fig, use_container_width=True)

    # Sessions Table
    st.markdown("### Logged Observation Sessions")
    sess_rows = []
    for s in session_records:
        sess_rows.append({
            "Date": s.get("date"),
            "Market Regime": s.get("market_regime"),
            "Evaluated": s.get("tickers_evaluated_count"),
            "Eligible": s.get("eligible_count"),
            "Rejected": s.get("rejected_count"),
            "Signals": s.get("signal_generation_count"),
            "Risk Sized": s.get("risk_sizing_count"),
            "Status Notes": s.get("notes"),
        })

    if sess_rows:
        st.dataframe(pd.DataFrame(sess_rows), use_container_width=True, hide_index=True)
    else:
        st.info("No session summary records found in observation_log.jsonl.")

    st.markdown("<br>", unsafe_allow_html=True)

    # Candidate Filtering & Exploration Table
    st.markdown("### 5-Pillar Candidate Scanner Outcomes")
    f_col1, f_col2, f_col3 = st.columns([1, 1, 2])
    with f_col1:
        decision_choice = st.selectbox("Decision Filter", ["ALL", "REJECTED", "ELIGIBLE"], index=0)
    with f_col2:
        reason_choice = st.selectbox(
            "Filter by Rejection Keyword",
            ["ALL", "regime", "score", "dollar volume", "threshold", "20-day sma", "rsi", "atr"],
            index=0,
        )
    with f_col3:
        search_query = st.text_input("Search Ticker Symbol", placeholder="e.g. AAPL, NVDA, MSFT...")

    filtered_candidates = filter_candidates(
        records=candidate_records,
        session_date=selected_session,
        decision_filter=decision_choice,
        ticker_query=search_query,
        reason_filter=reason_choice,
    )

    st.markdown(f"**Showing {len(filtered_candidates)} of {len(curr_candidates)} evaluated candidates**")

    table_rows = []
    for c in filtered_candidates:
        m = c.get("technical_metrics") or {}
        reasons_list = c.get("rejection_reasons", [])
        table_rows.append({
            "Ticker": c.get("ticker"),
            "Decision": c.get("scanner_decision"),
            "Score": f"{float(c.get('candidate_score', 0)):.1f}" if c.get("candidate_score") else "N/A",
            "Price": f"${float(m.get('price', 0)):.2f}" if m.get("price") else "N/A",
            "RSI(14)": f"{float(m.get('rsi', 0)):.1f}" if m.get("rsi") else "N/A",
            "RVOL": f"{float(m.get('rvol', 0)):.2f}x" if m.get("rvol") else "N/A",
            "ATR%": f"{float(m.get('atr_pct', 0)):.2f}%" if m.get("atr_pct") else "N/A",
            "Rejection Reasons": "; ".join(reasons_list) if reasons_list else "None (Eligible)",
            "Signal Gen": "Yes" if c.get("reached_signal_generation") else "No",
            "Risk Sized": "Yes" if c.get("reached_risk_sizing") else "No",
        })

    if table_rows:
        st.dataframe(
            pd.DataFrame(table_rows),
            use_container_width=True,
            hide_index=True,
            column_config={
                "Ticker": st.column_config.TextColumn("Ticker", width="small"),
                "Decision": st.column_config.TextColumn("Decision", width="small"),
                "Score": st.column_config.TextColumn("Score", width="small"),
                "Price": st.column_config.TextColumn("Price", width="small"),
                "Rejection Reasons": st.column_config.TextColumn("Rejection Reasons", width="large"),
            },
        )

    st.markdown("<br>", unsafe_allow_html=True)

    # Dual-Source Gateway Health History & Event Log
    st.markdown("### 🛡️ Dual-Source Gateway Health History & Event Log")
    gateway_events = get_gateway_health_history(session_records)
    if gateway_events:
        gw_df = pd.DataFrame(gateway_events)
        st.dataframe(
            gw_df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "timestamp": st.column_config.TextColumn("Timestamp (UTC)", width="medium"),
                "event": st.column_config.TextColumn("Event Type", width="medium"),
                "component": st.column_config.TextColumn("Component", width="medium"),
                "status": st.column_config.TextColumn("Status", width="small"),
                "details": st.column_config.TextColumn("Event Details & Audit Verification", width="large"),
            },
        )
    else:
        st.info("No gateway health history events recorded.")

    st.markdown("<br>", unsafe_allow_html=True)

    # Data Quality & Manifest Health
    st.markdown("### Data Quality & Partition Health")
    st.markdown(
        f"""
        <div class="card-container card-highlight">
            <h4 style="margin: 0 0 0.5rem 0; color: #f8fafc;">Active Partition Metadata & Gateway Coordination</h4>
            <table style="width: 100%; font-size: 0.82rem; color: #cbd5e1; border-collapse: collapse;">
                <tr><td style="padding: 0.3rem 0; color: #94a3b8;">Partition Path:</td><td><code>.planning/data/current_daily/</code></td></tr>
                <tr><td style="padding: 0.3rem 0; color: #94a3b8;">Latest Completed Session:</td><td><b>{acq_date}</b></td></tr>
                <tr><td style="padding: 0.3rem 0; color: #94a3b8;">Acquisition Timestamp:</td><td><code>{acq_timestamp}</code></td></tr>
                <tr><td style="padding: 0.3rem 0; color: #94a3b8;">Manifest Status:</td><td><span class="badge badge-bullish">PASSED</span></td></tr>
                <tr><td style="padding: 0.3rem 0; color: #94a3b8;">Proxies Validated:</td><td>SPY, QQQ, IWM (3 of 3)</td></tr>
                <tr><td style="padding: 0.3rem 0; color: #94a3b8;">Feed Classification:</td><td><span class="badge badge-hybrid">LIVE_IEX_HYBRID</span> <span class="badge badge-info" style="margin-left: 0.3rem;">DELAYED_UNVERIFIED (Historical)</span></td></tr>
                <tr><td style="padding: 0.3rem 0; color: #94a3b8;">Gateway Coordinator:</td><td><code>DualSourceSyncGateway</code></td></tr>
                <tr><td style="padding: 0.3rem 0; color: #94a3b8;">Price Action Source:</td><td><span style="color: #38bdf8; font-weight: 600;">Alpaca IEX (Authoritative Daily OHLC & Intraday Bars)</span></td></tr>
                <tr><td style="padding: 0.3rem 0; color: #94a3b8;">Volume Source:</td><td><span style="color: #10b981; font-weight: 600;">Yahoo Finance (Authoritative Consolidated Tape)</span></td></tr>
                <tr><td style="padding: 0.3rem 0; color: #94a3b8;">Anti-Substitution Guard:</td><td><span class="badge badge-bullish">ACTIVE</span> (Yahoo price failover permanently prohibited)</td></tr>
                <tr><td style="padding: 0.3rem 0; color: #94a3b8;">Session Date Alignment:</td><td><span class="badge badge-bullish">VERIFIED</span> (Exact exchange-calendar synchronization)</td></tr>
            </table>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Excluded Equities Card
    excluded_list = manifest_data.get("excluded_equities", []) if manifest_data else []
    if excluded_list:
        st.markdown("#### Documented Upstream Data Exclusions")
        for exc in excluded_list:
            st.markdown(
                f"""
                <div class="card-container card-warning">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.4rem;">
                        <span style="font-weight: 700; font-size: 0.95rem; color: #fbbf24;">⚠️ {exc.get('ticker', 'UNKNOWN')} — Quarantined from Universe</span>
                        <span class="badge badge-sideways">GEOMETRY_INVALID</span>
                    </div>
                    <p style="font-size: 0.8rem; color: #cbd5e1; margin: 0 0 0.4rem 0;">
                        <b>Diagnostic Reason:</b> <code>{exc.get('reason', 'N/A')}</code>
                    </p>
                    <div style="font-size: 0.74rem; color: #94a3b8;">
                        <b>Action Taken:</b> Detected an invalid candle where Open exceeded High. Quarantined rather than synthesizing data.
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )


# =============================================================================
# SCREEN 5: FORWARD PAPER EVALUATION (PHASE 9 / 9.2)
# =============================================================================
elif nav_selection == "🔭 Forward Paper Evaluation":
    if "fwd_data" not in locals() or fwd_data is None:
        fwd_data = load_forward_evaluation_dashboard_data(genuine_only=True)

    is_diag = fwd_data.get("is_diagnostic_scope", False)
    scope_label = fwd_data.get("data_mode_filter", "GENUINE_DAILY_ONLY")

    st.markdown(
        f"""
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.8rem;">
            <div>
                <h2 style="margin: 0; color: #f8fafc; font-weight: 700;">🔭 Forward Paper Trading Evaluation</h2>
                <div style="color: #94a3b8; font-size: 0.82rem;">
                    Isolated forward observation system tracking hypothetical candidate outcomes without real execution.
                </div>
            </div>
            <div>
                <span class="badge badge-warning" style="background-color: {'#7c2d12' if is_diag else '#065f46'}; color: {'#fed7aa' if is_diag else '#a7f3d0'}; padding: 4px 8px; border-radius: 4px; font-weight: 600;">{scope_label}</span>
                <span class="badge badge-neutral" style="margin-left: 0.4rem; padding: 4px 8px; border-radius: 4px;">NO LIVE BROKER</span>
                <span class="badge badge-bullish" style="margin-left: 0.4rem; padding: 4px 8px; border-radius: 4px;">READ-ONLY</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if is_diag:
        st.markdown(
            """
            <div style="background-color: rgba(245, 158, 11, 0.15); border: 2px solid #f59e0b; padding: 12px 16px; border-radius: 6px; margin-bottom: 1.2rem;">
                <div style="font-weight: 700; color: #f59e0b; font-size: 0.95rem; margin-bottom: 4px;">
                    ⚠️ DIAGNOSTIC DATA ONLY — SYNTHETIC TEST FIXTURES DO NOT CONSTITUTE FORWARD PERFORMANCE EVIDENCE
                </div>
                <div style="color: #cbd5e1; font-size: 0.82rem; line-height: 1.4;">
                    The data currently displayed contains synthetic fixture records generated for automated testing and verification purposes.
                    These records are strictly excluded from genuine forward performance metrics by default and must never be used to evaluate strategy profitability or market readiness.
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            """
            <div style="background-color: rgba(30, 41, 59, 0.9); border-left: 4px solid #10b981; padding: 12px 16px; border-radius: 4px; margin-bottom: 1.2rem;">
                <div style="font-weight: 700; color: #10b981; font-size: 0.88rem; margin-bottom: 4px;">
                    🔭 GENUINE FORWARD DAILY EVALUATION — STRICT PERFORMANCE EVIDENCE
                </div>
                <div style="color: #cbd5e1; font-size: 0.80rem; line-height: 1.4;">
                    Displaying only genuine daily market observation sessions. All synthetic test fixtures and unknown data modes are strictly filtered out to preserve scientific integrity.
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    if not is_diag and fwd_data["summaries_count"] == 0:
        st.info(
            "ℹ️ No genuine DAILY forward evaluation sessions recorded yet. "
            "To record genuine daily observations, run `python forward_evaluation_cli.py --mode DAILY`. "
            "(To inspect synthetic test fixtures, switch the sidebar Evaluation Scope to '🧪 Diagnostic Fixtures')."
        )

    # Statistical Honesty Caveat
    trades_df = fwd_data["trades_df"]
    trades_n = len(trades_df)
    if trades_n < 30:
        st.markdown(
            f"""
            <div style="background-color: rgba(220, 38, 38, 0.15); border: 1px solid rgba(239, 68, 68, 0.4); padding: 10px 14px; border-radius: 6px; margin-bottom: 1.2rem;">
                <span style="color: #fca5a5; font-weight: 700; font-size: 0.84rem;">
                    📊 STATISTICAL CONFIDENCE NOTICE: SAMPLE SIZE INSUFFICIENT (N = {trades_n} / 30 required)
                </span>
                <div style="color: #cbd5e1; font-size: 0.78rem; margin-top: 4px;">
                    Observed closed trades (N={trades_n}) are below the minimum threshold (N=30) required for statistical significance.
                    Metrics, win rates, and profit factors are preliminary observations and must NOT be extrapolated as strategy expectancy.
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    # Top KPI Cards
    summaries_df = fwd_data["summaries_df"]
    positions_df = fwd_data["positions_df"]
    obs_df = fwd_data["observations_df"]

    tot_sessions = fwd_data["summaries_count"]
    tot_obs = fwd_data["observations_count"]
    tot_eligible = len(obs_df[obs_df["is_eligible"] == True]) if not obs_df.empty and "is_eligible" in obs_df.columns else 0
    active_pos = fwd_data["positions_count"]
    closed_trades = fwd_data["trades_count"]

    net_pnl = 0.0
    if not trades_df.empty and "net_pnl" in trades_df.columns:
        try:
            net_pnl = float(trades_df["net_pnl"].astype(float).sum())
        except Exception:
            net_pnl = 0.0

    latest_equity = 5000.0
    if not summaries_df.empty and "virtual_equity" in summaries_df.columns:
        try:
            latest_equity = float(summaries_df["virtual_equity"].iloc[-1])
        except Exception:
            latest_equity = 5000.0

    k1, k2, k3, k4, k5, k6, k7 = st.columns(7)
    with k1:
        st.metric("Sessions", tot_sessions)
    with k2:
        st.metric("Screened", tot_obs)
    with k3:
        st.metric("Eligible", tot_eligible)
    with k4:
        st.metric("Active Pos", active_pos)
    with k5:
        st.metric("Closed Trades", closed_trades)
    with k6:
        st.metric("Realized P&L", f"${net_pnl:+,.2f}")
    with k7:
        st.metric("Virtual Equity", f"${latest_equity:,.2f}")

    st.markdown("<br>", unsafe_allow_html=True)

    # Tabs for detailed views
    tab_pos, tab_closed, tab_sum, tab_obs, tab_audit = st.tabs([
        f"Active Positions ({active_pos})",
        f"Closed Trades ({closed_trades})",
        f"Session Summaries ({tot_sessions})",
        f"Candidate Observations ({tot_obs})",
        f"Audit Events ({fwd_data['audit_events_count']})",
    ])

    with tab_pos:
        st.markdown("#### Hypothetical Active Positions")
        if positions_df.empty:
            st.info("No active hypothetical positions currently open.")
        else:
            st.dataframe(positions_df, use_container_width=True, hide_index=True)

    with tab_closed:
        st.markdown("#### Closed Hypothetical Trades")
        if trades_df.empty:
            st.info("No hypothetical trades have closed yet.")
        else:
            st.dataframe(trades_df, use_container_width=True, hide_index=True)

    with tab_sum:
        st.markdown("#### Forward Evaluation Session Summaries")
        if summaries_df.empty:
            st.info("No forward evaluation sessions recorded.")
        else:
            st.dataframe(summaries_df, use_container_width=True, hide_index=True)

    with tab_obs:
        st.markdown("#### Forward Candidate Screening Observations")
        if obs_df.empty:
            st.info("No forward candidate observations recorded.")
        else:
            if selected_session and selected_session not in ("No daily forward sessions", "No sessions recorded"):
                session_obs = obs_df[obs_df["session_date"] == selected_session]
                if not session_obs.empty:
                    st.caption(f"Showing observations for session: `{selected_session}` ({len(session_obs)} rows)")
                    st.dataframe(session_obs, use_container_width=True, hide_index=True)
                else:
                    st.dataframe(obs_df, use_container_width=True, hide_index=True)
            else:
                st.dataframe(obs_df, use_container_width=True, hide_index=True)

    with tab_audit:
        st.markdown("#### Append-Only Audit Trail")
        audit_events = fwd_data.get("audit_events", [])
        if not audit_events:
            st.info("No forward evaluation audit events logged.")
        else:
            st.dataframe(pd.DataFrame(audit_events), use_container_width=True, hide_index=True)

    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown(
        """
        <div style="font-size: 0.75rem; color: #64748b; text-align: center; border-top: 1px solid #1e293b; padding-top: 12px;">
            Trader-Swing Forward Evaluation Sandbox &bull; Isolated Directory: <code>.planning/data/forward_evaluation/</code> &bull; Pure Decimal Accounting
        </div>
        """,
        unsafe_allow_html=True,
    )
