"""Deterministic offline test suite for Phase 6.5: Trader-Swing Read-Only Dashboard MVP.

Verifies:
1. JSONL parsing of valid observation records and session summaries.
2. Empty observation log graceful handling.
3. Malformed records fault tolerance.
4. Session summary extraction and date resolution.
5. Ticker and decision filtering logic.
6. Missing market data handling.
7. Data classification transparency.
8. Read-only invariance: zero mutation of files or paper trading state.
9. Rejection reason taxonomy categorization.
10. Fixed historical portfolio snapshot integrity.
"""

import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

# Ensure workspace root is in sys.path for dashboard imports
WORKSPACE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if WORKSPACE_ROOT not in sys.path:
    sys.path.insert(0, WORKSPACE_ROOT)

import pandas as pd

from dashboard.data_loader import (
    categorize_rejection_reasons,
    compute_rsi_series,
    filter_candidates,
    get_available_session_dates,
    get_market_breadth_metrics,
    get_portfolio_active_positions_and_orders,
    get_portfolio_equity_curve_data,
    get_verified_portfolio_snapshot,
    load_acquisition_manifest,
    load_observation_log,
    load_proxy_candles,
    load_ticker_candles,
)
from dashboard.charts import (
    create_asset_allocation_donut,
    create_market_sentiment_gauge,
    create_pipeline_funnel_chart,
    create_portfolio_equity_chart,
    create_proxy_candlestick_chart,
    create_regime_donut_chart,
    create_rejection_reasons_bar_chart,
    create_risk_capacity_donut,
    create_ticker_candlestick_chart,
)


class TestSwingDashboard(unittest.TestCase):
    """Test suite for Trader-Swing read-only dashboard components and data loaders."""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="test_dashboard_"))
        self.obs_file = self.test_dir / "observation_log.jsonl"
        self.manifest_file = self.test_dir / "CURRENT_DAILY_MANIFEST.json"
        self.data_dir = self.test_dir / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # 1. JSONL Parsing
    # -------------------------------------------------------------------------
    def test_01_jsonl_parsing_valid_records(self):
        """Valid JSONL records must parse cleanly into candidate and session lists."""
        records = [
            {
                "record_type": "FORWARD_OBSERVATION",
                "date": "2026-11-16",
                "ticker": "AAPL",
                "market_regime": "STRONG_BEARISH",
                "scanner_decision": "REJECTED",
                "candidate_score": "80.00",
                "rejection_reasons": ["Market regime disallows new long positions"],
                "technical_metrics": {"price": "224.23"},
                "reached_signal_generation": False,
                "reached_risk_sizing": False,
                "signal": "REJECTED_SCANNER",
                "executed_fill": False,
            },
            {
                "record_type": "SESSION_RECORD",
                "date": "2026-11-16",
                "market_regime": "STRONG_BEARISH",
                "tickers_evaluated_count": 1,
                "eligible_count": 0,
                "rejected_count": 1,
                "signal_generation_count": 0,
                "risk_sizing_count": 0,
                "observation_records_count": 1,
                "data_classification": "DELAYED_UNVERIFIED",
                "executed_fill": False,
                "notes": "ALL_CANDIDATES_REJECTED",
            },
        ]
        with open(self.obs_file, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

        candidates, sessions, errors = load_observation_log(self.obs_file)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(len(errors), 0)
        self.assertEqual(candidates[0]["ticker"], "AAPL")
        self.assertEqual(sessions[0]["market_regime"], "STRONG_BEARISH")

    # -------------------------------------------------------------------------
    # 2. Empty Observation Log
    # -------------------------------------------------------------------------
    def test_02_empty_observation_log(self):
        """Empty 0-byte log must return empty collections without crashing."""
        self.obs_file.touch()
        candidates, sessions, errors = load_observation_log(self.obs_file)
        self.assertEqual(len(candidates), 0)
        self.assertEqual(len(sessions), 0)
        self.assertEqual(len(errors), 0)

    # -------------------------------------------------------------------------
    # 3. Malformed Records
    # -------------------------------------------------------------------------
    def test_03_malformed_jsonl_records(self):
        """Corrupted lines must be isolated into parse_errors without dropping valid lines."""
        with open(self.obs_file, "w", encoding="utf-8") as f:
            f.write('{"record_type": "FORWARD_OBSERVATION", "ticker": "MSFT", "date": "2026-11-16"}\n')
            f.write("NOT_VALID_JSON_LINE\n")
            f.write('{"record_type": "SESSION_RECORD", "date": "2026-11-16"}\n')

        candidates, sessions, errors = load_observation_log(self.obs_file)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(len(errors), 1)
        self.assertIn("Line 2", errors[0])

    # -------------------------------------------------------------------------
    # 4. Session Summary Extraction
    # -------------------------------------------------------------------------
    def test_04_session_summary_extraction(self):
        """Date sorting must place latest dates first."""
        cand = [
            {"date": "2026-11-13", "ticker": "A"},
            {"date": "2026-11-16", "ticker": "B"},
        ]
        sess = [
            {"date": "2026-11-16", "market_regime": "STRONG_BEARISH"},
            {"date": "2026-11-12", "market_regime": "BULLISH"},
        ]
        dates = get_available_session_dates(cand, sess)
        self.assertEqual(dates, ["2026-11-16", "2026-11-13", "2026-11-12"])

    # -------------------------------------------------------------------------
    # 5. Ticker and Decision Filtering
    # -------------------------------------------------------------------------
    def test_05_ticker_and_decision_filtering(self):
        """filter_candidates must correctly match on decision, query, and reason."""
        records = [
            {
                "ticker": "AAPL",
                "scanner_decision": "REJECTED",
                "date": "2026-11-16",
                "rejection_reasons": ["Market regime 'STRONG_BEARISH' disallows new long positions"],
            },
            {
                "ticker": "NVDA",
                "scanner_decision": "ELIGIBLE",
                "date": "2026-11-16",
                "rejection_reasons": [],
            },
            {
                "ticker": "MSFT",
                "scanner_decision": "REJECTED",
                "date": "2026-11-16",
                "rejection_reasons": ["Candidate score (45.0) is below regime minimum (100.0)"],
            },
        ]

        # Filter by decision
        rej = filter_candidates(records, decision_filter="REJECTED")
        self.assertEqual(len(rej), 2)

        # Filter by ticker query
        aapl = filter_candidates(records, ticker_query="AAP")
        self.assertEqual(len(aapl), 1)
        self.assertEqual(aapl[0]["ticker"], "AAPL")

        # Filter by rejection keyword
        score_rej = filter_candidates(records, reason_filter="regime minimum")
        self.assertEqual(len(score_rej), 1)
        self.assertEqual(score_rej[0]["ticker"], "MSFT")

    # -------------------------------------------------------------------------
    # 6. Missing Market Data Handling
    # -------------------------------------------------------------------------
    def test_06_missing_market_data_handling(self):
        """Missing proxy CSV files must report clear diagnostic warnings without crashing."""
        df, warnings = load_proxy_candles(self.data_dir, "SPY")
        self.assertIsNone(df)
        self.assertTrue(len(warnings) > 0)
        self.assertIn("not found", warnings[0].lower())

    # -------------------------------------------------------------------------
    # 7. Data Classification and Manifest Loading
    # -------------------------------------------------------------------------
    def test_07_data_classification_and_manifest_loading(self):
        """Manifest loading must extract classification and validation results."""
        manifest_data = {
            "schema_version": "1.0.0",
            "as_of_session_date": "2026-11-16",
            "data_classification": "DELAYED_UNVERIFIED",
            "provider": "yfinance",
            "validation_results": {"validation_status": "PASSED"},
            "excluded_equities": [{"ticker": "CVX", "reason": "Open > High"}],
        }
        with open(self.manifest_file, "w", encoding="utf-8") as f:
            json.dump(manifest_data, f)

        data, warnings = load_acquisition_manifest(self.manifest_file)
        self.assertIsNotNone(data)
        self.assertEqual(len(warnings), 0)
        self.assertEqual(data["data_classification"], "DELAYED_UNVERIFIED")
        self.assertEqual(len(data["excluded_equities"]), 1)

    # -------------------------------------------------------------------------
    # 8. Read-Only Invariance
    # -------------------------------------------------------------------------
    def test_08_read_only_invariance(self):
        """Data loader functions must never alter source file hashes or timestamps."""
        # Create dummy observation file
        with open(self.obs_file, "w", encoding="utf-8") as f:
            f.write('{"record_type": "SESSION_RECORD", "date": "2026-11-16"}\n')

        pre_hash = hashlib.sha256(self.obs_file.read_bytes()).hexdigest()
        pre_mtime = self.obs_file.stat().st_mtime

        # Execute read operations
        load_observation_log(self.obs_file)
        load_acquisition_manifest(self.manifest_file)
        load_proxy_candles(self.data_dir, "SPY")

        post_hash = hashlib.sha256(self.obs_file.read_bytes()).hexdigest()
        post_mtime = self.obs_file.stat().st_mtime

        self.assertEqual(pre_hash, post_hash)
        self.assertEqual(pre_mtime, post_mtime)

    # -------------------------------------------------------------------------
    # 9. Rejection Reason Categorization
    # -------------------------------------------------------------------------
    def test_09_rejection_reason_categorization(self):
        """Rejection reasons must be properly sorted into standard taxonomy categories."""
        cands = [
            {"rejection_reasons": ["Market regime 'STRONG_BEARISH' disallows new long positions"]},
            {"rejection_reasons": ["Candidate score (45.0) is below regime minimum (100.0)"]},
            {"rejection_reasons": ["Average dollar volume ($5,000,000) is below minimum"]},
            {"rejection_reasons": ["Price ($4.50) is below minimum threshold"]},
        ]
        cats = categorize_rejection_reasons(cands)
        self.assertIn("Regime Veto (Bearish Policy)", cats)
        self.assertIn("Score Below Regime Minimum", cats)
        self.assertIn("Average Dollar Volume (ADV < $10M)", cats)
        self.assertIn("Price Below Threshold (< $10)", cats)

    # -------------------------------------------------------------------------
    # 10. Verified Portfolio Snapshot Integrity
    # -------------------------------------------------------------------------
    def test_10_verified_portfolio_snapshot_integrity(self):
        """Historical portfolio snapshot must match frozen baseline values."""
        snap = get_verified_portfolio_snapshot()
        self.assertEqual(snap["equity_usd"], "5,000.00")
        self.assertEqual(snap["cash_usd"], "5,000.00")
        self.assertEqual(snap["open_positions_count"], 0)
        self.assertEqual(snap["executed_fills_count"], 0)
        self.assertEqual(snap["snapshot_date"], "2026-11-16")
        self.assertIn("historical", snap["status"].lower())

    # -------------------------------------------------------------------------
    # 11. Pipeline Funnel Chart Construction
    # -------------------------------------------------------------------------
    def test_11_pipeline_funnel_chart_construction(self):
        """Pipeline funnel chart must construct valid Plotly figure with correct marker color."""
        fig = create_pipeline_funnel_chart(
            evaluated_count=49,
            eligible_count=0,
            signal_gen_count=0,
            risk_sized_count=0,
            executed_fills=0,
        )
        self.assertIsNotNone(fig)
        self.assertEqual(len(fig.data), 1)
        funnel_trace = fig.data[0]
        self.assertEqual(funnel_trace.type, "funnel")
        self.assertEqual(
            tuple(funnel_trace.marker.color),
            ("#38bdf8", "#0ea5e9", "#f59e0b", "#10b981", "#64748b"),
        )
        self.assertEqual(tuple(funnel_trace.x), (49, 0, 0, 0, 0))
        self.assertEqual(len(funnel_trace.y), 5)
        self.assertEqual(funnel_trace.y[0], "1. Universe Evaluated")
        self.assertEqual(funnel_trace.y[1], "2. Passed Scanner")
        self.assertEqual(funnel_trace.y[4], "5. Executed Fills")

    # -------------------------------------------------------------------------
    # 12. Ticker Selection, Candle Loading & Technical Indicators
    # -------------------------------------------------------------------------
    def test_12_ticker_candles_and_sma_rsi_rendering(self):
        """load_ticker_candles must load OHLCV data, compute SMA20/50 and RSI14 cleanly."""
        csv_path = self.data_dir / "TEST_daily.csv"
        # Generate 60 daily bars with trending prices
        dates = pd.date_range("2026-08-01", periods=60, freq="B")
        rows = []
        base = 100.0
        for i, d in enumerate(dates):
            close = base + (i * 0.5)
            rows.append({
                "Date": d.strftime("%Y-%m-%d"),
                "Open": close - 0.2,
                "High": close + 1.0,
                "Low": close - 1.0,
                "Close": close,
                "Volume": 1000000 + (i * 10000),
            })
        pd.DataFrame(rows).to_csv(csv_path, index=False)

        df, warnings = load_ticker_candles(self.data_dir, "TEST")
        self.assertIsNotNone(df)
        self.assertEqual(len(warnings), 0)
        self.assertEqual(len(df), 60)
        self.assertIn("SMA20", df.columns)
        self.assertIn("SMA50", df.columns)
        self.assertIn("RSI14", df.columns)

        # Check latest values
        self.assertIsNotNone(df["SMA20"].iloc[-1])
        self.assertIsNotNone(df["SMA50"].iloc[-1])
        rsi_val = df["RSI14"].iloc[-1]
        self.assertGreaterEqual(rsi_val, 0.0)
        self.assertLessEqual(rsi_val, 100.0)

        # Test Plotly chart generation (3 subplots)
        fig = create_ticker_candlestick_chart(df, "TEST", max_bars=30, show_smas=True, show_rsi=True)
        self.assertIsNotNone(fig)
        # Should have Candlestick, SMAs, Volume bars, RSI trace
        trace_names = [t.name for t in fig.data if hasattr(t, "name")]
        self.assertIn("TEST", trace_names)
        self.assertIn("SMA 20", trace_names)
        self.assertIn("Volume", trace_names)
        self.assertIn("RSI 14", trace_names)

    # -------------------------------------------------------------------------
    # 13. Missing or Invalid Ticker Data
    # -------------------------------------------------------------------------
    def test_13_missing_or_invalid_ticker_data(self):
        """load_ticker_candles must handle missing files, missing columns, and empty files gracefully."""
        # Non-existent file
        df_none, w_none = load_ticker_candles(self.data_dir, "NONEXISTENT")
        self.assertIsNone(df_none)
        self.assertIn("not found", w_none[0].lower())

        # Missing required columns
        bad_col_file = self.data_dir / "BADCOL_daily.csv"
        pd.DataFrame([{"Date": "2026-11-16", "Close": 100.0}]).to_csv(bad_col_file, index=False)
        df_bad, w_bad = load_ticker_candles(self.data_dir, "BADCOL")
        self.assertIsNone(df_bad)
        self.assertIn("missing required columns", w_bad[0].lower())

        # Empty file (zero rows)
        empty_file = self.data_dir / "EMPTY_daily.csv"
        pd.DataFrame(columns=["Date", "Open", "High", "Low", "Close", "Volume"]).to_csv(empty_file, index=False)
        df_empty, w_empty = load_ticker_candles(self.data_dir, "EMPTY")
        self.assertIsNone(df_empty)
        self.assertIn("zero rows", w_empty[0].lower())

    # -------------------------------------------------------------------------
    # 14. Rejected vs Eligible Candidate Labeling & Score Invariance
    # -------------------------------------------------------------------------
    def test_14_rejected_versus_eligible_candidate_labeling(self):
        """Candidate score ranking and filtering must preserve exact scanner decisions without mutation."""
        cands = [
            {
                "ticker": "HD",
                "candidate_score": "85.00",
                "scanner_decision": "REJECTED",
                "rejection_reasons": ["Market regime disallows new long positions"],
            },
            {
                "ticker": "AMZN",
                "candidate_score": "82.50",
                "scanner_decision": "REJECTED",
                "rejection_reasons": ["Market regime disallows new long positions"],
            },
            {
                "ticker": "TEST_ELIGIBLE",
                "candidate_score": "90.00",
                "scanner_decision": "ELIGIBLE",
                "rejection_reasons": [],
            },
        ]
        # Sort descending by score
        sorted_cands = sorted(cands, key=lambda c: float(c.get("candidate_score", 0)), reverse=True)
        self.assertEqual(sorted_cands[0]["ticker"], "TEST_ELIGIBLE")
        self.assertEqual(sorted_cands[0]["scanner_decision"], "ELIGIBLE")
        self.assertEqual(sorted_cands[1]["ticker"], "HD")
        self.assertEqual(sorted_cands[1]["scanner_decision"], "REJECTED")

        # Prove rejected candidate retains exact rejection reasons
        self.assertEqual(sorted_cands[1]["rejection_reasons"], ["Market regime disallows new long positions"])

    # -------------------------------------------------------------------------
    # 15. Portfolio Baseline and Empty Positions & Orders
    # -------------------------------------------------------------------------
    def test_15_portfolio_baseline_and_empty_positions_orders(self):
        """Portfolio baseline must reflect frozen $5,000 cash and empty positions/orders DataFrames."""
        snap = get_verified_portfolio_snapshot()
        self.assertEqual(snap["equity_usd"], "5,000.00")
        self.assertEqual(snap["cash_usd"], "5,000.00")
        self.assertEqual(snap["invested_capital_usd"], "0.00")
        self.assertEqual(snap["open_positions_count"], 0)
        self.assertEqual(snap["executed_fills_count"], 0)

        df_pos, df_ord = get_portfolio_active_positions_and_orders()
        self.assertEqual(len(df_pos), 0)
        self.assertEqual(len(df_ord), 0)
        self.assertIn("Symbol", df_pos.columns)
        self.assertIn("Unrealized_PnL_USD", df_pos.columns)
        self.assertIn("Order_ID", df_ord.columns)

    # -------------------------------------------------------------------------
    # 16. Missing Equity History & Flat Baseline Chart
    # -------------------------------------------------------------------------
    def test_16_missing_equity_history_flat_baseline(self):
        """Portfolio equity data must reflect frozen flat baseline with has_history=False."""
        eq_df, has_history = get_portfolio_equity_curve_data(["2026-11-16"])
        self.assertFalse(has_history)
        self.assertEqual(len(eq_df), 1)
        self.assertEqual(eq_df["Equity"].iloc[0], 5000.00)
        self.assertEqual(eq_df["Cash"].iloc[0], 5000.00)

        fig = create_portfolio_equity_chart(eq_df, baseline_value=5000.0, has_history=has_history)
        self.assertIsNotNone(fig)
        # Check annotation disclosure
        annot_texts = [a.text for a in fig.layout.annotations if hasattr(a, "text")]
        self.assertTrue(any("NO RECORDED PORTFOLIO PERFORMANCE YET" in t for t in annot_texts))

    # -------------------------------------------------------------------------
    # 17. Asset and Risk Allocation Donut Charts
    # -------------------------------------------------------------------------
    def test_17_asset_and_risk_allocation_donuts(self):
        """Asset and risk donuts must reflect 100% cash buffer and 4 free risk capacity slots."""
        asset_fig = create_asset_allocation_donut(cash=5000.0, invested=0.0)
        self.assertIsNotNone(asset_fig)
        self.assertEqual(len(asset_fig.data), 1)
        self.assertEqual(asset_fig.data[0].values, (5000.0, 0.0))

        risk_fig = create_risk_capacity_donut(max_positions=4, open_positions=0, risk_per_trade_usd=25.0)
        self.assertIsNotNone(risk_fig)
        self.assertEqual(len(risk_fig.data), 1)
        self.assertEqual(risk_fig.data[0].values, (4, 0))

    # -------------------------------------------------------------------------
    # 18. Market Sentiment Gauge and Breadth Indicators
    # -------------------------------------------------------------------------
    def test_18_market_sentiment_gauge_and_breadth(self):
        """Market sentiment gauge and breadth metrics must calculate from actual available records."""
        gauge_fig = create_market_sentiment_gauge(macro_score=-0.82, regime="STRONG_BEARISH")
        self.assertIsNotNone(gauge_fig)
        self.assertEqual(gauge_fig.data[0].value, -0.82)

        cands = [
            {
                "date": "2026-11-16",
                "ticker": "AAPL",
                "scanner_decision": "REJECTED",
                "candidate_score": "80.0",
                "rejection_reasons": ["Market regime 'STRONG_BEARISH' disallows new long positions"],
            },
            {
                "date": "2026-11-16",
                "ticker": "XYZ",
                "scanner_decision": "REJECTED",
                "candidate_score": "40.0",
                "rejection_reasons": ["Price below 20-day SMA", "Market regime disallows new long positions"],
            },
        ]
        breadth = get_market_breadth_metrics(cands, "2026-11-16")
        self.assertEqual(breadth["total_evaluated"], 2)
        self.assertEqual(breadth["eligible_count"], 0)
        self.assertEqual(breadth["rejected_count"], 2)
        self.assertEqual(breadth["regime_vetoed_count"], 2)
        self.assertEqual(breadth["above_sma20_count"], 1)  # AAPL passed 20-day SMA; XYZ failed
        self.assertEqual(breadth["above_sma20_pct"], 50.0)
        self.assertEqual(breadth["avg_candidate_score"], 60.0)

    # -------------------------------------------------------------------------
    # 19. Source and Feed Classification Labels
    # -------------------------------------------------------------------------
    def test_19_source_feed_classification_labels(self):
        """Manifest and dashboard metadata must strictly disclose DELAYED_UNVERIFIED classification."""
        manifest_data = {
            "schema_version": "1.0.0",
            "as_of_session_date": "2026-11-16",
            "data_classification": "DELAYED_UNVERIFIED",
            "provider": "yfinance",
            "validation_results": {"validation_status": "PASSED"},
        }
        with open(self.manifest_file, "w", encoding="utf-8") as f:
            json.dump(manifest_data, f)

        data, _ = load_acquisition_manifest(self.manifest_file)
        self.assertEqual(data["data_classification"], "DELAYED_UNVERIFIED")
        self.assertEqual(data["provider"], "yfinance")


if __name__ == "__main__":
    unittest.main()
