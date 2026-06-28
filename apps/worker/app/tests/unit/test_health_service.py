from __future__ import annotations

from app.adapters.ai.openai_mock import OpenAIMock
from app.adapters.broker.toss_mock import TossMock
from app.adapters.fundamentals.opendart_mock import OpenDartMock
from app.adapters.market_data.krx_mock import KrxMock
from app.adapters.news.naver_news_mock import NaverNewsMock
from app.adapters.persistence.sql_repository import InMemoryRepository
from app.application.services.health_service import HealthService


async def test_health_service_records_configured_market_data_provider_name() -> None:
    repository = InMemoryRepository()
    service = HealthService(
        repository=repository,
        broker=TossMock(),
        market_data=KrxMock(),
        fundamentals=OpenDartMock(),
        news=NaverNewsMock(),
        ai=OpenAIMock(),
        market_data_provider_name="toss_market_data",
    )

    result = await service.check()

    assert result["toss_market_data"] is True
    assert "krx" not in result
    providers = {row["provider"] for row in repository.api_health}
    assert "toss_market_data" in providers
    assert "krx" not in providers
