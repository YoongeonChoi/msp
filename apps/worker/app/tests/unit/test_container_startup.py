from __future__ import annotations

import base64
from typing import Any, cast

import pytest

from app import container as container_module
from app.adapters.alerts.outbox_webhook_destination import OutboxWebhookDestination
from app.adapters.alerts.webhook_alert_notifier import WebhookAlertNotifier
from app.adapters.broker.contract_test_broker import ContractTestBroker
from app.adapters.broker.toss_client import TossClient
from app.adapters.persistence.execution_kernel_v2 import InMemoryExecutionKernelV2
from app.adapters.persistence.supabase_paper_execution_source import (
    SupabasePaperExecutionCommandSource,
)
from app.application.use_cases.apply_unknown_execution_resolutions_v2 import (
    ApplyUnknownExecutionResolutionsV2,
    RunExecutionReconciliationStageV2,
)
from app.application.use_cases.reconcile_execution_v2 import ReconcileExecutionV2
from app.application.use_cases.run_execution_supervisor_v2 import RunExecutionSupervisorV2
from app.config import Settings
from app.container import OperationsV2Runtime, build_container, build_operations_v2_runtime
from app.infrastructure.graceful_shutdown import ShutdownFlag


def test_real_provider_container_starts_without_toss_credentials() -> None:
    settings = Settings(
        MOCK_PROVIDERS=False,
        TOSS_CLIENT_ID=None,
        TOSS_CLIENT_SECRET=None,
        TOSS_ACCOUNT_ID=None,
    )

    container = build_container(settings, ShutdownFlag())

    assert container.trading_loop.settings.mock_providers is False
    assert isinstance(container.trading_loop.run_trading_cycle.broker, TossClient)


def test_execution_v2_is_not_constructed_without_explicit_enablement() -> None:
    container = build_container(Settings(), ShutdownFlag())

    assert container.execution_kernel_v2 is None
    assert container.run_execution_v2 is None
    assert container.contract_execution_service_v2 is None
    assert container.worker_api_v2 is None


def test_contract_execution_v2_uses_only_local_contract_broker() -> None:
    settings = Settings(
        MOCK_PROVIDERS=True,
        EXECUTION_V2_ENABLED=True,
        EXECUTION_V2_ENVIRONMENT="contract_test",
    )

    container = build_container(settings, ShutdownFlag())

    assert isinstance(container.execution_kernel_v2, InMemoryExecutionKernelV2)
    assert container.contract_execution_service_v2 is not None
    assert isinstance(container.contract_execution_service_v2.broker, ContractTestBroker)
    assert container.run_execution_v2 is not None


def test_legacy_container_rejects_worker_api_runtime_to_prevent_dual_loops() -> None:
    settings = Settings(
        EXECUTION_V2_ENABLED=True,
        EXECUTION_V2_WORKER_API_ENABLED=True,
        EXECUTION_V2_WORKER_ID="00000000-0000-4000-8000-000000000001",
        EXECUTION_V2_ACCOUNT_ID="paper-primary",
    )

    with pytest.raises(
        ValueError,
        match="worker_api_runtime_requires_operations_v2_entrypoint",
    ):
        build_container(settings, ShutdownFlag())


async def test_operations_runtime_wires_real_v2_executor_into_execution_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(container_module, "SupabaseWorkerApi", FakeWorkerApi)
    monkeypatch.setattr(
        container_module,
        "SupabasePaperExecutionCommandSource",
        FakePaperExecutionSource,
    )
    graph = _patch_durable_scheduler(monkeypatch)
    settings = Settings(
        EXECUTION_V2_ENABLED=True,
        EXECUTION_V2_WORKER_API_ENABLED=True,
        EXECUTION_V2_WORKER_ID="00000000-0000-4000-8000-000000000001",
        EXECUTION_V2_ACCOUNT_ID="paper-primary",
    )

    runtime = build_operations_v2_runtime(settings, ShutdownFlag())
    supervisor = cast(RunExecutionSupervisorV2, graph["execution"])

    assert supervisor.execution is runtime.run_execution_v2
    assert runtime.run_execution_v2.durable_port is runtime.worker_api
    assert isinstance(runtime.execution_source, FakePaperExecutionSource)
    assert supervisor.source is runtime.execution_source
    assert not hasattr(runtime.scheduler_loop, "runtime")
    assert cast(Any, runtime.scheduler_loop)._runtime is graph["facade"]
    await runtime.close()
    assert runtime.execution_source.closed is True
    assert cast(FakeDurableScheduler, runtime.scheduler).closed is True
    assert cast(FakeWorkerApi, runtime.worker_api).closed is True


async def test_operations_runtime_wires_dedicated_unknown_resolution_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(container_module, "SupabaseWorkerApi", FakeWorkerApi)
    monkeypatch.setattr(
        container_module,
        "SupabasePaperExecutionCommandSource",
        FakePaperExecutionSource,
    )
    graph = _patch_durable_scheduler(monkeypatch)
    settings = Settings(
        EXECUTION_V2_ENABLED=True,
        EXECUTION_V2_WORKER_API_ENABLED=True,
        EXECUTION_V2_WORKER_ID="00000000-0000-4000-8000-000000000001",
        EXECUTION_V2_ACCOUNT_ID="paper-primary",
    )

    runtime = build_operations_v2_runtime(settings, ShutdownFlag())
    stage = cast(RunExecutionReconciliationStageV2, graph["reconciliation"])
    unknown = cast(ApplyUnknownExecutionResolutionsV2, stage.unknown)
    generic = cast(ReconcileExecutionV2, stage.generic)

    assert isinstance(stage, RunExecutionReconciliationStageV2)
    assert unknown.port is runtime.worker_api
    assert generic.port is runtime.worker_api
    await runtime.close()


def test_contract_test_operations_runtime_is_rejected_before_composition() -> None:
    settings = Settings(
        EXECUTION_V2_ENABLED=True,
        EXECUTION_V2_WORKER_API_ENABLED=True,
        EXECUTION_V2_WORKER_ID="00000000-0000-4000-8000-000000000001",
        EXECUTION_V2_ACCOUNT_ID="paper-primary",
    )
    settings.execution_v2_environment = "contract_test"

    with pytest.raises(ValueError, match="worker_api_runtime_requires_paper_environment"):
        build_operations_v2_runtime(settings, ShutdownFlag())


async def test_main_receiver_key_ring_is_wired_to_legacy_and_outbox_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_key_b64 = base64.b64encode(b"c" * 32).decode("ascii")
    previous_key_b64 = base64.b64encode(b"p" * 32).decode("ascii")
    webhook_values: dict[str, object] = {
        "ALERT_WEBHOOK_URL": "https:" + "//alerts.example.test/events",
        "ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_ID": "main-current",
        "ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64": current_key_b64,
        "ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_ID": "main-previous",
        "ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_B64": previous_key_b64,
    }
    legacy = build_container(
        Settings.model_validate(webhook_values),
        ShutdownFlag(),
    )
    notifier = cast(
        WebhookAlertNotifier,
        legacy.trading_loop.run_trading_cycle.alert_notifier,
    )

    monkeypatch.setattr(container_module, "SupabaseWorkerApi", FakeWorkerApi)
    monkeypatch.setattr(
        container_module,
        "SupabasePaperExecutionCommandSource",
        FakePaperExecutionSource,
    )
    _patch_durable_scheduler(monkeypatch)
    operations = build_operations_v2_runtime(
        Settings.model_validate(
            webhook_values
            | {
                "EXECUTION_V2_ENABLED": True,
                "EXECUTION_V2_WORKER_API_ENABLED": True,
                "EXECUTION_V2_WORKER_ID": ("00000000-0000-4000-8000-000000000001"),
                "EXECUTION_V2_ACCOUNT_ID": "paper-primary",
            }
        ),
        ShutdownFlag(),
    )
    destination = cast(OutboxWebhookDestination, operations.destination)

    assert notifier._transport._key_ring.accepted_key_ids == (
        "main-current",
        "main-previous",
    )
    assert destination._transport._key_ring.accepted_key_ids == (
        "main-current",
        "main-previous",
    )

    await legacy.close()
    await operations.close()

    assert notifier._transport._client.is_closed


def test_mutated_production_settings_are_rechecked_at_container_composition() -> None:
    settings = Settings()
    settings.env = "production"

    with pytest.raises(ValueError, match="production_alert_webhook_is_required"):
        build_container(settings, ShutdownFlag())


async def test_operations_runtime_close_attempts_every_resource_once() -> None:
    lifecycle: list[str] = []
    source = FakeCloseResource("source", lifecycle)
    destination = FakeCloseResource(
        "destination",
        lifecycle,
        failure=RuntimeError("destination_close_failed"),
    )
    scheduler = FakeCloseResource("scheduler", lifecycle)
    worker_api = FakeCloseResource("worker_api", lifecycle)
    runtime = OperationsV2Runtime(
        scheduler_loop=cast(Any, object()),
        worker_api=cast(Any, worker_api),
        scheduler=cast(Any, scheduler),
        destination=cast(Any, destination),
        run_execution_v2=cast(Any, object()),
        execution_source=cast(Any, source),
    )

    with pytest.raises(RuntimeError, match="destination_close_failed"):
        await runtime.close()

    assert lifecycle == ["source", "destination", "scheduler", "worker_api"]
    await runtime.close()
    assert lifecycle == ["source", "destination", "scheduler", "worker_api"]


class FakeWorkerApi:
    release_sha = "a" * 40

    def __init__(self, settings: Settings) -> None:
        del settings
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    async def record_worker_heartbeat(self, **_kwargs: object) -> object:
        return object()


class FakePaperExecutionSource(SupabasePaperExecutionCommandSource):
    def __init__(
        self,
        settings: Settings,
        *,
        account_id: str,
        release_sha: str,
    ) -> None:
        del settings
        assert account_id == "paper-primary"
        assert release_sha == "a" * 40
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeDurableScheduler:
    def __init__(
        self,
        settings: Settings,
        *,
        release_sha: str,
    ) -> None:
        del settings
        assert release_sha == "a" * 40
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeDurableFacade:
    def assert_intact(self) -> None:
        return None

    async def converge_step(self) -> object:
        raise AssertionError("container_test_must_not_run_scheduler")

    async def run_once(self) -> object:
        raise AssertionError("container_test_must_not_run_scheduler")


class FakeCloseResource:
    def __init__(
        self,
        name: str,
        lifecycle: list[str],
        *,
        failure: BaseException | None = None,
    ) -> None:
        self.name = name
        self.lifecycle = lifecycle
        self.failure = failure

    async def close(self) -> None:
        self.lifecycle.append(self.name)
        if self.failure is not None:
            raise self.failure


def _patch_durable_scheduler(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, object]:
    graph: dict[str, object] = {}
    facade = FakeDurableFacade()

    def create_facade(**kwargs: object) -> FakeDurableFacade:
        graph.update(kwargs)
        graph["facade"] = facade
        return facade

    monkeypatch.setattr(
        container_module,
        "SupabaseDurableScheduler",
        FakeDurableScheduler,
    )
    monkeypatch.setattr(
        container_module,
        "create_supabase_durable_scheduler_facade",
        create_facade,
    )
    return graph
