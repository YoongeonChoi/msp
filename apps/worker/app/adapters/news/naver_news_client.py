from __future__ import annotations

from app.domain.common.errors import ProviderUnavailableError
from app.domain.news_intel.entities import NewsEvent


class NaverNewsClient:
    async def provider_health(self) -> bool:
        return False

    async def get_recent(self, symbol: str) -> list[NewsEvent]:
        raise ProviderUnavailableError("naver", "naver_news_contract_not_verified")
