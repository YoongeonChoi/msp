from __future__ import annotations

from dataclasses import dataclass

from app.adapters.ai.openai_mock import OpenAIMock
from app.adapters.broker.toss_mock import TossMock
from app.adapters.fundamentals.opendart_mock import OpenDartMock
from app.adapters.market_data.krx_mock import KrxMock
from app.adapters.news.naver_news_mock import NaverNewsMock
from app.adapters.persistence.sql_repository import InMemoryRepository
from app.adapters.persistence.supabase_repository import SupabaseRepository
from app.application.services.execution_service import ExecutionService
from app.application.services.feature_service import FeatureService
from app.application.services.health_service import HealthService
from app.application.services.risk_service import RiskService
from app.application.services.trading_loop import TradingLoop
from app.application.use_cases.run_trading_cycle import RunTradingCycle
from app.config import Settings
from app.domain.trading.entities import BotSettings
from app.infrastructure.graceful_shutdown import ShutdownFlag


@dataclass(frozen=True, slots=True)
class Container:
    trading_loop: TradingLoop
    repository: InMemoryRepository | SupabaseRepository


def build_container(settings: Settings, shutdown: ShutdownFlag) -> Container:
    repository = (
        SupabaseRepository(settings)
        if settings.use_supabase_repository()
        else InMemoryRepository(
            BotSettings(
                enabled=False,
                mode="paper",
                live_order_allowed=False,
                loop_interval_sec=settings.loop_interval_sec,
            )
        )
    )
    broker = TossMock()
    market_data = KrxMock()
    fundamentals = OpenDartMock()
    news = NaverNewsMock()
    ai = OpenAIMock()
    risk_service = RiskService()
    health_service = HealthService(repository, broker, market_data, fundamentals, news, ai)
    execution_service = ExecutionService(broker, repository, risk_service)
    feature_service = FeatureService()
    run_cycle = RunTradingCycle(
        repository=repository,
        market_data=market_data,
        health_service=health_service,
        execution_service=execution_service,
        risk_service=risk_service,
        feature_service=feature_service,
    )
    return Container(
        trading_loop=TradingLoop(settings, shutdown, run_cycle),
        repository=repository,
    )

