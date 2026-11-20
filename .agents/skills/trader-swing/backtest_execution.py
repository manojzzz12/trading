"""Simulated order execution engine for trader-swing historical backtesting.

Enforces:
1. No look-ahead fill timing: Signals on completed bar T execute at session T+1 open.
2. Opening gap modeling: If market opens below stop loss, fill occurs at open (with adverse slippage).
3. Conservative same-bar tie-breaking: If both stop and target touched on same bar, stop executes first.
4. Comprehensive transaction fees and configurable slippage modeling.
5. Deterministic trade fills with complete audit trail.
"""

from datetime import datetime
from decimal import Decimal
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from backtest_models import (
    ExecutionAssumptions,
    SimulatedOrder,
    SimulatedOrderSide,
    SimulatedOrderStatus,
    SimulatedOrderType,
    SlippageConfig,
)
from market_data_models import OHLCVCandle
from risk import FeeConfig


class SimulatedExecutionEngine:
    """Simulates realistic fill mechanics for market entries and protective exits."""

    def __init__(
        self,
        fee_config: Optional[FeeConfig] = None,
        slippage_config: Optional[SlippageConfig] = None,
        assumptions: Optional[ExecutionAssumptions] = None,
    ):
        self.fee_config = fee_config or FeeConfig()
        self.slippage_config = slippage_config or SlippageConfig()
        self.assumptions = assumptions or ExecutionAssumptions()

    def simulate_entry_fill(
        self,
        order: SimulatedOrder,
        session_candle: OHLCVCandle,
        trailing_adv20_shares: Optional[int] = None,
        trailing_atr14: Optional[Decimal] = None,
    ) -> SimulatedOrder:
        """Simulate execution of a pending market entry order at session open.

        Enforces:
        1. Execution ADV capacity (1.0% of trailing share ADV) with immediate residual cancellation.
        2. Adverse entry slippage using trailing completed ATR14,t-1 (buyer pays higher).
        """
        if order.status != SimulatedOrderStatus.PENDING:
            return order

        # 1. Execution Capacity Enforcement (1% of trailing ADV20,t-1)
        if trailing_adv20_shares is not None:
            cap_shares = int(Decimal(trailing_adv20_shares) * Decimal("0.01"))
            if cap_shares <= 0:
                order.status = SimulatedOrderStatus.REJECTED
                order.rejection_reason = "REJECTED_ZERO_CAPACITY: Trailing ADV20 capacity is 0 shares."
                return order
            if order.quantity > cap_shares:
                residual = order.quantity - cap_shares
                order.quantity = cap_shares
                order.residual_cancelled_shares += residual

        open_px = session_candle.open
        fill_px = self.slippage_config.calculate_entry_fill_price(open_px, atr_value=trailing_atr14)
        fee = self.fee_config.entry.calculate_fee(order.quantity, fill_px)
        slippage_per_share = max(Decimal("0.00"), fill_px - open_px)
        slippage_usd = (slippage_per_share * Decimal(order.quantity)).quantize(Decimal("0.01"))

        order.fill_timestamp = session_candle.timestamp
        order.fill_price = fill_px
        order.fee_usd = fee
        order.slippage_usd = slippage_usd
        order.status = SimulatedOrderStatus.FILLED
        order.reason = f"Filled at session open (${open_px}) with ${fill_px} adverse fill."
        return order

    def evaluate_exit_conditions(
        self,
        ticker: str,
        lot_id: str,
        shares: int,
        stop_loss_price: Decimal,
        target_price: Decimal,
        session_candle: OHLCVCandle,
        force_exit: bool = False,
        force_reason: str = "REGIME_EXIT",
        trailing_atr14: Optional[Decimal] = None,
    ) -> Optional[SimulatedOrder]:
        """Check if an open lot touches stop-loss, target, or forced exit during the session."""
        open_px = session_candle.open
        high_px = session_candle.high
        low_px = session_candle.low
        close_px = session_candle.close

        # 1. Check for Forced Exit (Regime deterioration or time exit)
        if force_exit:
            fill_px = self.slippage_config.calculate_exit_fill_price(open_px, atr_value=trailing_atr14)
            fee = self.fee_config.exit.calculate_fee(shares, fill_px)
            slippage_usd = ((open_px - fill_px) * Decimal(shares)).quantize(Decimal("0.01"))
            return SimulatedOrder(
                order_id=f"EXIT-{lot_id}-{session_candle.timestamp.strftime('%Y%m%d')}",
                ticker=ticker,
                side=SimulatedOrderSide.SELL,
                order_type=SimulatedOrderType.REGIME_EXIT,
                quantity=shares,
                signal_timestamp=session_candle.timestamp,
                submission_timestamp=session_candle.timestamp,
                reference_price=open_px,
                lot_id=lot_id,
                fill_timestamp=session_candle.timestamp,
                fill_price=fill_px,
                slippage_usd=slippage_usd,
                fee_usd=fee,
                status=SimulatedOrderStatus.FILLED,
                reason=f"Forced exit ({force_reason}) executed at session open ${open_px}.",
            )

        stop_breached = low_px <= stop_loss_price
        target_reached = high_px >= target_price

        # 2. Both stop and target touched on same session bar
        if stop_breached and target_reached:
            if self.assumptions.conservative_same_bar_tie_break:
                # Conservative assumption: Stop loss triggers first!
                return self._build_stop_exit(ticker, lot_id, shares, stop_loss_price, session_candle, trailing_atr14=trailing_atr14)
            else:
                return self._build_target_exit(ticker, lot_id, shares, target_price, session_candle, trailing_atr14=trailing_atr14)

        # 3. Stop loss breached
        if stop_breached:
            return self._build_stop_exit(ticker, lot_id, shares, stop_loss_price, session_candle, trailing_atr14=trailing_atr14)

        # 4. Target reached
        if target_reached:
            return self._build_target_exit(ticker, lot_id, shares, target_price, session_candle, trailing_atr14=trailing_atr14)

        return None

    def _build_stop_exit(
        self,
        ticker: str,
        lot_id: str,
        shares: int,
        stop_loss_price: Decimal,
        session_candle: OHLCVCandle,
        trailing_atr14: Optional[Decimal] = None,
    ) -> SimulatedOrder:
        """Construct stop-loss fill accounting for opening gap down."""
        open_px = session_candle.open
        is_gap_down = open_px < stop_loss_price

        # If market opens below stop, fill occurs at open (adverse gap)
        base_trigger_px = open_px if is_gap_down else stop_loss_price
        fill_px = self.slippage_config.calculate_exit_fill_price(base_trigger_px, atr_value=trailing_atr14)
        fee = self.fee_config.exit.calculate_fee(shares, fill_px)
        slippage_usd = ((base_trigger_px - fill_px) * Decimal(shares)).quantize(Decimal("0.01"))

        reason = (
            f"Gap-down through stop (${stop_loss_price}) filled at open ${open_px} (fill: ${fill_px})."
            if is_gap_down
            else f"Intraday stop touched (${stop_loss_price}) filled at ${fill_px}."
        )

        return SimulatedOrder(
            order_id=f"STOP-{lot_id}-{session_candle.timestamp.strftime('%Y%m%d')}",
            ticker=ticker,
            side=SimulatedOrderSide.SELL,
            order_type=SimulatedOrderType.STOP_LOSS,
            quantity=shares,
            signal_timestamp=session_candle.timestamp,
            submission_timestamp=session_candle.timestamp,
            reference_price=base_trigger_px,
            lot_id=lot_id,
            fill_timestamp=session_candle.timestamp,
            fill_price=fill_px,
            slippage_usd=slippage_usd,
            fee_usd=fee,
            status=SimulatedOrderStatus.FILLED,
            reason=reason,
        )

    def _build_target_exit(
        self,
        ticker: str,
        lot_id: str,
        shares: int,
        target_price: Decimal,
        session_candle: OHLCVCandle,
        trailing_atr14: Optional[Decimal] = None,
    ) -> SimulatedOrder:
        """Construct take-profit target fill."""
        open_px = session_candle.open
        # If market opens above target, execute at open (favorable gap)
        base_trigger_px = max(open_px, target_price)
        fill_px = self.slippage_config.calculate_exit_fill_price(base_trigger_px, atr_value=trailing_atr14)
        fee = self.fee_config.exit.calculate_fee(shares, fill_px)
        slippage_usd = ((base_trigger_px - fill_px) * Decimal(shares)).quantize(Decimal("0.01"))

        return SimulatedOrder(
            order_id=f"TARGET-{lot_id}-{session_candle.timestamp.strftime('%Y%m%d')}",
            ticker=ticker,
            side=SimulatedOrderSide.SELL,
            order_type=SimulatedOrderType.TAKE_PROFIT,
            quantity=shares,
            signal_timestamp=session_candle.timestamp,
            submission_timestamp=session_candle.timestamp,
            reference_price=base_trigger_px,
            lot_id=lot_id,
            fill_timestamp=session_candle.timestamp,
            fill_price=fill_px,
            slippage_usd=slippage_usd,
            fee_usd=fee,
            status=SimulatedOrderStatus.FILLED,
            reason=f"Target reached (${target_price}) filled at ${fill_px}.",
        )
