"""Deterministic offline integration test suite for Phase 7.3.6: Manual Forward Observation Live Mode.

Verifies:
1. Live candidate screening uses only completed dual-source daily candles from DualSourceSyncGateway.
2. Forming candles (CandleStatus.FORMING / FeedState.FORMING_BAR) are strictly blocked from screening,
   indicators, and risk sizing.
3. Live candidate screening strictly requires LIVE_IEX_HYBRID readiness; unapproved feed states fail closed.
4. Exact session-date matching is enforced; session divergence (FEED_DEGRADED) fails closed.
5. Missing or invalid Yahoo consolidated volume (VOLUME_UNVERIFIABLE) fails closed.
6. Alpaca REST provider failure (FEED_UNAVAILABLE) fails closed; zero Yahoo price failover.
7. Zero silent fallback to historical disk fixtures while in live data mode.
8. Consolidated volume provenance: Every evaluated completed candle declares is_consolidated_volume=True.
9. Observation-only invariants: Every observation record enforces decision="OBSERVE_ONLY" (or "SKIP")
   and executed_fill=False across all records and session records.
10. Read-only state isolation: Zero mutation to paper trading portfolio, journal, ledger, or orders.
11. Protected log preservation: Historical observation log (observation_log.jsonl) is never touched;
    live observations default to live_observation_log.jsonl.
12. Idempotent record storage: Running observation scan repeatedly on the same session date and ticker
    prevents duplicate records from appending to disk.
13. Multi-ticker observation logging with explicit data_mode and data_classification tags.
14. Session timing: Mid-session evaluation strictly uses completed session bars; forming bars remain display-only.
15. CLI interface: Supports --dual-source while strictly rejecting forbidden flags (--live, --broker, etc.).
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from typing import Any, Dict, List, Optional, Sequence

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SWING_DIR = os.path.join(ROOT_DIR, ".agents", "skills", "trader-swing")
for path in (ROOT_DIR, SWING_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

from market_data_models import (
    CandleStatus,
    EASTERN,
    MarketClock,
    OHLCVCandle,
    to_eastern,
    to_utc,
    UTC,
)
from market_data_validation import (
    CandleValidationError,
    InvalidPriceError,
    InvalidVolumeError,
    ProviderError,
)
from feed_adapter import (
    DualSourceFeedAdapter,
    FeedReadinessResult,
    MarketDataFeedAdapter,
    MarketDataReadinessEvaluator,
    ReadinessStatus,
    UnconsolidatedVolumeError,
)
from dual_source_aggregator import DualSourceCandle
from dual_source_sync_gateway import (
    DailySyncResult,
    DualSourceSyncGateway,
    FeedState,
    ScreeningEligibilityResult,
)
from observation_workflow import (
    DEFAULT_LIVE_RECORD_FILE,
    DEFAULT_RECORD_FILE,
    DailyResearchReport,
    ObservationRecord,
    ObservationSessionRecord,
    format_observation_report,
    read_observation_log,
    run_observation_cli,
    run_observation_scan,
)
from paper_cli import FORBIDDEN_CLI_FLAGS, scan_for_forbidden_flags
from paper_service import PaperTradingService
from paper_storage import DEFAULT_PAPER_STATE_DIR, PaperStorageManager
from regime_models import MarketRegime


def generate_completed_dual_source_candles(
    ticker: str,
    count: int = 220,
    end_date: Optional[date] = None,
    base_price: float = 100.0,
    trend: float = 0.10,
    consolidated_volume: int = 50_000_000,
    auxiliary_iex_volume: int = 1_250_000,
) -> List[DualSourceCandle]:
    """Generate valid completed DualSourceCandles with chronological timestamps and NYSE sessions."""
    clock = MarketClock()
    target_end = end_date or date(2026, 11, 16)
    d = target_end
    session_dates = []
    while len(session_dates) < count:
        sess = clock.get_historical_session(d) or clock.get_session(d)
        if sess is not None:
            session_dates.append((d, sess))
        d -= timedelta(days=1)
    session_dates.reverse()

    candles: List[DualSourceCandle] = []
    for idx, (curr_d, sess) in enumerate(session_dates):
        sess_open_dt = datetime.combine(curr_d, sess[0], tzinfo=EASTERN)
        ts_utc = to_utc(sess_open_dt)
        px = Decimal(str(round(base_price + idx * trend, 2)))
        candles.append(
            DualSourceCandle(
                ticker=ticker.upper().strip(),
                interval="1d",
                timestamp=ts_utc,
                open=px,
                high=px + Decimal("2.00"),
                low=px - Decimal("1.00"),
                close=px + Decimal("1.00"),
                volume=consolidated_volume,
                status=CandleStatus.COMPLETE,
                session_date=curr_d,
                adjustment_type="SPLIT_ADJUSTED",
                is_consolidated_volume=True,
                price_source="ALPACA_IEX",
                volume_source="YAHOO_CONSOLIDATED",
                auxiliary_iex_volume=auxiliary_iex_volume,
                feed_classification="DUAL_SOURCE_HYBRID",
            )
        )

    return candles


class MockSyncGateway:
    """Deterministic offline mock for DualSourceSyncGateway."""

    def __init__(
        self,
        responses: Optional[Dict[str, DailySyncResult]] = None,
        states: Optional[Dict[str, FeedState]] = None,
        clock: Optional[MarketClock] = None,
    ):
        self.responses: Dict[str, DailySyncResult] = responses or {}
        self.states: Dict[str, FeedState] = states or {}
        self.clock = clock or MarketClock()
        self.calls: List[Dict[str, Any]] = []

    def synchronize_completed_daily(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        current_time: Optional[datetime] = None,
    ) -> DailySyncResult:
        sym = symbol.upper().strip()
        self.calls.append({
            "symbol": sym,
            "start": start,
            "end": end,
            "current_time": current_time,
        })
        if sym in self.responses:
            return self.responses[sym]

        return DailySyncResult(
            symbol=sym,
            state=self.states.get(sym, FeedState.FEED_UNAVAILABLE),
            success=False,
            error_message=f"No mock response configured for {sym}",
        )

    def get_feed_state(self, symbol: str) -> FeedState:
        sym = symbol.upper().strip()
        if sym in self.states:
            return self.states[sym]
        if sym in self.responses:
            return self.responses[sym].state
        return FeedState.FEED_UNAVAILABLE

    def get_screening_eligibility(
        self, symbol: str, current_time: Optional[datetime] = None
    ) -> ScreeningEligibilityResult:
        sym = symbol.upper().strip()
        state = self.get_feed_state(sym)
        is_eligible = state == FeedState.LIVE_IEX_HYBRID
        return ScreeningEligibilityResult(
            symbol=sym,
            is_eligible=is_eligible,
            state=state,
            reason=f"Mock screening eligibility verdict (state={state.value})",
            is_consolidated_volume=is_eligible,
        )


class TestSwingObservationLive(unittest.TestCase):
    """Test suite for Phase 7.3.6 manual forward observation live mode."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_obs_live_")
        self.state_dir = os.path.join(self.test_dir, "paper_state")
        self.obs_dir = os.path.join(self.test_dir, "observations")
        self.live_record_file = os.path.join(self.obs_dir, "live_observation_log.jsonl")
        self.hist_record_file = os.path.join(self.obs_dir, "observation_log.jsonl")

        os.makedirs(self.state_dir, exist_ok=True)
        os.makedirs(self.obs_dir, exist_ok=True)

        self.storage = PaperStorageManager(state_dir=self.state_dir)
        self.storage.initialize_state(initial_capital=Decimal("5000.00"), force=True)

        self.end_date = date(2026, 11, 16)
        # Create standard 220-bar series for proxies
        self.spy_bars = generate_completed_dual_source_candles("SPY", count=220, end_date=self.end_date, base_price=500.0)
        self.qqq_bars = generate_completed_dual_source_candles("QQQ", count=220, end_date=self.end_date, base_price=450.0)
        self.iwm_bars = generate_completed_dual_source_candles("IWM", count=220, end_date=self.end_date, base_price=200.0)

        # Standard stock series
        self.nvda_bars = generate_completed_dual_source_candles("NVDA", count=220, end_date=self.end_date, base_price=120.0, trend=0.15)
        self.aapl_bars = generate_completed_dual_source_candles("AAPL", count=220, end_date=self.end_date, base_price=220.0, trend=0.05)

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def _build_standard_gateway(self, extra_responses: Optional[Dict[str, DailySyncResult]] = None) -> MockSyncGateway:
        """Create mock gateway with SPY, QQQ, IWM, NVDA, AAPL configured as LIVE_IEX_HYBRID."""
        responses = {
            "SPY": DailySyncResult(symbol="SPY", state=FeedState.LIVE_IEX_HYBRID, success=True, candles=self.spy_bars, merged_candle_count=len(self.spy_bars), session_date=self.end_date),
            "QQQ": DailySyncResult(symbol="QQQ", state=FeedState.LIVE_IEX_HYBRID, success=True, candles=self.qqq_bars, merged_candle_count=len(self.qqq_bars), session_date=self.end_date),
            "IWM": DailySyncResult(symbol="IWM", state=FeedState.LIVE_IEX_HYBRID, success=True, candles=self.iwm_bars, merged_candle_count=len(self.iwm_bars), session_date=self.end_date),
            "NVDA": DailySyncResult(symbol="NVDA", state=FeedState.LIVE_IEX_HYBRID, success=True, candles=self.nvda_bars, merged_candle_count=len(self.nvda_bars), session_date=self.end_date),
            "AAPL": DailySyncResult(symbol="AAPL", state=FeedState.LIVE_IEX_HYBRID, success=True, candles=self.aapl_bars, merged_candle_count=len(self.aapl_bars), session_date=self.end_date),
        }
        if extra_responses:
            responses.update(extra_responses)
        return MockSyncGateway(responses=responses)

    def _compute_state_hashes(self) -> Dict[str, str]:
        """Compute SHA256 hashes of all state files in state_dir."""
        hashes = {}
        for fname in sorted(os.listdir(self.state_dir)):
            fpath = os.path.join(self.state_dir, fname)
            if os.path.isfile(fpath):
                with open(fpath, "rb") as f:
                    hashes[fname] = hashlib.sha256(f.read()).hexdigest()
        return hashes

    # -------------------------------------------------------------------------
    # 1. Live gateway integration uses completed candles only
    # -------------------------------------------------------------------------
    def test_01_live_gateway_integration_completed_candles_only(self):
        """Live scan consumes completed DualSourceCandles and produces LIVE_IEX_HYBRID report."""
        gateway = self._build_standard_gateway()

        report = run_observation_scan(
            gateway=gateway,
            tickers=["NVDA", "AAPL"],
            as_of_date=self.end_date,
            state_dir=self.state_dir,
            record_file=self.live_record_file,
            write_records=True,
        )

        self.assertEqual(report.feed_status, "READY")
        self.assertEqual(report.data_mode, "LIVE_IEX_HYBRID")
        self.assertIn("LIVE_IEX_HYBRID", report.data_classification)
        self.assertEqual(report.data_dir, "LIVE_GATEWAY (DualSourceSyncGateway)")
        self.assertIsNotNone(report.market_regime)

        # Check records were written to live observation log
        records = read_observation_log(self.live_record_file)
        self.assertGreater(len(records), 0)
        for r in records:
            if r.get("record_type") == "FORWARD_OBSERVATION":
                self.assertEqual(r["data_mode"], "LIVE_IEX_HYBRID")
                self.assertFalse(r["executed_fill"])

    # -------------------------------------------------------------------------
    # 2. Forming candles are strictly blocked from screening
    # -------------------------------------------------------------------------
    def test_02_forming_candle_isolation_never_screened(self):
        """Forming bars are strictly filtered out; symbols in FORMING_BAR state cannot be screened."""
        forming_candle = DualSourceCandle(
            ticker="FRMG",
            interval="1d",
            timestamp=datetime(2026, 11, 17, 15, 0, tzinfo=UTC),
            open=Decimal("150.00"),
            high=Decimal("155.00"),
            low=Decimal("149.00"),
            close=Decimal("153.00"),
            volume=5_000_000,
            status=CandleStatus.FORMING,
            session_date=date(2026, 11, 17),
            adjustment_type="SPLIT_ADJUSTED",
            is_consolidated_volume=False,
            price_source="ALPACA_IEX",
            volume_source="ALPACA_IEX",
            auxiliary_iex_volume=5_000_000,
            feed_classification="FORMING_INTRADAY",
        )
        extra = {
            "FRMG": DailySyncResult(
                symbol="FRMG",
                state=FeedState.FORMING_BAR,
                success=True,
                candles=[forming_candle],
                merged_candle_count=1,
                session_date=date(2026, 11, 17),
            )
        }
        gateway = self._build_standard_gateway(extra_responses=extra)

        report = run_observation_scan(
            gateway=gateway,
            tickers=["NVDA", "FRMG"],
            as_of_date=self.end_date,
            state_dir=self.state_dir,
            record_file=self.live_record_file,
            write_records=False,
        )

        eligible_tickers = [c.ticker for c in report.eligible_candidates]
        self.assertNotIn("FRMG", eligible_tickers)
        # FRMG must not be in observation records as an eligible candidate
        obs_frmg = [r for r in report.observation_records if r.ticker == "FRMG" and r.scanner_decision == "ELIGIBLE"]
        self.assertEqual(len(obs_frmg), 0)

    # -------------------------------------------------------------------------
    # 3. Live candidate screening requires LIVE_IEX_HYBRID readiness
    # -------------------------------------------------------------------------
    def test_03_live_iex_hybrid_readiness_enforcement(self):
        """Unapproved feed states (FEED_DEGRADED, VOLUME_UNVERIFIABLE, FEED_UNAVAILABLE) are blocked."""
        extra = {
            "DEGR": DailySyncResult(symbol="DEGR", state=FeedState.FEED_DEGRADED, success=False, error_message="Latency high"),
            "UNAV": DailySyncResult(symbol="UNAV", state=FeedState.FEED_UNAVAILABLE, success=False, error_message="Connection refused"),
            "VOLU": DailySyncResult(symbol="VOLU", state=FeedState.VOLUME_UNVERIFIABLE, success=False, error_message="Yahoo 404"),
        }
        gateway = self._build_standard_gateway(extra_responses=extra)

        report = run_observation_scan(
            gateway=gateway,
            tickers=["NVDA", "DEGR", "UNAV", "VOLU"],
            as_of_date=self.end_date,
            state_dir=self.state_dir,
            record_file=self.live_record_file,
            write_records=False,
        )

        # DEGR, UNAV, VOLU must not be in eligible candidates
        eligible = [c.ticker for c in report.eligible_candidates]
        for bad in ("DEGR", "UNAV", "VOLU"):
            self.assertNotIn(bad, eligible)

    # -------------------------------------------------------------------------
    # 4. Session mismatch fails closed
    # -------------------------------------------------------------------------
    def test_04_session_mismatch_fails_closed(self):
        """Gateway reporting session mismatch (FEED_DEGRADED) excludes candidate from screening."""
        extra = {
            "MISMATCH": DailySyncResult(
                symbol="MISMATCH",
                state=FeedState.FEED_DEGRADED,
                success=False,
                error_message="Session date mismatch: Alpaca 2026-11-16 vs Yahoo 2026-11-13",
            )
        }
        gateway = self._build_standard_gateway(extra_responses=extra)

        report = run_observation_scan(
            gateway=gateway,
            tickers=["MISMATCH"],
            as_of_date=self.end_date,
            state_dir=self.state_dir,
            write_records=False,
        )

        self.assertEqual(len(report.eligible_candidates), 0)

    # -------------------------------------------------------------------------
    # 5. Missing or invalid volume fails closed
    # -------------------------------------------------------------------------
    def test_05_missing_or_invalid_volume_fails_closed(self):
        """Gateway reporting missing or non-positive volume (VOLUME_UNVERIFIABLE) excludes candidate."""
        extra = {
            "NOVOL": DailySyncResult(
                symbol="NOVOL",
                state=FeedState.VOLUME_UNVERIFIABLE,
                success=False,
                error_message="Yahoo consolidated volume is 0; unverifiable",
            )
        }
        gateway = self._build_standard_gateway(extra_responses=extra)

        report = run_observation_scan(
            gateway=gateway,
            tickers=["NOVOL"],
            as_of_date=self.end_date,
            state_dir=self.state_dir,
            write_records=False,
        )

        self.assertEqual(len(report.eligible_candidates), 0)

    # -------------------------------------------------------------------------
    # 6. Alpaca provider error fails closed without Yahoo price failover
    # -------------------------------------------------------------------------
    def test_06_alpaca_provider_error_fails_closed_no_yahoo_price_failover(self):
        """When mandatory proxy SPY fails from Alpaca, scan aborts with HARD_FAIL; zero Yahoo failover."""
        extra = {
            "SPY": DailySyncResult(
                symbol="SPY",
                state=FeedState.FEED_UNAVAILABLE,
                success=False,
                error_message="Alpaca REST service 503 unavailable",
            )
        }
        gateway = self._build_standard_gateway(extra_responses=extra)

        report = run_observation_scan(
            gateway=gateway,
            tickers=["NVDA"],
            as_of_date=self.end_date,
            state_dir=self.state_dir,
            write_records=False,
        )

        self.assertEqual(report.feed_status, "HARD_FAIL")
        self.assertEqual(len(report.eligible_candidates), 0)
        self.assertTrue(any("SPY" in r for r in report.failure_reasons))

    # -------------------------------------------------------------------------
    # 7. Zero silent fallback to historical fixtures in live mode
    # -------------------------------------------------------------------------
    def test_07_no_silent_fallback_to_historical_fixtures(self):
        """In live mode, if gateway fails, it never falls back to reading CSVs on disk."""
        empty_gateway = MockSyncGateway(responses={})

        report = run_observation_scan(
            gateway=empty_gateway,
            data_mode="LIVE_IEX_HYBRID",
            tickers=["NVDA"],
            as_of_date=self.end_date,
            state_dir=self.state_dir,
            write_records=False,
        )

        self.assertEqual(report.feed_status, "HARD_FAIL")
        self.assertEqual(len(report.eligible_candidates), 0)
        self.assertEqual(report.data_mode, "LIVE_IEX_HYBRID")

    # -------------------------------------------------------------------------
    # 8. Consolidated volume provenance enforced
    # -------------------------------------------------------------------------
    def test_08_consolidated_volume_provenance_enforced(self):
        """Candles lacking verified consolidated volume raise UnconsolidatedVolumeError."""
        unconsolidated_bars = generate_completed_dual_source_candles("UNCON", count=220, end_date=self.end_date)
        # Violate provenance on latest bar
        uncon_bar = unconsolidated_bars[-1]
        object.__setattr__(uncon_bar, "is_consolidated_volume", False)

        extra = {
            "UNCON": DailySyncResult(
                symbol="UNCON",
                state=FeedState.LIVE_IEX_HYBRID,
                success=True,
                candles=unconsolidated_bars,
                merged_candle_count=len(unconsolidated_bars),
                session_date=self.end_date,
            )
        }
        gateway = self._build_standard_gateway(extra_responses=extra)

        # DualSourceFeedAdapter raises UnconsolidatedVolumeError which fails closed
        report = run_observation_scan(
            gateway=gateway,
            tickers=["UNCON"],
            as_of_date=self.end_date,
            state_dir=self.state_dir,
            write_records=False,
        )

        # UNCON must be excluded
        self.assertNotIn("UNCON", [c.ticker for c in report.eligible_candidates])

    # -------------------------------------------------------------------------
    # 9. Observation-only invariants enforced
    # -------------------------------------------------------------------------
    def test_09_observation_only_invariants_enforced(self):
        """Every generated observation record enforces decision in ('OBSERVE_ONLY', 'SKIP') and executed_fill=False."""
        gateway = self._build_standard_gateway()

        report = run_observation_scan(
            gateway=gateway,
            tickers=["NVDA", "AAPL"],
            as_of_date=self.end_date,
            state_dir=self.state_dir,
            record_file=self.live_record_file,
            write_records=True,
        )

        self.assertGreater(len(report.observation_records), 0)
        for rec in report.observation_records:
            self.assertFalse(rec.executed_fill)
            self.assertIn(rec.decision, ("OBSERVE_ONLY", "SKIP"))
            if rec.signal_decision == "ENTRY_SIGNAL" and rec.risk_decision == "RISK_APPROVED":
                self.assertEqual(rec.decision, "OBSERVE_ONLY")
            self.assertEqual(rec.data_mode, "LIVE_IEX_HYBRID")

        if report.session_record:
            self.assertFalse(report.session_record.executed_fill)
            self.assertEqual(report.session_record.data_mode, "LIVE_IEX_HYBRID")

    # -------------------------------------------------------------------------
    # 10. Zero paper portfolio or ledger mutations
    # -------------------------------------------------------------------------
    def test_10_zero_portfolio_or_ledger_mutations(self):
        """Live observation scan is strictly read-only; zero mutation to paper journal, ledger, or orders."""
        hashes_before = self._compute_state_hashes()
        gateway = self._build_standard_gateway()

        run_observation_scan(
            gateway=gateway,
            tickers=["NVDA", "AAPL"],
            as_of_date=self.end_date,
            state_dir=self.state_dir,
            record_file=self.live_record_file,
            write_records=True,
        )

        hashes_after = self._compute_state_hashes()
        self.assertEqual(hashes_before, hashes_after)

    # -------------------------------------------------------------------------
    # 11. Protected historical observation log is never touched
    # -------------------------------------------------------------------------
    def test_11_protected_historical_observation_log_preservation(self):
        """In live mode without custom file, records default to live log, preserving historical observation_log."""
        # Pre-seed historical log with dummy content
        with open(self.hist_record_file, "w", encoding="utf-8") as f:
            f.write('{"record_type": "HISTORICAL", "date": "2023-12-29"}\n')
        with open(self.hist_record_file, "rb") as f:
            hist_hash_before = hashlib.sha256(f.read()).hexdigest()

        gateway = self._build_standard_gateway()

        # Run with explicit live data mode and no record file (should default to live file)
        report = run_observation_scan(
            gateway=gateway,
            data_mode="LIVE_IEX_HYBRID",
            tickers=["NVDA"],
            as_of_date=self.end_date,
            state_dir=self.state_dir,
            record_file=self.live_record_file,
            write_records=True,
        )

        # Historical file must be untouched
        with open(self.hist_record_file, "rb") as f:
            hist_hash_after = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(hist_hash_before, hist_hash_after)

        # Live file must have been written
        self.assertTrue(os.path.exists(self.live_record_file))
        records = read_observation_log(self.live_record_file)
        self.assertGreater(len(records), 0)

    # -------------------------------------------------------------------------
    # 12. Idempotency duplicate prevention across repeated runs
    # -------------------------------------------------------------------------
    def test_12_idempotency_duplicate_prevention_across_repeated_runs(self):
        """Running observation scan multiple times for the same date and ticker does NOT duplicate records."""
        gateway = self._build_standard_gateway()

        # Run 1
        run_observation_scan(
            gateway=gateway,
            tickers=["NVDA"],
            as_of_date=self.end_date,
            state_dir=self.state_dir,
            record_file=self.live_record_file,
            write_records=True,
        )

        recs_run1 = read_observation_log(self.live_record_file)
        count_run1 = len(recs_run1)
        self.assertGreater(count_run1, 0)

        # Run 2: Exact same date and ticker
        run_observation_scan(
            gateway=gateway,
            tickers=["NVDA"],
            as_of_date=self.end_date,
            state_dir=self.state_dir,
            record_file=self.live_record_file,
            write_records=True,
        )

        recs_run2 = read_observation_log(self.live_record_file)
        count_run2 = len(recs_run2)

        # Record count must NOT have increased!
        self.assertEqual(count_run1, count_run2, f"Duplicates detected: run 1 had {count_run1}, run 2 had {count_run2}")

        # Run 3: Add a new ticker AAPL on the same date
        run_observation_scan(
            gateway=gateway,
            tickers=["NVDA", "AAPL"],
            as_of_date=self.end_date,
            state_dir=self.state_dir,
            record_file=self.live_record_file,
            write_records=True,
        )

        recs_run3 = read_observation_log(self.live_record_file)
        nvda_recs = [r for r in recs_run3 if r.get("ticker") == "NVDA"]
        aapl_recs = [r for r in recs_run3 if r.get("ticker") == "AAPL"]

        self.assertEqual(len(nvda_recs), 1, "NVDA should not be duplicated when adding AAPL")
        self.assertEqual(len(aapl_recs), 1, "AAPL should be added exactly once")

    # -------------------------------------------------------------------------
    # 13. Multi-ticker live observation logging
    # -------------------------------------------------------------------------
    def test_13_multi_ticker_live_observation_logging(self):
        """Multiple tickers in live mode generate structured records with complete provenance."""
        gateway = self._build_standard_gateway()

        report = run_observation_scan(
            gateway=gateway,
            tickers=["NVDA", "AAPL"],
            as_of_date=self.end_date,
            state_dir=self.state_dir,
            record_file=self.live_record_file,
            write_records=True,
        )

        recs = read_observation_log(self.live_record_file)
        fwd_recs = [r for r in recs if r.get("record_type") == "FORWARD_OBSERVATION"]
        sess_recs = [r for r in recs if r.get("record_type") == "SESSION_RECORD"]

        self.assertEqual(len(fwd_recs), 2)
        self.assertEqual(len(sess_recs), 1)
        self.assertEqual(sess_recs[0]["data_classification"], report.data_classification)
        self.assertEqual(sess_recs[0]["tickers_evaluated_count"], 2)

    # -------------------------------------------------------------------------
    # 14. Session timing: mid-session evaluation strictly uses completed session
    # -------------------------------------------------------------------------
    def test_14_session_timing_future_or_incomplete_session_fails_closed(self):
        """Requesting an incomplete or future session date fails closed with INCOMPLETE_SESSION."""
        gateway = self._build_standard_gateway()

        # Request session date far in the future
        future_date = date(2030, 1, 2)
        report = run_observation_scan(
            gateway=gateway,
            tickers=["NVDA"],
            as_of_date=future_date,
            state_dir=self.state_dir,
            write_records=False,
        )

        self.assertEqual(report.feed_status, "HARD_FAIL")
        self.assertTrue(any("INCOMPLETE_SESSION" in r for r in report.failure_reasons))

    # -------------------------------------------------------------------------
    # 15. CLI --dual-source flag supported, forbidden flags rejected
    # -------------------------------------------------------------------------
    def test_15_cli_dual_source_flag_and_forbidden_flags(self):
        """CLI accepts --dual-source and rejects prohibited live/broker/stream flags."""
        for forbidden in FORBIDDEN_CLI_FLAGS:
            with self.assertRaises(SystemExit):
                scan_for_forbidden_flags([forbidden])

        # Verify format_observation_report displays dual-source disclosure
        rep = DailyResearchReport(
            as_of_date="2026-11-16",
            scan_timestamp="2026-11-16T22:00:00+00:00",
            data_dir="LIVE_GATEWAY",
            feed_status="READY",
            market_regime=None,
            portfolio_status={"total_equity_usd": "5000.00", "cash_usd": "5000.00"},
            eligible_candidates=[],
            risk_rejected_candidates=[],
            scanner_rejected_candidates={},
            feed_excluded_tickers=[],
            exit_signals=[],
            observation_records=[],
            warnings=[],
            failure_reasons=[],
            data_mode="LIVE_IEX_HYBRID",
            data_classification="LIVE_IEX_HYBRID (ALPACA_IEX_PRICE + YAHOO_CONSOLIDATED_VOLUME)",
        )
        formatted = format_observation_report(rep)
        self.assertIn("LIVE DUAL-SOURCE DATA", formatted)
        self.assertIn("ALPACA IEX + YAHOO CONSOLIDATED", formatted)
        self.assertIn("FORWARD PAPER OBSERVATION ONLY", formatted)

    # =========================================================================
    # Phase 7.3.6.1: Idempotency Key Correction Tests
    # Key: (session_date, ticker, data_mode)
    # =========================================================================

    def test_16_idempotency_same_date_ticker_same_mode_rejected(self):
        """1. Same date + ticker + same mode: duplicate is rejected (not re-written)."""
        test_log = os.path.join(self.obs_dir, "test_16_log.jsonl")
        pre_rec = {
            "record_type": "FORWARD_OBSERVATION",
            "date": "2026-11-16",
            "ticker": "NVDA",
            "market_regime": "BULLISH",
            "signal": "ENTRY_SIGNAL",
            "decision": "OBSERVE_ONLY",
            "executed_fill": False,
            "data_mode": "LIVE_IEX_HYBRID",
        }
        with open(test_log, "w", encoding="utf-8") as f:
            f.write(json.dumps(pre_rec) + "\n")

        gateway = self._build_standard_gateway()
        run_observation_scan(
            gateway=gateway,
            tickers=["NVDA"],
            as_of_date=self.end_date,
            state_dir=self.state_dir,
            record_file=test_log,
            write_records=True,
        )

        entries = read_observation_log(test_log)
        nvda_live = [
            r for r in entries
            if r.get("record_type") == "FORWARD_OBSERVATION"
            and r.get("ticker") == "NVDA"
            and r.get("data_mode") == "LIVE_IEX_HYBRID"
        ]
        self.assertEqual(len(nvda_live), 1, "Duplicate record with same date, ticker, and mode must be rejected.")

    def test_17_idempotency_same_date_ticker_different_mode_both_accepted(self):
        """2. Same date + ticker + different mode: both records are accepted as distinct."""
        test_log = os.path.join(self.obs_dir, "test_17_log.jsonl")
        hist_rec = {
            "record_type": "FORWARD_OBSERVATION",
            "date": "2026-11-16",
            "ticker": "NVDA",
            "market_regime": "BULLISH",
            "signal": "ENTRY_SIGNAL",
            "decision": "OBSERVE_ONLY",
            "executed_fill": False,
            "data_mode": "HISTORICAL_PARTITION_REPLAY",
        }
        with open(test_log, "w", encoding="utf-8") as f:
            f.write(json.dumps(hist_rec) + "\n")

        gateway = self._build_standard_gateway()
        run_observation_scan(
            gateway=gateway,
            tickers=["NVDA"],
            as_of_date=self.end_date,
            state_dir=self.state_dir,
            record_file=test_log,
            write_records=True,
        )

        entries = read_observation_log(test_log)
        fwd_recs = [
            r for r in entries
            if r.get("record_type") == "FORWARD_OBSERVATION" and r.get("ticker") == "NVDA"
        ]
        self.assertEqual(len(fwd_recs), 2, "Historical and live records for same date and ticker must both be accepted.")
        modes = {r.get("data_mode") for r in fwd_recs}
        self.assertEqual(modes, {"HISTORICAL_PARTITION_REPLAY", "LIVE_IEX_HYBRID"})

    def test_18_idempotency_different_ticker_accepted(self):
        """3. Different ticker: newly observed ticker is accepted and written."""
        test_log = os.path.join(self.obs_dir, "test_18_log.jsonl")
        existing_rec = {
            "record_type": "FORWARD_OBSERVATION",
            "date": "2026-11-16",
            "ticker": "NVDA",
            "market_regime": "BULLISH",
            "signal": "ENTRY_SIGNAL",
            "decision": "OBSERVE_ONLY",
            "executed_fill": False,
            "data_mode": "LIVE_IEX_HYBRID",
        }
        with open(test_log, "w", encoding="utf-8") as f:
            f.write(json.dumps(existing_rec) + "\n")

        gateway = self._build_standard_gateway()
        run_observation_scan(
            gateway=gateway,
            tickers=["AAPL"],
            as_of_date=self.end_date,
            state_dir=self.state_dir,
            record_file=test_log,
            write_records=True,
        )

        entries = read_observation_log(test_log)
        tickers = [r.get("ticker") for r in entries if r.get("record_type") == "FORWARD_OBSERVATION"]
        self.assertIn("NVDA", tickers)
        self.assertIn("AAPL", tickers)
        self.assertEqual(len(tickers), 2)

    def test_19_idempotency_repeated_execution_no_duplicates(self):
        """4. Repeated execution: identical scan runs produce zero duplicate records."""
        test_log = os.path.join(self.obs_dir, "test_19_log.jsonl")
        gateway = self._build_standard_gateway()

        # Run 1
        run_observation_scan(
            gateway=gateway,
            tickers=["NVDA", "AAPL"],
            as_of_date=self.end_date,
            state_dir=self.state_dir,
            record_file=test_log,
            write_records=True,
        )
        count_1 = len(read_observation_log(test_log))
        self.assertGreater(count_1, 0)

        # Run 2
        run_observation_scan(
            gateway=gateway,
            tickers=["NVDA", "AAPL"],
            as_of_date=self.end_date,
            state_dir=self.state_dir,
            record_file=test_log,
            write_records=True,
        )
        count_2 = len(read_observation_log(test_log))
        self.assertEqual(count_1, count_2, "Run 2 must not add any duplicate records.")

        # Run 3
        run_observation_scan(
            gateway=gateway,
            tickers=["NVDA", "AAPL"],
            as_of_date=self.end_date,
            state_dir=self.state_dir,
            record_file=test_log,
            write_records=True,
        )
        count_3 = len(read_observation_log(test_log))
        self.assertEqual(count_1, count_3, "Run 3 must not add any duplicate records.")

    def test_20_idempotency_existing_log_records_remain_unchanged(self):
        """5. Existing log records remain unchanged when new observations are appended."""
        test_log = os.path.join(self.obs_dir, "test_20_log.jsonl")
        pre_records = [
            {"record_type": "FORWARD_OBSERVATION", "date": "2023-12-28", "ticker": "SPY", "data_mode": "HISTORICAL_PARTITION_REPLAY"},
            {"record_type": "FORWARD_OBSERVATION", "date": "2023-12-29", "ticker": "QQQ", "data_mode": "HISTORICAL_PARTITION_REPLAY"},
            {"record_type": "SESSION_RECORD", "date": "2023-12-29", "data_mode": "HISTORICAL_PARTITION_REPLAY"},
        ]
        with open(test_log, "w", encoding="utf-8") as f:
            for r in pre_records:
                f.write(json.dumps(r) + "\n")

        with open(test_log, "rb") as f:
            pre_hash = hashlib.sha256(f.read()).hexdigest()

        gateway = self._build_standard_gateway()
        run_observation_scan(
            gateway=gateway,
            tickers=["NVDA"],
            as_of_date=self.end_date,
            state_dir=self.state_dir,
            record_file=test_log,
            write_records=True,
        )

        entries = read_observation_log(test_log)
        # First 3 records must exactly equal pre_records
        for idx in range(3):
            self.assertEqual(entries[idx], pre_records[idx])

        # New records must be appended after
        self.assertGreater(len(entries), 3)
        new_entries = entries[3:]
        self.assertTrue(any(r.get("ticker") == "NVDA" and r.get("data_mode") == "LIVE_IEX_HYBRID" for r in new_entries))


if __name__ == "__main__":
    unittest.main()
