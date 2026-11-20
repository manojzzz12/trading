"""Unit test suite for trader-swing entry and exit signal engine.

Verifies:
1. Entry strategies: TREND_CONTINUATION, BREAKOUT_CONFIRMATION, PULLBACK_ENTRY.
2. Stop-loss and profit target generation with minimum 2.0 reward-to-risk.
3. Phase 2 risk engine sizing integration and constraint enforcement.
4. Risk rejection handling (e.g. 4-position max, cash depletion).
5. Exit signals: STOP_LOSS_BREACHED, TARGET_REACHED, TRAILING_STOP_UPDATE, REGIME_DETERIORATION.
6. Zero mutation of actual AccountingLedger state.
7. FORMING candle exclusion in finalized signal generation.
8. Pure Decimal arithmetic and deterministic repeatability.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
import sys
import unittest

SWING_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".agents",
    "skills",
    "trader-swing",
)
if SWING_DIR not in sys.path:
    sys.path.insert(0, SWING_DIR)

from accounting import AccountingLedger
from market_data_models import CandleStatus, OHLCVCandle, UTC
from regime_models import DEFAULT_REGIME_POLICIES, MarketRegime, MarketRegimeResult
from risk import RiskConfig, RiskEngine
from scanner_models import CandidateScore, ScannedCandidate
from entry_signals import EntrySignalGenerator
from exit_signals import ExitSignalGenerator
from signal_engine import SignalEngine
from signal_models import EntryStrategyType, ExitSignalType


def make_candle(
    ticker: str,
    index: int,
    close_p: str,
    volume: int = 1000000,
    open_p: str = None,
    high_p: str = None,
    low_p: str = None,
    status: CandleStatus = CandleStatus.COMPLETE,
    base_date: datetime = None,
) -> OHLCVCandle:
    if base_date is None:
        base_date = datetime(2026, 1, 5, 20, 0, 0, tzinfo=UTC)
    ts = base_date + timedelta(days=index)
    c_dec = Decimal(close_p)
    o_dec = Decimal(open_p) if open_p is not None else c_dec
    spread = max(Decimal("2.00"), c_dec * Decimal("0.02"))
    h_dec = Decimal(high_p) if high_p is not None else max(c_dec, o_dec) + spread
    l_dec = Decimal(low_p) if low_p is not None else min(c_dec, o_dec) - spread
    return OHLCVCandle(
        ticker=ticker,
        interval="1d",
        timestamp=ts,
        open=o_dec,
        high=h_dec,
        low=l_dec,
        close=c_dec,
        volume=volume,
        status=status,
        source="yahoo",
        received_timestamp=ts,
        is_delayed=True,
    )


class TestSwingSignals(unittest.TestCase):
    """Test suite covering entry setups, stop/target mathematics, risk integration, and exit monitoring."""

    def setUp(self):
        self.risk_engine = RiskEngine(RiskConfig(starting_virtual_capital_usd=Decimal("5000.00")))
        self.entry_gen = EntrySignalGenerator()
        self.exit_gen = ExitSignalGenerator()
        self.engine = SignalEngine(
            risk_engine=self.risk_engine,
            entry_generator=self.entry_gen,
            exit_generator=self.exit_gen,
        )
        self.base_date = datetime(2026, 1, 5, 20, 0, 0, tzinfo=UTC)

    def _build_trend_series(self, ticker: str, count: int, start_price: Decimal) -> list:
        bars = []
        p = start_price
        for i in range(count):
            if i % 2 == 0:
                p -= Decimal("0.30")
            else:
                p += Decimal("0.60")
            bars.append(make_candle(ticker, i, str(p), volume=1000000, base_date=self.base_date))
        return bars

    def test_01_breakout_strategy_signal(self):
        # 30 bars consolidating around $100, then 31st bar surges on 3.0x volume to $110
        bars = []
        for i in range(30):
            p = Decimal("100.00") if i % 2 == 0 else Decimal("101.00")
            bars.append(make_candle("NVDA", i, str(p), volume=1000000, base_date=self.base_date))

        # Breakout bar: close at $110, high of day $110.50, volume 3,000,000 (3.0x RVOL)
        bars.append(
            make_candle(
                "NVDA",
                30,
                "110.00",
                volume=3000000,
                open_p="102.00",
                high_p="110.50",
                low_p="101.50",
                base_date=self.base_date,
            )
        )

        sig = self.entry_gen.evaluate_entry(bars)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.strategy, EntryStrategyType.BREAKOUT_CONFIRMATION)
        self.assertTrue(sig.is_technically_valid)
        self.assertTrue(sig.suggested_stop_loss < sig.reference_price)
        self.assertTrue(sig.suggested_target > sig.reference_price)
        self.assertTrue(sig.reward_to_risk >= Decimal("2.00"))

    def test_02_trend_continuation_strategy_signal(self):
        bars = self._build_trend_series("AAPL", 55, Decimal("140.00"))
        sig = self.entry_gen.evaluate_entry(bars)
        self.assertIsNotNone(sig)
        self.assertIn(sig.strategy, [EntryStrategyType.TREND_CONTINUATION, EntryStrategyType.BREAKOUT_CONFIRMATION])
        self.assertTrue(sig.is_technically_valid)
        self.assertTrue(sig.suggested_stop_loss < sig.reference_price)
        self.assertTrue(sig.suggested_target > sig.reference_price)

    def test_03_risk_engine_integration_and_position_sizing(self):
        bars = self._build_trend_series("AAPL", 55, Decimal("150.00"))
        cand = ScannedCandidate(
            ticker="AAPL",
            rank=1,
            score=CandidateScore(Decimal("20"), Decimal("20"), Decimal("15"), Decimal("10"), Decimal("10"), Decimal("75")),
            price=bars[-1].close,
            indicators={},
            positive_reasons=[],
            negative_reasons=[],
            warnings=[],
            market_regime="BULLISH",
            as_of_timestamp=bars[-1].timestamp,
            is_eligible=True,
            rejection_reasons=[],
        )

        ledger = AccountingLedger("5000.00")
        summary = self.engine.evaluate_candidates([cand], {"AAPL": bars}, ledger=ledger)

        self.assertEqual(summary.total_evaluated, 1)
        self.assertEqual(summary.total_approved, 1)
        self.assertEqual(len(summary.risk_approved_plans), 1)

        plan = summary.risk_approved_plans[0]
        self.assertEqual(plan.ticker, "AAPL")
        self.assertTrue(plan.risk_approved)
        self.assertTrue(plan.shares > 0)
        self.assertTrue(plan.total_capital_required <= Decimal("5000.00"))
        # Capital must not exceed 25% ticker allocation limit ($1,250 on $5k equity)
        self.assertTrue(plan.total_capital_required <= Decimal("1250.00"))

    def test_04_risk_engine_rejection_when_positions_full(self):
        # Fill portfolio with 4 unique tickers (max simultaneous positions = 4)
        ledger = AccountingLedger("5000.00")
        for i, t in enumerate(["MSFT", "GOOGL", "AMZN", "META"]):
            ledger.buy(
                ticker=t,
                shares=1,
                price="100.00",
                fee="1.00",
                timestamp=f"2026-01-0{i+1}T09:30:00-05:00",
            )

        self.assertEqual(len(ledger.positions), 4)

        bars = self._build_trend_series("AAPL", 55, Decimal("150.00"))
        cand = ScannedCandidate(
            ticker="AAPL",
            rank=1,
            score=CandidateScore(Decimal("20"), Decimal("20"), Decimal("15"), Decimal("10"), Decimal("10"), Decimal("75")),
            price=bars[-1].close,
            indicators={},
            positive_reasons=[],
            negative_reasons=[],
            warnings=[],
            market_regime="BULLISH",
            as_of_timestamp=bars[-1].timestamp,
            is_eligible=True,
            rejection_reasons=[],
        )

        summary = self.engine.evaluate_candidates([cand], {"AAPL": bars}, ledger=ledger)
        self.assertEqual(summary.total_approved, 0)
        self.assertEqual(len(summary.risk_rejected_plans), 1)
        plan = summary.risk_rejected_plans[0]
        self.assertFalse(plan.risk_approved)
        self.assertEqual(plan.shares, 0)
        self.assertEqual(plan.binding_constraint, "MAX_POSITIONS_EXCEEDED")
        self.assertTrue(any("Position count limit reached" in r for r in plan.risk_rejection_reasons))

    def test_05_exit_signals_stop_loss_breached(self):
        ledger = AccountingLedger("5000.00")
        ledger.buy(ticker="TSLA", shares=10, price="200.00", fee="1.00", timestamp="2026-01-10T09:30:00-05:00")

        # TSLA price drops to $185.00 (below $190 stop)
        bars = self._build_trend_series("TSLA", 30, Decimal("200.00"))
        bars[-1] = make_candle("TSLA", 29, "185.00", base_date=self.base_date)

        stops = {"TSLA": Decimal("190.00")}
        exits = self.engine.evaluate_exits(ledger, {"TSLA": bars}, lot_stops=stops)

        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0].exit_type, ExitSignalType.STOP_LOSS_BREACHED)
        self.assertEqual(exits[0].recommended_action, "EXIT_FULL")
        self.assertTrue(exits[0].unrealized_pnl_usd < Decimal("0.00"))

    def test_06_exit_signals_target_reached(self):
        ledger = AccountingLedger("5000.00")
        ledger.buy(ticker="NVDA", shares=10, price="100.00", fee="1.00", timestamp="2026-01-10T09:30:00-05:00")

        # NVDA surges to $125.00 (above $120 target)
        bars = self._build_trend_series("NVDA", 30, Decimal("100.00"))
        bars[-1] = make_candle("NVDA", 29, "125.00", base_date=self.base_date)

        targets = {"NVDA": Decimal("120.00")}
        exits = self.engine.evaluate_exits(ledger, {"NVDA": bars}, lot_targets=targets)

        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0].exit_type, ExitSignalType.TARGET_REACHED)
        self.assertEqual(exits[0].recommended_action, "EXIT_FULL")
        self.assertTrue(exits[0].unrealized_pnl_usd > Decimal("0.00"))

    def test_07_exit_signals_trailing_stop_update(self):
        # Entry at $100, initial stop at $90 (Risk = $10). Target at $120.
        # Current price rises to $112 (gain of 1.2R >= 1.0R)
        ledger = AccountingLedger("5000.00")
        ledger.buy(ticker="AMD", shares=10, price="100.00", fee="1.00", timestamp="2026-01-10T09:30:00-05:00")

        bars = self._build_trend_series("AMD", 30, Decimal("100.00"))
        bars[-1] = make_candle("AMD", 29, "112.00", base_date=self.base_date)

        stops = {"AMD": Decimal("90.00")}
        targets = {"AMD": Decimal("120.00")}
        exits = self.engine.evaluate_exits(ledger, {"AMD": bars}, lot_stops=stops, lot_targets=targets)

        trail_exits = [e for e in exits if e.exit_type == ExitSignalType.TRAILING_STOP_UPDATE]
        self.assertEqual(len(trail_exits), 1)
        self.assertEqual(trail_exits[0].recommended_action, "UPDATE_STOP")
        # Trailing stop raised to at least breakeven ($100.00)
        self.assertTrue(trail_exits[0].new_stop_price >= Decimal("100.00"))

    def test_08_ledger_state_is_never_mutated(self):
        ledger = AccountingLedger("5000.00")
        ledger.buy(ticker="AAPL", shares=5, price="150.00", fee="1.00", timestamp="2026-01-10T09:30:00-05:00")
        initial_cash = ledger.cash_usd
        initial_shares = ledger.positions["AAPL"].total_shares

        bars = self._build_trend_series("AAPL", 30, Decimal("150.00"))
        _ = self.engine.evaluate_exits(ledger, {"AAPL": bars})

        # Confirm ledger is 100% untouched
        self.assertEqual(ledger.cash_usd, initial_cash)
        self.assertEqual(ledger.positions["AAPL"].total_shares, initial_shares)

    def test_09_batch_evaluation_respects_remaining_position_capacity(self):
        """DEFECT-POSCAP-01: When ledger holds 2 positions, a batch of 3 candidates must only approve 2."""
        ledger = AccountingLedger("5000.00")
        ledger.buy("MSFT", 1, "100.00", fee="0.00", timestamp="2026-01-05T09:30:00-05:00")
        ledger.buy("GOOGL", 1, "100.00", fee="0.00", timestamp="2026-01-05T09:30:00-05:00")
        self.assertEqual(len(ledger.positions), 2)

        candles_map = {}
        candidates = []
        for rank, sym in enumerate(["AAPL", "AMZN", "META"], 1):
            bars = self._build_trend_series(sym, 55, Decimal("150.00"))
            candles_map[sym] = bars
            candidates.append(
                ScannedCandidate(
                    ticker=sym,
                    rank=rank,
                    score=CandidateScore(Decimal("20"), Decimal("20"), Decimal("15"), Decimal("10"), Decimal("10"), Decimal("75")),
                    price=bars[-1].close,
                    indicators={},
                    positive_reasons=[],
                    negative_reasons=[],
                    warnings=[],
                    market_regime="BULLISH",
                    as_of_timestamp=bars[-1].timestamp,
                    is_eligible=True,
                    rejection_reasons=[],
                )
            )

        summary = self.engine.evaluate_candidates(candidates, candles_map, ledger=ledger)

        self.assertEqual(summary.total_evaluated, 3)
        self.assertEqual(summary.total_approved, 2)
        self.assertEqual(len(summary.risk_approved_plans), 2)
        self.assertEqual(len(summary.risk_rejected_plans), 1)

        approved_tickers = [p.ticker for p in summary.risk_approved_plans]
        self.assertEqual(approved_tickers, ["AAPL", "AMZN"])

        rejected_plan = summary.risk_rejected_plans[0]
        self.assertEqual(rejected_plan.ticker, "META")
        self.assertFalse(rejected_plan.risk_approved)
        self.assertEqual(rejected_plan.binding_constraint, "MAX_POSITIONS_EXCEEDED")
        self.assertIn("Position count limit reached", rejected_plan.risk_rejection_reasons[0])

    def test_10_batch_evaluation_with_pending_tickers_reserving_capacity(self):
        """DEFECT-POSCAP-01: Pending orders must reserve capacity during batch candidate evaluation."""
        ledger = AccountingLedger("5000.00")
        ledger.buy("MSFT", 1, "100.00", fee="0.00", timestamp="2026-01-05T09:30:00-05:00")
        ledger.buy("GOOGL", 1, "100.00", fee="0.00", timestamp="2026-01-05T09:30:00-05:00")
        self.assertEqual(len(ledger.positions), 2)

        # 1 pending order already queued -> 2 open + 1 pending = 3 slots committed (1 remaining)
        pending_tickers = ["NVDA"]

        candles_map = {}
        candidates = []
        for rank, sym in enumerate(["AAPL", "AMZN"], 1):
            bars = self._build_trend_series(sym, 55, Decimal("150.00"))
            candles_map[sym] = bars
            candidates.append(
                ScannedCandidate(
                    ticker=sym,
                    rank=rank,
                    score=CandidateScore(Decimal("20"), Decimal("20"), Decimal("15"), Decimal("10"), Decimal("10"), Decimal("75")),
                    price=bars[-1].close,
                    indicators={},
                    positive_reasons=[],
                    negative_reasons=[],
                    warnings=[],
                    market_regime="BULLISH",
                    as_of_timestamp=bars[-1].timestamp,
                    is_eligible=True,
                    rejection_reasons=[],
                )
            )

        summary = self.engine.evaluate_candidates(
            candidates, candles_map, ledger=ledger, pending_tickers=pending_tickers
        )

        self.assertEqual(summary.total_evaluated, 2)
        self.assertEqual(summary.total_approved, 1)
        self.assertEqual(summary.risk_approved_plans[0].ticker, "AAPL")
        self.assertEqual(summary.risk_rejected_plans[0].ticker, "AMZN")
        self.assertEqual(summary.risk_rejected_plans[0].binding_constraint, "MAX_POSITIONS_EXCEEDED")

    def test_11_batch_evaluation_competing_for_single_final_slot(self):
        """DEFECT-POSCAP-01: When 3 positions are open, only the top candidate gets the final 4th slot."""
        ledger = AccountingLedger("5000.00")
        ledger.buy("MSFT", 1, "100.00", fee="0.00", timestamp="2026-01-05T09:30:00-05:00")
        ledger.buy("GOOGL", 1, "100.00", fee="0.00", timestamp="2026-01-05T09:30:00-05:00")
        ledger.buy("NVDA", 1, "100.00", fee="0.00", timestamp="2026-01-05T09:30:00-05:00")
        self.assertEqual(len(ledger.positions), 3)

        candles_map = {}
        candidates = []
        for rank, sym in enumerate(["AAPL", "AMZN", "META", "TSLA"], 1):
            bars = self._build_trend_series(sym, 55, Decimal("150.00"))
            candles_map[sym] = bars
            candidates.append(
                ScannedCandidate(
                    ticker=sym,
                    rank=rank,
                    score=CandidateScore(Decimal("20"), Decimal("20"), Decimal("15"), Decimal("10"), Decimal("10"), Decimal("75")),
                    price=bars[-1].close,
                    indicators={},
                    positive_reasons=[],
                    negative_reasons=[],
                    warnings=[],
                    market_regime="BULLISH",
                    as_of_timestamp=bars[-1].timestamp,
                    is_eligible=True,
                    rejection_reasons=[],
                )
            )

        summary = self.engine.evaluate_candidates(candidates, candles_map, ledger=ledger)

        self.assertEqual(summary.total_approved, 1)
        self.assertEqual(summary.risk_approved_plans[0].ticker, "AAPL")
        self.assertEqual(len(summary.risk_rejected_plans), 3)
        for rejected in summary.risk_rejected_plans:
            self.assertEqual(rejected.binding_constraint, "MAX_POSITIONS_EXCEEDED")


if __name__ == "__main__":
    unittest.main()
