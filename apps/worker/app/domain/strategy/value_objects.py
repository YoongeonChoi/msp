from __future__ import annotations

from typing import Protocol

from app.domain.strategy.entities import FeatureVector, StrategyContext
from app.domain.trading.entities import Signal


class StrategyPort(Protocol):
    def score(self, features: FeatureVector, context: StrategyContext) -> Signal:
        ...

