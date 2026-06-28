from __future__ import annotations

import anyio

from app.adapters.ai.openai_mock import OpenAIMock
from app.adapters.broker.toss_mock import TossMock
from app.adapters.fundamentals.opendart_mock import OpenDartMock
from app.adapters.market_data.krx_mock import KrxMock
from app.adapters.news.naver_news_mock import NaverNewsMock
from app.adapters.persistence.supabase_repository import SupabaseRepository
from app.application.services.execution_service import ExecutionService
from app.application.services.feature_service import FeatureService
from app.application.services.health_service import HealthService
from app.application.services.risk_service import RiskService
from app.application.use_cases.run_trading_cycle import RunTradingCycle
from app.config import load_settings
from app.domain.trading.entities import BotSettings


async def _run() -> None:
    settings = load_settings()
    repository = SupabaseRepository(
        settings,
        forced_settings=BotSettings(
            enabled=True,
            mode="paper",
            live_order_allowed=False,
            loop_interval_sec=settings.loop_interval_sec,
        ),
    )
    broker = TossMock()
    market_data = KrxMock()
    risk = RiskService()
    cycle = RunTradingCycle(
        repository=repository,
        broker=broker,
        market_data=market_data,
        health_service=HealthService(
            repository,
            broker,
            market_data,
            OpenDartMock(),
            NaverNewsMock(),
            OpenAIMock(),
        ),
        execution_service=ExecutionService(broker, repository, risk),
        risk_service=risk,
        feature_service=FeatureService(),
    )
    try:
        await cycle.execute()
    finally:
        await repository.aclose()
    print("completed one forced paper cycle; live_order_allowed=false")


def main() -> None:
    anyio.run(_run)


if __name__ == "__main__":
    main()
