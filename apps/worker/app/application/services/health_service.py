from __future__ import annotations

from app.application.ports.ai_port import AIPort
from app.application.ports.broker_port import BrokerPort
from app.application.ports.fundamentals_port import FundamentalsPort
from app.application.ports.market_data_port import MarketDataPort
from app.application.ports.news_port import NewsPort
from app.application.ports.repository_port import RepositoryPort


class HealthService:
    def __init__(
        self,
        repository: RepositoryPort,
        broker: BrokerPort,
        market_data: MarketDataPort,
        fundamentals: FundamentalsPort,
        news: NewsPort,
        ai: AIPort,
    ) -> None:
        self.repository = repository
        self.providers = {
            "toss": broker.provider_health,
            "krx": market_data.provider_health,
            "opendart": fundamentals.provider_health,
            "naver": news.provider_health,
            "openai": ai.provider_health,
        }

    async def check(self) -> dict[str, bool]:
        result: dict[str, bool] = {"supabase": True}
        for provider, check in self.providers.items():
            healthy = await check()
            result[provider] = healthy
            await self.repository.record_api_health(provider, healthy, {})
        return result

