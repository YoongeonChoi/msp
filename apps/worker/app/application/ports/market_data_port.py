from __future__ import annotations

from typing import Protocol

from app.domain.trading.entities import Quote


class MarketDataPort(Protocol):
    async def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        ...

    async def is_market_open(self) -> bool | None:
        ...

    async def provider_health(self) -> bool:
        ...

