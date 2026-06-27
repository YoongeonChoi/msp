from __future__ import annotations

from typing import Protocol

from app.domain.common.json import JsonObject
from app.domain.news_intel.entities import NewsClassification
from app.domain.strategy.research import AIUpgradeCandidate


class AIPort(Protocol):
    async def classify_news(self, symbol: str, title: str, summary: str) -> NewsClassification:
        ...

    async def provider_health(self) -> bool:
        ...

    async def generate_monthly_candidate(
        self, dataset_payload: JsonObject
    ) -> AIUpgradeCandidate:
        ...
