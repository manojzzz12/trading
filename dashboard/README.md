# Trader-Swing Read-Only Research Dashboard (Phase 6.5)

A professional, dark-themed Streamlit research interface for inspecting market data, scanner observations, macro regime diagnostics, and forward observation logs.

---

## 1. Quick Start / How to Launch

Ensure your Python environment is active and run:

```bash
streamlit run dashboard/app.py
```

Or run via Python module:

```bash
python -m streamlit run dashboard/app.py
```

The dashboard will launch locally in your browser (typically at `http://localhost:8501`).

---

## 2. Architecture & Data Sources

The dashboard is strictly decoupled from the trading engine and operates in **read-only mode**:

| Section | Data Source | Description |
| :--- | :--- | :--- |
| **Overview & History** | `.planning/data/observations/observation_log.jsonl` | Parsed forward observation records and session summaries. |
| **Market Overview** | `.planning/data/current_daily/SPY_daily.csv`, `QQQ_daily.csv`, `IWM_daily.csv` | Historical end-of-day candles and calculated rolling SMAs (20, 50, 200). |
| **Scanner & Candidates** | `.planning/data/observations/observation_log.jsonl` | 5-pillar candidate screening scores, rejection reasons, and metrics. |
| **Data Quality** | `.planning/data/current_daily/CURRENT_DAILY_MANIFEST.json` | Manifest verification status, upstream exclusions (e.g. CVX geometry anomaly). |
| **Portfolio Snapshot** | Fixed Independent Audit Baseline | Static verified snapshot as of 2026-11-16 ($5,000 equity, 0 positions, 0 fills). |

---

## 3. Strict Safety & Governance Boundaries

- **Zero Live Execution:** The dashboard cannot execute orders, connect to brokers, or place paper trades.
- **Pure Read-Only Access:** Does not mutate, append, or delete any local files.
- **Isolated Paper State:** Does not inspect or modify `TradingPaperState`, `paper_journal.json`, or root portfolio files.
- **Lockbox Protection:** Does not access the 2026 lockbox.
- **Historical Partition Preservation:** Historical partitions in `.planning/data/historical_partitions` remain untouched.
- **Zero Background Automation:** No cron jobs, daemons, background polling, or external API network calls.
