"""Independent simulated portfolio and trade tracker for trader-swing backtesting.

Enforces:
1. Reuses authoritative Phase 1 AccountingLedger with strict equity invariant validation.
2. Lot-level protective stop and target tracking.
3. Accurate round-trip trade record creation (R-multiple, holding periods, fee & slippage deductions).
4. Daily portfolio mark-to-market and snapshot logging.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from accounting import (
    AccountingLedger,
    DividendRecord,
    Lot,
    quantize_cent,
    to_decimal,
)
from backtest_models import (
    PortfolioDailySnapshot,
    SimulatedOrder,
    SimulatedTradeRecord,
)
from risk import PortfolioRiskMetrics, RiskEngine


class BacktestPortfolio:
    """Manages accounting ledger state, stops, targets, and round-trip trade history."""

    def __init__(
        self,
        initial_capital_usd: Decimal = Decimal("5000.00"),
        risk_engine: Optional[RiskEngine] = None,
    ):
        self.ledger = AccountingLedger(initial_capital=initial_capital_usd)
        self.risk_engine = risk_engine or RiskEngine()

        # lot_id -> Stop Loss Price
        self.lot_stops: Dict[str, Decimal] = {}
        # lot_id -> Take Profit Price
        self.lot_targets: Dict[str, Decimal] = {}
        # lot_id -> Metadata (entry_timestamp, initial_stop, initial_target, entry_slippage)
        self.lot_meta: Dict[str, Dict[str, Any]] = {}

        # Round-trip closed trade history
        self.closed_trades: List[SimulatedTradeRecord] = []
        # Daily end-of-session snapshots
        self.daily_snapshots: List[PortfolioDailySnapshot] = []

    def execute_entry(self, filled_order: SimulatedOrder) -> str:
        """Record buy execution into the ledger and register protective stops and targets."""
        ticker = filled_order.ticker
        shares = filled_order.quantity
        fill_price = filled_order.fill_price
        fee = filled_order.fee_usd
        ts_str = filled_order.fill_timestamp.isoformat()

        # Buy lot in ledger
        lot: Lot = self.ledger.buy(
            ticker=ticker,
            shares=shares,
            price=fill_price,
            fee=fee,
            timestamp=ts_str,
        )

        lot_id = lot.lot_id
        filled_order.lot_id = lot_id

        # Register stops and targets
        stop_px = filled_order.stop_loss_price or (fill_price * Decimal("0.95"))
        target_px = filled_order.target_price or (fill_price * Decimal("1.10"))

        self.lot_stops[lot_id] = stop_px
        self.lot_targets[lot_id] = target_px
        self.lot_meta[lot_id] = {
            "entry_timestamp": filled_order.fill_timestamp,
            "entry_price": fill_price,
            "initial_stop": stop_px,
            "initial_target": target_px,
            "entry_slippage": filled_order.slippage_usd,
            "entry_fee": fee,
            "shares": shares,
        }

        return lot_id

    def execute_exit(self, filled_order: SimulatedOrder) -> SimulatedTradeRecord:
        """Record sell execution into the ledger and finalize round-trip trade record."""
        if filled_order.fill_price <= Decimal("0.00"):
            return self.execute_delisting_liquidation(filled_order)

        ticker = filled_order.ticker
        shares = filled_order.quantity
        fill_price = filled_order.fill_price
        fee = filled_order.fee_usd
        lot_id = filled_order.lot_id
        ts_str = filled_order.fill_timestamp.isoformat()

        meta = self.lot_meta.get(lot_id, {})
        entry_ts = meta.get("entry_timestamp", filled_order.fill_timestamp)
        entry_px = meta.get("entry_price", fill_price)
        initial_stop = meta.get("initial_stop", entry_px * Decimal("0.95"))
        initial_target = meta.get("initial_target", entry_px * Decimal("1.10"))
        entry_slippage = meta.get("entry_slippage", Decimal("0.00"))
        entry_fee = meta.get("entry_fee", Decimal("0.00"))

        # Execute sell in ledger (handles proportional/residual fees automatically)
        trade_dict = self.ledger.sell(
            ticker=ticker,
            shares=shares,
            price=fill_price,
            fee=fee,
            lot_id=lot_id,
            timestamp=ts_str,
        )

        # Calculate metrics
        gross_pnl = (fill_price - entry_px) * Decimal(shares)
        allocated_entry_fee = Decimal(str(trade_dict.get("allocated_entry_fee", entry_fee)))
        meta_shares = meta.get("shares", shares)
        if meta_shares and meta_shares > 0:
            allocated_entry_slippage = quantize_cent(entry_slippage * (Decimal(shares) / Decimal(meta_shares)))
        else:
            allocated_entry_slippage = Decimal("0.00")

        total_fees = allocated_entry_fee + fee
        total_slippage = allocated_entry_slippage + filled_order.slippage_usd
        net_pnl = Decimal(str(trade_dict["realized_net_pnl"]))

        return_pct = Decimal("0.00")
        if entry_px > Decimal("0.00"):
            return_pct = ((fill_price - entry_px) / entry_px) * Decimal("100.0")
        return_pct = return_pct.quantize(Decimal("0.01"))

        # R-Multiple: (Fill Price - Entry Price) / (Entry Price - Initial Stop Price)
        initial_risk_per_share = entry_px - initial_stop
        r_mult = Decimal("0.00")
        if initial_risk_per_share > Decimal("0.00"):
            r_mult = (fill_price - entry_px) / initial_risk_per_share
        r_mult = r_mult.quantize(Decimal("0.01"))

        # Holding duration
        holding_sessions = max(1, (filled_order.fill_timestamp.date() - entry_ts.date()).days)

        trade_count_for_lot = len([t for t in self.closed_trades if t.lot_id == lot_id])
        trade_id = f"TRD-{lot_id}" if trade_count_for_lot == 0 else f"TRD-{lot_id}-{trade_count_for_lot + 1}"

        trade_record = SimulatedTradeRecord(
            trade_id=trade_id,
            ticker=ticker,
            lot_id=lot_id,
            entry_timestamp=entry_ts,
            exit_timestamp=filled_order.fill_timestamp,
            holding_sessions=holding_sessions,
            shares=shares,
            entry_price=entry_px,
            exit_price=fill_price,
            initial_stop_price=initial_stop,
            initial_target_price=initial_target,
            gross_pnl_usd=quantize_cent(gross_pnl),
            total_fees_usd=total_fees,
            total_slippage_usd=total_slippage,
            net_pnl_usd=net_pnl,
            return_pct=return_pct,
            r_multiple=r_mult,
            exit_reason=filled_order.reason,
        )

        self.closed_trades.append(trade_record)

        # Inspect actual resulting lot state in AccountingLedger
        lot = None
        pos = self.ledger.positions.get(ticker.upper())
        if pos is not None:
            for l in pos.lots:
                if l.lot_id == lot_id:
                    lot = l
                    break

        if lot is None or lot.status == "CLOSED" or lot.shares_remaining <= 0:
            # Full exit: clean up all associated protection state
            if lot_id in self.lot_stops:
                del self.lot_stops[lot_id]
            if lot_id in self.lot_targets:
                del self.lot_targets[lot_id]
            if lot_id in self.lot_meta:
                del self.lot_meta[lot_id]
        else:
            # Partial exit: preserve stop and target, update remaining shares
            if lot_id in self.lot_meta:
                self.lot_meta[lot_id]["shares"] = lot.shares_remaining
                self.lot_meta[lot_id]["entry_slippage"] = max(Decimal("0.00"), entry_slippage - allocated_entry_slippage)
                self.lot_meta[lot_id]["entry_fee"] = max(Decimal("0.00"), entry_fee - allocated_entry_fee)

        self.cleanup_protection_state()
        return trade_record

    def execute_delisting_liquidation(self, filled_order: SimulatedOrder) -> SimulatedTradeRecord:
        """Record delisting liquidation or write-off into the ledger and finalize trade record.

        Handles:
        1. Positive-price delisting liquidation (cash buyout, merger, OTC haircut):
           Executes through self.ledger.sell with configured exit fees.
        2. Zero-recovery delisting write-off (bankruptcy, undocumented):
           Executes through self.ledger.write_off with zero exit fee, closing the lot.
        """
        ticker = filled_order.ticker
        shares = filled_order.quantity
        fill_price = filled_order.fill_price
        fee = filled_order.fee_usd
        lot_id = filled_order.lot_id
        ts_str = filled_order.fill_timestamp.isoformat()

        # Obtain accounting lot from ledger to preserve original lot attributes if metadata is unavailable
        accounting_lot = None
        pos = self.ledger.positions.get(ticker.upper())
        if pos is not None:
            for l in pos.lots:
                if l.lot_id == lot_id:
                    accounting_lot = l
                    break

        meta = self.lot_meta.get(lot_id, {})
        entry_ts = meta.get("entry_timestamp")
        if entry_ts is None:
            if accounting_lot is not None and accounting_lot.entry_timestamp:
                try:
                    entry_ts = datetime.fromisoformat(accounting_lot.entry_timestamp)
                except Exception:
                    entry_ts = filled_order.fill_timestamp
            else:
                entry_ts = filled_order.fill_timestamp

        entry_px = meta.get("entry_price")
        if entry_px is None or (entry_px <= Decimal("0.00") and accounting_lot is not None and accounting_lot.entry_price > Decimal("0.00")):
            if accounting_lot is not None and accounting_lot.entry_price > Decimal("0.00"):
                entry_px = accounting_lot.entry_price
            else:
                entry_px = fill_price

        initial_stop = meta.get("initial_stop", self.lot_stops.get(lot_id, entry_px * Decimal("0.95")))
        initial_target = meta.get("initial_target", self.lot_targets.get(lot_id, entry_px * Decimal("1.10")))
        entry_slippage = meta.get("entry_slippage", Decimal("0.00"))
        entry_fee = meta.get("entry_fee", accounting_lot.entry_fee_remaining if accounting_lot is not None else Decimal("0.00"))

        meta_shares = meta.get("shares", accounting_lot.shares_remaining if accounting_lot is not None else shares)
        if meta_shares and meta_shares > 0:
            allocated_entry_slippage = quantize_cent(entry_slippage * (Decimal(shares) / Decimal(meta_shares)))
        else:
            allocated_entry_slippage = Decimal("0.00")

        if fill_price > Decimal("0.00"):
            trade_dict = self.ledger.sell(
                ticker=ticker,
                shares=shares,
                price=fill_price,
                fee=fee,
                lot_id=lot_id,
                timestamp=ts_str,
            )
            allocated_entry_fee = Decimal(str(trade_dict.get("allocated_entry_fee", entry_fee)))
            total_fees = allocated_entry_fee + fee
            total_slippage = allocated_entry_slippage + filled_order.slippage_usd
            gross_pnl = (fill_price - entry_px) * Decimal(shares)
            net_pnl = Decimal(str(trade_dict["realized_net_pnl"]))
            return_pct = Decimal("0.00")
            if entry_px > Decimal("0.00"):
                return_pct = (((fill_price - entry_px) / entry_px) * Decimal("100.0")).quantize(Decimal("0.01"))
        else:
            trade_dict = self.ledger.write_off(
                ticker=ticker,
                lot_id=lot_id,
                timestamp=ts_str,
            )
            allocated_entry_fee = Decimal(str(trade_dict.get("allocated_entry_fee", entry_fee)))
            total_fees = allocated_entry_fee
            total_slippage = allocated_entry_slippage
            gross_pnl = -(entry_px * Decimal(shares))
            net_pnl = Decimal(str(trade_dict["realized_net_pnl"]))
            return_pct = Decimal("-100.00")

        initial_risk_per_share = entry_px - initial_stop
        r_mult = Decimal("0.00")
        if initial_risk_per_share > Decimal("0.00"):
            r_mult = ((fill_price - entry_px) / initial_risk_per_share).quantize(Decimal("0.01"))

        holding_sessions = max(1, (filled_order.fill_timestamp.date() - entry_ts.date()).days)
        trade_count_for_lot = len([t for t in self.closed_trades if t.lot_id == lot_id])
        trade_id = f"TRD-{lot_id}" if trade_count_for_lot == 0 else f"TRD-{lot_id}-{trade_count_for_lot + 1}"

        trade_record = SimulatedTradeRecord(
            trade_id=trade_id,
            ticker=ticker,
            lot_id=lot_id,
            entry_timestamp=entry_ts,
            exit_timestamp=filled_order.fill_timestamp,
            holding_sessions=holding_sessions,
            shares=shares,
            entry_price=entry_px,
            exit_price=fill_price,
            initial_stop_price=initial_stop,
            initial_target_price=initial_target,
            gross_pnl_usd=quantize_cent(gross_pnl),
            total_fees_usd=total_fees,
            total_slippage_usd=total_slippage,
            net_pnl_usd=net_pnl,
            return_pct=return_pct,
            r_multiple=r_mult,
            exit_reason=filled_order.reason or "DELISTING_LIQUIDATION",
        )
        self.closed_trades.append(trade_record)

        if lot_id in self.lot_stops:
            del self.lot_stops[lot_id]
        if lot_id in self.lot_targets:
            del self.lot_targets[lot_id]
        if lot_id in self.lot_meta:
            del self.lot_meta[lot_id]

        self.cleanup_protection_state()
        return trade_record

    def apply_stock_split(
        self,
        ticker: str,
        ratio: Decimal,
        split_numerator: Optional[int] = None,
        split_denominator: Optional[int] = None,
        cil_settlement_price: Optional[Decimal] = None,
        effective_date: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Adjust open lots, stops, targets, and cost basis display for a stock split.

        For forward splits (ratio >= 1.0):
          Applies integer shares multiplication, rounds prices to cent.
        For reverse splits (ratio < 1.0):
          Reuses authoritative Phase 1 AccountingLedger.apply_reverse_split() with
          exact integer shares entitlement, fractional Cash-in-Lieu (CIL) calculation,
          and residual cent protection.

        Pure Decimal arithmetic. Invariant Assets == Claims is preserved down to the exact cent.
        """
        sym = ticker.upper()
        if ratio <= Decimal("0.0"):
            raise ValueError(f"Split ratio must be positive, got {ratio}")
        if sym not in self.ledger.positions:
            return None
        pos = self.ledger.positions[sym]
        if pos.total_shares == 0:
            return None

        eff_date_str = effective_date or datetime.now().date().isoformat()

        # Case A: Reverse Split (ratio < 1.0) -> Reuse Phase 1 AccountingLedger logic
        if ratio < Decimal("1.0"):
            if split_numerator is not None and split_denominator is not None:
                num, denom = int(split_numerator), int(split_denominator)
            else:
                frac = Fraction(str(ratio)).limit_denominator(1000)
                num, denom = frac.numerator, frac.denominator

            # Determine CIL settlement price: use explicit price, post-split mark price, or post-split entry price
            if cil_settlement_price is not None:
                cil_px = quantize_cent(cil_settlement_price)
            elif pos.mark_price > Decimal("0.00"):
                factor = Decimal(num) / Decimal(denom)
                cil_px = quantize_cent(pos.mark_price / factor)
            else:
                first_lot = next((lot for lot in pos.lots if lot.status == "OPEN"), None)
                base_px = first_lot.entry_price if first_lot else Decimal("10.00")
                factor = Decimal(num) / Decimal(denom)
                cil_px = quantize_cent(base_px / factor)

            # Authoritative Phase 1 reverse split with fractional CIL calculation
            summary = self.ledger.apply_reverse_split(
                ticker=sym,
                split_numerator=num,
                split_denominator=denom,
                cil_settlement_price=cil_px,
                effective_date=eff_date_str,
            )

            # Update lot stops, targets, and metadata
            factor = Decimal(num) / Decimal(denom)
            for lot in pos.lots:
                lot_id = lot.lot_id
                if lot.status == "CLOSED":
                    # Fully liquidated by reverse split
                    if lot_id in self.lot_stops:
                        del self.lot_stops[lot_id]
                    if lot_id in self.lot_targets:
                        del self.lot_targets[lot_id]
                    if lot_id in self.lot_meta:
                        del self.lot_meta[lot_id]
                else:
                    # Adjust stops, targets, and metadata by reverse split factor
                    if lot_id in self.lot_stops:
                        self.lot_stops[lot_id] = quantize_cent(self.lot_stops[lot_id] / factor)
                    if lot_id in self.lot_targets:
                        self.lot_targets[lot_id] = quantize_cent(self.lot_targets[lot_id] / factor)
                    if lot_id in self.lot_meta:
                        meta = self.lot_meta[lot_id]
                        meta["entry_price"] = lot.entry_price
                        meta["initial_stop"] = quantize_cent(meta["initial_stop"] / factor)
                        meta["initial_target"] = quantize_cent(meta["initial_target"] / factor)
                        meta["shares"] = lot.shares_remaining

            return summary

        # Case B: Forward Split (ratio >= 1.0)
        for lot in pos.lots:
            if lot.status == "OPEN":
                old_shares = lot.shares_remaining
                new_shares = int(Decimal(old_shares) * ratio)
                lot.shares_remaining = new_shares
                lot.shares_initial = int(Decimal(lot.shares_initial) * ratio)
                lot_id = lot.lot_id
                if new_shares == 0:
                    lot.status = "CLOSED"
                    if lot_id in self.lot_stops:
                        del self.lot_stops[lot_id]
                    if lot_id in self.lot_targets:
                        del self.lot_targets[lot_id]
                    if lot_id in self.lot_meta:
                        del self.lot_meta[lot_id]
                    continue
                if ratio != Decimal("0.0"):
                    lot.entry_price = quantize_cent(lot.entry_price / ratio)

                if lot_id in self.lot_stops:
                    self.lot_stops[lot_id] = quantize_cent(self.lot_stops[lot_id] / ratio)
                if lot_id in self.lot_targets:
                    self.lot_targets[lot_id] = quantize_cent(self.lot_targets[lot_id] / ratio)
                if lot_id in self.lot_meta:
                    meta = self.lot_meta[lot_id]
                    meta["entry_price"] = quantize_cent(meta["entry_price"] / ratio)
                    meta["initial_stop"] = quantize_cent(meta["initial_stop"] / ratio)
                    meta["initial_target"] = quantize_cent(meta["initial_target"] / ratio)
                    meta["shares"] = new_shares

        # Adjust position mark price if positive
        if pos.mark_price > Decimal("0.00"):
            pos.mark_price = quantize_cent(pos.mark_price / ratio)

        self.cleanup_protection_state()
        self.ledger.validate_equity_invariant()
        return {
            "ticker": sym,
            "split_ratio": f"{ratio}-for-1",
            "effective_date": eff_date_str,
            "status": "APPLIED",
        }

    def cleanup_protection_state(self) -> None:
        """Ensure no orphaned stops, targets, or lot metadata exist for non-existent or closed lots."""
        active_lot_ids = set()
        for pos in self.ledger.positions.values():
            for lot in pos.lots:
                if lot.status == "OPEN" and lot.shares_remaining > 0:
                    active_lot_ids.add(lot.lot_id)

        for lid in list(self.lot_stops.keys()):
            if lid not in active_lot_ids:
                del self.lot_stops[lid]
        for lid in list(self.lot_targets.keys()):
            if lid not in active_lot_ids:
                del self.lot_targets[lid]
        for lid in list(self.lot_meta.keys()):
            if lid not in active_lot_ids:
                del self.lot_meta[lid]

    def accrue_dividend(
        self,
        ticker: str,
        ex_date: str,
        amount_per_share: Decimal,
        payable_date: Optional[str] = None,
    ) -> Optional[DividendRecord]:
        """Accrue dividend receivable and recognize income on ex-date if ticker is held.

        If payable_date is omitted, a standard 14-day estimated settlement lag is applied
        to prevent premature liquidity on ex-date.
        """
        sym = ticker.upper()
        if sym in self.ledger.positions and self.ledger.positions[sym].total_shares > 0:
            record = self.ledger.accrue_dividend(
                ticker=sym,
                ex_date=ex_date,
                dividend_per_share=amount_per_share,
            )
            if payable_date is not None:
                record.payable_date = payable_date
            else:
                # Default 14 calendar day settlement lag estimate (never ex-date)
                ex_d = date.fromisoformat(ex_date) if isinstance(ex_date, str) else ex_date
                record.payable_date = (ex_d + timedelta(days=14)).isoformat()
            return record
        return None

    def settle_pending_dividends(self, current_date: date) -> List[DividendRecord]:
        """Settle all pending dividends whose payable date is on or before current_date."""
        settled: List[DividendRecord] = []
        for div in list(self.ledger.dividends):
            if div.status == "PENDING_PAYMENT":
                if div.payable_date is not None:
                    p_date = (
                        date.fromisoformat(div.payable_date)
                        if isinstance(div.payable_date, str)
                        else div.payable_date
                    )
                    if p_date <= current_date:
                        record = self.ledger.settle_dividend(
                            dividend_id=div.dividend_id,
                            payable_date=current_date.isoformat(),
                        )
                        settled.append(record)
        return settled

    def update_trailing_stop(self, lot_id: str, new_stop: Decimal) -> bool:
        """Raise protective stop for a lot."""
        if lot_id in self.lot_stops:
            current_stop = self.lot_stops[lot_id]
            if new_stop > current_stop:
                self.lot_stops[lot_id] = new_stop
                return True
        return False

    def mark_to_market(self, close_prices: Dict[str, Decimal]) -> None:
        """Update mark prices for all open positions."""
        for ticker, pos in self.ledger.positions.items():
            if pos.total_shares > 0 and ticker in close_prices:
                self.ledger.update_market_price(ticker, close_prices[ticker])

    def record_daily_snapshot(
        self,
        session_date: date,
        session_timestamp: datetime,
        market_regime: str = "UNKNOWN",
    ) -> PortfolioDailySnapshot:
        """Capture end-of-day financial, equity, and risk state."""
        # Calculate risk metrics
        risk_metrics: PortfolioRiskMetrics = self.risk_engine.calculate_portfolio_risk(
            ledger=self.ledger,
            current_stops=self.lot_stops,
        )

        snapshot = PortfolioDailySnapshot(
            session_date=session_date,
            timestamp=session_timestamp,
            equity_usd=self.ledger.total_equity_usd,
            cash_usd=self.ledger.cash_usd,
            gross_market_value_usd=self.ledger.gross_market_value_usd,
            invested_capital_pct=risk_metrics.invested_capital_pct,
            unrealized_pnl_usd=self.ledger.unrealized_trading_pnl_usd,
            cumulative_realized_pnl_usd=self.ledger.cumulative_realized_trading_pnl_usd,
            open_positions_count=risk_metrics.open_positions_count,
            open_lots_count=risk_metrics.total_open_lots_count,
            buffered_open_risk_usd=risk_metrics.total_buffered_open_risk_usd,
            aggregate_risk_pct=risk_metrics.aggregate_risk_pct,
            market_regime=market_regime,
        )

        self.daily_snapshots.append(snapshot)
        return snapshot

    def get_total_buffered_open_risk(self) -> Decimal:
        """Calculate and return aggregate buffered open risk across all active lots."""
        risk_metrics: PortfolioRiskMetrics = self.risk_engine.calculate_portfolio_risk(
            ledger=self.ledger,
            current_stops=self.lot_stops,
        )
        return risk_metrics.total_buffered_open_risk_usd

    def credit_cash_interest(self, interest_amount: Decimal) -> None:
        """Credit daily T-Bill cash yield interest to the accounting ledger."""
        amt = quantize_cent(interest_amount)
        if amt > Decimal("0.00"):
            self.ledger.deposit(amt)
