from __future__ import annotations

from typing import Protocol

from app.domain.market_data.entities import MarketSectorEvidence


class MarketSectorPort(Protocol):
    async def get_sector(self, symbol: str) -> MarketSectorEvidence | None:
        ...

    async def provider_health(self) -> bool:
        ...
