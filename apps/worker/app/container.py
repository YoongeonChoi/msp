from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from app.adapters.ai.openai_client import OpenAIClient
from app.adapters.ai.openai_mock import OpenAIMock
from app.adapters.alerts.outbox_webhook_destination import (
    OutboxWebhookDestination,
    UnavailableOutboxDestination,
)
from app.adapters.alerts.webhook_alert_notifier import WebhookAlertNotifier
from app.adapters.broker.contract_test_broker import ContractTestBroker
from app.adapters.broker.toss_client import TossClient
from app.adapters.broker.toss_mock import TossMock
from app.adapters.fundamentals.opendart_client import OpenDartClient
from app.adapters.fundamentals.opendart_mock import OpenDartMock
from app.adapters.market_data.krx_mock import KrxMock
from app.adapters.market_data.toss_market_data import TossMarketData
from app.adapters.news.naver_news_client import NaverNewsClient
from app.adapters.news.naver_news_mock import NaverNewsMock
from app.adapters.persistence.execution_kernel_v2 import InMemoryExecutionKernelV2
from app.adapters.persistence.sql_repository import InMemoryRepository
from app.adapters.persistence.supabase_paper_execution_source import (
    SupabasePaperExecutionCommandSource,
)
from app.adapters.persistence.supabase_repository import SupabaseRepository
from app.adapters.persistence.supabase_worker_api import SupabaseWorkerApi
from app.adapters.persistence.unavailable_paper_execution_source import (
    UnavailablePaperExecutionCommandSource,
)
from app.application.ports.ai_port import AIPort
from app.application.ports.broker_port import BrokerPort
from app.application.ports.fundamentals_port import FundamentalsPort
from app.application.ports.market_data_port import MarketDataPort
from app.application.ports.news_port import NewsPort
from app.application.services.execution_service import ExecutionService
from app.application.services.feature_service import FeatureService
from app.application.services.health_service import HealthService
from app.application.services.operations_loop import OperationsLoop
from app.application.services.order_reconciliation_service import OrderReconciliationService
from app.application.services.portfolio_service import PortfolioReadPort, PortfolioService
from app.application.services.risk_service import RiskService
from app.application.services.trading_loop import TradingLoop
from app.application.use_cases.apply_operation_commands import ApplyOperationCommands
from app.application.use_cases.apply_unknown_execution_resolutions_v2 import (
    ApplyUnknownExecutionResolutionsV2,
    RunExecutionReconciliationStageV2,
)
from app.application.use_cases.dispatch_alert_outbox import DispatchAlertOutbox
from app.application.use_cases.maintain_worker_lease import MaintainWorkerLease
from app.application.use_cases.mature_cash_settlements import MatureCashSettlements
from app.application.use_cases.reconcile_execution_v2 import (
    FailClosedExecutionReconciliationHandler,
    ReconcileExecutionV2,
)
from app.application.use_cases.run_execution_supervisor_v2 import (
    RunExecutionSupervisorV2,
)
from app.application.use_cases.run_execution_v2 import RunExecutionV2
from app.application.use_cases.run_operations_v2 import RunOperationsV2
from app.application.use_cases.run_trading_cycle import RunTradingCycle
from app.config import Settings
from app.domain.trading.entities import BotSettings
from app.infrastructure.authenticated_webhook import ReceiverAckKeyRing
from app.infrastructure.graceful_shutdown import ShutdownFlag


@dataclass(frozen=True, slots=True)
class Container:
    trading_loop: TradingLoop
    repository: InMemoryRepository | SupabaseRepository
    alert_notifier: WebhookAlertNotifier | None = None
    execution_kernel_v2: InMemoryExecutionKernelV2 | None = None
    contract_execution_service_v2: ExecutionService | None = None
    worker_api_v2: SupabaseWorkerApi | None = None
    run_execution_v2: RunExecutionV2 | None = None

    async def close(self) -> None:
        if self.alert_notifier is not None:
            await self.alert_notifier.aclose()


@dataclass(slots=True)
class OperationsV2Runtime:
    operations_loop: OperationsLoop
    worker_api: SupabaseWorkerApi
    destination: OutboxWebhookDestination | UnavailableOutboxDestination
    run_execution_v2: RunExecutionV2
    execution_source: SupabasePaperExecutionCommandSource | UnavailablePaperExecutionCommandSource

    async def close(self) -> None:
        try:
            if isinstance(self.execution_source, SupabasePaperExecutionCommandSource):
                await self.execution_source.close()
        finally:
            try:
                if isinstance(self.destination, OutboxWebhookDestination):
                    await self.destination.close()
            finally:
                await self.worker_api.close()


def build_container(settings: Settings, shutdown: ShutdownFlag) -> Container:
    if settings.execution_v2_worker_api_enabled:
        raise ValueError("worker_api_runtime_requires_operations_v2_entrypoint")
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
    portfolio_reader: PortfolioReadPort
    market_data_provider_name = "krx"
    if settings.mock_providers:
        broker = TossMock()
        portfolio_reader = broker
        market_data = KrxMock()
        fundamentals = OpenDartMock()
        news = NaverNewsMock()
        ai = OpenAIMock()
    else:
        toss = TossClient(settings)
        broker = toss
        portfolio_reader = toss
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
    alert_webhook = _configured_alert_webhook(settings)
    alert_notifier = (
        WebhookAlertNotifier(
            webhook_url=alert_webhook[0],
            key_ring=alert_webhook[1],
            timeout_sec=settings.alert_webhook_timeout_sec,
        )
        if alert_webhook is not None
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
    execution_service = ExecutionService(
        broker,
        repository,
        risk_service,
        shutdown_requested=lambda: shutdown.requested,
    )
    execution_kernel_v2 = (
        InMemoryExecutionKernelV2(settings.execution_v2_environment)
        if settings.execution_v2_enabled
        else None
    )
    contract_execution_service_v2 = (
        ExecutionService(
            ContractTestBroker(),
            repository,
            risk_service,
            shutdown_requested=lambda: shutdown.requested,
        )
        if settings.execution_v2_enabled and settings.execution_v2_environment == "contract_test"
        else None
    )
    worker_api_v2: SupabaseWorkerApi | None = None
    run_execution_v2 = (
        RunExecutionV2(
            execution_kernel_v2,
        )
        if execution_kernel_v2 is not None
        else None
    )
    order_reconciliation_service = OrderReconciliationService(
        broker,
        repository,
        alert_notifier=alert_notifier,
    )
    feature_service = FeatureService(
        fundamentals=fundamentals,
        news=news,
        fundamentals_provider_name=("opendart_mock" if settings.mock_providers else "opendart"),
        news_provider_name="naver_mock" if settings.mock_providers else "naver",
    )
    portfolio_service = PortfolioService(repository, portfolio_reader)
    run_cycle = RunTradingCycle(
        repository=repository,
        broker=broker,
        market_data=market_data,
        health_service=health_service,
        execution_service=execution_service,
        risk_service=risk_service,
        feature_service=feature_service,
        order_reconciliation_service=order_reconciliation_service,
        portfolio_service=portfolio_service,
        alert_notifier=alert_notifier,
        live_system_order_count_scope_accepted=settings.live_system_order_count_scope_accepted,
        shutdown_requested=lambda: shutdown.requested,
        mock_providers=settings.mock_providers,
    )
    return Container(
        trading_loop=TradingLoop(settings, shutdown, run_cycle),
        repository=repository,
        alert_notifier=alert_notifier,
        execution_kernel_v2=execution_kernel_v2,
        contract_execution_service_v2=contract_execution_service_v2,
        worker_api_v2=worker_api_v2,
        run_execution_v2=run_execution_v2,
    )


def build_operations_v2_runtime(
    settings: Settings,
    shutdown: ShutdownFlag,
) -> OperationsV2Runtime:
    if not settings.execution_v2_worker_api_enabled:
        raise ValueError("operations_v2_runtime_requires_worker_api_enablement")
    worker_id = settings.execution_v2_worker_id
    if worker_id is None:
        raise ValueError("operations_v2_runtime_requires_worker_id")
    account_id = settings.execution_v2_account_id
    if account_id is None:
        raise ValueError("operations_v2_runtime_requires_account_id")
    alert_webhook = _configured_alert_webhook(settings)
    worker_api = SupabaseWorkerApi(settings)
    destination: OutboxWebhookDestination | UnavailableOutboxDestination = (
        OutboxWebhookDestination(
            alert_webhook[0],
            key_ring=alert_webhook[1],
            timeout_sec=settings.alert_webhook_timeout_sec,
        )
        if alert_webhook is not None
        else UnavailableOutboxDestination()
    )
    execution_source: (
        SupabasePaperExecutionCommandSource | UnavailablePaperExecutionCommandSource
    ) = (
        SupabasePaperExecutionCommandSource(
            settings,
            account_id=account_id,
            release_sha=worker_api.release_sha,
        )
        if settings.execution_v2_environment == "paper"
        else UnavailablePaperExecutionCommandSource()
    )
    run_execution_v2 = RunExecutionV2(durable_port=worker_api)
    lease_manager = MaintainWorkerLease(
        worker_api,
        account_id=account_id,
        holder_id=worker_id,
        ttl=timedelta(seconds=settings.worker_lease_ttl_sec),
    )
    run_operations = RunOperationsV2(
        ApplyOperationCommands(
            worker_api,
            account_id=account_id,
            holder_id=worker_id,
            current_release_sha=worker_api.release_sha,
            lease_provider=lambda: lease_manager.current,
        ),
        RunExecutionSupervisorV2(
            execution_source,
            run_execution_v2,
            RiskService(),
            worker_id=worker_id,
            current_release_sha=worker_api.release_sha,
        ),
        MatureCashSettlements(
            worker_api,
            account_id=account_id,
            environment=settings.execution_v2_environment,
            holder_id=worker_id,
            release_sha=worker_api.release_sha,
            lease_provider=lambda: lease_manager.current,
        ),
        RunExecutionReconciliationStageV2(
            ApplyUnknownExecutionResolutionsV2(
                worker_api,
                account_id=account_id,
                environment=settings.execution_v2_environment,
                holder_id=worker_id,
                release_sha=worker_api.release_sha,
                lease_provider=lambda: lease_manager.current,
            ),
            ReconcileExecutionV2(
                worker_api,
                FailClosedExecutionReconciliationHandler(
                    worker_api,
                    worker_id=worker_id,
                    current_release_sha=worker_api.release_sha,
                ),
                account_id=account_id,
                worker_id=worker_id,
                current_release_sha=worker_api.release_sha,
                lease_provider=lambda: lease_manager.current,
            ),
        ),
        DispatchAlertOutbox(
            worker_api,
            destination,
            worker_id=worker_id,
        ),
        heartbeat=worker_api,
        worker_id=worker_id,
    )
    return OperationsV2Runtime(
        operations_loop=OperationsLoop(
            settings,
            shutdown,
            run_operations,
            lease_manager,
        ),
        worker_api=worker_api,
        destination=destination,
        run_execution_v2=run_execution_v2,
        execution_source=execution_source,
    )


def _configured_alert_webhook(
    settings: Settings,
) -> tuple[str, ReceiverAckKeyRing] | None:
    key_ring = settings.alert_webhook_receiver_key_ring()
    if settings.alert_webhook_url is None:
        if key_ring is not None:
            raise ValueError("alert_webhook_configuration_is_invalid")
        return None
    if key_ring is None:
        raise ValueError("alert_webhook_configuration_is_invalid")
    return settings.alert_webhook_url.get_secret_value(), key_ring
