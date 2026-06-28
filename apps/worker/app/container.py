from __future__ import annotations

from dataclasses import dataclass

from app.adapters.ai.openai_client import OpenAIClient
from app.adapters.ai.openai_mock import OpenAIMock
from app.adapters.alerts.webhook_alert_notifier import WebhookAlertNotifier
from app.adapters.broker.toss_client import TossClient
from app.adapters.broker.toss_mock import TossMock
from app.adapters.fundamentals.opendart_client import OpenDartClient
from app.adapters.fundamentals.opendart_mock import OpenDartMock
from app.adapters.market_data.krx_mock import KrxMock
from app.adapters.market_data.toss_market_data import TossMarketData
from app.adapters.news.naver_news_client import NaverNewsClient
from app.adapters.news.naver_news_mock import NaverNewsMock
from app.adapters.persistence.sql_repository import InMemoryRepository
from app.adapters.persistence.supabase_repository import SupabaseRepository
from app.application.ports.ai_port import AIPort
from app.application.ports.broker_port import BrokerPort
from app.application.ports.fundamentals_port import FundamentalsPort
from app.application.ports.market_data_port import MarketDataPort
from app.application.ports.news_port import NewsPort
from app.application.services.execution_service import ExecutionService
from app.application.services.feature_service import FeatureService
from app.application.services.health_service import HealthService
from app.application.services.order_reconciliation_service import OrderReconciliationService
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
    broker: BrokerPort
    market_data: MarketDataPort
    fundamentals: FundamentalsPort
    news: NewsPort
    ai: AIPort
    market_data_provider_name = "krx"
    if settings.mock_providers:
        broker = TossMock()
        market_data = KrxMock()
        fundamentals = OpenDartMock()
        news = NaverNewsMock()
        ai = OpenAIMock()
    else:
        toss = TossClient(settings)
        broker = toss
        market_data = TossMarketData(toss)
        market_data_provider_name = "toss_market_data"
        fundamentals = OpenDartClient(
            api_key=(
                settings.opendart_api_key.get_secret_value()
                if settings.opendart_api_key is not None
                else None
            ),
        )
        news = NaverNewsClient(
            client_id=(
                settings.naver_client_id.get_secret_value()
                if settings.naver_client_id is not None
                else None
            ),
            client_secret=(
                settings.naver_client_secret.get_secret_value()
                if settings.naver_client_secret is not None
                else None
            ),
        )
        ai = OpenAIClient(
            api_key=(
                settings.openai_api_key.get_secret_value()
                if settings.openai_api_key is not None
                else None
            ),
            model=settings.openai_model,
        )
    risk_service = RiskService()
    alert_notifier = (
        WebhookAlertNotifier(
            webhook_url=settings.alert_webhook_url.get_secret_value(),
            timeout_sec=settings.alert_webhook_timeout_sec,
        )
        if settings.alert_webhook_url is not None
        else None
    )
    health_service = HealthService(
        repository,
        broker,
        market_data,
        fundamentals,
        news,
        ai,
        market_data_provider_name=market_data_provider_name,
    )
    execution_service = ExecutionService(broker, repository, risk_service)
    order_reconciliation_service = OrderReconciliationService(
        broker,
        repository,
        alert_notifier=alert_notifier,
    )
    feature_service = FeatureService(
        fundamentals=fundamentals,
        news=news,
        fundamentals_provider_name=(
            "opendart_mock" if settings.mock_providers else "opendart"
        ),
        news_provider_name="naver_mock" if settings.mock_providers else "naver",
    )
    run_cycle = RunTradingCycle(
        repository=repository,
        broker=broker,
        market_data=market_data,
        health_service=health_service,
        execution_service=execution_service,
        risk_service=risk_service,
        feature_service=feature_service,
        order_reconciliation_service=order_reconciliation_service,
        alert_notifier=alert_notifier,
        live_system_order_count_scope_accepted=settings.live_system_order_count_scope_accepted,
    )
    return Container(
        trading_loop=TradingLoop(settings, shutdown, run_cycle),
        repository=repository,
    )
