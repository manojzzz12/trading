"""Custom dark theme CSS and styling for Trader-Swing Dashboard MVP."""

DARK_THEME_CSS = """
<style>
    /* Global App Container */
    .stApp {
        background-color: #0b0f19;
        color: #e2e8f0;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    }

    /* Top Header Bar */
    header[data-testid="stHeader"] {
        background-color: rgba(11, 15, 25, 0.90);
        backdrop-filter: blur(8px);
    }

    /* Sidebar Styling */
    section[data-testid="stSidebar"] {
        background-color: #080c14;
        border-right: 1px solid #1f2937;
    }

    /* Card Containers */
    .card-container {
        background: #111827;
        border: 1px solid #1f2937;
        border-radius: 8px;
        padding: 1.15rem;
        margin-bottom: 1rem;
    }

    .card-highlight {
        border-left: 4px solid #38bdf8;
    }

    .card-danger {
        border-left: 4px solid #f43f5e;
    }

    .card-success {
        border-left: 4px solid #10b981;
    }

    .card-warning {
        border-left: 4px solid #f59e0b;
    }

    /* KPI Metric Cards */
    .kpi-card {
        background: #111827;
        border: 1px solid #1f2937;
        border-radius: 8px;
        padding: 0.9rem;
        text-align: center;
        transition: border-color 0.15s ease;
    }
    .kpi-card:hover {
        border-color: #38bdf8;
    }
    .kpi-label {
        font-size: 0.72rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        color: #94a3b8;
        margin-bottom: 0.3rem;
    }
    .kpi-value {
        font-size: 1.5rem;
        font-weight: 700;
        font-family: "JetBrains Mono", "SF Mono", Menlo, Monaco, Consolas, monospace;
        color: #f8fafc;
        margin-bottom: 0.2rem;
    }
    .kpi-sub {
        font-size: 0.72rem;
        color: #64748b;
    }

    /* Top Ticker Cyber Cards (Matches Dark Terminal Reference Image) */
    .ticker-card {
        background: #111827;
        border: 1px solid #1f2937;
        border-radius: 8px;
        padding: 0.85rem 0.95rem;
        transition: border-color 0.15s ease;
    }
    .ticker-card:hover {
        border-color: #38bdf8;
    }
    .ticker-symbol {
        font-size: 1.1rem;
        font-weight: 800;
        letter-spacing: 0.04em;
        color: #f8fafc;
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 0.25rem;
    }
    .ticker-price {
        font-size: 1.25rem;
        font-weight: 700;
        font-family: "JetBrains Mono", monospace;
        color: #f1f5f9;
        margin-bottom: 0.35rem;
    }
    .ticker-metrics {
        font-size: 0.72rem;
        color: #94a3b8;
        line-height: 1.4;
        border-top: 1px solid #1f2937;
        padding-top: 0.35rem;
    }

    /* Five-Pillar Breakdown Grid */
    .pillar-box {
        background: #0f172a;
        border: 1px solid #1e293b;
        border-radius: 6px;
        padding: 0.65rem 0.8rem;
        margin-bottom: 0.5rem;
    }
    .pillar-title {
        font-size: 0.75rem;
        font-weight: 700;
        text-transform: uppercase;
        color: #94a3b8;
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 0.25rem;
    }
    .pillar-detail {
        font-size: 0.8rem;
        color: #cbd5e1;
        font-family: "JetBrains Mono", monospace;
    }

    /* Trade Setup Panel */
    .trade-setup-box {
        background: #0f172a;
        border: 1px solid #1e293b;
        border-radius: 6px;
        padding: 0.85rem;
        font-family: "JetBrains Mono", monospace;
        font-size: 0.82rem;
        line-height: 1.6;
    }

    /* Status Badges / Pills */
    .badge {
        display: inline-block;
        padding: 0.2rem 0.65rem;
        border-radius: 9999px;
        font-size: 0.68rem;
        font-weight: 700;
        letter-spacing: 0.03em;
        text-transform: uppercase;
    }
    .badge-bearish {
        background-color: rgba(244, 63, 94, 0.15);
        color: #fb7185;
        border: 1px solid rgba(244, 63, 94, 0.4);
    }
    .badge-bullish {
        background-color: rgba(16, 185, 129, 0.15);
        color: #34d399;
        border: 1px solid rgba(16, 185, 129, 0.4);
    }
    .badge-sideways {
        background-color: rgba(245, 158, 11, 0.15);
        color: #fbbf24;
        border: 1px solid rgba(245, 158, 11, 0.4);
    }
    .badge-info {
        background-color: rgba(56, 189, 248, 0.15);
        color: #38bdf8;
        border: 1px solid rgba(56, 189, 248, 0.4);
    }
    .badge-hybrid {
        background-color: rgba(16, 185, 129, 0.15);
        color: #10b981;
        border: 1px solid #38bdf8;
    }
    .badge-forming {
        background-color: rgba(245, 158, 11, 0.12);
        color: #fbbf24;
        border: 1px dashed rgba(245, 158, 11, 0.6);
    }
    .badge-complete {
        background-color: rgba(16, 185, 129, 0.15);
        color: #34d399;
        border: 1px solid rgba(16, 185, 129, 0.6);
    }
    .badge-unverified {
        background-color: rgba(244, 63, 94, 0.15);
        color: #fb7185;
        border: 1px dashed rgba(244, 63, 94, 0.5);
    }
    .badge-danger {
        background-color: rgba(225, 29, 72, 0.2);
        color: #f43f5e;
        border: 1px solid rgba(225, 29, 72, 0.6);
    }
    .badge-degraded {
        background-color: rgba(245, 158, 11, 0.18);
        color: #fbbf24;
        border: 1px solid rgba(245, 158, 11, 0.6);
    }
    .badge-unverifiable {
        background-color: rgba(217, 119, 6, 0.15);
        color: #f59e0b;
        border: 1px dashed rgba(217, 119, 6, 0.6);
    }
    .badge-live-price {
        background-color: rgba(56, 189, 248, 0.12);
        color: #38bdf8;
        border: 1px dashed rgba(56, 189, 248, 0.6);
    }
    .badge-neutral {
        background-color: rgba(148, 163, 184, 0.15);
        color: #94a3b8;
        border: 1px solid rgba(148, 163, 184, 0.3);
    }

    /* Forming Intraday Container */
    .forming-container {
        background: #0d1527;
        border: 1px dashed #f59e0b;
        border-radius: 8px;
        padding: 1rem 1.15rem;
        margin-bottom: 1rem;
    }

    /* Gateway Status Header */
    .gateway-header-box {
        background: #111827;
        border: 1px solid #1f2937;
        border-radius: 8px;
        padding: 0.75rem 1rem;
        margin-bottom: 1rem;
        display: flex;
        justify-content: space-between;
        align-items: center;
        flex-wrap: wrap;
        gap: 0.5rem;
    }

    /* Notice Banners */
    .banner-delayed {
        background: rgba(14, 165, 233, 0.08);
        border: 1px solid rgba(14, 165, 233, 0.3);
        border-radius: 6px;
        padding: 0.75rem 1rem;
        margin-bottom: 1.25rem;
        font-size: 0.8rem;
        color: #7dd3fc;
    }
    .banner-warning {
        background: rgba(245, 158, 11, 0.08);
        border: 1px solid rgba(245, 158, 11, 0.3);
        border-radius: 6px;
        padding: 0.75rem 1rem;
        margin-bottom: 1.25rem;
        font-size: 0.8rem;
        color: #fde68a;
    }

    /* Empty State Container */
    .empty-state-box {
        background: #111827;
        border: 1px dashed #334155;
        border-radius: 8px;
        padding: 2rem 1.5rem;
        text-align: center;
        color: #94a3b8;
        margin-bottom: 1rem;
    }

    /* Tables */
    .dataframe {
        border: 1px solid #1f2937 !important;
        border-radius: 6px !important;
        background-color: #111827 !important;
        font-family: "JetBrains Mono", monospace !important;
        font-size: 0.82rem !important;
    }

    /* Streamlit Tabs */
    button[data-baseweb="tab"] {
        font-weight: 600;
        color: #94a3b8;
        padding: 0.5rem 1rem;
    }
    button[data-baseweb="tab"][aria-selected="true"] {
        color: #38bdf8 !important;
        border-bottom-color: #38bdf8 !important;
    }
</style>
"""