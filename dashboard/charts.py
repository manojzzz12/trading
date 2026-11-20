"""Interactive Plotly charts for Trader-Swing Dashboard MVP.

Enforces:
1. Consistent terminal dark theme.
2. Read-only visualization: strictly graphs existing historical data.
3. Clean tooltips, zero clutter, and responsive formatting.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots


PLOTLY_DARK_LAYOUT = dict(
    paper_bgcolor="#0b0f19",
    plot_bgcolor="#0f172a",
    font=dict(color="#cbd5e1", family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"),
    xaxis=dict(
        gridcolor="#1e293b",
        linecolor="#334155",
        zerolinecolor="#334155",
    ),
    yaxis=dict(
        gridcolor="#1e293b",
        linecolor="#334155",
        zerolinecolor="#334155",
    ),
    margin=dict(l=40, r=40, t=40, b=40),
)


def create_ticker_candlestick_chart(
    df: pd.DataFrame,
    symbol: str,
    max_bars: int = 90,
    show_smas: bool = True,
    show_rsi: bool = True,
    price_source: str = "Alpaca IEX",
    volume_source: str = "Volume",
    feed_state: str = "LIVE_IEX_HYBRID",
) -> go.Figure:
    """Create interactive 3-tier candlestick chart with volume bars, SMAs, and RSI-14."""
    plot_df = df.tail(max_bars).copy()
    has_rsi = show_rsi and "RSI14" in plot_df.columns and plot_df["RSI14"].notna().any()

    if has_rsi:
        fig = make_subplots(
            rows=3,
            cols=1,
            shared_xaxes=True,
            vertical_spacing=0.03,
            row_heights=[0.58, 0.20, 0.22],
        )
    else:
        fig = make_subplots(
            rows=2,
            cols=1,
            shared_xaxes=True,
            vertical_spacing=0.04,
            row_heights=[0.75, 0.25],
        )

    # 1. Candlestick (Completed Daily Bars)
    fig.add_trace(
        go.Candlestick(
            x=plot_df["Date"],
            open=plot_df["Open"],
            high=plot_df["High"],
            low=plot_df["Low"],
            close=plot_df["Close"],
            name=symbol,
            increasing_line_color="#10b981",
            increasing_fillcolor="#10b981",
            decreasing_line_color="#f43f5e",
            decreasing_fillcolor="#f43f5e",
        ),
        row=1,
        col=1,
    )

    # 2. Moving Averages Overlay
    if show_smas:
        if "SMA20" in plot_df.columns and plot_df["SMA20"].notna().any():
            fig.add_trace(
                go.Scatter(
                    x=plot_df["Date"],
                    y=plot_df["SMA20"],
                    mode="lines",
                    name="SMA 20",
                    line=dict(color="#38bdf8", width=1.5),
                ),
                row=1,
                col=1,
            )
        if "SMA50" in plot_df.columns and plot_df["SMA50"].notna().any():
            fig.add_trace(
                go.Scatter(
                    x=plot_df["Date"],
                    y=plot_df["SMA50"],
                    mode="lines",
                    name="SMA 50",
                    line=dict(color="#f59e0b", width=1.5),
                ),
                row=1,
                col=1,
            )
        if "SMA200" in plot_df.columns and plot_df["SMA200"].notna().any():
            fig.add_trace(
                go.Scatter(
                    x=plot_df["Date"],
                    y=plot_df["SMA200"],
                    mode="lines",
                    name="SMA 200",
                    line=dict(color="#a855f7", width=1.5),
                ),
                row=1,
                col=1,
            )

    # 3. Volume Subplot (Authoritative Consolidated Tape Volume)
    colors = [
        "#10b981" if c >= o else "#f43f5e"
        for o, c in zip(plot_df["Open"], plot_df["Close"])
    ]
    vol_trace_name = "Volume" if volume_source == "Volume" else f"Volume ({volume_source})"
    fig.add_trace(
        go.Bar(
            x=plot_df["Date"],
            y=plot_df["Volume"],
            name=vol_trace_name,
            marker_color=colors,
            opacity=0.6,
        ),
        row=2,
        col=1,
    )

    # 4. RSI Subplot
    if has_rsi:
        fig.add_trace(
            go.Scatter(
                x=plot_df["Date"],
                y=plot_df["RSI14"],
                mode="lines",
                name="RSI 14",
                line=dict(color="#38bdf8", width=1.5),
            ),
            row=3,
            col=1,
        )
        fig.add_hline(y=70, line_dash="dash", line_color="rgba(244, 63, 94, 0.6)", row=3, col=1)
        fig.add_hline(y=50, line_dash="dot", line_color="rgba(100, 116, 139, 0.4)", row=3, col=1)
        fig.add_hline(y=30, line_dash="dash", line_color="rgba(16, 185, 129, 0.6)", row=3, col=1)
        fig.update_yaxes(title_text="RSI(14)", row=3, col=1, range=[0, 100])

    fig.update_layout(
        **PLOTLY_DARK_LAYOUT,
        title=(
            f"<b>{symbol}</b> — Completed Daily Candles ({len(plot_df)} bars) "
            f"<span style='font-size: 11px; color: #38bdf8;'>[{feed_state} — Price: {price_source} | Vol: {volume_source}]</span>"
        ),
        xaxis_rangeslider_visible=False,
        showlegend=True,
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="right",
            x=1,
            font=dict(size=11),
        ),
        hovermode="x unified",
    )
    fig.update_yaxes(title_text="Price ($)", row=1, col=1)
    fig.update_yaxes(title_text="Consolidated Vol", row=2, col=1)

    return fig


def create_proxy_candlestick_chart(
    df: pd.DataFrame,
    symbol: str,
    max_bars: int = 90,
    show_smas: bool = True,
) -> go.Figure:
    """Create interactive candlestick chart with volume bars and moving averages."""
    plot_df = df.tail(max_bars).copy()

    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.04,
        row_heights=[0.75, 0.25],
    )

    # 1. Candlestick
    fig.add_trace(
        go.Candlestick(
            x=plot_df["Date"],
            open=plot_df["Open"],
            high=plot_df["High"],
            low=plot_df["Low"],
            close=plot_df["Close"],
            name=symbol,
            increasing_line_color="#10b981",
            increasing_fillcolor="#10b981",
            decreasing_line_color="#f43f5e",
            decreasing_fillcolor="#f43f5e",
        ),
        row=1,
        col=1,
    )

    # 2. Moving Averages Overlay
    if show_smas:
        if "SMA20" in plot_df.columns and plot_df["SMA20"].notna().any():
            fig.add_trace(
                go.Scatter(
                    x=plot_df["Date"],
                    y=plot_df["SMA20"],
                    mode="lines",
                    name="SMA 20",
                    line=dict(color="#38bdf8", width=1.5),
                ),
                row=1,
                col=1,
            )
        if "SMA50" in plot_df.columns and plot_df["SMA50"].notna().any():
            fig.add_trace(
                go.Scatter(
                    x=plot_df["Date"],
                    y=plot_df["SMA50"],
                    mode="lines",
                    name="SMA 50",
                    line=dict(color="#f59e0b", width=1.5),
                ),
                row=1,
                col=1,
            )
        if "SMA200" in plot_df.columns and plot_df["SMA200"].notna().any():
            fig.add_trace(
                go.Scatter(
                    x=plot_df["Date"],
                    y=plot_df["SMA200"],
                    mode="lines",
                    name="SMA 200",
                    line=dict(color="#a855f7", width=1.5),
                ),
                row=1,
                col=1,
            )

    # 3. Volume Subplot
    colors = [
        "#10b981" if c >= o else "#f43f5e"
        for o, c in zip(plot_df["Open"], plot_df["Close"])
    ]
    fig.add_trace(
        go.Bar(
            x=plot_df["Date"],
            y=plot_df["Volume"],
            name="Volume",
            marker_color=colors,
            opacity=0.6,
        ),
        row=2,
        col=1,
    )

    # Apply terminal dark layout
    fig.update_layout(
        **PLOTLY_DARK_LAYOUT,
        title=f"<b>{symbol}</b> — Daily Candle Chart ({len(plot_df)} bars)",
        xaxis_rangeslider_visible=False,
        showlegend=True,
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="right",
            x=1,
            font=dict(size=11),
        ),
        hovermode="x unified",
    )
    fig.update_yaxes(title_text="Price ($)", row=1, col=1)
    fig.update_yaxes(title_text="Vol", row=2, col=1)

    return fig


def create_regime_donut_chart(session_records: List[Dict[str, Any]]) -> go.Figure:
    """Create donut chart showing market regime distribution across observed sessions."""
    regime_counts: Dict[str, int] = {}
    for r in session_records:
        reg = r.get("market_regime", "UNKNOWN")
        regime_counts[reg] = regime_counts.get(reg, 0) + 1

    if not regime_counts:
        regime_counts = {"NO_DATA": 1}

    labels = list(regime_counts.keys())
    values = list(regime_counts.values())

    color_map = {
        "STRONG_BULLISH": "#10b981",
        "BULLISH": "#34d399",
        "SIDEWAYS": "#f59e0b",
        "BEARISH": "#fb7185",
        "STRONG_BEARISH": "#e11d48",
        "UNKNOWN": "#64748b",
        "NO_DATA": "#334155",
    }
    colors = [color_map.get(k, "#64748b") for k in labels]

    fig = go.Figure(
        data=[
            go.Pie(
                labels=labels,
                values=values,
                hole=0.6,
                marker=dict(colors=colors, line=dict(color="#0f172a", width=2)),
                textinfo="label+percent",
                hoverinfo="label+value+percent",
            )
        ]
    )

    fig.update_layout(
        **PLOTLY_DARK_LAYOUT,
        title="<b>Market Regime Distribution</b> (Observed Sessions)",
        showlegend=True,
        legend=dict(orientation="h", yanchor="top", y=-0.1, xanchor="center", x=0.5),
    )
    return fig


def create_rejection_reasons_bar_chart(reasons_dict: Dict[str, int]) -> go.Figure:
    """Create horizontal bar chart of candidate rejection causes."""
    if not reasons_dict:
        fig = go.Figure()
        fig.update_layout(
            **PLOTLY_DARK_LAYOUT,
            title="<b>Rejection Reasons</b> (None Recorded)",
        )
        return fig

    # Sort ascending so highest appears at top of horizontal chart
    sorted_items = sorted(reasons_dict.items(), key=lambda x: x[1])
    labels = [x[0] for x in sorted_items]
    counts = [x[1] for x in sorted_items]

    fig = go.Figure(
        data=[
            go.Bar(
                x=counts,
                y=labels,
                orientation="h",
                marker=dict(
                    color=counts,
                    colorscale="Reds",
                    line=dict(color="#f43f5e", width=1),
                ),
                text=counts,
                textposition="auto",
            )
        ]
    )

    fig.update_layout(
        **PLOTLY_DARK_LAYOUT,
        title="<b>Screening Rejection Breakdown</b> (Cumulative)",
        xaxis_title="Candidate Count",
        yaxis_title="",
    )
    fig.update_yaxes(autorange="reversed")
    return fig


def create_pipeline_funnel_chart(
    evaluated_count: int,
    eligible_count: int,
    signal_gen_count: int,
    risk_sized_count: int,
    executed_fills: int = 0,
) -> go.Figure:
    """Create horizontal funnel visualization of the screening to execution pipeline."""
    stages = [
        "1. Universe Evaluated",
        "2. Passed Scanner",
        "3. Signal Evaluated",
        "4. Risk Sized",
        "5. Executed Fills",
    ]
    values = [
        evaluated_count,
        eligible_count,
        signal_gen_count,
        risk_sized_count,
        executed_fills,
    ]

    fig = go.Figure(
        go.Funnel(
            y=stages,
            x=values,
            textinfo="value+percent initial",
            marker=dict(
                color=["#38bdf8", "#0ea5e9", "#f59e0b", "#10b981", "#64748b"],
                line=dict(color="#1e293b", width=1.5),
            ),
        )
    )

    fig.update_layout(
        **PLOTLY_DARK_LAYOUT,
        title="<b>Research Observation Funnel</b>",
    )
    return fig


def create_top_equities_comparison_chart(
    data_dir: Path,
    tickers: List[str],
    max_bars: int = 30,
) -> Optional[go.Figure]:
    """Create normalized relative-performance line chart for top tickers over last N sessions."""
    fig = go.Figure()
    valid_series = 0

    palette = ["#38bdf8", "#10b981", "#f59e0b", "#ec4899", "#a855f7", "#06b6d4"]

    for idx, ticker in enumerate(tickers[:8]):
        csv_path = data_dir / f"{ticker}_daily.csv"
        if not csv_path.exists():
            continue
        try:
            df = pd.read_csv(csv_path)
            if len(df) < 5:
                continue
            df["Date"] = pd.to_datetime(df["Date"])
            sub = df.tail(max_bars).copy()
            base_px = sub["Close"].iloc[0]
            if base_px <= 0:
                continue
            sub["Return_Pct"] = ((sub["Close"] - base_px) / base_px) * 100.0

            if ticker == "SPY":
                line_style = dict(color="#cbd5e1", width=2.2, dash="dash")
                trace_name = "SPY (Benchmark)"
            else:
                line_style = dict(color=palette[idx % len(palette)], width=2)
                trace_name = ticker

            fig.add_trace(
                go.Scatter(
                    x=sub["Date"],
                    y=sub["Return_Pct"],
                    mode="lines",
                    name=trace_name,
                    line=line_style,
                )
            )
            valid_series += 1
        except Exception:
            continue

    if valid_series == 0:
        return None

    fig.update_layout(
        **PLOTLY_DARK_LAYOUT,
        title=f"<b>Comparative Price Trajectory</b> (Last {max_bars} Sessions, % Return)",
        yaxis_title="Return (%)",
        xaxis_title="Date",
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    fig.add_hline(y=0, line_dash="dash", line_color="#475569", opacity=0.7)

    return fig


def create_market_sentiment_gauge(
    macro_score: float = -0.82,
    regime: str = "STRONG_BEARISH",
) -> go.Figure:
    """Create market sentiment gauge based on recorded macro score and regime."""
    if macro_score <= -0.60:
        bar_color = "#e11d48"
    elif macro_score <= -0.20:
        bar_color = "#fb7185"
    elif macro_score < 0.20:
        bar_color = "#f59e0b"
    elif macro_score < 0.60:
        bar_color = "#34d399"
    else:
        bar_color = "#10b981"

    fig = go.Figure(
        go.Indicator(
            mode="gauge+number",
            value=macro_score,
            number=dict(
                valueformat="+.2f",
                font=dict(color=bar_color, size=32, family="JetBrains Mono, monospace"),
            ),
            gauge=dict(
                axis=dict(
                    range=[-1.0, 1.0],
                    tickmode="array",
                    tickvals=[-1.0, -0.6, -0.2, 0.0, 0.2, 0.6, 1.0],
                    ticktext=["-1.0", "-0.6", "-0.2", "0.0", "+0.2", "+0.6", "+1.0"],
                    tickfont=dict(size=10, color="#94a3b8"),
                ),
                bar=dict(color=bar_color, thickness=0.3),
                bgcolor="#1e293b",
                borderwidth=1,
                bordercolor="#334155",
                steps=[
                    dict(range=[-1.0, -0.6], color="rgba(225, 29, 72, 0.25)"),
                    dict(range=[-0.6, -0.2], color="rgba(251, 113, 133, 0.15)"),
                    dict(range=[-0.2, 0.2], color="rgba(245, 158, 11, 0.15)"),
                    dict(range=[0.2, 0.6], color="rgba(52, 211, 153, 0.15)"),
                    dict(range=[0.6, 1.0], color="rgba(16, 185, 129, 0.25)"),
                ],
                threshold=dict(
                    line=dict(color="#f8fafc", width=3),
                    thickness=0.75,
                    value=macro_score,
                ),
            ),
            title=dict(
                text=f"<b>Macro Regime Sentiment</b><br><span style='font-size: 11px; color: #94a3b8;'>Current: {regime} (Threshold: &le; -0.60)</span>",
                font=dict(size=14, color="#f8fafc"),
            ),
        )
    )

    fig.update_layout(**PLOTLY_DARK_LAYOUT)
    fig.update_layout(
        height=260,
        margin=dict(l=30, r=30, t=50, b=20),
    )
    return fig


def create_portfolio_equity_chart(
    equity_df: pd.DataFrame,
    baseline_value: float = 5000.0,
    has_history: bool = False,
) -> go.Figure:
    """Create interactive portfolio equity chart.

    SAFETY MANDATE:
    If has_history is False, renders the verified frozen baseline flat line ($5,000.00)
    with explicit disclosure that no trading performance has been recorded yet.
    """
    fig = go.Figure()

    if equity_df is not None and not equity_df.empty:
        fig.add_trace(
            go.Scatter(
                x=equity_df["Date"],
                y=equity_df["Equity"],
                mode="lines+markers",
                name="Portfolio Equity ($)",
                line=dict(color="#38bdf8", width=2.5),
                marker=dict(size=6, color="#38bdf8"),
                fill="tozeroy",
                fillcolor="rgba(56, 189, 248, 0.08)",
            )
        )

    # Add baseline reference line
    fig.add_hline(
        y=baseline_value,
        line_dash="dash",
        line_color="#10b981",
        line_width=1.5,
        annotation_text=f"Initial Capital Baseline (${baseline_value:,.2f})",
        annotation_position="bottom right",
        annotation_font=dict(color="#10b981", size=10),
    )

    # Explicit disclosure annotation when no trading history exists
    if not has_history:
        fig.add_annotation(
            text="<b>NO RECORDED PORTFOLIO PERFORMANCE YET</b><br>"
                 "<span style='font-size: 11px; color: #94a3b8;'>"
                 "Verified baseline: $5,000.00 Cash Buffer (0 Positions / 0 Fills)</span>",
            xref="paper",
            yref="paper",
            x=0.5,
            y=0.55,
            showarrow=False,
            font=dict(size=13, color="#cbd5e1"),
            align="center",
            bgcolor="rgba(15, 23, 42, 0.85)",
            bordercolor="#334155",
            borderwidth=1,
            borderpad=8,
        )

    fig.update_layout(
        **PLOTLY_DARK_LAYOUT,
        title="<b>Paper Portfolio Net Liquidation Value</b> (USD)",
        yaxis_title="Equity ($)",
        xaxis_title="Session Date",
        hovermode="x unified",
        height=320,
    )
    fig.update_yaxes(
        range=[baseline_value * 0.90, baseline_value * 1.10],
        gridcolor="#1e293b",
        linecolor="#334155",
    )
    return fig


def create_asset_allocation_donut(
    cash: float = 5000.0,
    invested: float = 0.0,
) -> go.Figure:
    """Create asset allocation donut chart showing cash buffer vs invested capital."""
    labels = ["Cash Buffer (Reserve)", "Invested Equities"]
    values = [cash, invested]
    colors = ["#10b981", "#334155"]

    fig = go.Figure(
        data=[
            go.Pie(
                labels=labels,
                values=values,
                hole=0.65,
                marker=dict(colors=colors, line=dict(color="#0f172a", width=2)),
                textinfo="label+percent",
                hoverinfo="label+value+percent",
            )
        ]
    )

    cash_pct = 100.0 if (cash + invested) == 0 else (cash / (cash + invested)) * 100.0

    fig.add_annotation(
        text=f"<b>{cash_pct:.0f}%</b><br><span style='font-size: 10px; color: #94a3b8;'>CASH</span>",
        x=0.5,
        y=0.5,
        showarrow=False,
        font=dict(size=16, color="#f8fafc", family="JetBrains Mono, monospace"),
        align="center",
    )

    fig.update_layout(
        **PLOTLY_DARK_LAYOUT,
        title="<b>Asset Allocation</b>",
        showlegend=True,
        legend=dict(orientation="h", yanchor="top", y=-0.05, xanchor="center", x=0.5, font=dict(size=10)),
        height=280,
    )
    fig.update_layout(margin=dict(l=20, r=20, t=40, b=20))
    return fig


def create_risk_capacity_donut(
    max_positions: int = 4,
    open_positions: int = 0,
    risk_per_trade_usd: float = 25.0,
) -> go.Figure:
    """Create risk capacity allocation donut chart."""
    available_slots = max(0, max_positions - open_positions)
    labels = ["Available Risk Capacity", "Committed Risk"]
    values = [available_slots, open_positions]
    colors = ["#38bdf8", "#f43f5e" if open_positions > 0 else "#334155"]

    fig = go.Figure(
        data=[
            go.Pie(
                labels=labels,
                values=values,
                hole=0.65,
                marker=dict(colors=colors, line=dict(color="#0f172a", width=2)),
                textinfo="label+percent",
                hoverinfo="label+value+percent",
            )
        ]
    )

    fig.add_annotation(
        text=f"<b>{available_slots}/{max_positions}</b><br><span style='font-size: 10px; color: #94a3b8;'>SLOTS FREE</span>",
        x=0.5,
        y=0.5,
        showarrow=False,
        font=dict(size=14, color="#f8fafc", family="JetBrains Mono, monospace"),
        align="center",
    )

    fig.update_layout(
        **PLOTLY_DARK_LAYOUT,
        title="<b>Risk Capacity Allocation</b>",
        showlegend=True,
        legend=dict(orientation="h", yanchor="top", y=-0.05, xanchor="center", x=0.5, font=dict(size=10)),
        height=280,
    )
    fig.update_layout(margin=dict(l=20, r=20, t=40, b=20))
    return fig


def create_forming_intraday_chart(
    forming_bar: Dict[str, Any],
    symbol: str,
) -> go.Figure:
    """Create dedicated visualization for an active forming intraday minute bar.

    Features:
    - Hollow candlestick representing incomplete price range (Open, High, Low, Close).
    - Horizontal reference line for VWAP if present.
    - Single-exchange IEX volume bar clearly labeled as unconsolidated.
    - Explicit FORMING / INCOMPLETE status badge and warnings.
    """
    ts_str = str(forming_bar.get("timestamp", "Live Bar"))
    o = float(forming_bar.get("open", 0.0))
    h = float(forming_bar.get("high", 0.0))
    l = float(forming_bar.get("low", 0.0))
    c = float(forming_bar.get("close", 0.0))
    vol = int(forming_bar.get("volume", 0))
    vwap = float(forming_bar.get("vwap", 0.0)) if forming_bar.get("vwap") else None
    trades = forming_bar.get("trade_count")

    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.08,
        row_heights=[0.68, 0.32],
    )

    # 1. Forming Hollow Candlestick
    cand_color = "#38bdf8" if c >= o else "#fb7185"
    fig.add_trace(
        go.Candlestick(
            x=[ts_str],
            open=[o],
            high=[h],
            low=[l],
            close=[c],
            name=f"{symbol} [FORMING]",
            increasing_line_color="#38bdf8",
            increasing_fillcolor="rgba(56, 189, 248, 0.2)",
            decreasing_line_color="#fb7185",
            decreasing_fillcolor="rgba(251, 113, 133, 0.2)",
            whiskerwidth=0.8,
        ),
        row=1,
        col=1,
    )

    # 2. VWAP reference line
    if vwap is not None and vwap > 0:
        fig.add_trace(
            go.Scatter(
                x=[ts_str],
                y=[vwap],
                mode="markers",
                marker=dict(symbol="line-ew", size=30, color="#f59e0b", line=dict(width=3, color="#f59e0b")),
                name=f"VWAP (${vwap:.2f})",
            ),
            row=1,
            col=1,
        )

    # 3. IEX Unconsolidated Volume Bar
    fig.add_trace(
        go.Bar(
            x=[ts_str],
            y=[vol],
            name="IEX Volume (Unconsolidated)",
            marker_color="rgba(245, 158, 11, 0.7)",
            marker_line=dict(color="#f59e0b", width=1),
        ),
        row=2,
        col=1,
    )

    # Annotation badge warning
    trades_str = f" | Trades: {trades}" if trades else ""
    fig.add_annotation(
        text=f"<b>FORMING INTRADAY MINUTE BAR</b><br><span style='font-size: 10px; color: #fbbf24;'>IEX UNCONSOLIDATED VOLUME — NEVER EVALUATED AS COMPLETED DAILY CANDLE{trades_str}</span>",
        xref="paper",
        yref="paper",
        x=0.5,
        y=1.18,
        showarrow=False,
        font=dict(size=11, color="#f8fafc", family="JetBrains Mono, monospace"),
        align="center",
        bgcolor="rgba(245, 158, 11, 0.15)",
        bordercolor="#f59e0b",
        borderwidth=1,
        borderpad=4,
    )

    fig.update_layout(
        **PLOTLY_DARK_LAYOUT,
        xaxis_rangeslider_visible=False,
        showlegend=True,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1, font=dict(size=10)),
        height=320,
    )
    fig.update_layout(margin=dict(l=30, r=30, t=55, b=25))
    fig.update_yaxes(title_text="Price ($)", row=1, col=1)
    fig.update_yaxes(title_text="IEX Vol", row=2, col=1)

    return fig
