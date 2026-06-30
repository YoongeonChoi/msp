from __future__ import annotations

from app.adapters.ai.openai_mock import OpenAIMock
from app.adapters.broker.toss_mock import TossMock
from app.adapters.fundamentals.opendart_mock import OpenDartMock
from app.adapters.market_data.krx_mock import KrxMock
from app.adapters.news.naver_news_mock import NaverNewsMock
from app.adapters.persistence.sql_repository import InMemoryRepository
from app.application.services.health_service import HealthService
from app.domain.common.errors import KnownFailClosedError


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


async def test_health_service_records_safe_provider_failure_details() -> None:
    repository = InMemoryRepository()
    broker = FailingHealthProvider(
        {
            "error_type": "ProviderAuthError",
            "reason": "toss_account_seq_ambiguous",
        }
    )
    service = HealthService(
        repository=repository,
        broker=broker,
        market_data=KrxMock(),
        fundamentals=OpenDartMock(),
        news=NaverNewsMock(),
        ai=OpenAIMock(),
    )

    result = await service.check()

    assert result["toss"] is False
    toss_health = next(row for row in repository.api_health if row["provider"] == "toss")
    assert toss_health["details"] == {
        "error_type": "ProviderAuthError",
        "reason": "toss_account_seq_ambiguous",
    }


async def test_health_service_redacts_sensitive_provider_failure_details() -> None:
    repository = InMemoryRepository()
    broker = FailingHealthProvider(
        {
            "error_type": "ProviderAuthError",
            "reason": "Bearer OPENAI_TEST_SECRET",
            "client_secret": "should_not_persist",
            "invalid-key": "should_not_persist",
        }
    )
    service = HealthService(
        repository=repository,
        broker=broker,
        market_data=KrxMock(),
        fundamentals=OpenDartMock(),
        news=NaverNewsMock(),
        ai=OpenAIMock(),
    )

    await service.check()

    toss_health = next(row for row in repository.api_health if row["provider"] == "toss")
    assert toss_health["details"] == {
        "error_type": "ProviderAuthError",
        "reason": "[redacted]",
    }


async def test_health_service_redacts_raised_fail_closed_details() -> None:
    repository = InMemoryRepository()
    service = HealthService(
        repository=repository,
        broker=RaisingHealthProvider(),
        market_data=KrxMock(),
        fundamentals=OpenDartMock(),
        news=NaverNewsMock(),
        ai=OpenAIMock(),
    )

    await service.check()

    toss_health = next(row for row in repository.api_health if row["provider"] == "toss")
    assert toss_health["details"] == {
        "error_type": "KnownFailClosedError",
        "reason": "[redacted]",
    }


class FailingHealthProvider(TossMock):
    def __init__(self, details: dict[str, object]) -> None:
        super().__init__()
        self._details = details

    async def provider_health(self) -> bool:
        return False

    def provider_health_details(self) -> dict[str, object]:
        return dict(self._details)


class RaisingHealthProvider(TossMock):
    async def provider_health(self) -> bool:
        raise KnownFailClosedError("toss", "Bearer OPENAI_TEST_SECRET")
