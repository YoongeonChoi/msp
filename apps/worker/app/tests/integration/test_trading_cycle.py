from datetime import timedelta

from app.adapters.ai.openai_mock import OpenAIMock
from app.adapters.broker.toss_mock import TossMock
from app.adapters.fundamentals.opendart_mock import OpenDartMock
from app.adapters.market_data.krx_mock import KrxMock
from app.adapters.news.naver_news_mock import NaverNewsMock
from app.adapters.persistence.sql_repository import InMemoryRepository
from app.application.ports.broker_port import BrokerOrderRequest, BrokerOrderResult
from app.application.services.execution_service import ExecutionService
from app.application.services.feature_service import FeatureService
from app.application.services.health_service import HealthService
from app.application.services.risk_service import RiskService
from app.application.use_cases.run_trading_cycle import RunTradingCycle
from app.domain.common.time import now_utc
from app.domain.strategy.entities import StrategyVersion
from app.domain.trading.entities import BotSettings, Quote
from app.domain.trading.value_objects import StrategyWeights


class CountingBroker(TossMock):
    def __init__(self, healthy: bool = True) -> None:
        self.healthy = healthy
        self.place_order_calls = 0

    async def provider_health(self) -> bool:
        return self.healthy

    async def place_order(self, request: BrokerOrderRequest) -> BrokerOrderResult:
        self.place_order_calls += 1
        return await super().place_order(request)


class QuoteOverrideMarketData(KrxMock):
    def __init__(self, quotes: dict[str, Quote]) -> None:
        self.quotes = quotes

    async def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        return self.quotes


async def test_bot_disabled_creates_no_decisions_or_orders() -> None:
    repository = InMemoryRepository(BotSettings(enabled=False))
    broker = CountingBroker()
    cycle = _cycle(repository, broker=broker)

    await cycle.execute()

    assert len(repository.heartbeats) == 1
    assert len(repository.api_health) == 5
    assert repository.decisions == []
    assert repository.orders == []
    assert broker.place_order_calls == 0


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
    assert repository.orders[0].idempotency_key
    assert repository.decisions[0].signal.reason_json
    assert repository.decisions[0].feature_snapshot
    assert repository.decisions[0].risk_snapshot
    assert "technical_score" in repository.decisions[0].feature_snapshot
    assert "fundamental_score" in repository.decisions[0].feature_snapshot
    assert "market_sector_score" in repository.decisions[0].feature_snapshot
    assert "news_event_score" in repository.decisions[0].feature_snapshot
    assert "portfolio_score" in repository.decisions[0].feature_snapshot
    assert "final_score" in repository.decisions[0].feature_snapshot


async def test_live_mode_is_blocked_when_live_permission_false() -> None:
    repository = InMemoryRepository(
        BotSettings(enabled=True, mode="live", live_order_allowed=False)
    )
    broker = CountingBroker()
    cycle = _cycle(repository, broker=broker)

    await cycle.execute()

    assert len(repository.orders) == 1
    assert repository.orders[0].status == "blocked"
    assert repository.orders[0].reason is not None
    assert "live_order_allowed_false" in repository.orders[0].reason
    assert broker.place_order_calls == 0


async def test_paper_mode_never_creates_live_statuses_or_calls_broker() -> None:
    repository = InMemoryRepository(
        BotSettings(enabled=True, mode="paper", live_order_allowed=False)
    )
    broker = CountingBroker()
    cycle = _cycle(repository, broker=broker)

    await cycle.execute()

    assert {order.status for order in repository.orders} <= {"paper", "proposed", "blocked"}
    assert broker.place_order_calls == 0


async def test_duplicate_paper_signal_is_blocked() -> None:
    repository = InMemoryRepository(
        BotSettings(enabled=True, mode="paper", live_order_allowed=False)
    )
    cycle = _cycle(repository)

    await cycle.execute()
    await cycle.execute()

    assert [order.status for order in repository.orders] == ["paper", "blocked"]
    assert repository.orders[1].reason is not None
    assert "duplicate_order_detected" in repository.orders[1].reason
    assert repository.engine_events[-1]["message"] == "paper_order_blocked"


async def test_stale_quote_blocks_paper_order() -> None:
    repository = InMemoryRepository(
        BotSettings(enabled=True, mode="paper", live_order_allowed=False)
    )
    stale_quote = Quote(
        symbol="005930",
        price_krw=75_000,
        as_of=now_utc() - timedelta(seconds=120),
    )
    cycle = _cycle(repository, market_data=QuoteOverrideMarketData({"005930": stale_quote}))

    await cycle.execute()

    assert len(repository.orders) == 1
    assert repository.orders[0].status == "blocked"
    assert repository.orders[0].reason is not None
    assert "stale_quote" in repository.orders[0].reason


async def test_missing_strategy_blocks_paper_order() -> None:
    repository = InMemoryRepository(
        BotSettings(enabled=True, mode="paper", live_order_allowed=False)
    )
    repository.strategy_version = None
    cycle = _cycle(repository)

    await cycle.execute()

    assert repository.decisions == []
    assert repository.orders == []
    assert repository.engine_events[-1]["message"] == "missing_strategy_version"


async def test_paper_cycle_uses_strategy_params_from_repository() -> None:
    repository = InMemoryRepository(
        BotSettings(enabled=True, mode="paper", live_order_allowed=False)
    )
    assert repository.strategy_version is not None
    repository.strategy_version = StrategyVersion(
        id=repository.strategy_version.id,
        version="strategy_v1_weighted_factor",
        status="paper",
        strategy_type="WeightedFactorStrategyV1",
        weights=StrategyWeights(),
        buy_threshold=0.99,
        sell_threshold=0.01,
    )
    cycle = _cycle(repository)

    await cycle.execute()

    assert len(repository.decisions) == 1
    assert repository.decisions[0].signal.action == "hold"
    assert repository.orders == []


async def test_provider_error_blocks_paper_order() -> None:
    repository = InMemoryRepository(
        BotSettings(enabled=True, mode="paper", live_order_allowed=False)
    )
    cycle = _cycle(repository, broker=CountingBroker(healthy=False))

    await cycle.execute()

    assert repository.decisions == []
    assert repository.orders == []
    assert repository.engine_events[-1]["message"] == "critical provider unhealthy"


def _cycle(
    repository: InMemoryRepository,
    broker: CountingBroker | None = None,
    market_data: KrxMock | None = None,
) -> RunTradingCycle:
    risk = RiskService()
    safe_broker = broker or CountingBroker()
    safe_market_data = market_data or KrxMock()
    return RunTradingCycle(
        repository=repository,
        market_data=safe_market_data,
        health_service=HealthService(
            repository,
            safe_broker,
            safe_market_data,
            OpenDartMock(),
            NaverNewsMock(),
            OpenAIMock(),
        ),
        execution_service=ExecutionService(safe_broker, repository, risk),
        risk_service=risk,
        feature_service=FeatureService(),
    )
