"""Deterministic multi-dimensional scoring and ranking engine for trader-swing.

Enforces:
1. Explainable scoring across 5 quantitative pillars (Trend, Momentum, RVOL, Volatility, Liquidity).
2. Pure Decimal arithmetic and deterministic tie-breaking.
3. Transparent positive and negative rationale generation.
"""

from decimal import Decimal, ROUND_HALF_UP
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from scanner_models import CandidateScore, ScannedCandidate

TWO_DECIMALS = Decimal("0.01")


def calculate_candidate_score(
    price: Decimal,
    adv: Decimal,
    rvol: Optional[Decimal],
    rsi: Optional[Decimal],
    macd_hist: Optional[Decimal],
    atr_pct: Optional[Decimal],
    above_sma_20: Optional[bool],
    above_sma_50: Optional[bool],
    above_sma_200: Optional[bool],
    sma_20_gt_50: Optional[bool],
) -> Tuple[CandidateScore, List[str], List[str]]:
    """Compute 5-pillar candidate score (0 to 100) and rationale.

    Pillars:
        1. Trend Quality (max 25.0)
        2. Momentum (max 25.0)
        3. Relative Volume (max 20.0)
        4. Volatility Suitability (max 15.0)
        5. Liquidity Quality (max 15.0)
    """
    pos_reasons: List[str] = []
    neg_reasons: List[str] = []

    # 1. Trend Quality (max 25.0)
    trend_score = Decimal("0.0")
    if above_sma_20:
        trend_score += Decimal("8.0")
        pos_reasons.append("Price is trading above the 20-day SMA.")
    else:
        neg_reasons.append("Price is below the 20-day SMA.")

    if above_sma_50:
        trend_score += Decimal("8.0")
        pos_reasons.append("Price is trading above the 50-day SMA.")
    else:
        neg_reasons.append("Price is below the 50-day SMA.")

    if sma_20_gt_50:
        trend_score += Decimal("5.0")
        pos_reasons.append("20-day SMA is above 50-day SMA (bullish alignment).")
    elif sma_20_gt_50 is False:
        neg_reasons.append("20-day SMA is below 50-day SMA.")

    if above_sma_200:
        trend_score += Decimal("4.0")
        pos_reasons.append("Price is above 200-day SMA (long-term macro bull trend).")
    elif above_sma_200 is False:
        neg_reasons.append("Price is below 200-day SMA.")
    else:
        # If SMA 200 unavailable, reallocate to SMA 50
        if above_sma_50:
            trend_score += Decimal("4.0")

    # 2. Momentum (max 25.0)
    momentum_score = Decimal("0.0")
    if rsi is not None:
        if Decimal("50.0") <= rsi <= Decimal("65.0"):
            momentum_score += Decimal("15.0")
            pos_reasons.append(f"RSI ({rsi}) is in the optimal bullish continuation zone (50-65).")
        elif (Decimal("45.0") <= rsi < Decimal("50.0")) or (Decimal("65.0") < rsi <= Decimal("72.0")):
            momentum_score += Decimal("10.0")
            pos_reasons.append(f"RSI ({rsi}) reflects constructive momentum.")
        elif rsi > Decimal("72.0"):
            momentum_score += Decimal("5.0")
            neg_reasons.append(f"RSI ({rsi}) is approaching overbought territory (>72).")
        else:
            neg_reasons.append(f"RSI ({rsi}) is below 45 (lagging momentum).")

    if macd_hist is not None:
        if macd_hist > Decimal("0.0"):
            momentum_score += Decimal("10.0")
            pos_reasons.append(f"MACD histogram ({macd_hist}) is positive.")
        else:
            neg_reasons.append(f"MACD histogram ({macd_hist}) is negative.")

    # 3. Relative Volume (max 20.0)
    rvol_score = Decimal("0.0")
    if rvol is not None:
        if Decimal("1.5") <= rvol <= Decimal("3.5"):
            rvol_score = Decimal("20.0")
            pos_reasons.append(f"RVOL ({rvol}x) indicates strong institutional accumulation.")
        elif Decimal("1.1") <= rvol < Decimal("1.5"):
            rvol_score = Decimal("15.0")
            pos_reasons.append(f"RVOL ({rvol}x) shows healthy above-average volume.")
        elif Decimal("0.9") <= rvol < Decimal("1.1"):
            rvol_score = Decimal("10.0")
            pos_reasons.append(f"RVOL ({rvol}x) reflects normal volume participation.")
        elif rvol > Decimal("3.5"):
            rvol_score = Decimal("10.0")
            neg_reasons.append(f"RVOL ({rvol}x) is extremely elevated; watch for blow-off exhaustion.")
        else:
            neg_reasons.append(f"RVOL ({rvol}x) is below average (<0.9x).")

    # 4. Volatility Suitability (max 15.0)
    volatility_score = Decimal("0.0")
    if atr_pct is not None:
        if Decimal("2.0") <= atr_pct <= Decimal("5.0"):
            volatility_score = Decimal("15.0")
            pos_reasons.append(f"ATR% ({atr_pct}%) offers ideal 2-10 day swing volatility.")
        elif (Decimal("1.5") <= atr_pct < Decimal("2.0")) or (Decimal("5.0") < atr_pct <= Decimal("7.0")):
            volatility_score = Decimal("10.0")
            pos_reasons.append(f"ATR% ({atr_pct}%) provides acceptable swing range.")
        elif atr_pct < Decimal("1.5"):
            volatility_score = Decimal("5.0")
            neg_reasons.append(f"ATR% ({atr_pct}%) is relatively low; price may move slowly.")
        else:
            volatility_score = Decimal("5.0")
            neg_reasons.append(f"ATR% ({atr_pct}%) is elevated (>7%); risk management requires wide stops.")

    # 5. Liquidity Quality (max 15.0)
    liquidity_score = Decimal("0.0")
    if adv >= Decimal("50000000.00"):
        liquidity_score = Decimal("15.0")
        pos_reasons.append(f"Average dollar volume (${adv:,.0f}) represents deep institutional liquidity.")
    elif adv >= Decimal("20000000.00"):
        liquidity_score = Decimal("12.0")
        pos_reasons.append(f"Average dollar volume (${adv:,.0f}) is strong.")
    elif adv >= Decimal("10000000.00"):
        liquidity_score = Decimal("8.0")
        pos_reasons.append(f"Average dollar volume (${adv:,.0f}) meets swing liquidity thresholds.")
    else:
        neg_reasons.append(f"Average dollar volume (${adv:,.0f}) is thin (<$10M).")

    total_score = (
        trend_score
        + momentum_score
        + rvol_score
        + volatility_score
        + liquidity_score
    )
    total_score = max(Decimal("0.0"), min(Decimal("100.0"), total_score))

    candidate_score = CandidateScore(
        trend_score=trend_score.quantize(TWO_DECIMALS, rounding=ROUND_HALF_UP),
        momentum_score=momentum_score.quantize(TWO_DECIMALS, rounding=ROUND_HALF_UP),
        rvol_score=rvol_score.quantize(TWO_DECIMALS, rounding=ROUND_HALF_UP),
        volatility_score=volatility_score.quantize(TWO_DECIMALS, rounding=ROUND_HALF_UP),
        liquidity_score=liquidity_score.quantize(TWO_DECIMALS, rounding=ROUND_HALF_UP),
        total_score=total_score.quantize(TWO_DECIMALS, rounding=ROUND_HALF_UP),
    )

    return candidate_score, pos_reasons, neg_reasons


def rank_candidates(
    candidates: List[ScannedCandidate],
    top_n: Optional[int] = None,
) -> List[ScannedCandidate]:
    """Sort and rank candidates deterministically by total score, then dollar volume, then ticker."""
    # Secondary tie-breakers:
    # 1. Total score (descending)
    # 2. Average Dollar Volume (descending) from indicators
    # 3. Ticker (alphabetical ascending)
    def sort_key(c: ScannedCandidate) -> Tuple[Decimal, Decimal, str]:
        adv = Decimal(str(c.indicators.get("adv", "0")))
        return (-c.score.total_score, -adv, c.ticker)

    sorted_candidates = sorted(candidates, key=sort_key)

    ranked: List[ScannedCandidate] = []
    for rank_idx, cand in enumerate(sorted_candidates, start=1):
        ranked.append(
            ScannedCandidate(
                ticker=cand.ticker,
                rank=rank_idx,
                score=cand.score,
                price=cand.price,
                indicators=cand.indicators,
                positive_reasons=cand.positive_reasons,
                negative_reasons=cand.negative_reasons,
                warnings=cand.warnings,
                market_regime=cand.market_regime,
                as_of_timestamp=cand.as_of_timestamp,
                is_eligible=cand.is_eligible,
                rejection_reasons=cand.rejection_reasons,
            )
        )
        if top_n is not None and len(ranked) >= top_n:
            break

    return ranked
