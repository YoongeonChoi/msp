from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class StrategyWeights:
    technical: float = 0.35
    fundamental: float = 0.25
    market_sector: float = 0.15
    news_event: float = 0.15
    portfolio: float = 0.10

    def total(self) -> float:
        return (
            self.technical
            + self.fundamental
            + self.market_sector
            + self.news_event
            + self.portfolio
        )


Action = Literal["hold", "buy", "sell"]

