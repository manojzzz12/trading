"""Deterministic swing-trading exit signal generators for trader-swing.

Enforces:
1. Zero mutation of actual positions or journal records.
2. Informational exit recommendations covering stop breaches, target hits, trend deterioration, momentum loss, and trailing stops.
3. Pure Decimal arithmetic for unrealized P&L and return percentages.
"""

from datetime import datetime, timezone
from decimal import Decimal
import os
import sys
from typing import Any, Dict, List, Optional, Sequence

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from accounting import AccountingLedger, quantize_cent
from market_data_models import OHLCVCandle
from indicators import TechnicalIndicatorsEngine
from regime_models import MarketRegime, MarketRegimeResult
from signal_models import ExitSignal, ExitSignalType

TWO_DECIMALS = Decimal("0.01")


class ExitSignalGenerator:
    """Evaluates open portfolio positions against technical exit rules and targets."""

    def __init__(self, time_based_max_sessions: int = 10):
        self.time_based_max_sessions = time_based_max_sessions
        self.engine = TechnicalIndicatorsEngine()

    def evaluate_exits(
        self,
        ledger: AccountingLedger,
        candles_map: Dict[str, Sequence[OHLCVCandle]],
        lot_stops: Optional[Dict[str, Decimal]] = None,
        lot_targets: Optional[Dict[str, Decimal]] = None,
        regime_result: Optional[MarketRegimeResult] = None,
        require_complete: bool = True,
    ) -> List[ExitSignal]:
        """Generate informational exit signals for open positions in the ledger."""
        lot_stops = lot_stops or {}
        lot_targets = lot_targets or {}
        signals: List[ExitSignal] = []

        # Iterate over open positions in accounting ledger
        for ticker, pos in ledger.positions.items():
            if pos.total_shares <= 0:
                continue
            if ticker not in candles_map or not candles_map[ticker]:
                continue

            candles = candles_map[ticker]
            terminal = candles[-1]
            current_price = terminal.close
            as_of = terminal.timestamp

            # Determine position-level cost basis and unrealized metrics
            entry_price = quantize_cent(pos.total_cost_basis_usd / Decimal(pos.total_shares))
            unrealized_pnl = (current_price - entry_price) * Decimal(pos.total_shares)
            unrealized_return_pct = Decimal("0.0")
            if entry_price > Decimal("0.0"):
                unrealized_return_pct = ((current_price - entry_price) / entry_price) * Decimal("100")
            unrealized_return_pct = unrealized_return_pct.quantize(TWO_DECIMALS)

            # Determine stop loss and target for this position or lot
            lot_id_key = pos.lots[0].lot_id if pos.lots else None
            stop_price = lot_stops.get(lot_id_key) if lot_id_key in lot_stops else lot_stops.get(ticker, entry_price * Decimal("0.95"))
            target_price = lot_targets.get(lot_id_key) if lot_id_key in lot_targets else lot_targets.get(ticker, entry_price * Decimal("1.10"))

            # Calculate indicators for position ticker
            snap = self.engine.compute_all(candles, require_complete=require_complete)

            # Estimate holding sessions (count of bars from first lot date or proxy)
            holding_sessions = 1
            if pos.lots:
                oldest_lot = pos.lots[0]
                try:
                    lot_date = datetime.fromisoformat(oldest_lot.entry_timestamp).date()
                    holding_sessions = max(1, sum(1 for c in candles if c.timestamp.date() >= lot_date))
                except Exception:
                    holding_sessions = 1

            # 1. Stop-Loss Breached
            if current_price <= stop_price:
                signals.append(
                    ExitSignal(
                        ticker=ticker,
                        lot_id=pos.lots[0].lot_id if pos.lots else None,
                        exit_type=ExitSignalType.STOP_LOSS_BREACHED,
                        signal_timestamp=as_of,
                        current_price=current_price,
                        entry_price=entry_price,
                        stop_loss_price=stop_price,
                        target_price=target_price,
                        recommended_action="EXIT_FULL",
                        new_stop_price=None,
                        unrealized_pnl_usd=quantize_cent(unrealized_pnl),
                        unrealized_return_pct=unrealized_return_pct,
                        holding_sessions=holding_sessions,
                        rationale=f"Price (${current_price}) breached stop loss (${stop_price}).",
                        evidence=[f"Current price ${current_price} <= stop loss ${stop_price}."],
                    )
                )
                continue  # Breached stop takes precedence

            # 2. Target Reached
            if current_price >= target_price:
                signals.append(
                    ExitSignal(
                        ticker=ticker,
                        lot_id=pos.lots[0].lot_id if pos.lots else None,
                        exit_type=ExitSignalType.TARGET_REACHED,
                        signal_timestamp=as_of,
                        current_price=current_price,
                        entry_price=entry_price,
                        stop_loss_price=stop_price,
                        target_price=target_price,
                        recommended_action="EXIT_FULL",
                        new_stop_price=None,
                        unrealized_pnl_usd=quantize_cent(unrealized_pnl),
                        unrealized_return_pct=unrealized_return_pct,
                        holding_sessions=holding_sessions,
                        rationale=f"Price (${current_price}) reached profit target (${target_price}).",
                        evidence=[f"Current price ${current_price} >= target ${target_price}."],
                    )
                )
                continue

            # 3. Market Regime Deterioration (Emergency exit under STRONG_BEARISH)
            if regime_result is not None and regime_result.regime == MarketRegime.STRONG_BEARISH:
                signals.append(
                    ExitSignal(
                        ticker=ticker,
                        lot_id=pos.lots[0].lot_id if pos.lots else None,
                        exit_type=ExitSignalType.REGIME_DETERIORATION,
                        signal_timestamp=as_of,
                        current_price=current_price,
                        entry_price=entry_price,
                        stop_loss_price=stop_price,
                        target_price=target_price,
                        recommended_action="EXIT_FULL",
                        new_stop_price=None,
                        unrealized_pnl_usd=quantize_cent(unrealized_pnl),
                        unrealized_return_pct=unrealized_return_pct,
                        holding_sessions=holding_sessions,
                        rationale="Market regime shifted to STRONG_BEARISH; defensive exit recommended.",
                        evidence=[f"Broad market regime is {regime_result.regime.value}."],
                    )
                )
                continue

            # 4. Trailing Stop Update (Move stop to breakeven or trailing 20 SMA when profit >= 1.0R)
            initial_risk = entry_price - stop_price
            if initial_risk > Decimal("0.0"):
                r_multiple = (current_price - entry_price) / initial_risk
                if r_multiple >= Decimal("1.00"):
                    # Suggest moving stop to entry_price (breakeven) or 20 SMA
                    trailing_stop = entry_price
                    if snap.sma_20.is_available and snap.sma_20.value is not None:
                        trailing_stop = max(trailing_stop, snap.sma_20.value)
                    trailing_stop = quantize_cent(trailing_stop)

                    if trailing_stop > stop_price:
                        signals.append(
                            ExitSignal(
                                ticker=ticker,
                                lot_id=pos.lots[0].lot_id if pos.lots else None,
                                exit_type=ExitSignalType.TRAILING_STOP_UPDATE,
                                signal_timestamp=as_of,
                                current_price=current_price,
                                entry_price=entry_price,
                                stop_loss_price=stop_price,
                                target_price=target_price,
                                recommended_action="UPDATE_STOP",
                                new_stop_price=trailing_stop,
                                unrealized_pnl_usd=quantize_cent(unrealized_pnl),
                                unrealized_return_pct=unrealized_return_pct,
                                holding_sessions=holding_sessions,
                                rationale=f"Position has gained {r_multiple:.2f}R. Proposing trailing stop raise to ${trailing_stop}.",
                                evidence=[
                                    f"Gain is {r_multiple:.2f}R (>=1.0R).",
                                    f"New stop ${trailing_stop} locks in risk-free status.",
                                ],
                            )
                        )

            # 5. Trend Deterioration (Price drops below 50 SMA)
            if snap.sma_50.is_available and snap.sma_50.value is not None:
                if current_price < snap.sma_50.value:
                    signals.append(
                        ExitSignal(
                            ticker=ticker,
                            lot_id=pos.lots[0].lot_id if pos.lots else None,
                            exit_type=ExitSignalType.TREND_DETERIORATION,
                            signal_timestamp=as_of,
                            current_price=current_price,
                            entry_price=entry_price,
                            stop_loss_price=stop_price,
                            target_price=target_price,
                            recommended_action="EXIT_FULL",
                            new_stop_price=None,
                            unrealized_pnl_usd=quantize_cent(unrealized_pnl),
                            unrealized_return_pct=unrealized_return_pct,
                            holding_sessions=holding_sessions,
                            rationale=f"Price (${current_price}) dropped below 50-day SMA (${snap.sma_50.value}).",
                            evidence=[f"50 SMA support broken at ${snap.sma_50.value}."],
                        )
                    )
                    continue

            # 6. Time-based Reevaluation (Holding sessions >= 10 without progress)
            if holding_sessions >= self.time_based_max_sessions and unrealized_return_pct < Decimal("1.00"):
                signals.append(
                    ExitSignal(
                        ticker=ticker,
                        lot_id=pos.lots[0].lot_id if pos.lots else None,
                        exit_type=ExitSignalType.TIME_BASED_REEVALUATION,
                        signal_timestamp=as_of,
                        current_price=current_price,
                        entry_price=entry_price,
                        stop_loss_price=stop_price,
                        target_price=target_price,
                        recommended_action="EXIT_FULL",
                        new_stop_price=None,
                        unrealized_pnl_usd=quantize_cent(unrealized_pnl),
                        unrealized_return_pct=unrealized_return_pct,
                        holding_sessions=holding_sessions,
                        rationale=f"Position held for {holding_sessions} sessions with stagnant progress ({unrealized_return_pct}%).",
                        evidence=[f"Time limit reached ({holding_sessions} sessions >= {self.time_based_max_sessions})."],
                    )
                )

        return signals
