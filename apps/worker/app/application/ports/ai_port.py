from __future__ import annotations

from typing import Protocol

from app.domain.news_intel.entities import NewsClassification


class AIPort(Protocol):
    async def classify_news(self, symbol: str, title: str, summary: str) -> NewsClassification:
        ...

    async def provider_health(self) -> bool:
        ...

