from __future__ import annotations

from typing import cast

import pytest

from app import container as container_module
from app.adapters.broker.contract_test_broker import ContractTestBroker
from app.adapters.broker.toss_client import TossClient
from app.adapters.persistence.execution_kernel_v2 import InMemoryExecutionKernelV2
from app.adapters.persistence.supabase_paper_execution_source import (
    SupabasePaperExecutionCommandSource,
)
from app.adapters.persistence.unavailable_paper_execution_source import (
    UnavailablePaperExecutionCommandSource,
)
from app.application.use_cases.apply_unknown_execution_resolutions_v2 import (
    ApplyUnknownExecutionResolutionsV2,
    RunExecutionReconciliationStageV2,
)
from app.application.use_cases.reconcile_execution_v2 import ReconcileExecutionV2
from app.application.use_cases.run_execution_supervisor_v2 import RunExecutionSupervisorV2
from app.config import Settings
from app.container import build_container, build_operations_v2_runtime
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
    settings = Settings(
        EXECUTION_V2_ENABLED=True,
        EXECUTION_V2_WORKER_API_ENABLED=True,
        EXECUTION_V2_WORKER_ID="00000000-0000-4000-8000-000000000001",
        EXECUTION_V2_ACCOUNT_ID="paper-primary",
    )

    runtime = build_operations_v2_runtime(settings, ShutdownFlag())
    supervisor = cast(
        RunExecutionSupervisorV2,
        runtime.operations_loop.run_operations.execution,
    )

    assert supervisor.execution is runtime.run_execution_v2
    assert runtime.run_execution_v2.durable_port is runtime.worker_api
    assert isinstance(runtime.execution_source, FakePaperExecutionSource)
    assert supervisor.source is runtime.execution_source
    await runtime.close()
    assert runtime.execution_source.closed is True
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
    settings = Settings(
        EXECUTION_V2_ENABLED=True,
        EXECUTION_V2_WORKER_API_ENABLED=True,
        EXECUTION_V2_WORKER_ID="00000000-0000-4000-8000-000000000001",
        EXECUTION_V2_ACCOUNT_ID="paper-primary",
    )

    runtime = build_operations_v2_runtime(settings, ShutdownFlag())
    stage = cast(
        RunExecutionReconciliationStageV2,
        runtime.operations_loop.run_operations.reconciliation,
    )
    unknown = cast(ApplyUnknownExecutionResolutionsV2, stage.unknown)
    generic = cast(ReconcileExecutionV2, stage.generic)

    assert isinstance(stage, RunExecutionReconciliationStageV2)
    assert unknown.port is runtime.worker_api
    assert generic.port is runtime.worker_api
    await runtime.close()


def test_contract_test_operations_runtime_keeps_paper_source_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(container_module, "SupabaseWorkerApi", FakeWorkerApi)
    settings = Settings(
        MOCK_PROVIDERS=True,
        EXECUTION_V2_ENABLED=True,
        EXECUTION_V2_ENVIRONMENT="contract_test",
        EXECUTION_V2_WORKER_API_ENABLED=True,
        EXECUTION_V2_WORKER_ID="00000000-0000-4000-8000-000000000001",
        EXECUTION_V2_ACCOUNT_ID="contract-test-primary",
    )

    runtime = build_operations_v2_runtime(settings, ShutdownFlag())

    assert isinstance(runtime.execution_source, UnavailablePaperExecutionCommandSource)


class FakeWorkerApi:
    release_sha = "a" * 40

    def __init__(self, settings: Settings) -> None:
        del settings
        self.closed = False

    async def close(self) -> None:
        self.closed = True


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
