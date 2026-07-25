from __future__ import annotations

import pickle
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from pydantic import SecretStr

from app.adapters.alerts.outbox_webhook_destination import (
    UnavailableOutboxDestination,
)
from app.adapters.persistence.supabase_durable_scheduler import (
    SupabaseDurableScheduler,
)
from app.adapters.persistence.supabase_paper_execution_source import (
    SupabasePaperExecutionCommandSource,
)
from app.adapters.persistence.supabase_worker_api import SupabaseWorkerApi
from app.application.services.risk_service import RiskService
from app.application.use_cases import scheduler_runtime_capability as capability_contract
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
from app.application.use_cases.scheduler_runtime_capability import (
    SchedulerRuntimeCapability,
)
from app.config import Settings
from app.domain.execution_v2.models import WorkerLease
from app.domain.scheduler.models import SchedulerInvariantError
from app.infrastructure.scheduler_runtime_capability_factory import (
    create_scheduler_runtime_capability,
    require_supabase_scheduler_runtime_capability,
)

ACCOUNT_ID = "paper-primary"
HOLDER_ID = "00000000-0000-4000-8000-000000000001"
RELEASE_SHA = "a" * 40
NOW = datetime(2026, 7, 25, 9, 0, tzinfo=UTC)


@dataclass(slots=True)
class _RuntimeGraph:
    scheduler: SupabaseDurableScheduler
    worker_api: SupabaseWorkerApi
    source: SupabasePaperExecutionCommandSource
    lease_manager: MaintainWorkerLease
    commands: ApplyOperationCommands
    execution: RunExecutionSupervisorV2
    settlement: MatureCashSettlements
    reconciliation: RunExecutionReconciliationStageV2
    outbox: DispatchAlertOutbox

    def issue(self) -> SchedulerRuntimeCapability:
        return create_scheduler_runtime_capability(
            scheduler=self.scheduler,
            worker_api=self.worker_api,
            lease_manager=self.lease_manager,
            commands=self.commands,
            execution=self.execution,
            settlement=self.settlement,
            reconciliation=self.reconciliation,
            outbox=self.outbox,
        )


@pytest.fixture
async def runtime_graph() -> AsyncIterator[_RuntimeGraph]:
    graph = _runtime_graph()
    source = graph.source
    scheduler = graph.scheduler
    worker_api = graph.worker_api
    try:
        yield graph
    finally:
        await source.close()
        await scheduler.close()
        await worker_api.close()


def test_capability_rejects_direct_construction_and_forged_arguments() -> None:
    with pytest.raises(TypeError, match="Protocols cannot be instantiated"):
        cast(Any, SchedulerRuntimeCapability)()
    assert not hasattr(capability_contract, "_issue_scheduler_runtime_capability")


async def test_factory_issues_read_only_identity_from_exact_graph(
    runtime_graph: _RuntimeGraph,
) -> None:
    capability = runtime_graph.issue()

    assert capability.scheduler_port is runtime_graph.scheduler
    assert capability.account_id == ACCOUNT_ID


async def test_infrastructure_gate_rejects_structural_capability_fake(
    runtime_graph: _RuntimeGraph,
) -> None:
    class StructuralFake:
        scheduler_port = runtime_graph.scheduler
        account_id = ACCOUNT_ID
        holder_id = HOLDER_ID
        release_sha = RELEASE_SHA
        persistence_authority = runtime_graph.worker_api.persistence_authority

        def assert_intact(self) -> None:
            return None

        def current_outer_lease(self) -> WorkerLease:
            raise AssertionError("must not be called")

    with pytest.raises(SchedulerInvariantError, match="capability_type"):
        require_supabase_scheduler_runtime_capability(StructuralFake())

    capability = runtime_graph.issue()
    assert require_supabase_scheduler_runtime_capability(capability) is capability
    assert capability.holder_id == HOLDER_ID
    assert capability.release_sha == RELEASE_SHA
    assert capability.persistence_authority == runtime_graph.worker_api.persistence_authority
    assert not hasattr(capability, "worker_api")
    assert not hasattr(capability, "lease_provider")
    assert not hasattr(capability, "bindings")
    assert not hasattr(capability, "__dict__")
    with pytest.raises(SchedulerInvariantError, match="capability_is_immutable"):
        cast(Any, capability)._account_id = "paper-secondary"


async def test_capability_rejects_pickle_and_state_restoration(
    runtime_graph: _RuntimeGraph,
) -> None:
    capability = runtime_graph.issue()

    with pytest.raises(TypeError, match="not_serializable"):
        pickle.dumps(capability)
    with pytest.raises(TypeError, match="not_serializable"):
        cast(Any, capability).__setstate__(("paper-secondary",))

    capability.assert_intact()
    assert capability.account_id == ACCOUNT_ID


async def test_capability_returns_only_canonical_current_outer_lease(
    runtime_graph: _RuntimeGraph,
) -> None:
    capability = runtime_graph.issue()
    with pytest.raises(SchedulerInvariantError, match="outer_lease_is_not_acquired"):
        capability.current_outer_lease()

    current = WorkerLease(
        account_id=ACCOUNT_ID,
        holder_id=HOLDER_ID,
        fencing_token=7,
        acquired_at=NOW,
        expires_at=NOW + timedelta(seconds=30),
    )
    runtime_graph.lease_manager.current = current

    returned = capability.current_outer_lease()

    assert returned == current
    assert returned is not current


@pytest.mark.parametrize(
    ("component", "reason"),
    (
        ("scheduler", "scheduler_type"),
        ("worker_api", "worker_api_type"),
        ("lease_manager", "lease_manager_type"),
        ("commands", "commands_type"),
        ("execution", "execution_type"),
        ("settlement", "settlement_type"),
        ("reconciliation", "reconciliation_type"),
        ("outbox", "outbox_type"),
    ),
)
async def test_factory_rejects_non_exact_runtime_components(
    runtime_graph: _RuntimeGraph,
    component: str,
    reason: str,
) -> None:
    setattr(runtime_graph, component, object())

    with pytest.raises(SchedulerInvariantError, match=reason):
        runtime_graph.issue()


_Mutation = Callable[[_RuntimeGraph], None]


def _replace_lease_port(graph: _RuntimeGraph) -> None:
    graph.lease_manager.port = cast(Any, object())


def _change_scheduler_release(graph: _RuntimeGraph) -> None:
    object.__setattr__(graph.scheduler, "_release_sha", "b" * 40)


def _change_scheduler_authority(graph: _RuntimeGraph) -> None:
    object.__setattr__(
        graph.scheduler,
        "_persistence_authority",
        "supabase-worker-api:" + "b" * 64,
    )


def _change_scheduler_base_url(graph: _RuntimeGraph) -> None:
    object.__setattr__(
        graph.scheduler,
        "_base_url",
        "https://other.supabase.co/rest/v1/rpc",
    )


def _change_commands_port(graph: _RuntimeGraph) -> None:
    graph.commands.port = cast(Any, object())


def _change_commands_account(graph: _RuntimeGraph) -> None:
    graph.commands.account_id = "paper-secondary"


def _change_commands_provider(graph: _RuntimeGraph) -> None:
    graph.commands.lease_provider = lambda: graph.lease_manager.current


def _change_execution_source_account(graph: _RuntimeGraph) -> None:
    object.__setattr__(graph.source, "_account_id", "paper-secondary")


def _change_execution_source_release(graph: _RuntimeGraph) -> None:
    object.__setattr__(graph.source, "_release_sha", "b" * 40)


def _change_execution_source_base_url(graph: _RuntimeGraph) -> None:
    object.__setattr__(
        graph.source,
        "_base_url",
        "https://other.supabase.co/rest/v1/rpc",
    )


def _change_execution_source_authority(graph: _RuntimeGraph) -> None:
    object.__setattr__(
        graph.source,
        "_persistence_authority",
        "supabase-worker-api:" + "b" * 64,
    )


def _change_scheduler_transport_ownership(graph: _RuntimeGraph) -> None:
    object.__setattr__(
        graph.scheduler,
        "_client",
        cast(Any, graph.worker_api)._client,
    )


def _change_worker_transport_ownership(graph: _RuntimeGraph) -> None:
    object.__setattr__(
        graph.worker_api,
        "_client",
        cast(Any, graph.source)._client,
    )


def _change_source_transport_ownership(graph: _RuntimeGraph) -> None:
    object.__setattr__(
        graph.source,
        "_client",
        cast(Any, graph.scheduler)._client,
    )


def _change_execution_port(graph: _RuntimeGraph) -> None:
    cast(Any, graph.execution.execution).durable_port = object()


def _change_execution_kernel(graph: _RuntimeGraph) -> None:
    cast(Any, graph.execution.execution).kernel = object()


def _change_execution_worker(graph: _RuntimeGraph) -> None:
    graph.execution.worker_id = "00000000-0000-4000-8000-000000000002"


def _change_settlement_environment(graph: _RuntimeGraph) -> None:
    graph.settlement.environment = "contract_test"


def _change_settlement_provider(graph: _RuntimeGraph) -> None:
    graph.settlement.lease_provider = lambda: graph.lease_manager.current


def _change_unknown_port(graph: _RuntimeGraph) -> None:
    cast(Any, graph.reconciliation.unknown).port = object()


def _change_unknown_provider(graph: _RuntimeGraph) -> None:
    cast(Any, graph.reconciliation.unknown).lease_provider = (
        lambda: graph.lease_manager.current
    )


def _change_generic_port(graph: _RuntimeGraph) -> None:
    cast(Any, graph.reconciliation.generic).port = object()


def _change_generic_provider(graph: _RuntimeGraph) -> None:
    cast(Any, graph.reconciliation.generic).lease_provider = (
        lambda: graph.lease_manager.current
    )


def _change_reconciliation_recovery(graph: _RuntimeGraph) -> None:
    generic = cast(ReconcileExecutionV2, graph.reconciliation.generic)
    cast(Any, generic.handler).recovery = object()


def _change_outbox_port(graph: _RuntimeGraph) -> None:
    graph.outbox.outbox = cast(Any, object())


def _change_outbox_destination(graph: _RuntimeGraph) -> None:
    graph.outbox.destination = cast(Any, object())


_BINDING_MUTATIONS: tuple[tuple[_Mutation, str], ...] = (
    (_replace_lease_port, "lease_manager_port"),
    (_change_scheduler_release, "release_identity"),
    (_change_scheduler_authority, "persistence_authority"),
    (_change_scheduler_base_url, "persistence_authority"),
    (_change_scheduler_transport_ownership, "transport_ownership"),
    (_change_worker_transport_ownership, "transport_ownership"),
    (_change_commands_port, "commands_binding"),
    (_change_commands_account, "commands_binding"),
    (_change_commands_provider, "commands_binding"),
    (_change_execution_source_account, "execution_source_binding"),
    (_change_execution_source_release, "execution_source_binding"),
    (_change_execution_source_base_url, "execution_source_binding"),
    (_change_execution_source_authority, "execution_source_binding"),
    (_change_source_transport_ownership, "execution_source_binding"),
    (_change_execution_port, "execution_runner_binding"),
    (_change_execution_kernel, "execution_runner_binding"),
    (_change_execution_worker, "execution_binding"),
    (_change_settlement_environment, "settlement_binding"),
    (_change_settlement_provider, "settlement_binding"),
    (_change_unknown_port, "reconciliation_unknown_binding"),
    (_change_unknown_provider, "reconciliation_unknown_binding"),
    (_change_generic_port, "reconciliation_generic_binding"),
    (_change_generic_provider, "reconciliation_generic_binding"),
    (_change_reconciliation_recovery, "reconciliation_handler_binding"),
    (_change_outbox_port, "outbox_binding"),
    (_change_outbox_destination, "outbox_destination_type"),
)


@pytest.mark.parametrize(("mutate", "reason"), _BINDING_MUTATIONS)
async def test_factory_rejects_cross_bound_or_unprovable_graphs(
    runtime_graph: _RuntimeGraph,
    mutate: _Mutation,
    reason: str,
) -> None:
    mutate(runtime_graph)

    with pytest.raises(SchedulerInvariantError, match=reason):
        runtime_graph.issue()


async def test_capability_detects_identity_and_graph_drift_after_issuance(
    runtime_graph: _RuntimeGraph,
) -> None:
    capability = runtime_graph.issue()
    runtime_graph.commands.port = cast(Any, object())

    with pytest.raises(SchedulerInvariantError, match="commands_binding"):
        capability.assert_intact()


async def test_capability_rejects_mismatched_current_lease_identity(
    runtime_graph: _RuntimeGraph,
) -> None:
    capability = runtime_graph.issue()
    runtime_graph.lease_manager.current = WorkerLease(
        account_id="paper-secondary",
        holder_id=HOLDER_ID,
        fencing_token=7,
        acquired_at=NOW,
        expires_at=NOW + timedelta(seconds=30),
    )

    with pytest.raises(SchedulerInvariantError, match="outer_lease_identity_mismatch"):
        capability.current_outer_lease()


def _runtime_graph() -> _RuntimeGraph:
    settings = _settings()
    scheduler = SupabaseDurableScheduler(
        settings,
        release_sha=RELEASE_SHA,
    )
    worker_api = SupabaseWorkerApi(
        settings,
        release_sha=RELEASE_SHA,
    )
    lease_manager = MaintainWorkerLease(
        worker_api,
        account_id=ACCOUNT_ID,
        holder_id=HOLDER_ID,
        clock=lambda: NOW,
    )
    lease_provider = lease_manager.current_lease
    commands = ApplyOperationCommands(
        worker_api,
        account_id=ACCOUNT_ID,
        holder_id=HOLDER_ID,
        current_release_sha=RELEASE_SHA,
        lease_provider=lease_provider,
        clock=lambda: NOW,
    )
    source = SupabasePaperExecutionCommandSource(
        settings,
        account_id=ACCOUNT_ID,
        release_sha=RELEASE_SHA,
    )
    execution = RunExecutionSupervisorV2(
        source,
        RunExecutionV2(durable_port=worker_api),
        RiskService(),
        worker_id=HOLDER_ID,
        current_release_sha=RELEASE_SHA,
        clock=lambda: NOW,
    )
    settlement = MatureCashSettlements(
        worker_api,
        account_id=ACCOUNT_ID,
        environment="paper",
        holder_id=HOLDER_ID,
        release_sha=RELEASE_SHA,
        lease_provider=lease_provider,
        clock=lambda: NOW,
    )
    reconciliation = RunExecutionReconciliationStageV2(
        ApplyUnknownExecutionResolutionsV2(
            worker_api,
            account_id=ACCOUNT_ID,
            environment="paper",
            holder_id=HOLDER_ID,
            release_sha=RELEASE_SHA,
            lease_provider=lease_provider,
            clock=lambda: NOW,
        ),
        ReconcileExecutionV2(
            worker_api,
            FailClosedExecutionReconciliationHandler(
                worker_api,
                worker_id=HOLDER_ID,
                current_release_sha=RELEASE_SHA,
            ),
            account_id=ACCOUNT_ID,
            worker_id=HOLDER_ID,
            current_release_sha=RELEASE_SHA,
            lease_provider=lease_provider,
            clock=lambda: NOW,
        ),
    )
    outbox = DispatchAlertOutbox(
        worker_api,
        UnavailableOutboxDestination(),
        worker_id=HOLDER_ID,
        clock=lambda: NOW,
    )
    return _RuntimeGraph(
        scheduler=scheduler,
        worker_api=worker_api,
        source=source,
        lease_manager=lease_manager,
        commands=commands,
        execution=execution,
        settlement=settlement,
        reconciliation=reconciliation,
        outbox=outbox,
    )


def _settings() -> Settings:
    return Settings(
        ENV="local",
        EXECUTION_V2_ENABLED=True,
        EXECUTION_V2_WORKER_API_ENABLED=True,
        EXECUTION_V2_ENVIRONMENT="paper",
        EXECUTION_V2_WORKER_ID=HOLDER_ID,
        EXECUTION_V2_ACCOUNT_ID=ACCOUNT_ID,
        MOCK_PROVIDERS=True,
        SUPABASE_URL="https://example.supabase.co",
        SUPABASE_SECRET_KEY=SecretStr("test-secret"),
    )
