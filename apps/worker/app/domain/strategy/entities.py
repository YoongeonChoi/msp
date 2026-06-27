from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from app.domain.trading.value_objects import StrategyWeights


@dataclass(frozen=True, slots=True)
class FeatureVector:
    symbol: str
    technical_score: float
    fundamental_score: float
    market_sector_score: float
    news_event_score: float
    portfolio_score: float
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StrategyContext:
    strategy_version_id: UUID
    weights: StrategyWeights
    buy_threshold: float = 0.68
    sell_threshold: float = 0.25
    order_amount_krw: int = 100_000
    sector: str = "unknown"


@dataclass(frozen=True, slots=True)
class StrategyVersion:
    id: UUID
    version: str
    status: str
    strategy_type: str
    weights: StrategyWeights
    buy_threshold: float
    sell_threshold: float
