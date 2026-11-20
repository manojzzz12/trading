"""Core precision accounting ledger for trader-swing.

Strict financial precision using Python Decimal and ROUND_HALF_UP cent quantization.
Enforces the fundamental equity invariant after every transaction.
Supports multi-lot tracking, proportional fee amortization on partial exits,
two-stage dividend accounting, and exact integer reverse-split CIL mathematics.
"""

from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Dict, List, Optional, Any
import datetime


CENT = Decimal("0.01")


class AccountingError(Exception):
    """Base exception for accounting failures."""
    pass


class AccountingInvariantViolation(AccountingError):
    """Raised when the fundamental equity invariant fails to reconcile."""
    pass


class InvalidFinancialValue(AccountingError):
    """Raised when an invalid, non-finite, negative, or binary float value is supplied."""
    pass


class InsufficientCashError(AccountingError):
    """Raised when a transaction requires more cash than available."""
    pass


class PositionNotFoundError(AccountingError):
    """Raised when referencing a ticker that is not in the portfolio."""
    pass


class LotNotFoundError(AccountingError):
    """Raised when referencing a lot ID that does not exist."""
    pass


class DividendError(AccountingError):
    """Raised when dividend accrual or settlement fails."""
    pass


def to_decimal(val: Any) -> Decimal:
    """Convert input to a valid, finite Decimal. Reject binary floats and non-finite values."""
    if isinstance(val, float):
        raise InvalidFinancialValue(
            f"Binary floating-point value ({val}) is not permitted in financial ledger. "
            "Pass Decimal, integer, or exact decimal string."
        )
    if isinstance(val, Decimal):
        d = val
    elif isinstance(val, (int, str)):
        try:
            d = Decimal(str(val).strip())
        except (InvalidOperation, ValueError) as err:
            raise InvalidFinancialValue(f"Cannot parse value '{val}' as Decimal: {err}") from err
    else:
        raise InvalidFinancialValue(f"Unsupported type for financial value: {type(val)}")

    if d.is_nan() or d.is_infinite():
        raise InvalidFinancialValue(f"Non-finite Decimal value is not permitted: {d}")
    return d


def quantize_cent(val: Any) -> Decimal:
    """Quantize to 2 decimal places using ROUND_HALF_UP."""
    d = to_decimal(val)
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


def validate_positive_shares(shares: Any) -> int:
    """Ensure share count is a positive integer."""
    if not isinstance(shares, int) or isinstance(shares, bool):
        raise InvalidFinancialValue(f"Shares must be an integer, got: {type(shares)}")
    if shares <= 0:
        raise InvalidFinancialValue(f"Share count must be strictly positive (> 0), got: {shares}")
    return shares


def validate_non_negative_amount(amount: Any, name: str = "Amount") -> Decimal:
    """Ensure a monetary amount is non-negative."""
    d = to_decimal(amount)
    if d < Decimal("0.00"):
        raise InvalidFinancialValue(f"{name} cannot be negative, got: {d}")
    return quantize_cent(d)


class Lot:
    """Represents a discrete tax lot of shares purchased together."""

    def __init__(
        self,
        lot_id: str,
        ticker: str,
        shares_initial: int,
        shares_remaining: int,
        entry_price: Decimal,
        entry_fee_initial: Decimal,
        entry_fee_remaining: Decimal,
        total_lot_cost_basis_usd: Decimal,
        entry_timestamp: str,
        status: str = "OPEN",
    ):
        self.lot_id = lot_id
        self.ticker = ticker.upper()
        self.shares_initial = validate_positive_shares(shares_initial)
        if not isinstance(shares_remaining, int) or shares_remaining < 0:
            raise InvalidFinancialValue(f"Shares remaining must be non-negative integer, got {shares_remaining}")
        self.shares_remaining = shares_remaining
        self.entry_price = quantize_cent(entry_price)
        self.entry_fee_initial = quantize_cent(entry_fee_initial)
        self.entry_fee_remaining = quantize_cent(entry_fee_remaining)
        self.total_lot_cost_basis_usd = quantize_cent(total_lot_cost_basis_usd)
        self.entry_timestamp = str(entry_timestamp)
        self.status = "CLOSED" if self.shares_remaining == 0 else status

    def to_dict(self) -> Dict[str, Any]:
        return {
            "lot_id": self.lot_id,
            "ticker": self.ticker,
            "shares_initial": self.shares_initial,
            "shares_remaining": self.shares_remaining,
            "entry_price": str(self.entry_price),
            "entry_fee_initial": str(self.entry_fee_initial),
            "entry_fee_remaining": str(self.entry_fee_remaining),
            "total_lot_cost_basis_usd": str(self.total_lot_cost_basis_usd),
            "entry_timestamp": self.entry_timestamp,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Lot":
        return cls(
            lot_id=d["lot_id"],
            ticker=d["ticker"],
            shares_initial=int(d["shares_initial"]),
            shares_remaining=int(d["shares_remaining"]),
            entry_price=to_decimal(d["entry_price"]),
            entry_fee_initial=to_decimal(d["entry_fee_initial"]),
            entry_fee_remaining=to_decimal(d["entry_fee_remaining"]),
            total_lot_cost_basis_usd=to_decimal(d["total_lot_cost_basis_usd"]),
            entry_timestamp=d["entry_timestamp"],
            status=d.get("status", "OPEN"),
        )


class Position:
    """Maintains active lots and aggregates metrics for a single ticker."""

    def __init__(self, ticker: str):
        self.ticker = ticker.upper()
        self.lots: List[Lot] = []
        self.mark_price: Decimal = Decimal("0.00")

    @property
    def total_shares(self) -> int:
        return sum(lot.shares_remaining for lot in self.lots if lot.status == "OPEN")

    @property
    def total_cost_basis_usd(self) -> Decimal:
        return sum(
            (lot.total_lot_cost_basis_usd for lot in self.lots if lot.status == "OPEN"),
            Decimal("0.00"),
        )

    @property
    def total_entry_fee_remaining_usd(self) -> Decimal:
        return sum(
            (lot.entry_fee_remaining for lot in self.lots if lot.status == "OPEN"),
            Decimal("0.00"),
        )

    @property
    def gross_market_value_usd(self) -> Decimal:
        if self.total_shares == 0:
            return Decimal("0.00")
        return quantize_cent(Decimal(self.total_shares) * self.mark_price)

    @property
    def unrealized_trading_pnl_usd(self) -> Decimal:
        """MTM Market Value minus Open Cost Basis minus Remaining Unamortized Entry Fees."""
        if self.total_shares == 0:
            return Decimal("0.00")
        return self.gross_market_value_usd - self.total_cost_basis_usd - self.total_entry_fee_remaining_usd

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "mark_price": str(self.mark_price),
            "total_shares": self.total_shares,
            "total_cost_basis_usd": str(self.total_cost_basis_usd),
            "total_entry_fee_remaining_usd": str(self.total_entry_fee_remaining_usd),
            "gross_market_value_usd": str(self.gross_market_value_usd),
            "unrealized_trading_pnl_usd": str(self.unrealized_trading_pnl_usd),
            "lots": [lot.to_dict() for lot in self.lots],
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Position":
        pos = cls(ticker=d["ticker"])
        pos.mark_price = to_decimal(d.get("mark_price", "0.00"))
        pos.lots = [Lot.from_dict(ld) for ld in d.get("lots", [])]
        return pos


class DividendRecord:
    """Tracks an accrual and settlement of a corporate dividend."""

    def __init__(
        self,
        dividend_id: str,
        ticker: str,
        ex_date: str,
        dividend_per_share: Decimal,
        eligible_shares: int,
        amount_usd: Decimal,
        payable_date: Optional[str] = None,
        status: str = "PENDING_PAYMENT",
    ):
        self.dividend_id = dividend_id
        self.ticker = ticker.upper()
        self.ex_date = ex_date
        self.dividend_per_share = to_decimal(dividend_per_share)
        self.eligible_shares = validate_positive_shares(eligible_shares)
        self.amount_usd = quantize_cent(amount_usd)
        self.payable_date = payable_date
        self.status = status

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dividend_id": self.dividend_id,
            "ticker": self.ticker,
            "ex_date": self.ex_date,
            "dividend_per_share": str(self.dividend_per_share),
            "eligible_shares": self.eligible_shares,
            "amount_usd": str(self.amount_usd),
            "payable_date": self.payable_date,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DividendRecord":
        return cls(
            dividend_id=d["dividend_id"],
            ticker=d["ticker"],
            ex_date=d["ex_date"],
            dividend_per_share=to_decimal(d["dividend_per_share"]),
            eligible_shares=int(d["eligible_shares"]),
            amount_usd=to_decimal(d["amount_usd"]),
            payable_date=d.get("payable_date"),
            status=d.get("status", "PENDING_PAYMENT"),
        )


class AccountingLedger:
    """Authoritative double-entry financial ledger for trader-swing.

    Identity:
    Cash + Dividends Receivable + Gross Market Value
    ==
    Initial Capital + Realized Trading P&L + Cumulative Dividend Income + Unrealized Trading P&L
    """

    def __init__(self, initial_capital: Any = Decimal("5000.00")):
        init_cap = to_decimal(initial_capital)
        if init_cap <= Decimal("0.00"):
            raise InvalidFinancialValue(f"Initial capital must be positive, got: {init_cap}")
        self.initial_capital_usd: Decimal = quantize_cent(init_cap)
        self.cash_usd: Decimal = self.initial_capital_usd
        self.positions: Dict[str, Position] = {}
        self.closed_trades: List[Dict[str, Any]] = []
        self.dividends: List[DividendRecord] = []
        self.cumulative_realized_trading_pnl_usd: Decimal = Decimal("0.00")
        self.cumulative_dividend_income_usd: Decimal = Decimal("0.00")
        self._lot_counter: int = 0
        self._dividend_counter: int = 0
        self.validate_equity_invariant()

    # --- Property Queries ---

    @property
    def dividends_receivable_usd(self) -> Decimal:
        """Accrued dividends not yet paid."""
        return sum(
            (d.amount_usd for d in self.dividends if d.status == "PENDING_PAYMENT"),
            Decimal("0.00"),
        )

    @property
    def gross_market_value_usd(self) -> Decimal:
        """Sum of market value of all open positions."""
        return sum(
            (p.gross_market_value_usd for p in self.positions.values()),
            Decimal("0.00"),
        )

    @property
    def unrealized_trading_pnl_usd(self) -> Decimal:
        """Sum of net unrealized trading P&L across all open positions."""
        return sum(
            (p.unrealized_trading_pnl_usd for p in self.positions.values()),
            Decimal("0.00"),
        )

    @property
    def total_equity_usd(self) -> Decimal:
        """Total portfolio equity (Cash + Dividends Receivable + Gross Market Value)."""
        return self.cash_usd + self.dividends_receivable_usd + self.gross_market_value_usd

    # --- Invariant Validation ---

    def validate_equity_invariant(self) -> Dict[str, Any]:
        """Verify the fundamental accounting identity down to the exact cent.

        Cash + Dividends Receivable + Gross Market Value
        ==
        Initial Capital + Realized Trading P&L + Cumulative Dividend Income + Unrealized Trading P&L
        """
        assets = quantize_cent(self.cash_usd + self.dividends_receivable_usd + self.gross_market_value_usd)
        claims = quantize_cent(
            self.initial_capital_usd
            + self.cumulative_realized_trading_pnl_usd
            + self.cumulative_dividend_income_usd
            + self.unrealized_trading_pnl_usd
        )
        discrepancy = assets - claims

        if discrepancy != Decimal("0.00"):
            raise AccountingInvariantViolation(
                f"Accounting invariant violated! "
                f"Assets (Cash {self.cash_usd} + DivRec {self.dividends_receivable_usd} + GMV {self.gross_market_value_usd} = {assets}) != "
                f"Claims (InitCap {self.initial_capital_usd} + Realized {self.cumulative_realized_trading_pnl_usd} + DivIncome {self.cumulative_dividend_income_usd} + Unrealized {self.unrealized_trading_pnl_usd} = {claims}). "
                f"Discrepancy: {discrepancy}"
            )

        return {
            "holds": True,
            "assets": assets,
            "claims": claims,
            "discrepancy": discrepancy,
        }

    # --- Trading Operations ---

    def buy(
        self,
        ticker: str,
        shares: int,
        price: Any,
        fee: Any = Decimal("0.00"),
        timestamp: Optional[str] = None,
        lot_id: Optional[str] = None,
    ) -> Lot:
        """Open a new lot of shares. Deducts total principal + entry fee from cash."""
        sym = ticker.upper()
        shs = validate_positive_shares(shares)
        px = to_decimal(price)
        if px <= Decimal("0.00"):
            raise InvalidFinancialValue(f"Entry price must be positive, got: {px}")
        entry_fee = validate_non_negative_amount(fee, "Entry fee")

        principal = quantize_cent(Decimal(shs) * px)
        total_cash_required = principal + entry_fee

        if total_cash_required > self.cash_usd:
            raise InsufficientCashError(
                f"Insufficient cash for buy: required {total_cash_required} "
                f"(principal {principal} + fee {entry_fee}), available {self.cash_usd}"
            )

        if lot_id:
            # Check for duplicate across all positions and historical lots (both OPEN and CLOSED)
            for existing_pos in self.positions.values():
                for existing_lot in existing_pos.lots:
                    if existing_lot.lot_id == lot_id:
                        raise InvalidFinancialValue(
                            f"Duplicate lot ID '{lot_id}' is not permitted. "
                            f"Lot already exists for ticker '{existing_pos.ticker}' (status: {existing_lot.status})."
                        )
            effective_lot_id = lot_id
        else:
            while True:
                self._lot_counter += 1
                effective_lot_id = f"LOT-{sym}-{self._lot_counter:04d}"
                collision = any(
                    any(l.lot_id == effective_lot_id for l in p.lots)
                    for p in self.positions.values()
                )
                if not collision:
                    break

        now_ts = timestamp or datetime.datetime.now(datetime.timezone.utc).isoformat()

        new_lot = Lot(
            lot_id=effective_lot_id,
            ticker=sym,
            shares_initial=shs,
            shares_remaining=shs,
            entry_price=px,
            entry_fee_initial=entry_fee,
            entry_fee_remaining=entry_fee,
            total_lot_cost_basis_usd=principal,
            entry_timestamp=now_ts,
            status="OPEN",
        )

        if sym not in self.positions:
            self.positions[sym] = Position(ticker=sym)

        pos = self.positions[sym]
        pos.lots.append(new_lot)
        pos.mark_price = px  # default mark to entry price

        self.cash_usd = quantize_cent(self.cash_usd - total_cash_required)
        self.validate_equity_invariant()
        return new_lot

    def sell(
        self,
        ticker: str,
        shares: int,
        price: Any,
        fee: Any = Decimal("0.00"),
        timestamp: Optional[str] = None,
        lot_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Exit shares (partial or full) using FIFO or a targeted lot ID.

        Proportionally amortizes entry fees. The final exit absorbs any remaining residual cents.
        """
        sym = ticker.upper()
        shs_to_sell = validate_positive_shares(shares)
        px = to_decimal(price)
        if px <= Decimal("0.00"):
            raise InvalidFinancialValue(f"Exit price must be positive, got: {px}")
        exit_fee = validate_non_negative_amount(fee, "Exit fee")

        if sym not in self.positions or self.positions[sym].total_shares == 0:
            raise PositionNotFoundError(f"No open position in {sym} to sell.")

        pos = self.positions[sym]
        if shs_to_sell > pos.total_shares:
            raise InvalidFinancialValue(
                f"Cannot sell {shs_to_sell} shares of {sym}; only {pos.total_shares} open shares available."
            )

        # Select candidate lots (FIFO by default, or specific lot)
        if lot_id:
            candidate_lots = [lot for lot in pos.lots if lot.lot_id == lot_id and lot.status == "OPEN"]
            if not candidate_lots:
                raise LotNotFoundError(f"Open lot with ID '{lot_id}' not found for {sym}.")
            if candidate_lots[0].shares_remaining < shs_to_sell:
                raise InvalidFinancialValue(
                    f"Lot '{lot_id}' has {candidate_lots[0].shares_remaining} shares, cannot sell {shs_to_sell}."
                )
        else:
            candidate_lots = [lot for lot in pos.lots if lot.status == "OPEN"]

        gross_proceeds = quantize_cent(Decimal(shs_to_sell) * px)
        now_ts = timestamp or datetime.datetime.now(datetime.timezone.utc).isoformat()

        shares_remaining_to_fill = shs_to_sell
        total_allocated_basis = Decimal("0.00")
        total_allocated_entry_fee = Decimal("0.00")
        executed_lot_fills = []

        for lot in candidate_lots:
            if shares_remaining_to_fill == 0:
                break

            fill_qty = min(shares_remaining_to_fill, lot.shares_remaining)
            is_final_for_lot = (fill_qty == lot.shares_remaining)

            if is_final_for_lot:
                # Absorb exact remaining unamortized basis and fee (residual cent protection)
                allocated_basis = lot.total_lot_cost_basis_usd
                allocated_entry_fee = lot.entry_fee_remaining
                lot.total_lot_cost_basis_usd = Decimal("0.00")
                lot.entry_fee_remaining = Decimal("0.00")
                lot.shares_remaining = 0
                lot.status = "CLOSED"
            else:
                # Proportional allocation
                proportion = Decimal(fill_qty) / Decimal(lot.shares_remaining)
                allocated_basis = quantize_cent(lot.total_lot_cost_basis_usd * proportion)
                allocated_entry_fee = quantize_cent(lot.entry_fee_remaining * proportion)

                lot.total_lot_cost_basis_usd -= allocated_basis
                lot.entry_fee_remaining -= allocated_entry_fee
                lot.shares_remaining -= fill_qty

            total_allocated_basis += allocated_basis
            total_allocated_entry_fee += allocated_entry_fee
            shares_remaining_to_fill -= fill_qty

            executed_lot_fills.append({
                "lot_id": lot.lot_id,
                "shares_sold": fill_qty,
                "allocated_basis": str(allocated_basis),
                "allocated_entry_fee": str(allocated_entry_fee),
                "shares_remaining_in_lot": lot.shares_remaining,
            })

        pos.mark_price = px

        # Realized P&L on this trade execution
        realized_net_pnl = gross_proceeds - total_allocated_basis - total_allocated_entry_fee - exit_fee
        self.cumulative_realized_trading_pnl_usd = quantize_cent(
            self.cumulative_realized_trading_pnl_usd + realized_net_pnl
        )

        net_cash_inflow = gross_proceeds - exit_fee
        self.cash_usd = quantize_cent(self.cash_usd + net_cash_inflow)

        trade_record = {
            "ticker": sym,
            "shares_sold": shs_to_sell,
            "exit_price": str(px),
            "gross_proceeds": str(gross_proceeds),
            "allocated_basis": str(total_allocated_basis),
            "allocated_entry_fee": str(total_allocated_entry_fee),
            "exit_fee": str(exit_fee),
            "realized_net_pnl": str(realized_net_pnl),
            "timestamp": now_ts,
            "lot_fills": executed_lot_fills,
        }
        self.closed_trades.append(trade_record)

        self.validate_equity_invariant()
        return trade_record

    def write_off(
        self,
        ticker: str,
        lot_id: str,
        timestamp: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Liquidate / write off an open lot at zero terminal value (e.g. bankruptcy delisting).

        Closes the lot, zeros remaining shares, and recognizes a total realized loss
        equal to unamortized cost basis and unamortized entry fees with zero exit fee.
        """
        sym = ticker.upper()
        if sym not in self.positions or self.positions[sym].total_shares == 0:
            raise PositionNotFoundError(f"No open position in {sym} to write off.")

        pos = self.positions[sym]
        candidate_lots = [lot for lot in pos.lots if lot.lot_id == lot_id and lot.status == "OPEN"]
        if not candidate_lots:
            raise LotNotFoundError(f"Open lot with ID '{lot_id}' not found for {sym}.")

        lot = candidate_lots[0]
        shs_to_write_off = lot.shares_remaining
        allocated_basis = lot.total_lot_cost_basis_usd
        allocated_entry_fee = lot.entry_fee_remaining

        lot.total_lot_cost_basis_usd = Decimal("0.00")
        lot.entry_fee_remaining = Decimal("0.00")
        lot.shares_remaining = 0
        lot.status = "CLOSED"

        if pos.total_shares == 0:
            pos.mark_price = Decimal("0.00")

        # Zero proceeds, zero exit fee -> realized net loss is -(basis + entry fee)
        realized_net_pnl = -(allocated_basis + allocated_entry_fee)
        self.cumulative_realized_trading_pnl_usd = quantize_cent(
            self.cumulative_realized_trading_pnl_usd + realized_net_pnl
        )

        now_ts = timestamp or datetime.datetime.now(datetime.timezone.utc).isoformat()
        trade_record = {
            "ticker": sym,
            "shares_sold": shs_to_write_off,
            "exit_price": "0.00",
            "gross_proceeds": "0.00",
            "allocated_basis": str(allocated_basis),
            "allocated_entry_fee": str(allocated_entry_fee),
            "exit_fee": "0.00",
            "realized_net_pnl": str(realized_net_pnl),
            "timestamp": now_ts,
            "lot_fills": [{
                "lot_id": lot.lot_id,
                "shares_sold": shs_to_write_off,
                "allocated_basis": str(allocated_basis),
                "allocated_entry_fee": str(allocated_entry_fee),
                "shares_remaining_in_lot": 0,
            }],
        }
        self.closed_trades.append(trade_record)

        self.validate_equity_invariant()
        return trade_record

    def update_market_price(self, ticker: str, mark_price: Any) -> None:
        """Mark position to current market price and verify invariant."""
        sym = ticker.upper()
        px = to_decimal(mark_price)
        if px < Decimal("0.00"):
            raise InvalidFinancialValue(f"Market mark price cannot be negative, got: {px}")
        if sym in self.positions:
            self.positions[sym].mark_price = px
        self.validate_equity_invariant()

    # --- Two-Stage Dividend Operations ---

    def accrue_dividend(
        self,
        ticker: str,
        ex_date: str,
        dividend_per_share: Any,
        eligible_shares: Optional[int] = None,
    ) -> DividendRecord:
        """Stage 1: Ex-Dividend Date accrual.

        Recognizes dividend receivable and dividend income exactly once.
        Does not assume stock price falls by the exact dividend amount.
        """
        sym = ticker.upper()
        d_per_sh = to_decimal(dividend_per_share)
        if d_per_sh <= Decimal("0.00"):
            raise InvalidFinancialValue(f"Dividend per share must be positive, got: {d_per_sh}")

        if eligible_shares is None:
            if sym not in self.positions or self.positions[sym].total_shares == 0:
                raise DividendError(f"Cannot accrue dividend for {sym}: no open position found.")
            shares_count = self.positions[sym].total_shares
        else:
            shares_count = validate_positive_shares(eligible_shares)

        div_amount = quantize_cent(Decimal(shares_count) * d_per_sh)

        self._dividend_counter += 1
        div_id = f"DIV-{sym}-{ex_date}-{self._dividend_counter:03d}"

        record = DividendRecord(
            dividend_id=div_id,
            ticker=sym,
            ex_date=ex_date,
            dividend_per_share=d_per_sh,
            eligible_shares=shares_count,
            amount_usd=div_amount,
            status="PENDING_PAYMENT",
        )
        self.dividends.append(record)

        # Recognize income on ex-date
        self.cumulative_dividend_income_usd = quantize_cent(
            self.cumulative_dividend_income_usd + div_amount
        )

        self.validate_equity_invariant()
        return record

    def settle_dividend(self, dividend_id: str, payable_date: str) -> DividendRecord:
        """Stage 2: Payable Date settlement.

        Transfers receivable into cash. Does NOT recognize dividend income again.
        """
        matching = [d for d in self.dividends if d.dividend_id == dividend_id]
        if not matching:
            raise DividendError(f"Dividend record with ID '{dividend_id}' not found.")
        record = matching[0]

        if record.status == "SETTLED":
            raise DividendError(f"Dividend '{dividend_id}' has already been settled.")

        self.cash_usd = quantize_cent(self.cash_usd + record.amount_usd)
        record.status = "SETTLED"
        record.payable_date = payable_date

        self.validate_equity_invariant()
        return record

    # --- Reverse Split & Cash-in-Lieu (CIL) Operations ---

    def apply_reverse_split(
        self,
        ticker: str,
        split_numerator: int,
        split_denominator: int,
        cil_settlement_price: Any,
        effective_date: str,
    ) -> Dict[str, Any]:
        """Apply reverse split with exact integer arithmetic and CIL residual-cent protection.

        Formula:
          total_num = old_shares * split_numerator
          whole_shares, rem_numerator = divmod(total_num, split_denominator)
          alpha_cil = rem_numerator / (old_shares * split_numerator)
        """
        sym = ticker.upper()
        if not isinstance(split_numerator, int) or split_numerator <= 0:
            raise InvalidFinancialValue("Split numerator must be positive integer.")
        if not isinstance(split_denominator, int) or split_denominator <= 0:
            raise InvalidFinancialValue("Split denominator must be positive integer.")
        if split_numerator >= split_denominator:
            raise InvalidFinancialValue("Reverse split must have numerator < denominator.")

        cil_px = to_decimal(cil_settlement_price)
        if cil_px < Decimal("0.00"):
            raise InvalidFinancialValue("CIL settlement price cannot be negative.")

        if sym not in self.positions or self.positions[sym].total_shares == 0:
            raise PositionNotFoundError(f"No open position in {sym} to apply reverse split.")

        pos = self.positions[sym]
        total_cil_proceeds = Decimal("0.00")
        total_cil_basis = Decimal("0.00")
        total_cil_fee = Decimal("0.00")
        lot_split_records = []

        for lot in pos.lots:
            if lot.status != "OPEN":
                continue

            old_shares = lot.shares_remaining
            total_num = old_shares * split_numerator
            whole_shares, rem_numerator = divmod(total_num, split_denominator)

            if rem_numerator > 0:
                # Fractional entitlement exists
                denom = Decimal(old_shares * split_numerator)
                alpha_cil = Decimal(rem_numerator) / denom

                # Proportional CIL allocation with residual cent protection
                cil_basis = quantize_cent(lot.total_lot_cost_basis_usd * alpha_cil)
                remaining_lot_basis = lot.total_lot_cost_basis_usd - cil_basis

                cil_fee = quantize_cent(lot.entry_fee_remaining * alpha_cil)
                remaining_lot_fee = lot.entry_fee_remaining - cil_fee

                # Cash in lieu proceeds: fractional entitlement * settlement price
                f_frac = Decimal(rem_numerator) / Decimal(split_denominator)
                cil_proceeds = quantize_cent(f_frac * cil_px)
                cil_pnl = cil_proceeds - cil_basis - cil_fee

                total_cil_proceeds += cil_proceeds
                total_cil_basis += cil_basis
                total_cil_fee += cil_fee

                lot.shares_remaining = whole_shares
                lot.total_lot_cost_basis_usd = remaining_lot_basis
                lot.entry_fee_remaining = remaining_lot_fee

                if whole_shares == 0:
                    lot.status = "CLOSED"
                else:
                    # Update per-share entry price display
                    lot.entry_price = quantize_cent(
                        remaining_lot_basis / Decimal(whole_shares)
                    )

                lot_split_records.append({
                    "lot_id": lot.lot_id,
                    "old_shares": old_shares,
                    "new_shares": whole_shares,
                    "fractional_shares": str(f_frac),
                    "cil_proceeds": str(cil_proceeds),
                    "cil_cost_basis": str(cil_basis),
                    "cil_entry_fee": str(cil_fee),
                    "cil_realized_pnl": str(cil_pnl),
                    "remaining_basis": str(remaining_lot_basis),
                    "remaining_fee": str(remaining_lot_fee),
                    "status": lot.status,
                })
            else:
                # Exact integer split (no fractional entitlement)
                lot.shares_remaining = whole_shares
                lot.entry_price = quantize_cent(
                    lot.total_lot_cost_basis_usd / Decimal(whole_shares)
                )
                lot_split_records.append({
                    "lot_id": lot.lot_id,
                    "old_shares": old_shares,
                    "new_shares": whole_shares,
                    "fractional_shares": "0.0",
                    "cil_proceeds": "0.00",
                    "cil_cost_basis": "0.00",
                    "cil_entry_fee": "0.00",
                    "cil_realized_pnl": "0.00",
                    "remaining_basis": str(lot.total_lot_cost_basis_usd),
                    "remaining_fee": str(lot.entry_fee_remaining),
                    "status": lot.status,
                })

        # Update cash and cumulative realized P&L from CIL
        net_cil_pnl = total_cil_proceeds - total_cil_basis - total_cil_fee
        self.cash_usd = quantize_cent(self.cash_usd + total_cil_proceeds)
        self.cumulative_realized_trading_pnl_usd = quantize_cent(
            self.cumulative_realized_trading_pnl_usd + net_cil_pnl
        )

        # Update position mark price to post-split level
        factor = Decimal(split_numerator) / Decimal(split_denominator)
        if pos.mark_price > Decimal("0.00"):
            pos.mark_price = quantize_cent(pos.mark_price / factor)

        split_summary = {
            "ticker": sym,
            "split_ratio": f"{split_numerator}-for-{split_denominator}",
            "effective_date": effective_date,
            "cil_settlement_price": str(cil_px),
            "total_cil_proceeds": str(total_cil_proceeds),
            "total_cil_cost_basis": str(total_cil_basis),
            "total_cil_entry_fee": str(total_cil_fee),
            "total_cil_net_pnl": str(net_cil_pnl),
            "lot_records": lot_split_records,
        }

        self.validate_equity_invariant()
        return split_summary

    # --- Serialization ---

    def to_dict(self) -> Dict[str, Any]:
        """Serialize complete ledger state to a dictionary with exact string Decimals."""
        self.validate_equity_invariant()
        return {
            "initial_capital_usd": str(self.initial_capital_usd),
            "cash_usd": str(self.cash_usd),
            "dividends_receivable_usd": str(self.dividends_receivable_usd),
            "gross_market_value_usd": str(self.gross_market_value_usd),
            "cumulative_realized_trading_pnl_usd": str(self.cumulative_realized_trading_pnl_usd),
            "cumulative_dividend_income_usd": str(self.cumulative_dividend_income_usd),
            "unrealized_trading_pnl_usd": str(self.unrealized_trading_pnl_usd),
            "total_equity_usd": str(self.total_equity_usd),
            "lot_counter": self._lot_counter,
            "dividend_counter": self._dividend_counter,
            "positions": {ticker: pos.to_dict() for ticker, pos in self.positions.items()},
            "closed_trades": self.closed_trades,
            "dividends": [d.to_dict() for d in self.dividends],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AccountingLedger":
        """Reconstruct ledger from a serialized dictionary and verify invariant."""
        init_cap = to_decimal(data["initial_capital_usd"])
        ledger = cls(initial_capital=init_cap)
        ledger.cash_usd = to_decimal(data["cash_usd"])
        ledger.cumulative_realized_trading_pnl_usd = to_decimal(data["cumulative_realized_trading_pnl_usd"])
        ledger.cumulative_dividend_income_usd = to_decimal(data.get("cumulative_dividend_income_usd", "0.00"))
        ledger._lot_counter = int(data.get("lot_counter", 0))
        ledger._dividend_counter = int(data.get("dividend_counter", 0))
        ledger.closed_trades = list(data.get("closed_trades", []))

        ledger.positions = {}
        for ticker, pos_dict in data.get("positions", {}).items():
            ledger.positions[ticker.upper()] = Position.from_dict(pos_dict)

        ledger.dividends = []
        for d_dict in data.get("dividends", []):
            ledger.dividends.append(DividendRecord.from_dict(d_dict))

        # Assert reconstructed state preserves the equity invariant
        ledger.validate_equity_invariant()
        return ledger
