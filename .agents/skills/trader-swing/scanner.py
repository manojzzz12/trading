"""Quantitative US stock scanner and filtering engine for trader-swing.

Enforces:
1. Strict filtering on price, dollar liquidity, relative volume, volatility, and trend.
2. Market-regime-aware candidate qualification and score thresholding.
3. Transparent recording of rejection reasons without silent omission or data fabrication.
4. Pure Decimal arithmetic for all financial calculations.
"""

from datetime import datetime, timezone
from decimal import Decimal
import os
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from market_data_models import OHLCVCandle, to_utc
from indicator_models import TechnicalIndicatorSnapshot
from indicators import TechnicalIndicatorsEngine, quantize_indicator
from regime_models import MarketRegimeResult
from ranking import calculate_candidate_score, rank_candidates
from scanner_models import CandidateScore, ScanResult, ScannedCandidate, ScreeningCriteria
from universe import normalize_universe

FOUR_DECIMALS = Decimal("0.0001")
TWO_DECIMALS = Decimal("0.01")


class StockScanner:
    """Quantitative swing-trading scanner and ranking coordinator."""

    def __init__(
        self,
        criteria: Optional[ScreeningCriteria] = None,
        indicator_precision: Decimal = FOUR_DECIMALS,
    ):
        self.criteria = criteria or ScreeningCriteria()
        self.engine = TechnicalIndicatorsEngine(precision=indicator_precision)

    def evaluate_candidate(
        self,
        ticker: str,
        candles: Sequence[OHLCVCandle],
        regime_result: Optional[MarketRegimeResult] = None,
        require_complete: bool = True,
    ) -> ScannedCandidate:
        """Evaluate a single ticker's candle series against screening and ranking rules."""
        rejection_reasons: List[str] = []
        warnings: List[str] = []

        if not candles:
            rejection_reasons.append("No candle data available.")
            score = CandidateScore(
                trend_score=Decimal("0.0"),
                momentum_score=Decimal("0.0"),
                rvol_score=Decimal("0.0"),
                volatility_score=Decimal("0.0"),
                liquidity_score=Decimal("0.0"),
                total_score=Decimal("0.0"),
            )
            return ScannedCandidate(
                ticker=ticker,
                rank=0,
                score=score,
                price=Decimal("0.00"),
                indicators={},
                positive_reasons=[],
                negative_reasons=["No candle data available."],
                warnings=["Candle sequence is empty."],
                market_regime=regime_result.regime.value if regime_result else None,
                as_of_timestamp=datetime.now(timezone.utc),
                is_eligible=False,
                rejection_reasons=rejection_reasons,
            )

        terminal = candles[-1]
        price = terminal.close
        as_of = terminal.timestamp

        if len(candles) < self.criteria.min_history_bars:
            rejection_reasons.append(
                f"History length ({len(candles)} bars) is below minimum {self.criteria.min_history_bars} bars."
            )

        # Compute full indicators
        snap = self.engine.compute_all(candles, require_complete=require_complete)

        # 1. Price filters
        if price < self.criteria.min_price:
            rejection_reasons.append(
                f"Price (${price}) is below minimum threshold (${self.criteria.min_price})."
            )
        if self.criteria.max_price is not None and price > self.criteria.max_price:
            rejection_reasons.append(
                f"Price (${price}) exceeds maximum threshold (${self.criteria.max_price})."
            )

        # 2. Average Dollar Volume (ADV = 20-day average volume * close price)
        avg_vol_20 = snap.average_volume_20.value
        adv = Decimal("0.00")
        if avg_vol_20 is not None:
            adv = (avg_vol_20 * price).quantize(TWO_DECIMALS)
            if adv < self.criteria.min_average_dollar_volume:
                rejection_reasons.append(
                    f"Average dollar volume (${adv:,.0f}) is below minimum (${self.criteria.min_average_dollar_volume:,.0f})."
                )
        else:
            rejection_reasons.append("20-day average volume is unavailable.")

        # 3. Relative Volume (RVOL)
        rvol = snap.relative_volume.value
        if rvol is not None:
            if rvol < self.criteria.min_rvol:
                rejection_reasons.append(
                    f"RVOL ({rvol}x) is below minimum threshold ({self.criteria.min_rvol}x)."
                )
        else:
            warnings.append(f"RVOL is unavailable ({snap.relative_volume.reason}).")

        # 4. Momentum (RSI)
        rsi = snap.rsi_14.value
        if rsi is not None:
            if rsi < self.criteria.min_rsi:
                rejection_reasons.append(
                    f"RSI ({rsi}) is below minimum momentum boundary ({self.criteria.min_rsi})."
                )
            elif rsi > self.criteria.max_rsi:
                rejection_reasons.append(
                    f"RSI ({rsi}) exceeds maximum overbought boundary ({self.criteria.max_rsi})."
                )
        else:
            warnings.append(f"RSI is unavailable ({snap.rsi_14.reason}).")

        # 5. Volatility (ATR%)
        atr_14 = snap.atr_14.value
        atr_pct: Optional[Decimal] = None
        if atr_14 is not None and price > Decimal("0.0"):
            atr_pct = quantize_indicator((atr_14 / price) * Decimal("100"), TWO_DECIMALS)
            if atr_pct < self.criteria.min_atr_percent:
                rejection_reasons.append(
                    f"ATR% ({atr_pct}%) is below minimum swing volatility ({self.criteria.min_atr_percent}%)."
                )
            elif atr_pct > self.criteria.max_atr_percent:
                rejection_reasons.append(
                    f"ATR% ({atr_pct}%) exceeds maximum safe volatility ({self.criteria.max_atr_percent}%)."
                )
        else:
            warnings.append("ATR volatility is unavailable.")

        # 6. Trend Structure (Moving Average Alignment)
        above_sma_20: Optional[bool] = None
        if snap.sma_20.is_available and snap.sma_20.value is not None:
            above_sma_20 = price > snap.sma_20.value
            if self.criteria.require_above_sma_20 and not above_sma_20:
                rejection_reasons.append(f"Price (${price}) is not above 20-day SMA (${snap.sma_20.value}).")

        above_sma_50: Optional[bool] = None
        if snap.sma_50.is_available and snap.sma_50.value is not None:
            above_sma_50 = price > snap.sma_50.value
            if self.criteria.require_above_sma_50 and not above_sma_50:
                rejection_reasons.append(f"Price (${price}) is not above 50-day SMA (${snap.sma_50.value}).")

        above_sma_200: Optional[bool] = None
        if snap.sma_200.is_available and snap.sma_200.value is not None:
            above_sma_200 = price > snap.sma_200.value

        sma_20_gt_50: Optional[bool] = None
        if (
            snap.sma_20.is_available
            and snap.sma_20.value is not None
            and snap.sma_50.is_available
            and snap.sma_50.value is not None
        ):
            sma_20_gt_50 = snap.sma_20.value > snap.sma_50.value

        # Calculate 5-pillar score
        candidate_score, pos_reasons, neg_reasons = calculate_candidate_score(
            price=price,
            adv=adv,
            rvol=rvol,
            rsi=rsi,
            macd_hist=snap.macd.histogram if snap.macd.is_available else None,
            atr_pct=atr_pct,
            above_sma_20=above_sma_20,
            above_sma_50=above_sma_50,
            above_sma_200=above_sma_200,
            sma_20_gt_50=sma_20_gt_50,
        )

        # 7. Market Regime Policy Enforcement
        regime_label: Optional[str] = None
        if regime_result is not None:
            regime_label = regime_result.regime.value
            policy = regime_result.policy
            if not policy.allow_new_longs:
                rejection_reasons.append(
                    f"Market regime '{regime_label}' disallows new long positions ({policy.description})."
                )
            if candidate_score.total_score < policy.min_scanner_score:
                rejection_reasons.append(
                    f"Candidate score ({candidate_score.total_score}) is below regime minimum ({policy.min_scanner_score})."
                )

        is_eligible = len(rejection_reasons) == 0

        indicators_summary = {
            "price": str(price),
            "adv": str(adv),
            "rvol": str(rvol) if rvol is not None else None,
            "rsi": str(rsi) if rsi is not None else None,
            "macd_hist": str(snap.macd.histogram) if snap.macd.is_available else None,
            "atr_14": str(atr_14) if atr_14 is not None else None,
            "atr_pct": str(atr_pct) if atr_pct is not None else None,
            "sma_20": str(snap.sma_20.value) if snap.sma_20.is_available else None,
            "sma_50": str(snap.sma_50.value) if snap.sma_50.is_available else None,
            "sma_200": str(snap.sma_200.value) if snap.sma_200.is_available else None,
        }

        return ScannedCandidate(
            ticker=ticker,
            rank=0,  # Assigned during ranking
            score=candidate_score,
            price=price,
            indicators=indicators_summary,
            positive_reasons=pos_reasons,
            negative_reasons=neg_reasons,
            warnings=warnings,
            market_regime=regime_label,
            as_of_timestamp=as_of,
            is_eligible=is_eligible,
            rejection_reasons=rejection_reasons,
        )

    def scan_universe(
        self,
        universe: Sequence[str],
        candles_map: Dict[str, Sequence[OHLCVCandle]],
        regime_result: Optional[MarketRegimeResult] = None,
        top_n: Optional[int] = 10,
        require_complete: bool = True,
    ) -> ScanResult:
        """Scan and rank an entire ticker universe."""
        clean_universe, drop_warnings = normalize_universe(universe, allow_empty=True)
        warnings: List[str] = list(drop_warnings)

        if not clean_universe:
            return ScanResult(
                candidates=[],
                total_scanned=0,
                total_passed=0,
                rejected_candidates={},
                market_regime=regime_result.regime.value if regime_result else None,
                as_of_timestamp=None,
                warnings=warnings + ["Universe is empty or contains no valid US tickers."],
                evaluated_candidates={},
            )

        all_candidates: List[ScannedCandidate] = []
        rejected_log: Dict[str, List[str]] = {}
        evaluated_candidates: Dict[str, ScannedCandidate] = {}
        latest_ts: Optional[datetime] = None

        for ticker in clean_universe:
            if ticker not in candles_map or not candles_map[ticker]:
                rejected_log[ticker] = ["No candle data provided in candles_map."]
                continue

            candles = candles_map[ticker]
            candidate = self.evaluate_candidate(
                ticker=ticker,
                candles=candles,
                regime_result=regime_result,
                require_complete=require_complete,
            )
            evaluated_candidates[ticker] = candidate

            if latest_ts is None or candidate.as_of_timestamp > latest_ts:
                latest_ts = candidate.as_of_timestamp

            if candidate.is_eligible:
                all_candidates.append(candidate)
            else:
                rejected_log[ticker] = candidate.rejection_reasons

        # Rank eligible candidates
        ranked_candidates = rank_candidates(all_candidates, top_n=top_n)

        return ScanResult(
            candidates=ranked_candidates,
            total_scanned=len(clean_universe),
            total_passed=len(all_candidates),
            rejected_candidates=rejected_log,
            market_regime=regime_result.regime.value if regime_result else None,
            as_of_timestamp=latest_ts,
            warnings=warnings,
            evaluated_candidates=evaluated_candidates,
        )
