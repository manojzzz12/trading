"""Historical universe management, point-in-time data integrity, and liquidity gating for trader-swing.

Phase 5.10.2 Implementation:
1. Security identity architecture separating Issuer CIK from Security/Share-Class Identity (FIGI).
2. Effective-date ticker alias schedule resolution (e.g. FB -> META).
3. Evidence-based delisting treatment with conservative zero-recovery assumption when unknown.
4. Deterministic decision-time liquidity acceptance gates strictly excluding session t data:
   - Gate 1: P_{t-1} >= $10.00
   - Gate 2: ADV_{20, t-1}^{shares} >= 1,000,000 shares/day
   - Gate 3: Dollar ADV_{20, t-1} >= $25,000,000 / day
   - Gate 4: Order quantity <= 1.0% of trailing share ADV
5. Modeled opening-fill capacity with immediate residual cancellation (no invented fills).
6. Fill-price risk re-validation (0.50% trade risk, 2.0% aggregate risk, 1.25x gap multiplier) with gap downsizing.
7. Deterministic risk non-override invariant protecting risk.py controls from AI/heuristic bypass.
8. Tier 1 (50-ticker curated) registry with mandatory survivorship bias disclosure.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_HALF_UP
from enum import Enum
import math
import os
import sys
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from market_data_models import OHLCVCandle
from risk import FeeConfig, RiskConfig, to_decimal
from universe import DEFAULT_CURATED_WATCHLIST, validate_ticker

TWO_DECIMALS = Decimal("0.01")
FOUR_DECIMALS = Decimal("0.0001")

# Standard mandatory disclosure for Tier 1 survivor backtests
SURVIVORSHIP_BIAS_DISCLOSURE = (
    "SURVIVORSHIP BIAS DISCLOSURE: This backtest universe consists of 50 equities actively trading as of 2026. "
    "Companies that were delisted, liquidated, or acquired between 2021 and 2025 are excluded. In accordance "
    "with quantitative research standards, these results represent an upper-bound simulation and must not be "
    "interpreted as survivorship-free. Full survivorship-free validation requires Tier 2/3 institutional point-in-time data feeds."
)

# Standard mandatory execution realism disclaimer
EXECUTION_REALISM_DISCLAIMER = (
    "The 1.0% trailing share ADV cap is a deterministic backtesting modeling abstraction for market-open fills. "
    "It does not represent an actual quote, depth of book, or auction queue position, and does not claim or "
    "guarantee that identical liquidity or fill prices would be executable in live market conditions."
)


# --- 1. SECURITY IDENTITY & TICKER ALIAS ARCHITECTURE ---

@dataclass(frozen=True)
class IssuerIdentity:
    """Legal corporate entity identity registered with the SEC."""
    cik: str  # 10-digit SEC Central Index Key (e.g. '0001652044')
    legal_name: str
    country: str = "US"

    def __post_init__(self):
        if not self.cik or not self.cik.isdigit() or len(self.cik) > 10:
            raise ValueError(f"Invalid SEC CIK '{self.cik}'. CIK must be a numeric string up to 10 digits.")
        if not self.legal_name:
            raise ValueError("Issuer legal_name cannot be empty.")


@dataclass(frozen=True)
class SecurityIdentity:
    """Individual security or share-class issued by a corporate entity."""
    security_id: str  # Internal immutable UUID / identifier (e.g. 'SEC_GOOGL_01')
    issuer_cik: str  # Foreign key to IssuerIdentity
    figi: str  # Financial Instrument Global Identifier (e.g. 'BBG009S39JX6')
    share_class: str  # 'Common', 'Class A', 'Class B', 'Class C'
    primary_exchange: str  # 'NASDAQ', 'NYSE', 'NYSE_AMERICAN'
    ipo_date: Optional[date] = None
    delisting_date: Optional[date] = None

    def __post_init__(self):
        if not self.security_id:
            raise ValueError("Security security_id cannot be empty.")
        if not self.issuer_cik:
            raise ValueError("Security issuer_cik cannot be empty.")
        if not self.figi:
            raise ValueError("Security figi cannot be empty.")
        if self.ipo_date and self.delisting_date and self.ipo_date > self.delisting_date:
            raise ValueError(f"IPO date {self.ipo_date} cannot be after delisting date {self.delisting_date}.")


@dataclass(frozen=True)
class TickerAliasSchedule:
    """Temporal validity window mapping a ticker symbol to a specific security."""
    security_id: str
    ticker: str
    valid_from: date
    valid_to: Optional[date] = None  # None indicates active / current alias

    def __post_init__(self):
        norm = validate_ticker(self.ticker)
        object.__setattr__(self, "ticker", norm)
        if self.valid_to and self.valid_from > self.valid_to:
            raise ValueError(f"valid_from ({self.valid_from}) cannot be after valid_to ({self.valid_to}).")

    def is_active_on(self, query_date: date) -> bool:
        if query_date < self.valid_from:
            return False
        if self.valid_to and query_date > self.valid_to:
            return False
        return True


class DelistingCause(str, Enum):
    """Terminal economic cause of equity delisting."""
    CASH_ACQUISITION = "CASH_ACQUISITION"
    STOCK_MERGER = "STOCK_MERGER"
    OTC_TRANSITION = "OTC_TRANSITION"
    BANKRUPTCY_LIQUIDATION = "BANKRUPTCY_LIQUIDATION"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class DelistingResolution:
    """Terminal resolution and disposition payout for a delisting security."""
    ticker: str
    security_id: str
    delist_date: date
    cause: DelistingCause
    cash_per_share: Optional[Decimal] = None
    stock_exchange_ratio: Optional[Decimal] = None
    acquirer_ticker: Optional[str] = None
    recovery_rate_pct: Decimal = Decimal("0.00")
    audit_flag: str = "TERMINAL_RECOVERY_UNKNOWN_CONSERVATIVE_ZERO"


class PointInTimeUniverseManager:
    """Maintains point-in-time constituent schedules, security identities, and delisting resolutions."""

    def __init__(self):
        self._issuers: Dict[str, IssuerIdentity] = {}  # cik -> IssuerIdentity
        self._securities: Dict[str, SecurityIdentity] = {}  # security_id -> SecurityIdentity
        self._aliases_by_sec: Dict[str, List[TickerAliasSchedule]] = {}
        self._aliases_by_ticker: Dict[str, List[TickerAliasSchedule]] = {}
        self._delistings: Dict[Tuple[str, date], DelistingResolution] = {}

    def register_issuer(self, issuer: IssuerIdentity) -> None:
        self._issuers[issuer.cik] = issuer

    def register_security(self, security: SecurityIdentity) -> None:
        if security.issuer_cik not in self._issuers:
            raise ValueError(f"Cannot register security '{security.security_id}': Issuer CIK '{security.issuer_cik}' is not registered.")
        self._securities[security.security_id] = security

    def register_alias(self, alias: TickerAliasSchedule) -> None:
        if alias.security_id not in self._securities:
            raise ValueError(f"Cannot register alias for unknown security_id '{alias.security_id}'.")
        self._aliases_by_sec.setdefault(alias.security_id, []).append(alias)
        self._aliases_by_ticker.setdefault(alias.ticker, []).append(alias)

    def register_delisting(self, resolution: DelistingResolution) -> None:
        self._delistings[(resolution.security_id, resolution.delist_date)] = resolution

    def get_security(self, security_id: str) -> Optional[SecurityIdentity]:
        return self._securities.get(security_id)

    def get_issuer(self, cik: str) -> Optional[IssuerIdentity]:
        return self._issuers.get(cik)

    def resolve_ticker_at_date(self, security_id: str, as_of: date) -> Optional[str]:
        """Resolve which ticker symbol was active for a security on a specific date."""
        for alias in self._aliases_by_sec.get(security_id, []):
            if alias.is_active_on(as_of):
                return alias.ticker
        return None

    def resolve_security_at_date(self, ticker: str, as_of: date) -> Optional[SecurityIdentity]:
        """Resolve which security was represented by a ticker on a specific date."""
        norm = validate_ticker(ticker)
        for alias in self._aliases_by_ticker.get(norm, []):
            if alias.is_active_on(as_of):
                return self._securities.get(alias.security_id)
        return None

    def is_security_active(self, security_id: str, as_of: date) -> bool:
        """Verify whether a security was actively traded (post-IPO, pre-delisting) on as_of date."""
        sec = self._securities.get(security_id)
        if not sec:
            return False
        if sec.ipo_date and as_of < sec.ipo_date:
            return False
        if sec.delisting_date and as_of > sec.delisting_date:
            return False
        return True

    def is_delisting_date(self, ticker_or_sec_id: str, as_of: date) -> bool:
        """Check if a ticker or security has a delisting event on as_of date."""
        if (ticker_or_sec_id, as_of) in self._delistings:
            return True
        sec = self._securities.get(ticker_or_sec_id)
        if not sec:
            try:
                sec = self.resolve_security_at_date(ticker_or_sec_id, as_of)
            except Exception:
                sec = None
        if sec:
            if (sec.security_id, as_of) in self._delistings:
                return True
            if sec.delisting_date == as_of:
                return True
        return False

    def resolve_security_id(self, ticker: str, as_of: Optional[date] = None) -> Optional[str]:
        """Resolve security_id for ticker, optionally as of date."""
        if as_of is not None:
            sec = self.resolve_security_at_date(ticker, as_of)
            return sec.security_id if sec else None
        try:
            norm = validate_ticker(ticker)
            aliases = self._aliases_by_ticker.get(norm, [])
            if aliases:
                return aliases[-1].security_id
            return f"SEC_{norm}_01"
        except Exception:
            return None

    def calculate_terminal_delisting_price(
        self,
        security_id: str,
        delist_date: date,
        final_main_board_close: Decimal,
        acquirer_close: Optional[Decimal] = None,
    ) -> Tuple[Decimal, str]:
        """Determine evidence-based terminal exit price on delisting date.

        Rules:
        - CASH_ACQUISITION: official cash offer price per share.
        - STOCK_MERGER: ratio * acquirer_close.
        - OTC_TRANSITION: adverse illiquidity gap exit (25% haircut + spread proxy).
        - BANKRUPTCY_LIQUIDATION or UNKNOWN: conservative zero-recovery ($0.00 / 100% loss)
          unless explicit court-approved distribution is documented.
        """
        res = self._delistings.get((security_id, delist_date))
        if not res:
            # Undocumented delisting: enforce conservative zero recovery
            return Decimal("0.00"), "TERMINAL_RECOVERY_UNKNOWN_CONSERVATIVE_ZERO"

        if res.cause == DelistingCause.CASH_ACQUISITION:
            if res.cash_per_share is not None and res.cash_per_share >= Decimal("0.00"):
                return res.cash_per_share, "CASH_ACQUISITION_PAYOUT"
            return Decimal("0.00"), "TERMINAL_RECOVERY_UNKNOWN_CONSERVATIVE_ZERO"

        if res.cause == DelistingCause.STOCK_MERGER:
            if res.stock_exchange_ratio is not None and acquirer_close is not None:
                term_px = (res.stock_exchange_ratio * acquirer_close).quantize(TWO_DECIMALS, ROUND_HALF_UP)
                return term_px, "STOCK_MERGER_CONVERSION"
            return Decimal("0.00"), "TERMINAL_RECOVERY_UNKNOWN_CONSERVATIVE_ZERO"

        if res.cause == DelistingCause.OTC_TRANSITION:
            # Model 25% adverse gap haircut on last main-board close
            haircut_px = (final_main_board_close * Decimal("0.75")).quantize(TWO_DECIMALS, ROUND_HALF_UP)
            return max(Decimal("0.01"), haircut_px), "OTC_TRANSITION_HAIRCUT_25PCT"

        if res.cause == DelistingCause.BANKRUPTCY_LIQUIDATION:
            if res.cash_per_share is not None and res.cash_per_share > Decimal("0.00"):
                return res.cash_per_share, "DOCUMENTED_BANKRUPTCY_DISTRIBUTION"
            return Decimal("0.00"), "TERMINAL_RECOVERY_UNKNOWN_CONSERVATIVE_ZERO"

        return Decimal("0.00"), "TERMINAL_RECOVERY_UNKNOWN_CONSERVATIVE_ZERO"


# --- 2. DETERMINISTIC DECISION-TIME LIQUIDITY ACCEPTANCE GATES ---

@dataclass(frozen=True)
class LiquidityGateResult:
    """Audit record for four-tier decision-time liquidity acceptance screening."""
    session_date: date  # Decision date t-1
    ticker: str
    prev_close: Decimal
    adv20_shares: int
    dollar_adv20: Decimal
    max_order_shares: int
    passed: bool
    rejection_reasons: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_date": self.session_date.isoformat(),
            "ticker": self.ticker,
            "prev_close": str(self.prev_close),
            "adv20_shares": self.adv20_shares,
            "dollar_adv20": str(self.dollar_adv20),
            "max_order_shares": self.max_order_shares,
            "passed": self.passed,
            "rejection_reasons": self.rejection_reasons,
        }


class HistoricalLiquidityGates:
    """Enforces deterministic decision-time liquidity gates strictly excluding session t data."""

    MIN_PRICE = Decimal("10.00")
    MIN_ADV_SHARES = 1000000  # 1,000,000 shares/day
    MIN_DOLLAR_ADV = Decimal("25000000.00")  # $25,000,000 / day
    MAX_VOLUME_PARTICIPATION_PCT = Decimal("0.01")  # 1.0%

    @classmethod
    def evaluate_gates(
        cls,
        candles_slice_up_to_t_minus_1: Sequence[OHLCVCandle],
        execution_session_date: date,
        ticker: str,
    ) -> LiquidityGateResult:
        """Evaluate liquidity gates strictly using completed sessions t-20 through t-1.

        Raises:
            ValueError: If any candle timestamp equals or exceeds execution_session_date (lookahead violation).
        """
        if not candles_slice_up_to_t_minus_1:
            return LiquidityGateResult(
                session_date=execution_session_date,
                ticker=ticker,
                prev_close=Decimal("0.00"),
                adv20_shares=0,
                dollar_adv20=Decimal("0.00"),
                max_order_shares=0,
                passed=False,
                rejection_reasons=["EMPTY_CANDLE_HISTORY"],
            )

        # Lookahead Prohibition: Verify NO candle is from execution_session_date or later
        for c in candles_slice_up_to_t_minus_1:
            c_date = c.timestamp.date()
            if c_date >= execution_session_date:
                raise ValueError(
                    f"Temporal Isolation Violation: Candle dated {c_date} is >= execution session date {execution_session_date}. "
                    "Liquidity gates must strictly use trailing data available prior to market open."
                )

        if len(candles_slice_up_to_t_minus_1) < 20:
            return LiquidityGateResult(
                session_date=candles_slice_up_to_t_minus_1[-1].timestamp.date(),
                ticker=ticker,
                prev_close=candles_slice_up_to_t_minus_1[-1].close,
                adv20_shares=0,
                dollar_adv20=Decimal("0.00"),
                max_order_shares=0,
                passed=False,
                rejection_reasons=[f"INSUFFICIENT_HISTORY_FOR_ADV20 (Got {len(candles_slice_up_to_t_minus_1)}, need 20)"],
            )

        # Slice strictly trailing 20 completed bars: t-20 to t-1
        trailing_20 = candles_slice_up_to_t_minus_1[-20:]
        t_minus_1_bar = trailing_20[-1]
        p_t_minus_1 = t_minus_1_bar.close

        # Gate 2: 20-day share ADV
        total_vol = sum(c.volume for c in trailing_20)
        adv20_shares = total_vol // 20

        # Gate 3: 20-day dollar ADV
        total_dollar_vol = sum(c.close * Decimal(c.volume) for c in trailing_20)
        dollar_adv20 = (total_dollar_vol / Decimal("20.0")).quantize(TWO_DECIMALS, ROUND_HALF_UP)

        # Gate 4: 1.0% volume capacity cap in whole shares
        max_order_shares = int(math.floor(float(adv20_shares) * 0.01))

        rejections = []

        # Gate 1 check: P_{t-1} >= $10.00
        if p_t_minus_1 < cls.MIN_PRICE:
            rejections.append(f"GATE1_PRICE_BELOW_MINIMUM: ${p_t_minus_1} < ${cls.MIN_PRICE}")

        # Gate 2 check: Share ADV20 >= 1,000,000 sh/day
        if adv20_shares < cls.MIN_ADV_SHARES:
            rejections.append(f"GATE2_SHARE_ADV_BELOW_MINIMUM: {adv20_shares:,} < {cls.MIN_ADV_SHARES:,} sh/day")

        # Gate 3 check: Dollar ADV20 >= $25,000,000 / day
        if dollar_adv20 < cls.MIN_DOLLAR_ADV:
            rejections.append(f"GATE3_DOLLAR_ADV_BELOW_MINIMUM: ${dollar_adv20:,.2f} < ${cls.MIN_DOLLAR_ADV:,.2f}")

        # Gate 4 check: Capacity must allow at least 1 share
        if max_order_shares < 1:
            rejections.append("GATE4_ZERO_ORDER_CAPACITY")

        passed = len(rejections) == 0

        return LiquidityGateResult(
            session_date=t_minus_1_bar.timestamp.date(),
            ticker=ticker,
            prev_close=p_t_minus_1,
            adv20_shares=adv20_shares,
            dollar_adv20=dollar_adv20,
            max_order_shares=max_order_shares,
            passed=passed,
            rejection_reasons=rejections,
        )


# --- 3. VOLUME EXECUTION CAPACITY & IMMEDIATE RESIDUAL CANCELLATION ---

@dataclass(frozen=True)
class VolumeCapacityExecutionResult:
    """Audit record for order volume capacity capping and residual cancellation."""
    desired_shares: int
    max_capacity_shares: int
    filled_shares: int
    residual_cancelled_shares: int
    status: str
    reason: str


class VolumeCapacityModel:
    """Applies trailing ADV capacity limits with immediate cancellation of residual quantities."""

    @staticmethod
    def apply_capacity_limit(
        desired_shares: int,
        max_capacity_shares: int,
    ) -> VolumeCapacityExecutionResult:
        """Cap market-open entry order to 1.0% of trailing share ADV.

        Invariant: Residual quantity is immediately cancelled at market open (zero invented fills).
        """
        if desired_shares <= 0:
            return VolumeCapacityExecutionResult(
                desired_shares=0,
                max_capacity_shares=max_capacity_shares,
                filled_shares=0,
                residual_cancelled_shares=0,
                status="REJECTED_ZERO_SHARES",
                reason="Desired shares must be positive.",
            )

        if max_capacity_shares <= 0:
            return VolumeCapacityExecutionResult(
                desired_shares=desired_shares,
                max_capacity_shares=0,
                filled_shares=0,
                residual_cancelled_shares=desired_shares,
                status="REJECTED_ZERO_CAPACITY",
                reason="Max capacity is zero shares (insufficient trailing liquidity).",
            )

        if desired_shares <= max_capacity_shares:
            return VolumeCapacityExecutionResult(
                desired_shares=desired_shares,
                max_capacity_shares=max_capacity_shares,
                filled_shares=desired_shares,
                residual_cancelled_shares=0,
                status="FULL_FILL",
                reason="Order quantity fits within 1.0% trailing share ADV cap.",
            )

        # Capacity exceeded: partial fill at open, cancel residual immediately
        filled = max_capacity_shares
        residual = desired_shares - filled
        return VolumeCapacityExecutionResult(
            desired_shares=desired_shares,
            max_capacity_shares=max_capacity_shares,
            filled_shares=filled,
            residual_cancelled_shares=residual,
            status="PARTIAL_FILL_RESIDUAL_CANCELLED",
            reason=f"Order capped to 1.0% ADV ({filled} shares). Residual {residual} shares immediately cancelled at market open.",
        )


# --- 4. FILL-PRICE RISK RE-VALIDATION & DETERMINISTIC NON-OVERRIDE ---

class DeterministicRiskViolationError(RuntimeError):
    """Raised when an external or AI component attempts to bypass or override risk controls."""
    pass


class DeterministicRiskGuard:
    """Enforces non-override boundary: AI and expansion components cannot bypass risk.py."""

    @staticmethod
    def assert_deterministic_override_prohibited(caller_name: str) -> None:
        """Assert caller does not have authorization to weaken or bypass deterministic risk controls."""
        prohibited_keywords = ["ai_agent", "heuristic", "universe_expansion", "optimizer", "relax_risk"]
        for kw in prohibited_keywords:
            if kw in caller_name.lower():
                raise DeterministicRiskViolationError(
                    f"Deterministic Risk Violation: Caller '{caller_name}' attempted to modify, bypass, or relax "
                    "deterministic risk controls in risk.py. Deterministic risk controls are immutable."
                )


@dataclass(frozen=True)
class FillPriceRiskRevalidationResult:
    """Audit record of fill-price risk recalculation and potential gap downsizing."""
    modeled_fill_price: Decimal
    stop_price: Decimal
    original_shares: int
    final_shares: int
    is_downsized: bool
    is_rejected: bool
    calculated_risk_usd: Decimal
    allowed_risk_usd: Decimal
    reason: str


class FillPriceRiskValidator:
    """Recalculates buffered dollar risk at actual modeled open fill price with gap downsizing."""

    @staticmethod
    def revalidate_and_downsize(
        account_equity: Decimal,
        modeled_fill_price: Decimal,
        stop_price: Decimal,
        initial_shares: int,
        risk_config: Optional[RiskConfig] = None,
        fee_config: Optional[FeeConfig] = None,
        current_buffered_open_risk: Decimal = Decimal("0.00"),
    ) -> FillPriceRiskRevalidationResult:
        """Recalculate buffered risk at modeled opening fill price P_open.

        Formula:
          unbuffered_downside = max(0, P_open - P_stop)
          buffered_downside = unbuffered_downside * gap_multiplier (1.25x)
          effective_risk = (buffered_downside * shares) + entry_fee + exit_fee

        If effective_risk exceeds 0.50% trade risk or 2.0% aggregate risk, downsizes shares to fit.
        """
        cfg = risk_config or RiskConfig()
        fees = fee_config or FeeConfig()

        p_open = to_decimal(modeled_fill_price)
        p_stop = to_decimal(stop_price)
        eq = to_decimal(account_equity)

        if p_open <= Decimal("0.00") or p_stop <= Decimal("0.00") or eq <= Decimal("0.00"):
            return FillPriceRiskRevalidationResult(
                modeled_fill_price=p_open,
                stop_price=p_stop,
                original_shares=initial_shares,
                final_shares=0,
                is_downsized=False,
                is_rejected=True,
                calculated_risk_usd=Decimal("0.00"),
                allowed_risk_usd=Decimal("0.00"),
                reason="Invalid financial values: equity, fill price, and stop price must be positive.",
            )

        # Single-trade risk ceiling: 0.50% of account equity
        trade_risk_ceiling = (eq * cfg.risk_per_trade_pct).quantize(TWO_DECIMALS, ROUND_FLOOR)
        # Aggregate open risk ceiling: 2.00% of account equity
        agg_risk_ceiling = (eq * cfg.max_aggregate_buffered_risk_pct).quantize(TWO_DECIMALS, ROUND_FLOOR)
        available_agg_risk = max(Decimal("0.00"), agg_risk_ceiling - current_buffered_open_risk)

        allowed_risk_budget = min(trade_risk_ceiling, available_agg_risk)

        # Calculate downside per share with 1.25x gap-risk buffer
        unbuffered_per_share = max(Decimal("0.00"), p_open - p_stop)
        buffered_per_share = unbuffered_per_share * cfg.gap_risk_multiplier

        if buffered_per_share <= Decimal("0.00"):
            # Stop above open price (invalid order bracket)
            return FillPriceRiskRevalidationResult(
                modeled_fill_price=p_open,
                stop_price=p_stop,
                original_shares=initial_shares,
                final_shares=0,
                is_downsized=False,
                is_rejected=True,
                calculated_risk_usd=Decimal("0.00"),
                allowed_risk_usd=allowed_risk_budget,
                reason=f"Invalid stop bracket: stop price ${p_stop} is >= modeled fill price ${p_open}.",
            )

        def calc_risk_for_qty(q: int) -> Decimal:
            p_risk = (buffered_per_share * Decimal(q)).quantize(TWO_DECIMALS, ROUND_HALF_UP)
            in_fee = fees.entry.calculate_fee(q, p_open)
            out_fee = fees.exit.calculate_fee(q, p_stop)
            return p_risk + in_fee + out_fee

        initial_risk = calc_risk_for_qty(initial_shares)

        if initial_risk <= allowed_risk_budget:
            # Fits within risk limits at actual fill price
            return FillPriceRiskRevalidationResult(
                modeled_fill_price=p_open,
                stop_price=p_stop,
                original_shares=initial_shares,
                final_shares=initial_shares,
                is_downsized=False,
                is_rejected=False,
                calculated_risk_usd=initial_risk,
                allowed_risk_usd=allowed_risk_budget,
                reason="Risk validated at actual open fill price within 0.50% trade limit.",
            )

        # Risk exceeded due to adverse opening gap: downsize shares iteratively
        current_q = initial_shares
        while current_q > 0 and calc_risk_for_qty(current_q) > allowed_risk_budget:
            current_q -= 1

        if current_q < 1:
            return FillPriceRiskRevalidationResult(
                modeled_fill_price=p_open,
                stop_price=p_stop,
                original_shares=initial_shares,
                final_shares=0,
                is_downsized=False,
                is_rejected=True,
                calculated_risk_usd=initial_risk,
                allowed_risk_usd=allowed_risk_budget,
                reason=f"REJECTED_RISK_LIMIT_EXCEEDED_AT_OPEN_FILL: Even 1 share exceeds allowed risk ${allowed_risk_budget}.",
            )

        downsized_risk = calc_risk_for_qty(current_q)
        return FillPriceRiskRevalidationResult(
            modeled_fill_price=p_open,
            stop_price=p_stop,
            original_shares=initial_shares,
            final_shares=current_q,
            is_downsized=True,
            is_rejected=False,
            calculated_risk_usd=downsized_risk,
            allowed_risk_usd=allowed_risk_budget,
            reason=f"Adverse opening gap ($open ${p_open} vs stop ${p_stop}): downsized from {initial_shares} to {current_q} shares to fit 0.50% risk limit.",
        )


# --- 5. TIER 1 (50 CURATED LARGE-CAP US TICKERS) REGISTRY ---

def build_tier_1_universe_manager() -> PointInTimeUniverseManager:
    """Construct pre-populated PointInTimeUniverseManager for Tier 1 (50 Curated Watchlist)."""
    mgr = PointInTimeUniverseManager()

    # Representative CIK mapping for Tier 1 symbols
    # Key anchor issuers with two-share class disambiguation
    issuers = [
        IssuerIdentity("0000320193", "Apple Inc."),
        IssuerIdentity("0000789019", "Microsoft Corp."),
        IssuerIdentity("0001045810", "NVIDIA Corp."),
        IssuerIdentity("0001018724", "Amazon.com Inc."),
        IssuerIdentity("0001652044", "Alphabet Inc."),
        IssuerIdentity("0001326801", "Meta Platforms Inc."),
        IssuerIdentity("0001318605", "Tesla Inc."),
        IssuerIdentity("0000002488", "Advanced Micro Devices Inc."),
        IssuerIdentity("0000019617", "JPMorgan Chase & Co."),
        IssuerIdentity("0000731766", "UnitedHealth Group Inc."),
    ]
    for iss in issuers:
        mgr.register_issuer(iss)

    # Register Securities
    # Alphabet Class A and Class C multi-share class demonstration
    googl_sec = SecurityIdentity("SEC_GOOGL_01", "0001652044", "BBG009S39JX6", "Class A", "NASDAQ")
    goog_sec = SecurityIdentity("SEC_GOOG_01", "0001652044", "BBG009S39JY5", "Class C", "NASDAQ")
    meta_sec = SecurityIdentity("SEC_META_01", "0001326801", "BBG000MM2P62", "Common", "NASDAQ")

    mgr.register_security(googl_sec)
    mgr.register_security(goog_sec)
    mgr.register_security(meta_sec)

    # Register Ticker Aliases
    mgr.register_alias(TickerAliasSchedule("SEC_GOOGL_01", "GOOGL", valid_from=date(2004, 8, 19)))
    mgr.register_alias(TickerAliasSchedule("SEC_GOOG_01", "GOOG", valid_from=date(2014, 4, 3)))

    # Meta FB -> META rename schedule
    mgr.register_alias(TickerAliasSchedule("SEC_META_01", "FB", valid_from=date(2012, 5, 18), valid_to=date(2022, 6, 8)))
    mgr.register_alias(TickerAliasSchedule("SEC_META_01", "META", valid_from=date(2022, 6, 9)))

    # Register standard Tier 1 remaining securities
    for sym in DEFAULT_CURATED_WATCHLIST:
        if sym in ["GOOGL", "META"]:
            continue
        dummy_sec_id = f"SEC_{sym}_01"
        dummy_cik = "0000000001"
        if dummy_cik not in mgr._issuers:
            mgr.register_issuer(IssuerIdentity(dummy_cik, f"Generic Issuer for {sym}"))
        sec = SecurityIdentity(dummy_sec_id, dummy_cik, f"BBG_DUMMY_{sym}", "Common", "NYSE")
        mgr.register_security(sec)
        mgr.register_alias(TickerAliasSchedule(dummy_sec_id, sym, valid_from=date(2000, 1, 1)))

    return mgr
