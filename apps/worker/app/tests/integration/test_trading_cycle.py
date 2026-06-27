from app.adapters.ai.openai_mock import OpenAIMock
from app.adapters.broker.toss_mock import TossMock
from app.adapters.fundamentals.opendart_mock import OpenDartMock
from app.adapters.market_data.krx_mock import KrxMock
from app.adapters.news.naver_news_mock import NaverNewsMock
from app.adapters.persistence.sql_repository import InMemoryRepository
from app.application.services.execution_service import ExecutionService
from app.application.services.feature_service import FeatureService
from app.application.services.health_service import HealthService
from app.application.services.risk_service import RiskService
from app.application.use_cases.run_trading_cycle import RunTradingCycle
from app.domain.trading.entities import BotSettings


async def test_bot_disabled_creates_no_decisions_or_orders() -> None:
    repository = InMemoryRepository(BotSettings(enabled=False))
    cycle = _cycle(repository)

    await cycle.execute()

    assert len(repository.heartbeats) == 1
    assert repository.decisions == []
    assert repository.orders == []


async def test_paper_enabled_creates_paper_order_only() -> None:
    repository = InMemoryRepository(
        BotSettings(enabled=True, mode="paper", live_order_allowed=False)
    )
    cycle = _cycle(repository)

    await cycle.execute()

    assert len(repository.decisions) == 1
    assert len(repository.orders) == 1
    assert repository.orders[0].mode == "paper"
    assert repository.orders[0].status == "paper"


async def test_live_mode_is_blocked_when_live_permission_false() -> None:
    repository = InMemoryRepository(
        BotSettings(enabled=True, mode="live", live_order_allowed=False)
    )
    cycle = _cycle(repository)

    await cycle.execute()

    assert len(repository.orders) == 1
    assert repository.orders[0].status == "blocked"
    assert repository.orders[0].reason is not None
    assert "live_order_allowed_false" in repository.orders[0].reason


def _cycle(repository: InMemoryRepository) -> RunTradingCycle:
    risk = RiskService()
    broker = TossMock()
    return RunTradingCycle(
        repository=repository,
        market_data=KrxMock(),
        health_service=HealthService(
            repository,
            broker,
            KrxMock(),
            OpenDartMock(),
            NaverNewsMock(),
            OpenAIMock(),
        ),
        execution_service=ExecutionService(broker, repository, risk),
        risk_service=risk,
        feature_service=FeatureService(),
    )

