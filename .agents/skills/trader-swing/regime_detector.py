"""Deterministic market regime detection engine for trader-swing.

Enforces:
1. Pure Decimal arithmetic for all scores and weights.
2. Transparent rule-based classification across broad-market proxies (SPY, QQQ, IWM).
3. Graceful handling of missing or partial proxy data without silent fabrication.
4. Comprehensive evidence tracking (supporting, contradicting, warnings, indicators used).
5. Automatic policy attachment mapping regime to downstream strategy constraints.
"""

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import os
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from market_data_models import OHLCVCandle, to_utc
from indicator_models import TechnicalIndicatorSnapshot
from indicators import TechnicalIndicatorsEngine, quantize_indicator
from regime_models import (
    DEFAULT_REGIME_POLICIES,
    MarketRegime,
    MarketRegimeResult,
    ProxyAnalysis,
    RegimePolicy,
)
from regime_validation import (
    InsufficientRegimeDataError,
    validate_proxy_map,
    validate_regime_candles,
)

FOUR_DECIMALS = Decimal("0.0001")


def classify_composite_score(
    score: Decimal,
    strong_bull_threshold: Decimal = Decimal("0.6000"),
    bull_threshold: Decimal = Decimal("0.2000"),
    bear_threshold: Decimal = Decimal("-0.2000"),
    strong_bear_threshold: Decimal = Decimal("-0.6000"),
) -> MarketRegime:
    """Map composite regime score strictly to 5 mutually exclusive, non-overlapping boundary bins.
    
    Deterministic boundaries:
      [+0.6000, +1.0000] -> STRONG_BULLISH
      [+0.2000, +0.6000) -> BULLISH
      [-0.2000, +0.2000) -> SIDEWAYS
      (-0.6000, -0.2000) -> BEARISH
      [-1.0000, -0.6000] -> STRONG_BEARISH
    """
    s = Decimal(str(score))
    if s >= strong_bull_threshold:
        return MarketRegime.STRONG_BULLISH
    elif s >= bull_threshold:
        return MarketRegime.BULLISH
    elif s >= bear_threshold:
        return MarketRegime.SIDEWAYS
    elif s > strong_bear_threshold:
        return MarketRegime.BEARISH
    else:
        return MarketRegime.STRONG_BEARISH


class MarketRegimeDetector:
    """Deterministic, explainable US equity market regime detector."""

    def __init__(
        self,
        strong_bull_threshold: Decimal = Decimal("0.6000"),
        bull_threshold: Decimal = Decimal("0.2000"),
        bear_threshold: Decimal = Decimal("-0.2000"),
        strong_bear_threshold: Decimal = Decimal("-0.6000"),
        sma_neutral_buffer: Decimal = Decimal("0.0025"),  # 0.25% neutral consolidation band
        proxy_weights: Optional[Dict[str, Decimal]] = None,
        max_stale_days: int = 5,
    ):
        self.strong_bull_threshold = strong_bull_threshold
        self.bull_threshold = bull_threshold
        self.bear_threshold = bear_threshold
        self.strong_bear_threshold = strong_bear_threshold
        self.sma_neutral_buffer = sma_neutral_buffer
        self.max_stale_days = max_stale_days
        self.proxy_weights = proxy_weights or {
            "SPY": Decimal("0.50"),
            "QQQ": Decimal("0.30"),
            "IWM": Decimal("0.20"),
        }
        self.engine = TechnicalIndicatorsEngine(precision=FOUR_DECIMALS)

    def analyze_proxy(
        self,
        candles: Sequence[OHLCVCandle],
        require_complete: bool = True,
        current_time: Optional[datetime] = None,
    ) -> ProxyAnalysis:
        """Evaluate technical indicators for an individual market proxy."""
        validate_regime_candles(candles, min_count=20, require_complete=require_complete, current_time=current_time)

        ticker = candles[-1].ticker
        terminal = candles[-1]
        price = terminal.close
        as_of = terminal.timestamp
        evidence: List[str] = []

        # Compute full indicator snapshot
        snap = self.engine.compute_all(candles, require_complete=require_complete, current_time=current_time)

        # 1. Price vs SMA 200 (Long-term trend filter)
        above_sma_200: Optional[bool] = None
        score_sma_200 = Decimal("0.0")
        if snap.sma_200.is_available and snap.sma_200.value is not None:
            sma_200 = snap.sma_200.value
            pct_diff_200 = (price - sma_200) / sma_200
            if pct_diff_200 > self.sma_neutral_buffer:
                above_sma_200 = True
                score_sma_200 = Decimal("0.25")
                evidence.append(f"{ticker} price (${price}) is above 200 SMA (${sma_200}).")
            elif pct_diff_200 < -self.sma_neutral_buffer:
                above_sma_200 = False
                score_sma_200 = Decimal("-0.25")
                evidence.append(f"{ticker} price (${price}) is below 200 SMA (${sma_200}).")
            else:
                above_sma_200 = None
                evidence.append(f"{ticker} price (${price}) is consolidating at 200 SMA (${sma_200}).")
        else:
            evidence.append(f"{ticker} 200 SMA is unavailable (requires 200 bars).")

        # 2. Price vs SMA 50 (Intermediate trend filter)
        above_sma_50: Optional[bool] = None
        score_sma_50 = Decimal("0.0")
        if snap.sma_50.is_available and snap.sma_50.value is not None:
            sma_50 = snap.sma_50.value
            pct_diff_50 = (price - sma_50) / sma_50
            if pct_diff_50 > self.sma_neutral_buffer:
                above_sma_50 = True
                score_sma_50 = Decimal("0.20")
                evidence.append(f"{ticker} price (${price}) is above 50 SMA (${sma_50}).")
            elif pct_diff_50 < -self.sma_neutral_buffer:
                above_sma_50 = False
                score_sma_50 = Decimal("-0.20")
                evidence.append(f"{ticker} price (${price}) is below 50 SMA (${sma_50}).")
            else:
                above_sma_50 = None
                evidence.append(f"{ticker} price (${price}) is consolidating at 50 SMA (${sma_50}).")
        else:
            evidence.append(f"{ticker} 50 SMA is unavailable.")

        # 3. Price vs SMA 20 (Short-term trend filter)
        above_sma_20: Optional[bool] = None
        score_sma_20 = Decimal("0.0")
        if snap.sma_20.is_available and snap.sma_20.value is not None:
            sma_20 = snap.sma_20.value
            pct_diff_20 = (price - sma_20) / sma_20
            if pct_diff_20 > self.sma_neutral_buffer:
                above_sma_20 = True
                score_sma_20 = Decimal("0.15")
                evidence.append(f"{ticker} price (${price}) is above 20 SMA (${sma_20}).")
            elif pct_diff_20 < -self.sma_neutral_buffer:
                above_sma_20 = False
                score_sma_20 = Decimal("-0.15")
                evidence.append(f"{ticker} price (${price}) is below 20 SMA (${sma_20}).")
            else:
                above_sma_20 = None
                evidence.append(f"{ticker} price (${price}) is consolidating at 20 SMA (${sma_20}).")

        # 4. Moving Average Alignment (20 SMA vs 50 SMA)
        score_alignment = Decimal("0.0")
        if (
            snap.sma_20.is_available
            and snap.sma_20.value is not None
            and snap.sma_50.is_available
            and snap.sma_50.value is not None
        ):
            if snap.sma_20.value > snap.sma_50.value:
                score_alignment = Decimal("0.10")
                evidence.append(f"{ticker} 20 SMA (${snap.sma_20.value}) is above 50 SMA (${snap.sma_50.value}).")
            else:
                score_alignment = Decimal("-0.10")
                evidence.append(f"{ticker} 20 SMA (${snap.sma_20.value}) is below 50 SMA (${snap.sma_50.value}).")

        # 5. Moving Average Slopes (5 bars lookback)
        sma_20_rising: Optional[bool] = None
        sma_50_rising: Optional[bool] = None
        score_slope = Decimal("0.0")
        if len(candles) >= 25:
            prior_snap = self.engine.compute_all(candles[:-5], require_complete=require_complete)
            if (
                snap.sma_20.is_available
                and snap.sma_20.value is not None
                and prior_snap.sma_20.is_available
                and prior_snap.sma_20.value is not None
            ):
                sma_20_rising = snap.sma_20.value > prior_snap.sma_20.value

            if (
                snap.sma_50.is_available
                and snap.sma_50.value is not None
                and prior_snap.sma_50.is_available
                and prior_snap.sma_50.value is not None
            ):
                sma_50_rising = snap.sma_50.value > prior_snap.sma_50.value
                if sma_50_rising:
                    score_slope += Decimal("0.10")
                    evidence.append(f"{ticker} 50 SMA slope is rising over last 5 bars.")
                else:
                    score_slope -= Decimal("0.10")
                    evidence.append(f"{ticker} 50 SMA slope is falling over last 5 bars.")

        # 6. Momentum: RSI 14
        rsi_val: Optional[Decimal] = snap.rsi_14.value if snap.rsi_14.is_available else None
        score_rsi = Decimal("0.0")
        if rsi_val is not None:
            if rsi_val >= Decimal("60.0"):
                score_rsi = Decimal("0.10")
                evidence.append(f"{ticker} RSI ({rsi_val}) shows strong bullish momentum.")
            elif rsi_val >= Decimal("50.0"):
                score_rsi = Decimal("0.05")
                evidence.append(f"{ticker} RSI ({rsi_val}) is moderately bullish (>50).")
            elif rsi_val >= Decimal("40.0"):
                score_rsi = Decimal("-0.05")
                evidence.append(f"{ticker} RSI ({rsi_val}) is moderately bearish (<50).")
            else:
                score_rsi = Decimal("-0.10")
                evidence.append(f"{ticker} RSI ({rsi_val}) shows oversold/bearish momentum.")

        # 7. Momentum: MACD Histogram
        macd_hist: Optional[Decimal] = snap.macd.histogram if snap.macd.is_available else None
        score_macd = Decimal("0.0")
        if macd_hist is not None:
            if macd_hist > Decimal("0.0"):
                score_macd = Decimal("0.10")
                evidence.append(f"{ticker} MACD histogram ({macd_hist}) is positive.")
            else:
                score_macd = Decimal("-0.10")
                evidence.append(f"{ticker} MACD histogram ({macd_hist}) is negative.")

        # 8. Volatility: ATR% (ATR / Close)
        atr_val: Optional[Decimal] = snap.atr_14.value if snap.atr_14.is_available else None
        atr_pct: Optional[Decimal] = None
        if atr_val is not None and price > Decimal("0.0"):
            atr_pct = quantize_indicator((atr_val / price) * Decimal("100"), FOUR_DECIMALS)

        # Aggregate raw score
        raw_score = (
            score_sma_200
            + score_sma_50
            + score_sma_20
            + score_alignment
            + score_slope
            + score_rsi
            + score_macd
        )
        # Clamp to [-1.0000, 1.0000]
        score = max(Decimal("-1.0000"), min(Decimal("1.0000"), raw_score))

        return ProxyAnalysis(
            ticker=ticker,
            price=price,
            as_of_timestamp=as_of,
            above_sma_20=above_sma_20,
            above_sma_50=above_sma_50,
            above_sma_200=above_sma_200,
            sma_20_rising=sma_20_rising,
            sma_50_rising=sma_50_rising,
            rsi=rsi_val,
            macd_histogram=macd_hist,
            atr_14=atr_val,
            atr_percent=atr_pct,
            score=quantize_indicator(score, FOUR_DECIMALS),
            evidence=evidence,
        )

    def detect_regime(
        self,
        proxy_candles_map: Dict[str, Sequence[OHLCVCandle]],
        current_time: Optional[datetime] = None,
        require_complete: bool = True,
    ) -> MarketRegimeResult:
        """Determine broad market regime from available index proxy data."""
        warnings: List[str] = []
        missing_inputs: List[str] = []
        supporting: List[str] = []
        contradicting: List[str] = []

        # Validate inputs
        try:
            val_warnings = validate_proxy_map(
                proxy_candles_map,
                primary_proxy="SPY",
                min_bars=20,
                require_complete=require_complete,
                current_time=current_time,
            )
            warnings.extend(val_warnings)
        except InsufficientRegimeDataError as exc:
            # Cannot determine regime without primary proxy
            return MarketRegimeResult(
                regime=MarketRegime.UNKNOWN,
                confidence=Decimal("0.0000"),
                aggregate_score=Decimal("0.0000"),
                supporting_evidence=[],
                contradicting_evidence=[str(exc)],
                proxy_analyses={},
                indicators_used={},
                as_of_timestamp=None,
                data_freshness="UNKNOWN",
                explanation=f"Cannot evaluate market regime: {str(exc)}",
                warnings=[str(exc)],
                missing_inputs=["SPY"],
                policy=DEFAULT_REGIME_POLICIES[MarketRegime.UNKNOWN],
            )

        # Analyze available proxies
        analyses: Dict[str, ProxyAnalysis] = {}
        for ticker in ["SPY", "QQQ", "IWM"]:
            if ticker in proxy_candles_map and len(proxy_candles_map[ticker]) >= 20:
                analyses[ticker] = self.analyze_proxy(
                    proxy_candles_map[ticker],
                    require_complete=require_complete,
                    current_time=current_time,
                )
            else:
                missing_inputs.append(ticker)
                warnings.append(f"Proxy '{ticker}' not available in input dataset.")

        if "SPY" not in analyses:
            return MarketRegimeResult(
                regime=MarketRegime.UNKNOWN,
                confidence=Decimal("0.0000"),
                aggregate_score=Decimal("0.0000"),
                supporting_evidence=[],
                contradicting_evidence=["Primary proxy SPY analysis failed."],
                proxy_analyses={},
                indicators_used={},
                as_of_timestamp=None,
                data_freshness="UNKNOWN",
                explanation="Primary market proxy SPY could not be analyzed.",
                warnings=warnings,
                missing_inputs=["SPY"],
                policy=DEFAULT_REGIME_POLICIES[MarketRegime.UNKNOWN],
            )

        # Check data freshness against current_time
        now_dt = to_utc(current_time) if current_time is not None else datetime.now(timezone.utc)
        spy_ts = to_utc(analyses["SPY"].as_of_timestamp)
        age_days = (now_dt.date() - spy_ts.date()).days
        freshness = "FRESH"
        if age_days > self.max_stale_days:
            freshness = "STALE"
            warnings.append(
                f"Market proxy data is {age_days} days old (exceeds max_stale_days={self.max_stale_days})."
            )

        # Calculate weighted aggregate score
        active_weights: Dict[str, Decimal] = {}
        total_weight = Decimal("0.0")
        for ticker in analyses:
            w = self.proxy_weights.get(ticker, Decimal("0.10"))
            active_weights[ticker] = w
            total_weight += w

        aggregate_score = Decimal("0.0")
        for ticker, analysis in analyses.items():
            normalized_w = active_weights[ticker] / total_weight
            aggregate_score += analysis.score * normalized_w

        aggregate_score = quantize_indicator(aggregate_score, FOUR_DECIMALS)

        # Classify regime based on thresholds (mutually exclusive boundaries)
        regime = classify_composite_score(
            aggregate_score,
            self.strong_bull_threshold,
            self.bull_threshold,
            self.bear_threshold,
            self.strong_bear_threshold,
        )

        # If data is stale, force UNKNOWN
        if freshness == "STALE":
            regime = MarketRegime.UNKNOWN
            warnings.append("Regime overridden to UNKNOWN due to stale proxy data.")

        # Aggregate evidence
        for ticker, analysis in analyses.items():
            if analysis.score > Decimal("0.0"):
                supporting.extend([f"[{ticker}] {ev}" for ev in analysis.evidence if "above" in ev or "rising" in ev or "positive" in ev or "bullish" in ev])
                contradicting.extend([f"[{ticker}] {ev}" for ev in analysis.evidence if "below" in ev or "falling" in ev or "negative" in ev or "bearish" in ev])
            else:
                supporting.extend([f"[{ticker}] {ev}" for ev in analysis.evidence if "below" in ev or "falling" in ev or "negative" in ev or "bearish" in ev])
                contradicting.extend([f"[{ticker}] {ev}" for ev in analysis.evidence if "above" in ev or "rising" in ev or "positive" in ev or "bullish" in ev])

        # Deterministic Confidence Calculation
        # Factor 1: Proxy agreement (do proxies point in the same direction?)
        signs = [1 if a.score > 0 else (-1 if a.score < 0 else 0) for a in analyses.values()]
        agreement_ratio = Decimal(abs(sum(signs))) / Decimal(len(signs)) if signs else Decimal("0.0")

        # Factor 2: Coverage (presence of QQQ, IWM in addition to SPY)
        coverage_ratio = Decimal(len(analyses)) / Decimal(len(self.proxy_weights))

        # Factor 3: Indicator completeness (was 200 SMA available?)
        sma_200_count = sum(1 for a in analyses.values() if a.above_sma_200 is not None)
        completeness_ratio = Decimal(sma_200_count) / Decimal(len(analyses))

        raw_confidence = (
            (agreement_ratio * Decimal("0.40"))
            + (coverage_ratio * Decimal("0.30"))
            + (completeness_ratio * Decimal("0.30"))
        )
        if freshness == "STALE":
            raw_confidence = Decimal("0.0")

        confidence = quantize_indicator(raw_confidence, FOUR_DECIMALS)

        # Build explanation
        explanation = (
            f"Market classified as {regime.value} with aggregate score {aggregate_score} "
            f"and confidence {confidence}. Evaluated {len(analyses)} proxies ({', '.join(analyses.keys())})."
        )

        indicators_used = {
            t: {
                "price": str(a.price),
                "above_sma_20": a.above_sma_20,
                "above_sma_50": a.above_sma_50,
                "above_sma_200": a.above_sma_200,
                "rsi": str(a.rsi) if a.rsi is not None else None,
                "macd_histogram": str(a.macd_histogram) if a.macd_histogram is not None else None,
                "score": str(a.score),
            }
            for t, a in analyses.items()
        }

        policy = DEFAULT_REGIME_POLICIES[regime]

        return MarketRegimeResult(
            regime=regime,
            confidence=confidence,
            aggregate_score=aggregate_score,
            supporting_evidence=supporting,
            contradicting_evidence=contradicting,
            proxy_analyses=analyses,
            indicators_used=indicators_used,
            as_of_timestamp=analyses["SPY"].as_of_timestamp,
            data_freshness=freshness,
            explanation=explanation,
            warnings=warnings,
            missing_inputs=missing_inputs,
            policy=policy,
        )
