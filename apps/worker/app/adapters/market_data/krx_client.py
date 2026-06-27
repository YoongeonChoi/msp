from __future__ import annotations

from app.domain.common.errors import ProviderUnavailableError
from app.domain.trading.entities import Quote


class KrxClient:
    async def provider_health(self) -> bool:
        return False

    async def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        raise ProviderUnavailableError("krx", "krx_quote_contract_not_verified")

    async def is_market_open(self) -> bool | None:
        return None
