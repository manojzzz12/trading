"""Hypothetical Forward Trade Tracker for Trader-Swing Phase 9.

Tracks hypothetical trade proposals, active forward positions, and closed trades
for candidates that successfully pass all frozen screening, regime, and risk gates.

CRITICAL PRINCIPLES:
1. Conservative execution modeling:
   - Same-bar stop/target ambiguity resolved via the 'stop-first' assumption.
2. Holding period governance:
   - Positions held for 10 trading sessions without hitting stop or target exit
     at the close of the 10th session (MAX_HOLDING_PERIOD_EXPIRED).
3. Realistic fee amortization:
   - Deducts $1.00 entry fee and $1.00 exit fee on every hypothetical trade.
4. Bar-by-bar MFE and MAE tracking:
   - Quantifies Maximum Favorable Excursion and Maximum Adverse Excursion.
5. Strict position caps and risk enforcement:
   - Enforces max 4 simultaneous positions, 25% single-ticker allocation,
     and 20% minimum cash reserve on virtual capital.
"""

from datetime import datetime, timezone
from decimal import Decimal
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, "..", "..", ".."))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from accounting import quantize_cent, to_decimal
from forward_evaluation_models import (
    ForwardDataMode,
    ForwardTradeProposal,
    HypotheticalClosedTrade,
    HypotheticalPosition,
    HypotheticalTradeStatus,
    MARKER_SIMULATED_TRADE,
)
from forward_evaluation_storage import ForwardEvaluationStorage
from market_data_models import CandleStatus, OHLCVCandle
from risk import RiskConfig, RiskEngine


class ForwardTradeTracker:
    """Manages the lifecycle and bar-by-bar tracking of hypothetical forward trades."""

    def __init__(
        self,
        storage: ForwardEvaluationStorage,
        initial_capital: Decimal = Decimal("5000.00"),
        risk_config: Optional[RiskConfig] = None,
    ):
        self.storage = storage
        self.initial_capital = quantize_cent(to_decimal(initial_capital))
        self.risk_config = risk_config or RiskConfig()
        self.risk_engine = RiskEngine(risk_config=self.risk_config)

    def create_proposal_and_open_position(
        self,
        observation_id: str,
        session_date: str,
        ticker: str,
        data_mode: str,
        shares: int,
        entry_price: Decimal,
        stop_loss: Decimal,
        target: Decimal,
        virtual_available_cash: Decimal,
        active_positions: List[HypotheticalPosition],
        entry_fee: Decimal = Decimal("1.00"),
        estimated_exit_fee: Decimal = Decimal("1.00"),
    ) -> Tuple[Optional[ForwardTradeProposal], Optional[HypotheticalPosition], Optional[str]]:
        """Evaluate position constraints and open a hypothetical position if all limits are satisfied.
        
        Returns:
            Tuple of (proposal, position, rejection_reason). If rejected, returns (None, None, reason).
        """
        px = quantize_cent(to_decimal(entry_price))
        sl = quantize_cent(to_decimal(stop_loss))
        tp = quantize_cent(to_decimal(target))
        sh = int(shares)

        if sh <= 0:
            return None, None, "INVALID_SHARES: Shares must be positive."
        if sl >= px:
            return None, None, f"INVALID_STOP: Stop-loss (${sl}) must be strictly below entry (${px})."
        if tp <= px:
            return None, None, f"INVALID_TARGET: Target (${tp}) must be strictly above entry (${px})."

        # 1. Enforce Max 4 Simultaneous Positions
        if len(active_positions) >= self.risk_config.max_simultaneous_positions:
            return None, None, f"MAX_POSITIONS_REACHED: Currently holding {len(active_positions)} / {self.risk_config.max_simultaneous_positions} positions."

        # 2. Check Single-Ticker Allocation Cap (Max 25% of virtual equity)
        capital_required = (Decimal(sh) * px) + entry_fee
        max_ticker_alloc = self.initial_capital * self.risk_config.max_allocation_per_ticker_pct
        if capital_required > max_ticker_alloc:
            return None, None, f"TICKER_CAP_EXCEEDED: Capital required (${capital_required}) exceeds 25% allocation limit (${max_ticker_alloc})."

        # 3. Check Minimum Cash Reserve (20% of virtual equity = $1,000.00)
        min_reserve = self.initial_capital * self.risk_config.min_cash_reserve_pct
        remaining_cash = virtual_available_cash - capital_required
        if remaining_cash < min_reserve:
            return None, None, f"CASH_RESERVE_BREACH: Cash after hypothetical trade (${remaining_cash}) would breach 20% reserve (${min_reserve})."

        # 4. Check whether ticker is already held
        if any(p.ticker == ticker for p in active_positions):
            return None, None, f"TICKER_ALREADY_HELD: Position already open for '{ticker}'."

        # Compute risk amount
        buffered_risk = (px - sl) * Decimal(sh) * self.risk_config.gap_risk_multiplier
        proposal_id = f"PROP-{session_date}-{ticker}-{data_mode}"
        pos_id = f"HPOS-{session_date}-{ticker}"

        proposal = ForwardTradeProposal(
            proposal_id=proposal_id,
            observation_id=observation_id,
            session_date=session_date,
            ticker=ticker,
            data_mode=data_mode,
            shares=sh,
            entry_reference_price=px,
            stop_loss_price=sl,
            target_price=tp,
            entry_fee=entry_fee,
            estimated_exit_fee=estimated_exit_fee,
            total_capital_required=capital_required,
            risk_amount=quantize_cent(buffered_risk),
            status=HypotheticalTradeStatus.OPEN.value,
        )
        self.storage.save_proposal(proposal)

        now_str = datetime.now(timezone.utc).isoformat()
        position = HypotheticalPosition(
            position_id=pos_id,
            proposal_id=proposal_id,
            session_date=session_date,
            ticker=ticker,
            data_mode=data_mode,
            shares=sh,
            entry_price=px,
            entry_fee=entry_fee,
            stop_loss=sl,
            target=tp,
            current_price=px,
            highest_high=px,
            lowest_low=px,
            mfe_pct=Decimal("0.0000"),
            mfe_usd=Decimal("0.00"),
            mae_pct=Decimal("0.0000"),
            mae_usd=Decimal("0.00"),
            bars_held=0,
            unrealized_pnl=Decimal("0.00"),
            status=HypotheticalTradeStatus.OPEN.value,
            opened_at=now_str,
            last_updated_at=now_str,
        )
        self.storage.save_position(position)

        return proposal, position, None

    def update_position_with_bar(
        self,
        position: HypotheticalPosition,
        candle: OHLCVCandle,
        exit_fee: Decimal = Decimal("1.00"),
    ) -> Tuple[Optional[HypotheticalClosedTrade], HypotheticalPosition]:
        """Update a hypothetical position with a newly completed daily candle.
        
        Evaluates MFE/MAE excursions and exit conditions.
        If an exit is triggered, removes the position from storage and persists a HypotheticalClosedTrade.
        Otherwise, updates the position state in storage.
        
        Returns:
            Tuple of (closed_trade_or_None, updated_position).
        """
        if candle.status != CandleStatus.COMPLETE:
            raise ValueError(f"Cannot update position with non-complete candle for '{candle.ticker}' at {candle.timestamp}.")

        bar_h = candle.high
        bar_l = candle.low
        bar_c = candle.close

        # Update excursions
        new_highest = max(position.highest_high, bar_h)
        new_lowest = min(position.lowest_low, bar_l)
        bars_held = position.bars_held + 1

        mfe_gain_usd = (new_highest - position.entry_price) * Decimal(position.shares)
        mfe_usd = max(Decimal("0.00"), quantize_cent(mfe_gain_usd))
        mfe_pct = max(Decimal("0.0000"), quantize_cent((new_highest - position.entry_price) / position.entry_price))

        mae_loss_usd = (position.entry_price - new_lowest) * Decimal(position.shares)
        mae_usd = max(Decimal("0.00"), quantize_cent(mae_loss_usd))
        mae_pct = max(Decimal("0.0000"), quantize_cent((position.entry_price - new_lowest) / position.entry_price))

        unrealized = quantize_cent((bar_c - position.entry_price) * Decimal(position.shares))
        now_str = datetime.now(timezone.utc).isoformat()

        # Check Exit Conditions
        hit_stop = (bar_l <= position.stop_loss)
        hit_target = (bar_h >= position.target)
        max_duration_reached = (bars_held >= 10)

        closed_trade: Optional[HypotheticalClosedTrade] = None

        if hit_stop and hit_target:
            # Conservative rule: Same-bar ambiguity assumed STOP-FIRST
            closed_trade = self._close_trade(
                position=position,
                exit_price=position.stop_loss,
                exit_date=candle.timestamp.strftime("%Y-%m-%d"),
                exit_reason="STOP_LOSS_CONSERVATIVE_AMBIGUITY",
                bars_held=bars_held,
                exit_fee=exit_fee,
                mfe_usd=mfe_usd,
                mae_usd=mae_usd,
            )
        elif hit_stop:
            closed_trade = self._close_trade(
                position=position,
                exit_price=position.stop_loss,
                exit_date=candle.timestamp.strftime("%Y-%m-%d"),
                exit_reason="STOP_LOSS_TRIGGERED",
                bars_held=bars_held,
                exit_fee=exit_fee,
                mfe_usd=mfe_usd,
                mae_usd=mae_usd,
            )
        elif hit_target:
            closed_trade = self._close_trade(
                position=position,
                exit_price=position.target,
                exit_date=candle.timestamp.strftime("%Y-%m-%d"),
                exit_reason="TAKE_PROFIT_TRIGGERED",
                bars_held=bars_held,
                exit_fee=exit_fee,
                mfe_usd=mfe_usd,
                mae_usd=mae_usd,
            )
        elif max_duration_reached:
            closed_trade = self._close_trade(
                position=position,
                exit_price=bar_c,
                exit_date=candle.timestamp.strftime("%Y-%m-%d"),
                exit_reason="MAX_HOLDING_PERIOD_EXPIRED",
                bars_held=bars_held,
                exit_fee=exit_fee,
                mfe_usd=mfe_usd,
                mae_usd=mae_usd,
            )
        else:
            # Position remains open; update in storage
            position.current_price = bar_c
            position.highest_high = new_highest
            position.lowest_low = new_lowest
            position.mfe_pct = mfe_pct
            position.mfe_usd = mfe_usd
            position.mae_pct = mae_pct
            position.mae_usd = mae_usd
            position.bars_held = bars_held
            position.unrealized_pnl = unrealized
            position.last_updated_at = now_str
            self.storage.save_position(position)

        return closed_trade, position

    def _close_trade(
        self,
        position: HypotheticalPosition,
        exit_price: Decimal,
        exit_date: str,
        exit_reason: str,
        bars_held: int,
        exit_fee: Decimal,
        mfe_usd: Decimal,
        mae_usd: Decimal,
    ) -> HypotheticalClosedTrade:
        """Create a closed trade record, save it, and remove position from active tracking."""
        px_exit = quantize_cent(to_decimal(exit_price))
        fee_exit = quantize_cent(to_decimal(exit_fee))
        gross_pnl = quantize_cent((px_exit - position.entry_price) * Decimal(position.shares))
        net_pnl = quantize_cent(gross_pnl - position.entry_fee - fee_exit)
        cost_basis = (position.entry_price * Decimal(position.shares)) + position.entry_fee
        ret_pct = quantize_cent(net_pnl / cost_basis) if cost_basis > 0 else Decimal("0.0000")

        now_str = datetime.now(timezone.utc).isoformat()
        trade_id = f"HTRD-{position.session_date}-{exit_date}-{position.ticker}"

        closed = HypotheticalClosedTrade(
            trade_id=trade_id,
            position_id=position.position_id,
            proposal_id=position.proposal_id,
            ticker=position.ticker,
            data_mode=position.data_mode,
            shares=position.shares,
            entry_date=position.session_date,
            entry_price=position.entry_price,
            exit_date=exit_date,
            exit_price=px_exit,
            entry_fee=position.entry_fee,
            exit_fee=fee_exit,
            gross_pnl=gross_pnl,
            net_pnl=net_pnl,
            return_pct=ret_pct,
            exit_reason=exit_reason,
            bars_held=bars_held,
            mfe_usd=mfe_usd,
            mae_usd=mae_usd,
            closed_at=now_str,
            marker=MARKER_SIMULATED_TRADE,
        )
        self.storage.save_closed_trade(closed)
        self.storage.delete_position(position.position_id)
        return closed
