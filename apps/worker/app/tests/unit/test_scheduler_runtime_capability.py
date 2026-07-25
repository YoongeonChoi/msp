from __future__ import annotations

import asyncio
import copy
import pickle
import ssl
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Never, cast

import httpx
import pytest
from pydantic import SecretStr

from app.adapters.alerts.outbox_webhook_destination import (
    OutboxWebhookDestination,
    UnavailableOutboxDestination,
)
from app.adapters.persistence.supabase_durable_scheduler import (
    SupabaseDurableScheduler,
)
from app.adapters.persistence.supabase_paper_execution_source import (
    SupabasePaperExecutionCommandSource,
)
from app.adapters.persistence.supabase_worker_api import SupabaseWorkerApi
from app.application.services import scheduler_invocation_deadline as deadline_module
from app.application.services.risk_service import RiskService
from app.application.services.scheduler_invocation_deadline import (
    SchedulerInvocationBinding,
    SchedulerInvocationEffectAuthorization,
    SchedulerInvocationPermit,
    SchedulerInvocationPermitRevoked,
    _claim_scheduler_invocation_with_clock,
    issue_scheduler_invocation_effect_authorization,
    require_scheduler_invocation_effect_authorization,
    run_with_scheduler_deadline,
)
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
from app.application.use_cases.run_durable_scheduler import (
    SCHEDULER_RESULT_VALIDATORS,
    SchedulerJobBinding,
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
from app.domain.scheduler.models import (
    SCHEDULER_EFFECTFUL_JOB_KEYS,
    SCHEDULER_JOB_KEYS,
    ScheduledJobClaimReceiptV1,
    ScheduledJobClaimV1,
    ScheduledJobDefinitionV1,
    ScheduledJobLeaseV1,
    ScheduledJobRunV1,
    SchedulerInvariantError,
    SchedulerJobKey,
)
from app.infrastructure.authenticated_webhook import ReceiverAckKeyRing
from app.infrastructure.scheduler_runtime_capability_factory import (
    create_scheduler_runtime_capability,
    create_supabase_scheduler_job_bindings,
    require_supabase_scheduler_runtime_capability,
)

ACCOUNT_ID = "paper-primary"
HOLDER_ID = "00000000-0000-4000-8000-000000000001"
RELEASE_SHA = "a" * 40
NOW = datetime(2026, 7, 25, 9, 0, tzinfo=UTC)
_SCHEDULER_HANDLER_ORDER: tuple[SchedulerJobKey, ...] = (
    "operations.commands",
    "operations.execution",
    "operations.settlement",
    "operations.reconciliation",
    "operations.outbox",
)
_Clock = deadline_module._SchedulerTestMonotonicClock
_RUN_IDS: dict[SchedulerJobKey, str] = {
    "operations.commands": "11111111-1111-4111-8111-111111111111",
    "operations.execution": "22222222-2222-4222-8222-222222222222",
    "operations.settlement": "33333333-3333-4333-8333-333333333333",
    "operations.reconciliation": "44444444-4444-4444-8444-444444444444",
    "operations.outbox": "55555555-5555-4555-8555-555555555555",
}
_LEASE_TOKENS: dict[SchedulerJobKey, str] = {
    "operations.commands": "61111111-1111-4111-8111-111111111111",
    "operations.execution": "62222222-2222-4222-8222-222222222222",
    "operations.settlement": "63333333-3333-4333-8333-333333333333",
    "operations.reconciliation": "64444444-4444-4444-8444-444444444444",
    "operations.outbox": "65555555-5555-4555-8555-555555555555",
}


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


def _scheduler_definition(job_key: SchedulerJobKey) -> ScheduledJobDefinitionV1:
    effectful = job_key in SCHEDULER_EFFECTFUL_JOB_KEYS
    return ScheduledJobDefinitionV1(
        job_key=job_key,
        interval_seconds=2,
        lease_ttl_seconds=30,
        max_attempts=1 if effectful else 3,
        retry_base_seconds=2,
        retry_max_seconds=8,
        max_manual_replays=0 if effectful else 1,
    )


def _scheduler_claim(job_key: SchedulerJobKey) -> ScheduledJobClaimV1:
    definition = _scheduler_definition(job_key)
    run = ScheduledJobRunV1(
        run_id=_RUN_IDS[job_key],
        account_id=ACCOUNT_ID,
        job_key=job_key,
        definition_sha256=definition.definition_sha256,
        state="leased",
        revision=2,
        attempt_count=1,
        replay_generation=0,
        replay_of_run_id=None,
        scheduled_for=NOW - timedelta(seconds=2),
        available_at=NOW - timedelta(seconds=2),
        created_at=NOW - timedelta(seconds=2),
        updated_at=NOW,
    )
    return ScheduledJobClaimV1(
        definition=definition,
        run=run,
        lease=ScheduledJobLeaseV1(
            lease_token=_LEASE_TOKENS[job_key],
            run_id=run.run_id,
            account_id=ACCOUNT_ID,
            holder_id=HOLDER_ID,
            release_sha=RELEASE_SHA,
            outer_fencing_token=7,
            attempt_number=1,
            run_revision=run.revision,
            leased_at=NOW,
            lease_expires_at=NOW + timedelta(seconds=30),
        ),
        observed_at=NOW,
    )


def _scheduler_outer_lease() -> WorkerLease:
    return WorkerLease(
        account_id=ACCOUNT_ID,
        holder_id=HOLDER_ID,
        fencing_token=7,
        acquired_at=NOW - timedelta(seconds=1),
        expires_at=NOW + timedelta(seconds=40),
    )


async def _wait_for_scheduler_deadline(_cutoff: float) -> None:
    await asyncio.Event().wait()


def _must_not_fail_stop(reason: str) -> Never:
    raise AssertionError(f"unexpected fail-stop: {reason}")


async def _capture_runtime_permit(
    runtime_graph: _RuntimeGraph,
    capability: SchedulerRuntimeCapability,
    monkeypatch: pytest.MonkeyPatch,
    job_key: SchedulerJobKey,
    *,
    effect_issuer: object,
) -> tuple[
    SchedulerInvocationPermit,
    SchedulerInvocationBinding,
    asyncio.Task[None],
]:
    outer_lease = _scheduler_outer_lease()
    runtime_graph.lease_manager.current = outer_lease
    receipt = ScheduledJobClaimReceiptV1(
        claim=_scheduler_claim(job_key),
        observed_at=NOW,
    )

    async def claim_due_job(
        _scheduler: SupabaseDurableScheduler,
        *,
        outer_lease: WorkerLease,
    ) -> ScheduledJobClaimReceiptV1:
        assert outer_lease == _scheduler_outer_lease()
        return receipt

    monkeypatch.setattr(SupabaseDurableScheduler, "claim_due_job", claim_due_job)
    invocation = await _claim_scheduler_invocation_with_clock(
        capability,
        monotonic_clock=_Clock(100.0),
    )
    assert invocation is not None
    captured: list[SchedulerInvocationPermit] = []
    entered = asyncio.Event()

    async def hold_permit(permit: SchedulerInvocationPermit) -> None:
        captured.append(permit)
        entered.set()
        await asyncio.Event().wait()

    run_task = asyncio.create_task(
        run_with_scheduler_deadline(
            hold_permit,
            invocation=invocation,
            wait_until=_wait_for_scheduler_deadline,
            fail_stop=_must_not_fail_stop,
            effect_issuer=effect_issuer,
            effect_runtime=capability,
        )
    )
    await entered.wait()
    return captured[0], invocation.binding, run_task


async def _cancel_runtime_permit(task: asyncio.Task[None]) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


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
    with pytest.raises(SchedulerInvariantError, match="capability_type"):
        create_supabase_scheduler_job_bindings(StructuralFake())

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


async def test_scheduler_binding_factory_is_fixed_and_uses_exact_validators(
    runtime_graph: _RuntimeGraph,
) -> None:
    bindings = create_supabase_scheduler_job_bindings(runtime_graph.issue())

    assert tuple(bindings) == _SCHEDULER_HANDLER_ORDER
    assert frozenset(bindings) == SCHEDULER_JOB_KEYS
    for job_key in _SCHEDULER_HANDLER_ORDER:
        binding = bindings[job_key]
        assert type(binding) is SchedulerJobBinding
        assert binding.job_key == job_key
        assert binding.result_validator is SCHEDULER_RESULT_VALIDATORS[job_key]
    with pytest.raises(TypeError):
        cast(Any, bindings)["operations.commands"] = bindings["operations.outbox"]


async def test_scheduler_binding_handlers_are_immutable_and_non_serializable(
    runtime_graph: _RuntimeGraph,
) -> None:
    handler = create_supabase_scheduler_job_bindings(runtime_graph.issue())[
        "operations.commands"
    ].handler

    assert not hasattr(handler, "__dict__")
    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_runtime_handler_is_immutable",
    ):
        cast(Any, handler)._job_key = "operations.outbox"
    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_runtime_handler_is_immutable",
    ):
        del cast(Any, handler)._runtime
    with pytest.raises(TypeError, match="scheduler_runtime_handler_is_not_serializable"):
        copy.copy(handler)
    with pytest.raises(TypeError, match="scheduler_runtime_handler_is_not_serializable"):
        pickle.dumps(handler)


@pytest.mark.parametrize(
    ("job_key", "stage_attribute", "scheduled_method"),
    (
        ("operations.commands", "commands", "run_scheduled"),
        ("operations.execution", "execution", "run_scheduled"),
        ("operations.settlement", "settlement", "run_scheduled"),
        ("operations.reconciliation", "reconciliation", "run_scheduled"),
        ("operations.outbox", "outbox", "dispatch_scheduled"),
    ),
)
async def test_scheduler_binding_dispatches_only_its_fixed_scheduled_stage(
    runtime_graph: _RuntimeGraph,
    monkeypatch: pytest.MonkeyPatch,
    job_key: SchedulerJobKey,
    stage_attribute: str,
    scheduled_method: str,
) -> None:
    capability = runtime_graph.issue()
    bindings = create_supabase_scheduler_job_bindings(capability)
    permit, invocation_binding, run_task = await _capture_runtime_permit(
        runtime_graph,
        capability,
        monkeypatch,
        job_key,
        effect_issuer=bindings[job_key].handler,
    )
    stage = getattr(runtime_graph, stage_attribute)
    marker = object()
    received: list[tuple[object, SchedulerInvocationEffectAuthorization]] = []

    async def scheduled_dispatch(
        invoked_stage: object,
        authorization: SchedulerInvocationEffectAuthorization,
    ) -> object:
        assert (
            require_scheduler_invocation_effect_authorization(
                authorization,
                expected_job_key=job_key,
            )
            is authorization
        )
        received.append((invoked_stage, authorization))
        return marker

    monkeypatch.setattr(type(stage), scheduled_method, scheduled_dispatch)
    try:
        result = await bindings[job_key].handler(permit, invocation_binding)
        assert result is marker
        assert len(received) == 1
        assert received[0][0] is stage
    finally:
        await _cancel_runtime_permit(run_task)


async def test_effect_runtime_rejects_structural_fake_with_the_exact_issuer(
    runtime_graph: _RuntimeGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capability = runtime_graph.issue()
    binding = create_supabase_scheduler_job_bindings(capability)[
        "operations.commands"
    ]
    permit, invocation_binding, run_task = await _capture_runtime_permit(
        runtime_graph,
        capability,
        monkeypatch,
        "operations.commands",
        effect_issuer=binding.handler,
    )

    class StructuralFake:
        scheduler_port = capability.scheduler_port
        account_id = capability.account_id
        holder_id = capability.holder_id
        release_sha = capability.release_sha
        persistence_authority = capability.persistence_authority

        def assert_intact(self) -> None:
            return None

        def current_outer_lease(self) -> WorkerLease:
            return _scheduler_outer_lease()

    try:
        with pytest.raises(SchedulerInvocationPermitRevoked) as revoked:
            issue_scheduler_invocation_effect_authorization(
                cast(Any, StructuralFake()),
                permit,
                invocation_binding,
                expected_job_key="operations.commands",
                effect_issuer=binding.handler,
            )
        assert revoked.value.reason == "binding_mismatch"
    finally:
        await _cancel_runtime_permit(run_task)


async def test_effect_runtime_rejects_cross_graph_handler_with_same_metadata(
    runtime_graph: _RuntimeGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    second_graph = _runtime_graph()
    first_capability = runtime_graph.issue()
    second_capability = second_graph.issue()
    second_binding = create_supabase_scheduler_job_bindings(second_capability)[
        "operations.commands"
    ]
    permit, invocation_binding, run_task = await _capture_runtime_permit(
        runtime_graph,
        first_capability,
        monkeypatch,
        "operations.commands",
        effect_issuer=second_binding.handler,
    )
    try:
        with pytest.raises(SchedulerInvocationPermitRevoked) as revoked:
            await second_binding.handler(permit, invocation_binding)
        assert revoked.value.reason == "binding_mismatch"
    finally:
        await _cancel_runtime_permit(run_task)
        await second_graph.source.close()
        await second_graph.scheduler.close()
        await second_graph.worker_api.close()


_RuntimeAuthorizationMutation = Callable[
    [SchedulerRuntimeCapability, _RuntimeGraph],
    None,
]


def _replace_authorized_scheduler_port(
    capability: SchedulerRuntimeCapability,
    _runtime_graph: _RuntimeGraph,
) -> None:
    object.__setattr__(cast(Any, capability), "_scheduler", object())


def _change_authorized_persistence_authority(
    capability: SchedulerRuntimeCapability,
    _runtime_graph: _RuntimeGraph,
) -> None:
    object.__setattr__(
        cast(Any, capability),
        "_persistence_authority",
        "supabase-worker-api:" + "b" * 64,
    )


def _change_authorized_outer_fence(
    _capability: SchedulerRuntimeCapability,
    runtime_graph: _RuntimeGraph,
) -> None:
    current = runtime_graph.lease_manager.current
    assert current is not None
    runtime_graph.lease_manager.current = replace(current, fencing_token=8)


def _change_authorized_worker_client_headers(
    _capability: SchedulerRuntimeCapability,
    runtime_graph: _RuntimeGraph,
) -> None:
    runtime_graph.worker_api._client.headers["Authorization"] = "Bearer unexpected"


@pytest.mark.parametrize(
    "mutate_runtime",
    (
        _replace_authorized_scheduler_port,
        _change_authorized_persistence_authority,
        _change_authorized_outer_fence,
        _change_authorized_worker_client_headers,
    ),
)
async def test_scheduler_handler_blocks_runtime_drift_before_stage_effect(
    runtime_graph: _RuntimeGraph,
    monkeypatch: pytest.MonkeyPatch,
    mutate_runtime: _RuntimeAuthorizationMutation,
) -> None:
    capability = runtime_graph.issue()
    bindings = create_supabase_scheduler_job_bindings(capability)
    permit, invocation_binding, run_task = await _capture_runtime_permit(
        runtime_graph,
        capability,
        monkeypatch,
        "operations.commands",
        effect_issuer=bindings["operations.commands"].handler,
    )
    effect_reached = False

    async def scheduled_dispatch(
        _stage: ApplyOperationCommands,
        authorization: SchedulerInvocationEffectAuthorization,
    ) -> object:
        nonlocal effect_reached
        mutate_runtime(capability, runtime_graph)
        require_scheduler_invocation_effect_authorization(
            authorization,
            expected_job_key="operations.commands",
        )
        effect_reached = True
        return object()

    monkeypatch.setattr(ApplyOperationCommands, "run_scheduled", scheduled_dispatch)
    try:
        with pytest.raises(SchedulerInvocationPermitRevoked) as revoked:
            await bindings["operations.commands"].handler(
                permit,
                invocation_binding,
            )
        assert revoked.value.reason == "binding_mismatch"
        assert not effect_reached
    finally:
        await _cancel_runtime_permit(run_task)


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


async def test_capability_rejects_same_type_outbox_destination_replacement(
    runtime_graph: _RuntimeGraph,
) -> None:
    capability = runtime_graph.issue()
    runtime_graph.outbox.destination = UnavailableOutboxDestination()

    with pytest.raises(
        SchedulerInvariantError,
        match="outbox_destination_identity",
    ):
        capability.assert_intact()


async def test_capability_rejects_outbox_target_drift_after_issuance(
    runtime_graph: _RuntimeGraph,
) -> None:
    destination = OutboxWebhookDestination(
        "https://receiver.example.com/events",
        key_ring=ReceiverAckKeyRing("current", b"k" * 32),
    )
    runtime_graph.outbox.destination = destination
    try:
        capability = runtime_graph.issue()
        target = cast(Any, destination)._transport._target
        object.__setattr__(target, "url", "https://other.example.com/events")

        with pytest.raises(
            SchedulerInvariantError,
            match="outbox_destination_pin",
        ):
            capability.assert_intact()
    finally:
        await destination.close()


async def test_capability_rejects_outbox_effective_transport_replacement(
    runtime_graph: _RuntimeGraph,
) -> None:
    destination = OutboxWebhookDestination(
        "https://receiver.example.com/events",
        key_ring=ReceiverAckKeyRing("current", b"k" * 32),
    )
    runtime_graph.outbox.destination = destination
    replacement = httpx.AsyncHTTPTransport()
    client = cast(Any, destination)._transport._client
    original = client._transport
    try:
        capability = runtime_graph.issue()
        client._transport = replacement

        with pytest.raises(
                SchedulerInvariantError,
                match="scheduler_http_client_route",
        ):
            capability.assert_intact()
    finally:
        client._transport = original
        await replacement.aclose()
        await destination.close()


async def test_capability_rejects_instance_shadowed_outbox_send(
    runtime_graph: _RuntimeGraph,
) -> None:
    destination = OutboxWebhookDestination(
        "https://receiver.example.com/events",
        key_ring=ReceiverAckKeyRing("current", b"k" * 32),
    )
    runtime_graph.outbox.destination = destination
    client = cast(Any, destination)._transport._client

    async def replacement_send(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("replacement send must not run")

    try:
        capability = runtime_graph.issue()
        client.send = replacement_send

        with pytest.raises(
                SchedulerInvariantError,
                match="scheduler_http_client_route",
        ):
            capability.assert_intact()
    finally:
        del client.send
        await destination.close()


async def test_capability_rejects_outbox_receiver_key_drift(
    runtime_graph: _RuntimeGraph,
) -> None:
    key_ring = ReceiverAckKeyRing("current", b"k" * 32)
    destination = OutboxWebhookDestination(
        "https://receiver.example.com/events",
        key_ring=key_ring,
    )
    runtime_graph.outbox.destination = destination
    try:
        capability = runtime_graph.issue()
        object.__setattr__(key_ring, "_current_key", b"z" * 32)

        with pytest.raises(
            SchedulerInvariantError,
            match="outbox_key_ring_state",
        ):
            capability.assert_intact()
    finally:
        await destination.close()


@pytest.mark.parametrize(
    "client_name",
    ("scheduler", "worker_api", "source"),
)
async def test_capability_rejects_supabase_client_request_hook_drift(
    runtime_graph: _RuntimeGraph,
    client_name: str,
) -> None:
    capability = runtime_graph.issue()
    client = cast(Any, getattr(runtime_graph, client_name))._client
    request_hooks = client._event_hooks["request"]

    async def request_hook(_request: httpx.Request) -> None:
        raise AssertionError("mutated request hook must not run")

    request_hooks.append(request_hook)

    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_http_client_route",
    ):
        capability.assert_intact()


@pytest.mark.parametrize(
    "adapter_name",
    ("scheduler", "worker_api", "source"),
)
async def test_capability_rejects_supabase_request_header_drift(
    runtime_graph: _RuntimeGraph,
    adapter_name: str,
) -> None:
    capability = runtime_graph.issue()
    adapter = cast(Any, getattr(runtime_graph, adapter_name))
    adapter._headers["content-profile"] = "public"

    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_http_headers",
    ):
        capability.assert_intact()


async def test_capability_rejects_outbox_pool_dispatch_shadow(
    runtime_graph: _RuntimeGraph,
) -> None:
    destination = OutboxWebhookDestination(
        "https://receiver.example.com/events",
        key_ring=ReceiverAckKeyRing("current", b"k" * 32),
    )
    runtime_graph.outbox.destination = destination
    pool = cast(Any, destination)._transport._client._transport._pool

    async def replacement_dispatch(_request: object) -> object:
        raise AssertionError("replacement pool dispatch must not run")

    try:
        capability = runtime_graph.issue()
        pool.handle_async_request = replacement_dispatch

        with pytest.raises(
            SchedulerInvariantError,
            match="scheduler_http_client_route",
        ):
            capability.assert_intact()
    finally:
        del pool.handle_async_request
        await destination.close()


async def test_capability_rejects_outbox_tls_verification_drift(
    runtime_graph: _RuntimeGraph,
) -> None:
    destination = OutboxWebhookDestination(
        "https://receiver.example.com/events",
        key_ring=ReceiverAckKeyRing("current", b"k" * 32),
    )
    runtime_graph.outbox.destination = destination
    ssl_context = cast(Any, destination)._transport._client._transport._pool._ssl_context
    try:
        capability = runtime_graph.issue()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        with pytest.raises(
            SchedulerInvariantError,
            match="scheduler_http_client_tls",
        ):
            capability.assert_intact()
    finally:
        ssl_context.verify_mode = ssl.CERT_REQUIRED
        ssl_context.check_hostname = True
        await destination.close()


async def test_capability_rejects_outbox_client_header_drift(
    runtime_graph: _RuntimeGraph,
) -> None:
    destination = OutboxWebhookDestination(
        "https://receiver.example.com/events",
        key_ring=ReceiverAckKeyRing("current", b"k" * 32),
    )
    runtime_graph.outbox.destination = destination
    client = cast(Any, destination)._transport._client
    try:
        capability = runtime_graph.issue()
        client.headers["Authorization"] = "Bearer unexpected"

        with pytest.raises(
            SchedulerInvariantError,
            match="scheduler_http_client_route",
        ):
            capability.assert_intact()
    finally:
        client.headers.pop("Authorization", None)
        await destination.close()


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
