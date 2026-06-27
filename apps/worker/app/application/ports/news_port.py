from __future__ import annotations

from typing import Protocol

from app.domain.news_intel.entities import NewsEvent


class NewsPort(Protocol):
    async def get_recent(self, symbol: str) -> list[NewsEvent]:
        ...

    async def provider_health(self) -> bool:
        ...

