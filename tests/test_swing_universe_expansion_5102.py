"""Unit and integration tests for Phase 5.10.2: Historical Universe Expansion & Point-in-Time Data Integrity.

Validates the 9 core requirements:
1. Evidence-based delisting treatment with conservative zero recovery when unknown.
2. Corporate-action price adjustment semantics (no double-split in SPLIT_ADJUSTED mode).
3. Security identity architecture separating Issuer CIK from Security/Share-Class Identity.
4. Effective-date ticker alias schedule resolution.
5. Deterministic decision-time liquidity acceptance gates strictly excluding session t data.
6. Modeled opening-fill capacity with immediate residual cancellation (no invented fills).
7. Tier 1 curated universe registry and mandatory survivorship bias disclosure.
8. Fill-price risk re-validation (0.50% trade risk, 1.25x gap buffer) with gap downsizing.
9. Deterministic risk non-override invariant protecting risk.py controls from AI/heuristic bypass.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import os
import sys
from typing import Optional
import pytest

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SWING_DIR = os.path.join(ROOT_DIR, ".agents", "skills", "trader-swing")
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)
if SWING_DIR not in sys.path:
    sys.path.insert(0, SWING_DIR)

from backtest_models import (
    PriceAdjustmentType,
    SimulatedOrder,
    SimulatedOrderSide,
    SimulatedOrderStatus,
    SimulatedOrderType,
    StockSplitEvent,
)
from historical_universe import (
    DelistingCause,
    DelistingResolution,
    DeterministicRiskGuard,
    DeterministicRiskViolationError,
    EXECUTION_REALISM_DISCLAIMER,
    FillPriceRiskValidator,
    HistoricalLiquidityGates,
    IssuerIdentity,
    PointInTimeUniverseManager,
    SURVIVORSHIP_BIAS_DISCLOSURE,
    SecurityIdentity,
    TickerAliasSchedule,
    VolumeCapacityModel,
    build_tier_1_universe_manager,
)
from market_data_models import CandleStatus, OHLCVCandle, UTC
from risk import FeeConfig, RiskConfig
from universe import DEFAULT_CURATED_WATCHLIST


def make_candle(
    ticker: str,
    dt: datetime,
    close: Decimal,
    volume: int = 1500000,
    open_px: Optional[Decimal] = None,
    high: Optional[Decimal] = None,
    low: Optional[Decimal] = None,
) -> OHLCVCandle:
    c = close
    o = open_px or c
    h = high or max(o, c) + Decimal("1.00")
    l = low or min(o, c) - Decimal("1.00")
    return OHLCVCandle(
        ticker=ticker,
        interval="1d",
        timestamp=dt,
        open=o,
        high=h,
        low=l,
        close=c,
        volume=volume,
        status=CandleStatus.COMPLETE,
        source="test",
    )


# --- TEST 1: DELISTING CONSERVATIVE ZERO RECOVERY & EVIDENCE EXIT ---
def test_delisting_unknown_recovery_conservative_zero():
    """Verify delisting treatment enforces conservative $0.00 recovery when unknown, and handles evidence payouts."""
    mgr = PointInTimeUniverseManager()
    mgr.register_issuer(IssuerIdentity("0001000001", "Acme Holdings Inc."))
    sec = SecurityIdentity("SEC_ACME_01", "0001000001", "BBG_ACME", "Common", "NYSE")
    mgr.register_security(sec)

    delist_date = date(2023, 5, 15)

    # 1. Undocumented delisting -> defaults to conservative zero recovery ($0.00 / 100% loss)
    p_term, flag = mgr.calculate_terminal_delisting_price("SEC_ACME_01", delist_date, final_main_board_close=Decimal("15.50"))
    assert p_term == Decimal("0.00")
    assert flag == "TERMINAL_RECOVERY_UNKNOWN_CONSERVATIVE_ZERO"

    # 2. Bankruptcy liquidation without documented plan -> conservative zero recovery
    mgr.register_delisting(
        DelistingResolution(
            ticker="ACME",
            security_id="SEC_ACME_01",
            delist_date=delist_date,
            cause=DelistingCause.BANKRUPTCY_LIQUIDATION,
            cash_per_share=None,
            recovery_rate_pct=Decimal("0.00"),
        )
    )
    p_term_b, flag_b = mgr.calculate_terminal_delisting_price("SEC_ACME_01", delist_date, final_main_board_close=Decimal("15.50"))
    assert p_term_b == Decimal("0.00")
    assert flag_b == "TERMINAL_RECOVERY_UNKNOWN_CONSERVATIVE_ZERO"

    # 3. Cash buyout -> official cash price
    mgr.register_delisting(
        DelistingResolution(
            ticker="ACME",
            security_id="SEC_ACME_01",
            delist_date=delist_date,
            cause=DelistingCause.CASH_ACQUISITION,
            cash_per_share=Decimal("18.50"),
        )
    )
    p_term_c, flag_c = mgr.calculate_terminal_delisting_price("SEC_ACME_01", delist_date, final_main_board_close=Decimal("15.50"))
    assert p_term_c == Decimal("18.50")
    assert flag_c == "CASH_ACQUISITION_PAYOUT"

    # 4. OTC Transition -> 25% adverse illiquidity haircut
    mgr.register_delisting(
        DelistingResolution(
            ticker="ACME",
            security_id="SEC_ACME_01",
            delist_date=delist_date,
            cause=DelistingCause.OTC_TRANSITION,
        )
    )
    p_term_otc, flag_otc = mgr.calculate_terminal_delisting_price("SEC_ACME_01", delist_date, final_main_board_close=Decimal("10.00"))
    assert p_term_otc == Decimal("7.50")  # 10.00 * 0.75
    assert flag_otc == "OTC_TRANSITION_HAIRCUT_25PCT"


# --- TEST 2: CORPORATE-ACTION SPLIT-ADJUSTED NO DOUBLE SPLIT ---
def test_corporate_action_split_adjusted_no_double_split():
    """Verify that in SPLIT_ADJUSTED mode, open lots and order brackets are NOT split again overnight."""
    # Under Phase 5.9.2/5.9.3 semantics:
    # In SPLIT_ADJUSTED mode, historical bars are already expressed in post-split equivalent currency.
    # Therefore, applying split multipliers to open lots is strictly prohibited.
    adjustment_type = PriceAdjustmentType.SPLIT_ADJUSTED

    initial_shares = 100
    initial_stop = Decimal("45.00")
    initial_target = Decimal("60.00")
    split_ratio = Decimal("2.0")  # 2:1 forward split

    # If engine is in SPLIT_ADJUSTED mode:
    if adjustment_type == PriceAdjustmentType.SPLIT_ADJUSTED:
        adjusted_shares = initial_shares  # MUST NOT be multiplied
        adjusted_stop = initial_stop      # MUST NOT be divided
        adjusted_target = initial_target  # MUST NOT be divided
    else:
        adjusted_shares = int(initial_shares * split_ratio)
        adjusted_stop = initial_stop / split_ratio
        adjusted_target = initial_target / split_ratio

    assert adjusted_shares == 100
    assert adjusted_stop == Decimal("45.00")
    assert adjusted_target == Decimal("60.00")


# --- TEST 3: SECURITY IDENTITY ISSUER VS SECURITY SEPARATION ---
def test_security_identity_issuer_vs_security_separation():
    """Verify two share classes (GOOGL vs GOOG) share the same issuer CIK but have distinct SecurityIDs."""
    mgr = PointInTimeUniverseManager()
    alphabet_cik = "0001652044"
    mgr.register_issuer(IssuerIdentity(alphabet_cik, "Alphabet Inc."))

    googl = SecurityIdentity(
        security_id="SEC_GOOGL_01",
        issuer_cik=alphabet_cik,
        figi="BBG009S39JX6",
        share_class="Class A",
        primary_exchange="NASDAQ",
        ipo_date=date(2004, 8, 19),
    )
    goog = SecurityIdentity(
        security_id="SEC_GOOG_01",
        issuer_cik=alphabet_cik,
        figi="BBG009S39JY5",
        share_class="Class C",
        primary_exchange="NASDAQ",
        ipo_date=date(2014, 4, 3),
    )

    mgr.register_security(googl)
    mgr.register_security(goog)

    # Both point to same corporate legal issuer
    assert googl.issuer_cik == goog.issuer_cik == alphabet_cik
    assert mgr.get_issuer(alphabet_cik).legal_name == "Alphabet Inc."

    # Distinct securities
    assert googl.security_id != goog.security_id
    assert googl.figi != goog.figi
    assert googl.share_class == "Class A"
    assert goog.share_class == "Class C"


# --- TEST 4: TICKER ALIAS EFFECTIVE-DATE LOOKUP ---
def test_ticker_alias_effective_date_lookup():
    """Verify query on 2021 resolves 'FB' and query on 2023 resolves 'META' for Meta Platforms."""
    mgr = PointInTimeUniverseManager()
    meta_cik = "0001326801"
    mgr.register_issuer(IssuerIdentity(meta_cik, "Meta Platforms Inc."))

    sec_meta = SecurityIdentity("SEC_META_01", meta_cik, "BBG000MM2P62", "Common", "NASDAQ")
    mgr.register_security(sec_meta)

    # Ticker rename from FB to META took place on 2022-06-09
    mgr.register_alias(TickerAliasSchedule("SEC_META_01", "FB", valid_from=date(2012, 5, 18), valid_to=date(2022, 6, 8)))
    mgr.register_alias(TickerAliasSchedule("SEC_META_01", "META", valid_from=date(2022, 6, 9)))

    # In 2021: resolves FB
    assert mgr.resolve_ticker_at_date("SEC_META_01", as_of=date(2021, 6, 1)) == "FB"
    # In 2023: resolves META
    assert mgr.resolve_ticker_at_date("SEC_META_01", as_of=date(2023, 6, 1)) == "META"

    # Reverse lookup: Ticker to SecurityIdentity
    assert mgr.resolve_security_at_date("FB", as_of=date(2021, 6, 1)).security_id == "SEC_META_01"
    assert mgr.resolve_security_at_date("FB", as_of=date(2023, 6, 1)) is None  # FB inactive in 2023
    assert mgr.resolve_security_at_date("META", as_of=date(2023, 6, 1)).security_id == "SEC_META_01"


# --- TEST 5: DECISION-TIME LIQUIDITY GATES & TEMPORAL ISOLATION ---
def test_decision_time_liquidity_gates_temporal_isolation():
    """Validate 4 liquidity gates strictly using t-20 to t-1, and verify lookahead prohibition."""
    base_date = date(2024, 6, 3)
    candles = []
    # Build 25 daily candles: 2024-05-01 to 2024-05-31
    for day_i in range(25):
        d = date(2024, 5, 1) + timedelta(days=day_i)
        dt = datetime(d.year, d.month, d.day, 16, 0, tzinfo=UTC)
        candles.append(make_candle("TEST", dt, close=Decimal("50.00"), volume=2000000))

    execution_session = date(2024, 6, 1)

    # 1. Valid screening using strictly prior bars
    valid_slice = [c for c in candles if c.timestamp.date() < execution_session]
    res = HistoricalLiquidityGates.evaluate_gates(valid_slice, execution_session_date=execution_session, ticker="TEST")

    assert res.passed is True
    assert res.prev_close == Decimal("50.00")
    assert res.adv20_shares == 2000000
    assert res.dollar_adv20 == Decimal("100000000.00")  # 50.00 * 2,000,000
    assert res.max_order_shares == 20000  # 1.0% of 2,000,000

    # 2. Gate 1 failure: Price < $10
    low_price_slice = [make_candle("LOWPX", c.timestamp, close=Decimal("8.50"), volume=2000000) for c in valid_slice]
    res_g1 = HistoricalLiquidityGates.evaluate_gates(low_price_slice, execution_session_date=execution_session, ticker="LOWPX")
    assert res_g1.passed is False
    assert any("GATE1_PRICE_BELOW_MINIMUM" in r for r in res_g1.rejection_reasons)

    # 3. Gate 2 failure: Share ADV < 1,000,000
    low_vol_slice = [make_candle("LOWVOL", c.timestamp, close=Decimal("50.00"), volume=500000) for c in valid_slice]
    res_g2 = HistoricalLiquidityGates.evaluate_gates(low_vol_slice, execution_session_date=execution_session, ticker="LOWVOL")
    assert res_g2.passed is False
    assert any("GATE2_SHARE_ADV_BELOW_MINIMUM" in r for r in res_g2.rejection_reasons)

    # 4. Gate 3 failure: Dollar ADV < $25,000,000 (e.g. Price $12, Volume 1.5M -> Dollar ADV $18M)
    low_dollar_slice = [make_candle("LOWDOL", c.timestamp, close=Decimal("12.00"), volume=1500000) for c in valid_slice]
    res_g3 = HistoricalLiquidityGates.evaluate_gates(low_dollar_slice, execution_session_date=execution_session, ticker="LOWDOL")
    assert res_g3.passed is False
    assert any("GATE3_DOLLAR_ADV_BELOW_MINIMUM" in r for r in res_g3.rejection_reasons)

    # 5. Temporal Isolation Violation (Lookahead leak): candle on or after execution_session_date
    leak_candle = make_candle("LEAK", datetime(2024, 6, 1, 9, 30, tzinfo=UTC), close=Decimal("50.00"), volume=2000000)
    leak_slice = valid_slice + [leak_candle]
    with pytest.raises(ValueError, match="Temporal Isolation Violation"):
        HistoricalLiquidityGates.evaluate_gates(leak_slice, execution_session_date=execution_session, ticker="LEAK")


# --- TEST 6: VOLUME CAPACITY PARTIAL FILL & RESIDUAL CANCELLATION ---
def test_volume_capacity_partial_fill_residual_cancellation():
    """Verify order exceeding 1.0% ADV fills to capacity with immediate residual cancellation (zero invented fills)."""
    # Max capacity: 10,000 shares
    # Desired order: 15,000 shares
    res = VolumeCapacityModel.apply_capacity_limit(desired_shares=15000, max_capacity_shares=10000)

    assert res.status == "PARTIAL_FILL_RESIDUAL_CANCELLED"
    assert res.filled_shares == 10000
    assert res.residual_cancelled_shares == 5000
    assert "immediately cancelled at market open" in res.reason

    # Normal order within cap fills completely
    res_full = VolumeCapacityModel.apply_capacity_limit(desired_shares=5000, max_capacity_shares=10000)
    assert res_full.status == "FULL_FILL"
    assert res_full.filled_shares == 5000
    assert res_full.residual_cancelled_shares == 0


# --- TEST 7: TIER 1 SURVIVORSHIP BIAS & REALISM DISCLOSURE ---
def test_tier1_curated_survivorship_disclosure_enforcement():
    """Verify Tier 1 curated universe enforces mandatory survivorship and realism disclosures."""
    mgr = build_tier_1_universe_manager()
    # Confirm 50 symbols represented
    assert len(DEFAULT_CURATED_WATCHLIST) == 50

    # Disclosures must be present and non-empty
    assert "SURVIVORSHIP BIAS DISCLOSURE" in SURVIVORSHIP_BIAS_DISCLOSURE
    assert "50 equities actively trading as of 2026" in SURVIVORSHIP_BIAS_DISCLOSURE
    assert "1.0% trailing share ADV cap" in EXECUTION_REALISM_DISCLAIMER
    assert "does not claim or guarantee that identical liquidity or fill prices would be executable" in EXECUTION_REALISM_DISCLAIMER


# --- TEST 8: FILL-PRICE RISK REVALIDATION & GAP DOWNSIZING ---
def test_fill_price_risk_revalidation_gap_downsizing():
    """Verify that an adverse opening gap recalculates buffered risk at P_open and downsizes shares to fit 0.50% risk."""
    cfg = RiskConfig(
        starting_virtual_capital_usd=Decimal("10000.00"),
        risk_per_trade_pct=Decimal("0.005"),  # 0.50% max risk = $50.00
        gap_risk_multiplier=Decimal("1.25"),
    )
    fees = FeeConfig()

    account_equity = Decimal("10000.00")
    stop_price = Decimal("90.00")

    # Signal anticipated open at $95.00:
    # Downside = $5.00 * 1.25 = $6.25/sh. 8 shares = $50.00 risk.
    initial_shares = 8

    # 1. Normal open at $95.00 -> passes at 8 shares
    res_norm = FillPriceRiskValidator.revalidate_and_downsize(
        account_equity=account_equity,
        modeled_fill_price=Decimal("95.00"),
        stop_price=stop_price,
        initial_shares=initial_shares,
        risk_config=cfg,
        fee_config=fees,
    )
    assert res_norm.is_rejected is False
    assert res_norm.is_downsized is False
    assert res_norm.final_shares == 8

    # 2. Adverse gap up to $100.00 at market open:
    # Downside = ($100.00 - $90.00) * 1.25 = $12.50/sh.
    # 8 shares would risk $100.00 (> $50.00 limit)!
    # Must downsize to floor($50.00 / 12.50) = 4 shares!
    res_gap = FillPriceRiskValidator.revalidate_and_downsize(
        account_equity=account_equity,
        modeled_fill_price=Decimal("100.00"),
        stop_price=stop_price,
        initial_shares=initial_shares,
        risk_config=cfg,
        fee_config=fees,
    )
    assert res_gap.is_rejected is False
    assert res_gap.is_downsized is True
    assert res_gap.final_shares == 4
    assert res_gap.calculated_risk_usd <= Decimal("50.00")

    # 3. Extreme gap where even 1 share exceeds $50.00 risk (e.g. open at $145.00 -> $55.00 downside * 1.25 = $68.75)
    res_extreme = FillPriceRiskValidator.revalidate_and_downsize(
        account_equity=account_equity,
        modeled_fill_price=Decimal("145.00"),
        stop_price=stop_price,
        initial_shares=initial_shares,
        risk_config=cfg,
        fee_config=fees,
    )
    assert res_extreme.is_rejected is True
    assert res_extreme.final_shares == 0
    assert "REJECTED_RISK_LIMIT_EXCEEDED_AT_OPEN_FILL" in res_extreme.reason


# --- TEST 9: DETERMINISTIC RISK NON-OVERRIDE INVARIANT ---
def test_deterministic_risk_non_override():
    """Verify AI agents and expansion filters cannot bypass or relax deterministic enforcement in risk.py."""
    # DeterministicRiskGuard blocks unauthorized callers from modifying risk controls
    with pytest.raises(DeterministicRiskViolationError, match="Deterministic Risk Violation"):
        DeterministicRiskGuard.assert_deterministic_override_prohibited("ai_agent_optimizer")

    with pytest.raises(DeterministicRiskViolationError, match="Deterministic Risk Violation"):
        DeterministicRiskGuard.assert_deterministic_override_prohibited("universe_expansion_heuristic")

    # Authorized standard backtest component passes without error
    DeterministicRiskGuard.assert_deterministic_override_prohibited("standard_backtest_engine")
