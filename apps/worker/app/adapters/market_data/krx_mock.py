from __future__ import annotations

from app.domain.common.time import now_utc
from app.domain.trading.entities import Quote


class KrxMock:
    async def provider_health(self) -> bool:
        return True

    async def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        return {
            symbol: Quote(symbol=symbol, price_krw=75_000, as_of=now_utc())
            for symbol in symbols
        }

    async def is_market_open(self) -> bool | None:
        return True
