"""Event-driven chronological backtest engine for trader-swing.

Enforces:
1. Strict event-driven simulation session-by-session.
2. ZERO LOOK-AHEAD BIAS: Signals generated at session T close execute at session T+1 open.
3. Stop losses and targets evaluated on subsequent price action with conservative same-bar tie-breaking.
4. Deterministic Phase 2 RiskEngine sizing and constraint enforcement at fill time.
5. Integration with MarketRegimeDetector, StockScanner, SignalEngine, and BacktestPortfolio.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
import os
import sys
from typing import Any, Dict, List, Optional, Sequence, Set

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from backtest_data import HistoricalDataManager, HistoricalDataValidationError
from backtest_execution import SimulatedExecutionEngine
from backtest_metrics import (
    BacktestMetricsCalculator,
    BenchmarkComparison,
    PortfolioMetrics,
    RiskAuditMetrics,
    TradeMetrics,
)
from backtest_models import (
    BacktestConfig,
    PortfolioDailySnapshot,
    PriceAdjustmentType,
    SimulatedOrder,
    SimulatedOrderSide,
    SimulatedOrderStatus,
    SimulatedOrderType,
    SimulatedTradeRecord,
    SlippageConfig,
    StockSplitEvent,
)
from backtest_portfolio import BacktestPortfolio
from market_data_models import EASTERN, MarketClock, OHLCVCandle
from regime_detector import MarketRegimeDetector
from regime_models import MarketRegime, MarketRegimeResult
from risk import FeeConfig, FeeSchedule, RiskConfig, RiskEngine, SizingResult
from scanner import StockScanner
from signal_engine import SignalEngine
from signal_models import ExitSignalType, ProposedTradePlan

from benchmark import (
    SPYTotalReturnCalculator,
    SPYTotalReturnResult,
    TBillCashYieldCalculator,
    CashYieldResult,
)
from historical_universe import (
    PointInTimeUniverseManager,
    HistoricalLiquidityGates,
    VolumeCapacityModel,
    FillPriceRiskValidator,
    DelistingCause,
)
from indicators import TechnicalIndicatorsEngine
from regime_detector import classify_composite_score


class EventDrivenBacktestEngine:
    """Simulates the trader-swing strategy chronologically bar by bar across historical sessions."""

    def __init__(
        self,
        config: BacktestConfig,
        data_manager: HistoricalDataManager,
        regime_detector: Optional[MarketRegimeDetector] = None,
        scanner: Optional[StockScanner] = None,
        signal_engine: Optional[SignalEngine] = None,
        universe_manager: Optional[PointInTimeUniverseManager] = None,
        enable_cash_yield: bool = False,
    ):
        self.config = config
        self.data_manager = data_manager
        self.universe_manager = universe_manager
        self.enable_cash_yield = enable_cash_yield
        self.cash_yield_calc = TBillCashYieldCalculator() if enable_cash_yield else None
        self.indicators_engine = TechnicalIndicatorsEngine()
        self.risk_engine = RiskEngine(risk_config=config.risk_config, fee_config=config.fee_config)
        self.regime_detector = regime_detector or MarketRegimeDetector()
        self.scanner = scanner or StockScanner()
        self.signal_engine = signal_engine or SignalEngine(risk_engine=self.risk_engine)

        self.execution_engine = SimulatedExecutionEngine(
            fee_config=config.fee_config,
            slippage_config=config.slippage_config,
            assumptions=config.execution_assumptions,
        )

        self.portfolio = BacktestPortfolio(
            initial_capital_usd=config.initial_capital_usd,
            risk_engine=self.risk_engine,
        )

        # Audit logs
        self.all_orders: List[SimulatedOrder] = []
        self.pending_entry_orders: List[SimulatedOrder] = []
        self.risk_rejection_counts: Dict[str, int] = {
            "RISK_LIMIT": 0,
            "POSITION_LIMIT": 0,
            "CASH_RESERVE": 0,
            "OTHER": 0,
        }

        # Enforce consistency between BacktestConfig and HistoricalDataManager
        for ticker in self.config.universe:
            mgr_adj = self.data_manager.get_price_adjustment_type(ticker)
            if mgr_adj != self.config.price_adjustment_type:
                raise HistoricalDataValidationError(
                    f"Configuration mismatch for '{ticker}': BacktestConfig specifies "
                    f"{self.config.price_adjustment_type.value}, but dataset is registered as {mgr_adj.value}."
                )

        # Incompatible dividend configuration check:
        if self.config.price_adjustment_type == PriceAdjustmentType.SPLIT_AND_DIVIDEND_ADJUSTED:
            for ticker in self.config.universe:
                divs = self.data_manager.get_dividends_for_ticker(ticker)
                if divs:
                    raise HistoricalDataValidationError(
                        f"Incompatible configuration for '{ticker}': Registered cash dividends cannot be "
                        f"accrued against SPLIT_AND_DIVIDEND_ADJUSTED dataset (returns already in prices)."
                    )

    def run(self) -> Dict[str, Any]:
        """Execute the full backtest across configured date range."""
        sessions = self.data_manager.get_all_session_dates(
            start_date=self.config.start_date,
            end_date=self.config.end_date,
        )

        if not sessions:
            raise ValueError(f"No historical trading sessions found between {self.config.start_date} and {self.config.end_date}.")

        for session_idx, session_date in enumerate(sessions):
            self._process_session(session_date, session_idx, sessions)

        # Finalize and calculate metrics
        return self._generate_results(sessions)

    def _process_session(self, session_date: date, session_idx: int, all_sessions: List[date]) -> None:
        """Process a single historical trading session in 4 strict phases with corporate actions."""

        # -------------------------------------------------------------
        # CORPORATE ACTIONS (Pre-Open)
        # Apply splits and process dividend events for held positions
        # -------------------------------------------------------------
        # 1. Stock Splits effective on session_date
        # CRITICAL F-01 FIX:
        # If dataset is RAW_UNADJUSTED, market open execution prices reflect raw split drop,
        # so active portfolio lots MUST be adjusted on split_date.
        # If dataset is SPLIT_ADJUSTED (or SPLIT_AND_DIVIDEND_ADJUSTED), historical execution
        # prices are ALREADY in post-split equivalent dollars, so applying a portfolio split
        # would double-adjust the position.
        if self.config.price_adjustment_type == PriceAdjustmentType.RAW_UNADJUSTED:
            splits = self.data_manager.get_splits_for_session(session_date)
            for split in splits:
                self.portfolio.apply_stock_split(
                    ticker=split.ticker,
                    ratio=split.ratio,
                    split_numerator=split.split_numerator,
                    split_denominator=split.split_denominator,
                    effective_date=session_date.isoformat(),
                )
                self._adjust_pending_orders_for_split(split)

        # 2. Dividends: Settle any pending dividends payable on or before session_date
        self.portfolio.settle_pending_dividends(session_date)

        # 3. Dividends: Accrue dividends on ex-date for positions held overnight
        # CRITICAL F-02 FIX:
        # Only accrue cash dividends if data is RAW_UNADJUSTED or SPLIT_ADJUSTED.
        # If SPLIT_AND_DIVIDEND_ADJUSTED, historical price series already reflects total return.
        if self.config.price_adjustment_type != PriceAdjustmentType.SPLIT_AND_DIVIDEND_ADJUSTED:
            dividends = self.data_manager.get_dividends_for_session(session_date)
            for div in dividends:
                pay_str = div.payable_date.isoformat() if div.payable_date else None
                self.portfolio.accrue_dividend(
                    div.ticker,
                    div.ex_date.isoformat(),
                    div.amount_per_share,
                    payable_date=pay_str,
                )

        # 4. Delistings: Force liquidate positions in delisted securities effective on session_date
        if self.universe_manager and session_idx > 0:
            prior_session_date = all_sessions[session_idx - 1]
            for ticker, pos in list(self.portfolio.ledger.positions.items()):
                if pos.total_shares > 0 and self.universe_manager.is_delisting_date(ticker, session_date):
                    last_c = self.data_manager.get_session_candle(ticker, prior_session_date)
                    last_close = last_c.close if last_c else (pos.total_cost_basis_usd / Decimal(pos.total_shares) if pos.total_shares > 0 else Decimal("0.00"))
                    sec_id = self.universe_manager.resolve_security_id(ticker, session_date) or f"SEC_{ticker}_01"
                    term_px, audit_flag = self.universe_manager.calculate_terminal_delisting_price(
                        security_id=sec_id,
                        delist_date=session_date,
                        final_main_board_close=last_close,
                    )
                    for lot in pos.lots:
                        if lot.status == "OPEN" and lot.shares_remaining > 0:
                            # Apply configured exit fee for positive-price liquidations; no fee on zero write-offs
                            if term_px > Decimal("0.00"):
                                fee = self.config.fee_config.exit.calculate_fee(lot.shares_remaining, term_px)
                            else:
                                fee = Decimal("0.00")
                            exit_order = SimulatedOrder(
                                order_id=f"DELIST-{lot.lot_id}-{session_date.strftime('%Y%m%d')}",
                                ticker=ticker,
                                side=SimulatedOrderSide.SELL,
                                order_type=SimulatedOrderType.REGIME_EXIT,
                                quantity=lot.shares_remaining,
                                signal_timestamp=datetime(session_date.year, session_date.month, session_date.day, 9, 30),
                                submission_timestamp=datetime(session_date.year, session_date.month, session_date.day, 9, 30),
                                reference_price=term_px,
                                lot_id=lot.lot_id,
                                fill_timestamp=datetime(session_date.year, session_date.month, session_date.day, 9, 30),
                                fill_price=term_px,
                                fee_usd=fee,
                                status=SimulatedOrderStatus.FILLED,
                                reason=f"DELISTING_LIQUIDATION: {audit_flag}",
                            )
                            self.portfolio.execute_delisting_liquidation(exit_order)
                            self.all_orders.append(exit_order)

        # -------------------------------------------------------------
        # PHASE A: MORNING EXECUTION (Session Open)
        # Execute pending orders generated at previous session close
        # -------------------------------------------------------------
        orders_to_process = list(self.pending_entry_orders)
        self.pending_entry_orders.clear()

        for order in orders_to_process:
            candle = self.data_manager.get_session_candle(order.ticker, session_date)
            if candle is None:
                order.status = SimulatedOrderStatus.EXPIRED
                order.rejection_reason = f"No candle data for {order.ticker} on {session_date}."
                self.all_orders.append(order)
                continue

            # Execution-time position-count safety rejection gate
            if order.side == SimulatedOrderSide.BUY:
                current_open_tickers = set(t for t, p in self.portfolio.ledger.positions.items() if p.total_shares > 0)
                if order.ticker not in current_open_tickers and len(current_open_tickers) >= self.config.risk_config.max_simultaneous_positions:
                    order.status = SimulatedOrderStatus.REJECTED
                    order.rejection_reason = (
                        f"Position limit reached: maximum simultaneous positions "
                        f"({self.config.risk_config.max_simultaneous_positions}) already active."
                    )
                    self._record_risk_rejection("MAX_POSITIONS_EXCEEDED")
                    self.all_orders.append(order)
                    continue

            # Trailing ADV and completed ATR14,t-1 (strictly prior completed sessions, NO lookahead!)
            trailing_adv20: Optional[int] = None
            trailing_atr14: Optional[Decimal] = None
            if session_idx > 0:
                prior_session_date = all_sessions[session_idx - 1]
                prior_bars = self.data_manager.get_execution_candles_slice(
                    order.ticker,
                    as_of_date=prior_session_date,
                    max_bars=30,
                )
                if prior_bars:
                    if len(prior_bars) >= 20:
                        trailing_adv20 = sum(c.volume for c in prior_bars[-20:]) // 20
                    if len(prior_bars) >= 15:
                        try:
                            snap = self.indicators_engine.compute_all(prior_bars, require_complete=False)
                            if snap.atr_14.is_available and snap.atr_14.value is not None:
                                trailing_atr14 = snap.atr_14.value
                        except Exception:
                            pass

            # Fill order at session open with slippage, fees, and execution capacity enforcement
            filled_order = self.execution_engine.simulate_entry_fill(
                order=order,
                session_candle=candle,
                trailing_adv20_shares=trailing_adv20,
                trailing_atr14=trailing_atr14,
            )

            if filled_order.status == SimulatedOrderStatus.REJECTED or filled_order.quantity <= 0:
                self.all_orders.append(filled_order)
                continue

            # Fill-Price Risk Revalidation at actual modeled open fill price P_fill
            reval_result = FillPriceRiskValidator.revalidate_and_downsize(
                account_equity=self.portfolio.ledger.total_equity_usd,
                modeled_fill_price=filled_order.fill_price,
                stop_price=filled_order.stop_loss_price or (filled_order.fill_price * Decimal("0.95")),
                initial_shares=filled_order.quantity,
                risk_config=self.risk_engine.risk_config,
                fee_config=self.risk_engine.fee_config,
                current_buffered_open_risk=self.portfolio.get_total_buffered_open_risk(),
            )

            if reval_result.is_rejected or reval_result.final_shares <= 0:
                filled_order.status = SimulatedOrderStatus.REJECTED
                filled_order.rejection_reason = f"Risk engine rejected fill: REJECTED_FILL_RISK: {reval_result.reason}"
                self._record_risk_rejection("RISK_LIMIT")
                self.all_orders.append(filled_order)
                continue

            # Final Booked Quantity Q_final
            q_final = reval_result.final_shares
            cancelled_diff = filled_order.quantity - q_final
            filled_order.quantity = q_final
            filled_order.residual_cancelled_shares += cancelled_diff
            filled_order.fee_usd = self.config.fee_config.entry.calculate_fee(q_final, filled_order.fill_price)

            # Only Q_final enters the portfolio ledger
            self.portfolio.execute_entry(filled_order)
            self.all_orders.append(filled_order)

        # -------------------------------------------------------------
        # PHASE B: INTRADAY PROTECTIVE EXITS & TARGETS
        # Check held lots against session price action
        # -------------------------------------------------------------
        held_lots_to_check = []
        for ticker, pos in self.portfolio.ledger.positions.items():
            if pos.total_shares > 0:
                for lot in pos.lots:
                    if lot.status == "OPEN" and lot.shares_remaining > 0:
                        held_lots_to_check.append((ticker, lot.lot_id, lot.shares_remaining))

        for ticker, lot_id, shares in held_lots_to_check:
            candle = self.data_manager.get_session_candle(ticker, session_date)
            if candle is None:
                continue

            trailing_atr14: Optional[Decimal] = None
            if session_idx > 0:
                prior_date = all_sessions[session_idx - 1]
                prior_bars = self.data_manager.get_execution_candles_slice(
                    ticker,
                    as_of_date=prior_date,
                    max_bars=30,
                )
                if prior_bars and len(prior_bars) >= 15:
                    try:
                        snap = self.indicators_engine.compute_all(prior_bars, require_complete=False)
                        if snap.atr_14.is_available and snap.atr_14.value is not None:
                            trailing_atr14 = snap.atr_14.value
                    except Exception:
                        pass

            stop_px = self.portfolio.lot_stops.get(lot_id, Decimal("0.00"))
            target_px = self.portfolio.lot_targets.get(lot_id, Decimal("999999.00"))

            exit_order = self.execution_engine.evaluate_exit_conditions(
                ticker=ticker,
                lot_id=lot_id,
                shares=shares,
                stop_loss_price=stop_px,
                target_price=target_px,
                session_candle=candle,
                trailing_atr14=trailing_atr14,
            )

            if exit_order is not None and exit_order.status == SimulatedOrderStatus.FILLED:
                self.portfolio.execute_exit(exit_order)
                self.all_orders.append(exit_order)

        # -------------------------------------------------------------
        # PHASE C: AFTERNOON MARK-TO-MARKET & SNAPSHOT
        # -------------------------------------------------------------
        close_prices: Dict[str, Decimal] = {}
        session_ts: Optional[datetime] = None
        for ticker in list(self.portfolio.ledger.positions.keys()):
            c = self.data_manager.get_session_candle(ticker, session_date)
            if c is not None:
                close_prices[ticker] = c.close
                session_ts = c.timestamp

        if session_ts is None:
            # Fallback timestamp from benchmark or SPY
            spy_c = self.data_manager.get_session_candle("SPY", session_date)
            session_ts = spy_c.timestamp if spy_c else datetime(session_date.year, session_date.month, session_date.day, 20, 0)

        self.portfolio.mark_to_market(close_prices)

        # Accrue calendar-day T-Bill cash yield if enabled
        if self.enable_cash_yield and self.cash_yield_calc and session_idx > 0:
            prior_session_date = all_sessions[session_idx - 1]
            unencumbered_cash = self.portfolio.ledger.cash_usd
            days_accrued = max(1, (session_date - prior_session_date).days)
            if unencumbered_cash > Decimal("0.00"):
                d_rate = self.cash_yield_calc.get_observed_rate(prior_session_date)
                bey_rate = self.cash_yield_calc.discount_to_bond_equivalent_yield(d_rate)
                daily_interest = (
                    unencumbered_cash * (bey_rate / Decimal("100.0")) * (Decimal(days_accrued) / Decimal("365.0"))
                ).quantize(Decimal("0.01"), ROUND_HALF_UP)
                if daily_interest > Decimal("0.00"):
                    self.portfolio.credit_cash_interest(daily_interest)

        # -------------------------------------------------------------
        # PHASE D: POST-CLOSE STRATEGY ANALYSIS & SIGNAL GENERATION
        # Slice data strictly up to session_date (NO LOOK-AHEAD)
        # -------------------------------------------------------------
        proxy_slice = self.data_manager.get_universe_candles_slice(
            universe=["SPY", "QQQ", "IWM"],
            as_of_date=session_date,
            max_bars=250,
        )

        sess = MarketClock.get_historical_session(session_date) or MarketClock.get_session(session_date)
        if sess is not None:
            close_t = sess[1]
            post_close_ts = datetime.combine(session_date, close_t, tzinfo=EASTERN)
            regime_result = self.regime_detector.detect_regime(
                proxy_candles_map=proxy_slice,
                current_time=post_close_ts,
                require_complete=True,
            )
        else:
            # Non-trading calendar day (e.g. synthetic test fixtures with weekend bars)
            regime_result = self.regime_detector.detect_regime(
                proxy_candles_map=proxy_slice,
                current_time=session_ts,
                require_complete=False,
            )

        # Record daily snapshot with current detected regime
        self.portfolio.record_daily_snapshot(
            session_date=session_date,
            session_timestamp=session_ts,
            market_regime=regime_result.regime.value,
        )

        # Trailing stop update check for held lots
        if self.portfolio.ledger.positions:
            candles_map = self.data_manager.get_universe_candles_slice(
                universe=list(self.portfolio.ledger.positions.keys()),
                as_of_date=session_date,
                max_bars=100,
            )
            exit_signals = self.signal_engine.evaluate_exits(
                ledger=self.portfolio.ledger,
                candles_map=candles_map,
                lot_stops=self.portfolio.lot_stops,
                lot_targets=self.portfolio.lot_targets,
                regime_result=regime_result,
                require_complete=True,
            )
            for sig in exit_signals:
                if sig.exit_type == ExitSignalType.TRAILING_STOP_UPDATE and sig.new_stop_price:
                    if sig.lot_id:
                        self.portfolio.update_trailing_stop(sig.lot_id, sig.new_stop_price)

        # If regime allows new entries and not the final backtest session, scan and queue entries
        if regime_result.policy.allow_new_longs and session_idx < len(all_sessions) - 1:
            # 1. Pre-Scan Liquidity Gates 1-3 Filtering on completed bars through session_date (t)
            liquidity_approved_universe: List[str] = []
            for ticker in self.config.universe:
                if self.universe_manager:
                    sec_id = self.universe_manager.resolve_security_id(ticker, session_date) or f"SEC_{ticker}_01"
                    if not self.universe_manager.is_security_active(sec_id, session_date):
                        continue
                bars = self.data_manager.get_execution_candles_slice(
                    ticker,
                    as_of_date=session_date,
                    max_bars=25,
                )
                if len(bars) < 20:
                    continue
                trailing_20 = bars[-20:]
                p_close = trailing_20[-1].close
                adv20 = sum(c.volume for c in trailing_20) // 20
                dadv20 = (sum(c.close * Decimal(c.volume) for c in trailing_20) / Decimal("20")).quantize(Decimal("0.01"))
                if p_close >= Decimal("10.00") and adv20 >= 1000000 and dadv20 >= Decimal("25000000.00"):
                    liquidity_approved_universe.append(ticker)

            if not liquidity_approved_universe:
                stock_slice = {}
                eligible_candidates = []
            else:
                stock_slice = self.data_manager.get_universe_candles_slice(
                    universe=liquidity_approved_universe,
                    as_of_date=session_date,
                    max_bars=250,
                )

                scan_res = self.scanner.scan_universe(
                    universe=liquidity_approved_universe,
                    candles_map=stock_slice,
                    regime_result=regime_result,
                    top_n=self.config.top_candidates_per_session,
                    require_complete=True,
                )

                eligible_candidates = scan_res.candidates[:self.config.top_candidates_per_session]

            if eligible_candidates:
                pending_buy_tickers = [o.ticker for o in self.pending_entry_orders if o.side == SimulatedOrderSide.BUY]
                signal_summary = self.signal_engine.evaluate_candidates(
                    candidates=eligible_candidates,
                    candles_map=stock_slice,
                    ledger=self.portfolio.ledger,
                    require_complete=True,
                    pending_tickers=pending_buy_tickers,
                )

                # Queue approved trade plans for execution at NEXT session open
                for plan in signal_summary.risk_approved_plans:
                    # Prevent queuing duplicate orders for same ticker
                    if any(o.ticker == plan.ticker for o in self.pending_entry_orders):
                        continue
                    if self.portfolio.ledger.positions.get(plan.ticker, None) and self.portfolio.ledger.positions[plan.ticker].total_shares > 0:
                        continue  # Already holding ticker

                    # Strict position-cap reservation during queuing (open positions + pending buy orders)
                    current_open_tickers = set(t for t, p in self.portfolio.ledger.positions.items() if p.total_shares > 0)
                    current_pending_tickers = set(o.ticker for o in self.pending_entry_orders if o.side == SimulatedOrderSide.BUY)
                    total_committed_tickers = current_open_tickers | current_pending_tickers
                    if plan.ticker not in total_committed_tickers and len(total_committed_tickers) >= self.config.risk_config.max_simultaneous_positions:
                        self._record_risk_rejection("MAX_POSITIONS_EXCEEDED")
                        continue

                    # Gate 4: Post-Sizing ADV Capacity Check (1.0% of trailing share ADV20,t)
                    bars = self.data_manager.get_execution_candles_slice(
                        plan.ticker,
                        as_of_date=session_date,
                        max_bars=20,
                    )
                    if len(bars) >= 20:
                        adv20 = sum(c.volume for c in bars[-20:]) // 20
                        max_order_shares = int(Decimal(adv20) * Decimal("0.01"))
                        capped_shares = min(plan.shares, max_order_shares)
                    else:
                        capped_shares = plan.shares

                    if capped_shares <= 0:
                        continue

                    entry_order = SimulatedOrder(
                        order_id=f"ORD-BUY-{plan.ticker}-{session_date.strftime('%Y%m%d')}",
                        ticker=plan.ticker,
                        side=SimulatedOrderSide.BUY,
                        order_type=SimulatedOrderType.MARKET_NEXT_OPEN,
                        quantity=capped_shares,
                        signal_timestamp=session_ts,
                        submission_timestamp=session_ts,
                        reference_price=plan.entry_price,
                        stop_loss_price=plan.stop_loss_price,
                        target_price=plan.target_price,
                        status=SimulatedOrderStatus.PENDING,
                        reason=f"Approved {plan.strategy_type.value} signal sized to {capped_shares} shares.",
                    )
                    self.pending_entry_orders.append(entry_order)

    def _adjust_pending_orders_for_split(self, split: StockSplitEvent) -> None:
        """Adjust all pending entry orders for the split ticker on split date in RAW_UNADJUSTED mode.

        Maintains identical economic exposure and risk by adjusting price-based fields and share quantities.
        If an order cannot retain a valid whole-share quantity under the defined order policy,
        it is cancelled safely and recorded.
        """
        remaining_pending: List[SimulatedOrder] = []
        for order in self.pending_entry_orders:
            if order.ticker.upper() == split.ticker.upper():
                valid = order.adjust_for_stock_split(
                    ratio=split.ratio,
                    split_numerator=split.split_numerator,
                    split_denominator=split.split_denominator,
                )
                if not valid:
                    self.all_orders.append(order)
                else:
                    remaining_pending.append(order)
            else:
                remaining_pending.append(order)
        self.pending_entry_orders = remaining_pending

    def _record_risk_rejection(self, constraint: Optional[str]) -> None:
        if constraint in ("TRADE_RISK_LIMIT_EXCEEDED", "AGGREGATE_RISK_LIMIT_EXCEEDED", "RISK_LIMIT"):
            self.risk_rejection_counts["RISK_LIMIT"] += 1
        elif constraint in ("MAX_POSITIONS_EXCEEDED", "POSITION_LIMIT"):
            self.risk_rejection_counts["POSITION_LIMIT"] += 1
        elif constraint in ("CASH_RESERVE_VIOLATED", "CASH_RESERVE"):
            self.risk_rejection_counts["CASH_RESERVE"] += 1
        else:
            self.risk_rejection_counts["OTHER"] += 1

    def _generate_results(self, sessions: List[date]) -> Dict[str, Any]:
        """Aggregate performance, risk, and benchmark comparison into a structured dictionary."""
        snapshots = self.portfolio.daily_snapshots
        trades = self.portfolio.closed_trades

        # 1. Portfolio Metrics
        portfolio_metrics = BacktestMetricsCalculator.calculate_portfolio_metrics(
            snapshots=snapshots,
            initial_capital=self.config.initial_capital_usd,
        )

        # 2. Trade Metrics
        trade_metrics = BacktestMetricsCalculator.calculate_trade_metrics(trades)

        # 3. Benchmark Comparison (SPY)
        benchmark_candles = self.data_manager.get_candles_slice(
            self.config.benchmark_ticker,
            as_of_date=sessions[-1],
            max_bars=len(sessions) + 5,
        )
        # Filter to exact backtest date range
        bench_filtered = [c for c in benchmark_candles if self.config.start_date <= c.timestamp.date() <= self.config.end_date]
        benchmark_comp = BacktestMetricsCalculator.compare_with_benchmark(
            strategy_metrics=portfolio_metrics,
            benchmark_candles=bench_filtered,
            benchmark_ticker=self.config.benchmark_ticker,
        )

        # 4. SPY Total Return Benchmark Calculation
        spy_total_return: Optional[Dict[str, Any]] = None
        if bench_filtered and self.config.benchmark_ticker == "SPY":
            spy_divs = []
            if hasattr(self.data_manager, "_dividends_by_ticker"):
                spy_divs = self.data_manager._dividends_by_ticker.get("SPY", [])
            try:
                spy_res = SPYTotalReturnCalculator.calculate(
                    benchmark_candles=bench_filtered,
                    dividends=spy_divs,
                    start_date=self.config.start_date,
                    end_date=self.config.end_date,
                    benchmark_ticker=self.config.benchmark_ticker,
                )
                spy_total_return = spy_res.to_dict()
            except Exception:
                spy_total_return = None

        # 5. Cash Yield Attribution
        cash_yield_dict: Optional[Dict[str, Any]] = None
        if self.enable_cash_yield and self.cash_yield_calc and snapshots:
            try:
                cy_res = self.cash_yield_calc.calculate_cash_accrual(
                    snapshots=snapshots,
                    initial_capital=self.config.initial_capital_usd,
                )
                cash_yield_dict = cy_res.to_dict()
            except Exception:
                cash_yield_dict = None

        # 6. Mutually Exclusive 5-Regime Performance Attribution
        regime_names = ["STRONG_BULLISH", "BULLISH", "SIDEWAYS", "BEARISH", "STRONG_BEARISH"]
        session_regime_map = {s.session_date: s.market_regime for s in snapshots}
        regime_trades: Dict[str, List[SimulatedTradeRecord]] = {r: [] for r in regime_names}
        regime_trades["UNKNOWN"] = []

        for t in trades:
            t_date = t.entry_timestamp.date()
            t_regime = session_regime_map.get(t_date, "UNKNOWN")
            if t_regime in regime_trades:
                regime_trades[t_regime].append(t)
            else:
                regime_trades["UNKNOWN"].append(t)

        regime_attribution: Dict[str, Any] = {}
        for r in regime_names:
            r_list = regime_trades[r]
            n_trades = len(r_list)
            wins = [tr for tr in r_list if tr.net_pnl_usd > Decimal("0.00")]
            losses = [tr for tr in r_list if tr.net_pnl_usd < Decimal("0.00")]
            win_count = len(wins)
            loss_count = len(losses)
            win_rate = (Decimal(win_count) / Decimal(n_trades) * Decimal("100.0")).quantize(Decimal("0.01")) if n_trades > 0 else Decimal("0.00")
            net_pnl = sum((tr.net_pnl_usd for tr in r_list), Decimal("0.00"))
            gross_pnl = sum((tr.gross_pnl_usd for tr in r_list), Decimal("0.00"))
            fees = sum((tr.total_fees_usd for tr in r_list), Decimal("0.00"))
            slippage = sum((tr.total_slippage_usd for tr in r_list), Decimal("0.00"))
            gross_profit = sum((tr.gross_pnl_usd for tr in wins), Decimal("0.00"))
            gross_loss = abs(sum((tr.gross_pnl_usd for tr in losses), Decimal("0.00")))
            profit_factor = (gross_profit / gross_loss).quantize(Decimal("0.01")) if gross_loss > Decimal("0.00") else (Decimal("999.00") if gross_profit > Decimal("0.00") else Decimal("0.00"))

            regime_attribution[r] = {
                "regime": r,
                "trade_count": n_trades,
                "win_count": win_count,
                "loss_count": loss_count,
                "win_rate_pct": str(win_rate),
                "total_net_pnl_usd": str(net_pnl),
                "total_gross_pnl_usd": str(gross_pnl),
                "total_fees_usd": str(fees),
                "total_slippage_usd": str(slippage),
                "profit_factor": str(profit_factor),
            }

        # 7. Risk Audit Metrics
        max_open_risk = max((s.buffered_open_risk_usd for s in snapshots), default=Decimal("0.00"))
        max_agg_risk_pct = max((s.aggregate_risk_pct for s in snapshots), default=Decimal("0.00"))
        avg_open_risk = (sum(s.buffered_open_risk_usd for s in snapshots) / Decimal(len(snapshots))).quantize(Decimal("0.01")) if snapshots else Decimal("0.00")
        max_positions = max((s.open_positions_count for s in snapshots), default=0)
        if max_positions > self.config.risk_config.max_simultaneous_positions:
            raise RuntimeError(
                f"Position cap invariant violated: max_simultaneous_positions observed was {max_positions}, "
                f"exceeding configured limit of {self.config.risk_config.max_simultaneous_positions}."
            )

        risk_audit = RiskAuditMetrics(
            max_open_buffered_risk_usd=max_open_risk,
            max_aggregate_risk_pct=max_agg_risk_pct,
            avg_open_buffered_risk_usd=avg_open_risk,
            risk_limit_rejections=self.risk_rejection_counts["RISK_LIMIT"],
            position_limit_rejections=self.risk_rejection_counts["POSITION_LIMIT"],
            cash_reserve_violations=self.risk_rejection_counts["CASH_RESERVE"],
            max_simultaneous_positions=max_positions,
            max_single_ticker_allocation_pct=Decimal("25.00"),
        )

        return {
            "config": self.config.to_dict(),
            "portfolio_metrics": portfolio_metrics.to_dict(),
            "trade_metrics": trade_metrics.to_dict(),
            "benchmark_comparison": benchmark_comp.to_dict(),
            "spy_total_return": spy_total_return,
            "cash_yield_result": cash_yield_dict,
            "regime_attribution": regime_attribution,
            "risk_audit_metrics": risk_audit.to_dict(),
            "daily_snapshots": [s.to_dict() for s in snapshots],
            "closed_trades": [t.to_dict() for t in trades],
            "all_orders": [o.to_dict() for o in self.all_orders],
            "quality_warnings": self.data_manager.get_quality_warnings(),
            "survivorship_bias_notice": self.data_manager.get_survivorship_bias_notice(),
        }


@dataclass
class SensitivityRunResult:
    level: int
    label: str
    total_trades: int
    total_shares: int
    gross_pnl_usd: Decimal
    total_fees_usd: Decimal
    total_slippage_usd: Decimal
    net_pnl_usd: Decimal
    final_equity_usd: Decimal
    total_return_pct: Decimal
    max_drawdown_pct: Decimal
    win_rate_pct: Decimal
    profit_factor: Decimal
    total_friction_usd: Decimal
    friction_per_share_roundtrip: Decimal

    def to_dict(self) -> Dict[str, Any]:
        return {
            "level": self.level,
            "label": self.label,
            "total_trades": self.total_trades,
            "total_shares": self.total_shares,
            "gross_pnl_usd": str(self.gross_pnl_usd),
            "total_fees_usd": str(self.total_fees_usd),
            "total_slippage_usd": str(self.total_slippage_usd),
            "net_pnl_usd": str(self.net_pnl_usd),
            "final_equity_usd": str(self.final_equity_usd),
            "total_return_pct": str(self.total_return_pct),
            "max_drawdown_pct": str(self.max_drawdown_pct),
            "win_rate_pct": str(self.win_rate_pct),
            "profit_factor": str(self.profit_factor),
            "total_friction_usd": str(self.total_friction_usd),
            "friction_per_share_roundtrip": str(self.friction_per_share_roundtrip),
        }


@dataclass
class SensitivityGridReport:
    runs: List[SensitivityRunResult]
    total_breakeven_friction_per_share: Decimal
    additional_headroom_per_share: Decimal
    baseline_level_1_cost_per_share: Decimal
    mathematical_identity_verified: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "runs": [r.to_dict() for r in self.runs],
            "total_breakeven_friction_per_share": str(self.total_breakeven_friction_per_share),
            "additional_headroom_per_share": str(self.additional_headroom_per_share),
            "baseline_level_1_cost_per_share": str(self.baseline_level_1_cost_per_share),
            "mathematical_identity_verified": self.mathematical_identity_verified,
        }


class ChronologicalPartitionHarness:
    """Chronological Partitioned Multi-Regime Backtesting Harness.

    Coordinates standardized multi-regime backtests across chronological partitions:
    - Baseline B (In-Sample): 2021-01-04 to 2023-12-29 (753 sessions, $5,000 starting equity)
    - Baseline C (Observed Out-of-Sample): 2024-01-02 to 2025-12-31 (502 sessions, $5,000 starting equity, CONTAMINATED)
    - Full Contiguous Run: 2021-01-04 to 2025-12-31 (1,255 sessions, $5,000 starting equity)
    - Sensitivity Grid: Levels 0-3 with Breakeven and Headroom calculations.
    """

    IN_SAMPLE_START: date = date(2021, 1, 4)
    IN_SAMPLE_END: date = date(2023, 12, 29)
    IN_SAMPLE_EXPECTED_SESSIONS: int = 753

    PREV_OOS_START: date = date(2024, 1, 2)
    PREV_OOS_END: date = date(2025, 12, 31)
    PREV_OOS_EXPECTED_SESSIONS: int = 502

    FULL_CONTIGUOUS_START: date = date(2021, 1, 4)
    FULL_CONTIGUOUS_END: date = date(2025, 12, 31)
    FULL_CONTIGUOUS_EXPECTED_SESSIONS: int = 1255

    STARTING_CAPITAL: Decimal = Decimal("5000.00")

    CONTAMINATION_DISCLOSURE: str = (
        "MANDATORY CONTAMINATION DISCLOSURE: The 2024-2025 historical period was previously accessed, "
        "inspected, and analyzed during Phase 5.9.4 pilot backtesting and discrepancy reconciliation. "
        "Consequently, the 2024-2025 period is formally classified as PREVIOUSLY OBSERVED (CONTAMINATED) "
        "and cannot be treated as an unobserved or pristine out-of-sample holdout. No parameter tuning "
        "or heuristic modifications are permitted on this dataset. The certified untouched lockbox is 2026."
    )

    EXECUTION_REALISM_DISCLAIMER: str = (
        "The 1.0% trailing share ADV cap is a deterministic backtesting modeling abstraction for market-open fills. "
        "It does not represent an actual quote, depth of book, or auction queue position, and does not claim or "
        "guarantee that identical liquidity or fill prices would be executable in live market conditions."
    )

    @classmethod
    def _clone_config(
        cls,
        base_config: BacktestConfig,
        start_date: date,
        end_date: date,
        initial_capital: Decimal,
        fee_config: Optional[FeeConfig] = None,
        slippage_config: Optional[SlippageConfig] = None,
        risk_config: Optional[RiskConfig] = None,
    ) -> BacktestConfig:
        return BacktestConfig(
            start_date=start_date,
            end_date=end_date,
            universe=list(base_config.universe),
            initial_capital_usd=initial_capital,
            benchmark_ticker=base_config.benchmark_ticker,
            top_candidates_per_session=base_config.top_candidates_per_session,
            price_adjustment_type=base_config.price_adjustment_type,
            risk_config=risk_config or base_config.risk_config,
            fee_config=fee_config or base_config.fee_config,
            slippage_config=slippage_config or base_config.slippage_config,
            execution_assumptions=base_config.execution_assumptions,
        )

    @classmethod
    def run_in_sample(
        cls,
        base_config: BacktestConfig,
        data_manager: HistoricalDataManager,
        universe_manager: Optional[PointInTimeUniverseManager] = None,
        enable_cash_yield: bool = False,
    ) -> Dict[str, Any]:
        """Execute Baseline B (In-Sample): 2021-01-04 to 2023-12-29 with $5,000 capital."""
        cfg = cls._clone_config(
            base_config=base_config,
            start_date=cls.IN_SAMPLE_START,
            end_date=cls.IN_SAMPLE_END,
            initial_capital=cls.STARTING_CAPITAL,
        )
        engine = EventDrivenBacktestEngine(
            config=cfg,
            data_manager=data_manager,
            universe_manager=universe_manager,
            enable_cash_yield=enable_cash_yield,
        )
        results = engine.run()
        results["partition"] = "IN_SAMPLE_2021_2023"
        results["is_contaminated"] = False
        results["contamination_disclosure"] = None
        results["execution_realism_disclaimer"] = cls.EXECUTION_REALISM_DISCLAIMER
        return results

    @classmethod
    def run_observed_oos(
        cls,
        base_config: BacktestConfig,
        data_manager: HistoricalDataManager,
        universe_manager: Optional[PointInTimeUniverseManager] = None,
        enable_cash_yield: bool = False,
    ) -> Dict[str, Any]:
        """Execute Baseline C (Observed OOS): 2024-01-02 to 2025-12-31 with $5,000 capital and CONTAMINATION flag."""
        cfg = cls._clone_config(
            base_config=base_config,
            start_date=cls.PREV_OOS_START,
            end_date=cls.PREV_OOS_END,
            initial_capital=cls.STARTING_CAPITAL,
        )
        engine = EventDrivenBacktestEngine(
            config=cfg,
            data_manager=data_manager,
            universe_manager=universe_manager,
            enable_cash_yield=enable_cash_yield,
        )
        results = engine.run()
        results["partition"] = "OBSERVED_OOS_2024_2025"
        results["is_contaminated"] = True
        results["contamination_disclosure"] = cls.CONTAMINATION_DISCLOSURE
        results["execution_realism_disclaimer"] = cls.EXECUTION_REALISM_DISCLAIMER
        return results

    @classmethod
    def run_contiguous_full(
        cls,
        base_config: BacktestConfig,
        data_manager: HistoricalDataManager,
        universe_manager: Optional[PointInTimeUniverseManager] = None,
        enable_cash_yield: bool = False,
    ) -> Dict[str, Any]:
        """Execute Full Contiguous Period: 2021-01-04 to 2025-12-31 with $5,000 capital across 1,255 sessions."""
        cfg = cls._clone_config(
            base_config=base_config,
            start_date=cls.FULL_CONTIGUOUS_START,
            end_date=cls.FULL_CONTIGUOUS_END,
            initial_capital=cls.STARTING_CAPITAL,
        )
        engine = EventDrivenBacktestEngine(
            config=cfg,
            data_manager=data_manager,
            universe_manager=universe_manager,
            enable_cash_yield=enable_cash_yield,
        )
        results = engine.run()
        results["partition"] = "CONTIGUOUS_FULL_2021_2025"
        results["is_contaminated"] = True
        results["contamination_disclosure"] = cls.CONTAMINATION_DISCLOSURE
        results["execution_realism_disclaimer"] = cls.EXECUTION_REALISM_DISCLAIMER
        return results

    @classmethod
    def run_sensitivity_grid(
        cls,
        base_config: BacktestConfig,
        data_manager: HistoricalDataManager,
        universe_manager: Optional[PointInTimeUniverseManager] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> SensitivityGridReport:
        """Execute Levels 0-3 Transaction Friction Sensitivity Grid.

        Level 0: Zero Friction (gross theoretical)
        Level 1: Baseline Target (max($0.0100, 0.010 * ATR_14,t-1), $0.005/sh min $1.00 fee)
        Level 2: 2x Stress (max($0.0200, 0.020 * ATR_14,t-1), $0.010/sh min $2.00 fee)
        Level 3: 3x Severe Stress (max($0.0300, 0.030 * ATR_14,t-1), $0.015/sh min $3.00 fee)

        Computes Total Break-Even Friction vs Additional Friction Headroom.
        """
        s_date = start_date or cls.FULL_CONTIGUOUS_START
        e_date = end_date or cls.FULL_CONTIGUOUS_END

        levels = [
            (
                0,
                "Level 0: Zero Friction",
                SlippageConfig(entry_bps=Decimal("0.0"), exit_bps=Decimal("0.0"), fixed_per_share=Decimal("0.00"), atr_multiplier=Decimal("0.00"), min_cents=Decimal("0.00")),
                FeeConfig(
                    entry=FeeSchedule(per_share=Decimal("0.00"), minimum=Decimal("0.00")),
                    exit=FeeSchedule(per_share=Decimal("0.00"), minimum=Decimal("0.00")),
                ),
            ),
            (
                1,
                "Level 1: Baseline Target",
                SlippageConfig(entry_bps=Decimal("0.0"), exit_bps=Decimal("0.0"), fixed_per_share=Decimal("0.00"), atr_multiplier=Decimal("0.010"), min_cents=Decimal("0.01")),
                FeeConfig(
                    entry=FeeSchedule(per_share=Decimal("0.005"), minimum=Decimal("1.00")),
                    exit=FeeSchedule(per_share=Decimal("0.005166"), basis_points=Decimal("0.278"), minimum=Decimal("1.00")),
                ),
            ),
            (
                2,
                "Level 2: 2x Stress",
                SlippageConfig(entry_bps=Decimal("0.0"), exit_bps=Decimal("0.0"), fixed_per_share=Decimal("0.00"), atr_multiplier=Decimal("0.020"), min_cents=Decimal("0.02")),
                FeeConfig(
                    entry=FeeSchedule(per_share=Decimal("0.010"), minimum=Decimal("2.00")),
                    exit=FeeSchedule(per_share=Decimal("0.010166"), basis_points=Decimal("0.278"), minimum=Decimal("2.00")),
                ),
            ),
            (
                3,
                "Level 3: 3x Severe Stress",
                SlippageConfig(entry_bps=Decimal("0.0"), exit_bps=Decimal("0.0"), fixed_per_share=Decimal("0.00"), atr_multiplier=Decimal("0.030"), min_cents=Decimal("0.03")),
                FeeConfig(
                    entry=FeeSchedule(per_share=Decimal("0.015"), minimum=Decimal("3.00")),
                    exit=FeeSchedule(per_share=Decimal("0.015166"), basis_points=Decimal("0.278"), minimum=Decimal("3.00")),
                ),
            ),
        ]

        run_results: List[SensitivityRunResult] = []

        for lvl, lbl, slip_cfg, fee_cfg in levels:
            cfg = cls._clone_config(
                base_config=base_config,
                start_date=s_date,
                end_date=e_date,
                initial_capital=cls.STARTING_CAPITAL,
                fee_config=fee_cfg,
                slippage_config=slip_cfg,
            )
            eng = EventDrivenBacktestEngine(
                config=cfg,
                data_manager=data_manager,
                universe_manager=universe_manager,
                enable_cash_yield=False,
            )
            res = eng.run()
            cl_trades = res["closed_trades"]
            n_trades = len(cl_trades)
            tot_shares = sum(int(t["shares"]) for t in cl_trades)
            gross_pnl = sum((Decimal(str(t["gross_pnl_usd"])) for t in cl_trades), Decimal("0.00"))
            tot_fees = sum((Decimal(str(t["total_fees_usd"])) for t in cl_trades), Decimal("0.00"))
            tot_slip = sum((Decimal(str(t["total_slippage_usd"])) for t in cl_trades), Decimal("0.00"))
            net_pnl = sum((Decimal(str(t["net_pnl_usd"])) for t in cl_trades), Decimal("0.00"))
            fin_eq = Decimal(str(res["portfolio_metrics"]["final_equity_usd"]))
            tot_ret = Decimal(str(res["portfolio_metrics"]["total_return_pct"]))
            max_dd = Decimal(str(res["portfolio_metrics"]["max_drawdown_pct"]))
            win_rt = Decimal(str(res["trade_metrics"]["win_rate_pct"]))
            raw_pf = res["trade_metrics"]["profit_factor"]
            if raw_pf is not None and str(raw_pf).upper() not in ("NONE", "N/A", ""):
                try:
                    prof_fac = Decimal(str(raw_pf))
                except Exception:
                    prof_fac = Decimal("0.00")
            else:
                prof_fac = Decimal("0.00")
            tot_fric = tot_fees + tot_slip
            fric_per_share = (tot_fric / Decimal(tot_shares)).quantize(Decimal("0.0001"), ROUND_HALF_UP) if tot_shares > 0 else Decimal("0.0000")

            run_results.append(
                SensitivityRunResult(
                    level=lvl,
                    label=lbl,
                    total_trades=n_trades,
                    total_shares=tot_shares,
                    gross_pnl_usd=gross_pnl,
                    total_fees_usd=tot_fees,
                    total_slippage_usd=tot_slip,
                    net_pnl_usd=net_pnl,
                    final_equity_usd=fin_eq,
                    total_return_pct=tot_ret,
                    max_drawdown_pct=max_dd,
                    win_rate_pct=win_rt,
                    profit_factor=prof_fac,
                    total_friction_usd=tot_fric,
                    friction_per_share_roundtrip=fric_per_share,
                )
            )

        # Calculate Total Break-Even Friction vs Additional Friction Headroom
        l0 = run_results[0]
        l1 = run_results[1]

        eval_shares = l1.total_shares if l1.total_shares > 0 else l0.total_shares
        eval_gross = l1.gross_pnl_usd if l1.total_shares > 0 else l0.gross_pnl_usd

        if eval_shares > 0:
            total_breakeven = (eval_gross / Decimal(eval_shares)).quantize(Decimal("0.0001"), ROUND_HALF_UP)
            baseline_cost = l1.friction_per_share_roundtrip
            additional_headroom = (l1.net_pnl_usd / Decimal(eval_shares)).quantize(Decimal("0.0001"), ROUND_HALF_UP)
            identity_diff = abs(total_breakeven - (baseline_cost + additional_headroom))
            identity_verified = identity_diff <= Decimal("0.0005")
        else:
            total_breakeven = Decimal("0.0000")
            baseline_cost = Decimal("0.0000")
            additional_headroom = Decimal("0.0000")
            identity_verified = True

        return SensitivityGridReport(
            runs=run_results,
            total_breakeven_friction_per_share=total_breakeven,
            additional_headroom_per_share=additional_headroom,
            baseline_level_1_cost_per_share=baseline_cost,
            mathematical_identity_verified=identity_verified,
        )
