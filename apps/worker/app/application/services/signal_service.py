from __future__ import annotations

from typing import Literal

from app.domain.strategy.entities import FeatureVector, StrategyContext
from app.domain.strategy.value_objects import StrategyPort
from app.domain.trading.entities import Signal


def clamp_score(value: float) -> float:
    return max(0.0, min(1.0, value))


class WeightedFactorStrategyV1(StrategyPort):
    def score(self, features: FeatureVector, context: StrategyContext) -> Signal:
        weights = context.weights
        total = weights.total()
        if total <= 0:
            final_score = 0.0
        else:
            final_score = (
                weights.technical * features.technical_score
                + weights.fundamental * features.fundamental_score
                + weights.market_sector * features.market_sector_score
                + weights.news_event * features.news_event_score
                + weights.portfolio * features.portfolio_score
            ) / total
        score = clamp_score(final_score)
        action: Literal["hold", "buy", "sell"]
        if score >= context.buy_threshold:
            action = "buy"
        elif score <= context.sell_threshold:
            action = "sell"
        else:
            action = "hold"
        return Signal(
            symbol=features.symbol,
            action=action,
            final_score=score,
            confidence=score,
            order_amount_krw=context.order_amount_krw,
            sector=context.sector,
            reason_json={
                "strategy": "WeightedFactorStrategyV1",
                "component_scores": {
                    "technical": features.technical_score,
                    "fundamental": features.fundamental_score,
                    "market_sector": features.market_sector_score,
                    "news_event": features.news_event_score,
                    "portfolio": features.portfolio_score,
                },
            },
        )


class RuleOnlyStrategyV1(StrategyPort):
    def score(self, features: FeatureVector, context: StrategyContext) -> Signal:
        technical_ok = features.technical_score >= 0.7
        news_ok = features.news_event_score >= 0.5
        action: Literal["hold", "buy", "sell"]
        action = "buy" if technical_ok and news_ok else "hold"
        return Signal(
            symbol=features.symbol,
            action=action,
            final_score=1.0 if action == "buy" else 0.5,
            confidence=0.6,
            order_amount_krw=context.order_amount_krw,
            sector=context.sector,
            reason_json={"strategy": "RuleOnlyStrategyV1"},
        )


class NoOpStrategy(StrategyPort):
    def score(self, features: FeatureVector, context: StrategyContext) -> Signal:
        return Signal(
            symbol=features.symbol,
            action="hold",
            final_score=0.0,
            confidence=1.0,
            order_amount_krw=0,
            sector=context.sector,
            reason_json={"strategy": "NoOpStrategy"},
        )


class MLMetaLabelStrategy:
    status = "placeholder_requires_paper_testing"
