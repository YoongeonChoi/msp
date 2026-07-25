from __future__ import annotations

import re
from collections.abc import Callable
from typing import Never, SupportsIndex
from uuid import UUID

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
from app.application.ports.durable_scheduler_port import (
    DurableSchedulerPort,
    canonical_scheduler_outer_lease,
)
from app.application.ports.persistence_authority import (
    PersistenceAuthority,
    is_persistence_authority,
)
from app.application.services.paper_execution_v2 import (
    DeterministicPaperExecutionSimulator,
)
from app.application.services.risk_service import RiskService
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
from app.domain.execution_v2.models import WorkerLease
from app.domain.scheduler.models import SchedulerInvariantError

_ACCOUNT_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
_RELEASE_SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


class _SupabaseSchedulerRuntimeCapability:
    """Non-serializable concrete attestation for one exact Supabase graph."""

    __slots__ = (
        "_scheduler",
        "_worker_api",
        "_lease_manager",
        "_commands",
        "_execution",
        "_settlement",
        "_reconciliation",
        "_outbox",
        "_account_id",
        "_holder_id",
        "_release_sha",
        "_persistence_authority",
    )
    _scheduler: SupabaseDurableScheduler
    _worker_api: SupabaseWorkerApi
    _lease_manager: MaintainWorkerLease
    _commands: ApplyOperationCommands
    _execution: RunExecutionSupervisorV2
    _settlement: MatureCashSettlements
    _reconciliation: RunExecutionReconciliationStageV2
    _outbox: DispatchAlertOutbox
    _account_id: str
    _holder_id: str
    _release_sha: str
    _persistence_authority: PersistenceAuthority

    def __init__(
        self,
        *,
        scheduler: SupabaseDurableScheduler,
        worker_api: SupabaseWorkerApi,
        lease_manager: MaintainWorkerLease,
        commands: ApplyOperationCommands,
        execution: RunExecutionSupervisorV2,
        settlement: MatureCashSettlements,
        reconciliation: RunExecutionReconciliationStageV2,
        outbox: DispatchAlertOutbox,
    ) -> None:
        if type(scheduler) is not SupabaseDurableScheduler:
            _invalid("scheduler_type")
        if type(worker_api) is not SupabaseWorkerApi:
            _invalid("worker_api_type")
        if type(lease_manager) is not MaintainWorkerLease:
            _invalid("lease_manager_type")
        account_id = lease_manager.account_id
        holder_id = lease_manager.holder_id
        release_sha = worker_api.release_sha
        persistence_authority = worker_api.persistence_authority
        _require_runtime_identity(
            account_id=account_id,
            holder_id=holder_id,
            release_sha=release_sha,
            persistence_authority=persistence_authority,
        )
        object.__setattr__(self, "_scheduler", scheduler)
        object.__setattr__(self, "_worker_api", worker_api)
        object.__setattr__(self, "_lease_manager", lease_manager)
        object.__setattr__(self, "_commands", commands)
        object.__setattr__(self, "_execution", execution)
        object.__setattr__(self, "_settlement", settlement)
        object.__setattr__(self, "_reconciliation", reconciliation)
        object.__setattr__(self, "_outbox", outbox)
        object.__setattr__(self, "_account_id", account_id)
        object.__setattr__(self, "_holder_id", holder_id)
        object.__setattr__(self, "_release_sha", release_sha)
        object.__setattr__(self, "_persistence_authority", persistence_authority)
        self.assert_intact()

    def __setattr__(self, _name: str, _value: object) -> Never:
        raise SchedulerInvariantError("scheduler_runtime_capability_is_immutable")

    def __delattr__(self, _name: str) -> Never:
        raise SchedulerInvariantError("scheduler_runtime_capability_is_immutable")

    def __reduce__(self) -> Never:
        raise TypeError("scheduler_runtime_capability_is_not_serializable")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> Never:
        raise TypeError("scheduler_runtime_capability_is_not_serializable")

    def __getstate__(self) -> Never:
        raise TypeError("scheduler_runtime_capability_is_not_serializable")

    def __setstate__(self, _state: object) -> Never:
        raise TypeError("scheduler_runtime_capability_is_not_serializable")

    @property
    def scheduler_port(self) -> DurableSchedulerPort:
        return self._scheduler

    @property
    def account_id(self) -> str:
        return self._account_id

    @property
    def holder_id(self) -> str:
        return self._holder_id

    @property
    def release_sha(self) -> str:
        return self._release_sha

    @property
    def persistence_authority(self) -> PersistenceAuthority:
        return self._persistence_authority

    def assert_intact(self) -> None:
        _require_runtime_identity(
            account_id=self._account_id,
            holder_id=self._holder_id,
            release_sha=self._release_sha,
            persistence_authority=self._persistence_authority,
        )
        _assert_scheduler_runtime_graph(
            scheduler=self._scheduler,
            worker_api=self._worker_api,
            lease_manager=self._lease_manager,
            commands=self._commands,
            execution=self._execution,
            settlement=self._settlement,
            reconciliation=self._reconciliation,
            outbox=self._outbox,
            expected_account_id=self._account_id,
            expected_holder_id=self._holder_id,
            expected_release_sha=self._release_sha,
            expected_persistence_authority=self._persistence_authority,
        )

    def current_outer_lease(self) -> WorkerLease:
        self.assert_intact()
        current = self._lease_manager.current_lease()
        if current is None:
            raise SchedulerInvariantError("scheduler_runtime_outer_lease_is_not_acquired")
        lease = canonical_scheduler_outer_lease(current)
        if lease.account_id != self._account_id or lease.holder_id != self._holder_id:
            raise SchedulerInvariantError("scheduler_runtime_outer_lease_identity_mismatch")
        return lease


def create_scheduler_runtime_capability(
    *,
    scheduler: SupabaseDurableScheduler,
    worker_api: SupabaseWorkerApi,
    lease_manager: MaintainWorkerLease,
    commands: ApplyOperationCommands,
    execution: RunExecutionSupervisorV2,
    settlement: MatureCashSettlements,
    reconciliation: RunExecutionReconciliationStageV2,
    outbox: DispatchAlertOutbox,
) -> SchedulerRuntimeCapability:
    """Attest one exact production scheduler graph and return a sealed proof."""

    return _SupabaseSchedulerRuntimeCapability(
        scheduler=scheduler,
        worker_api=worker_api,
        lease_manager=lease_manager,
        commands=commands,
        execution=execution,
        settlement=settlement,
        reconciliation=reconciliation,
        outbox=outbox,
    )


def require_supabase_scheduler_runtime_capability(
    value: object,
) -> SchedulerRuntimeCapability:
    """Reject structural fakes before a runtime consumer receives authority."""

    if type(value) is not _SupabaseSchedulerRuntimeCapability:
        _invalid("capability_type")
    capability = value
    capability.assert_intact()
    return capability


def _assert_scheduler_runtime_graph(
    *,
    scheduler: SupabaseDurableScheduler,
    worker_api: SupabaseWorkerApi,
    lease_manager: MaintainWorkerLease,
    commands: ApplyOperationCommands,
    execution: RunExecutionSupervisorV2,
    settlement: MatureCashSettlements,
    reconciliation: RunExecutionReconciliationStageV2,
    outbox: DispatchAlertOutbox,
    expected_account_id: str,
    expected_holder_id: str,
    expected_release_sha: str,
    expected_persistence_authority: PersistenceAuthority,
) -> None:
    if type(scheduler) is not SupabaseDurableScheduler:
        _invalid("scheduler_type")
    if type(worker_api) is not SupabaseWorkerApi:
        _invalid("worker_api_type")
    if type(lease_manager) is not MaintainWorkerLease:
        _invalid("lease_manager_type")
    if lease_manager.port is not worker_api:
        _invalid("lease_manager_port")
    if (
        lease_manager.account_id != expected_account_id
        or lease_manager.holder_id != expected_holder_id
    ):
        _invalid("lease_manager_identity")
    if (
        worker_api.release_sha != expected_release_sha
        or worker_api.current_release_sha != expected_release_sha
        or scheduler.release_sha != expected_release_sha
    ):
        _invalid("release_identity")
    if (
        worker_api.persistence_authority != expected_persistence_authority
        or scheduler.persistence_authority != expected_persistence_authority
        or worker_api.base_url != scheduler.base_url
    ):
        _invalid("persistence_authority")
    if not worker_api.transport_is_managed or not scheduler.transport_is_managed:
        _invalid("transport_ownership")

    _assert_commands(
        commands,
        worker_api=worker_api,
        lease_manager=lease_manager,
        account_id=expected_account_id,
        holder_id=expected_holder_id,
        release_sha=expected_release_sha,
    )
    _assert_execution(
        execution,
        worker_api=worker_api,
        account_id=expected_account_id,
        holder_id=expected_holder_id,
        release_sha=expected_release_sha,
        persistence_authority=expected_persistence_authority,
    )
    _assert_settlement(
        settlement,
        worker_api=worker_api,
        lease_manager=lease_manager,
        account_id=expected_account_id,
        holder_id=expected_holder_id,
        release_sha=expected_release_sha,
    )
    _assert_reconciliation(
        reconciliation,
        worker_api=worker_api,
        lease_manager=lease_manager,
        account_id=expected_account_id,
        holder_id=expected_holder_id,
        release_sha=expected_release_sha,
    )
    _assert_outbox(outbox, worker_api=worker_api, holder_id=expected_holder_id)


def _assert_commands(
    stage: ApplyOperationCommands,
    *,
    worker_api: SupabaseWorkerApi,
    lease_manager: MaintainWorkerLease,
    account_id: str,
    holder_id: str,
    release_sha: str,
) -> None:
    if type(stage) is not ApplyOperationCommands:
        _invalid("commands_type")
    if (
        stage.port is not worker_api
        or stage.account_id != account_id
        or stage.holder_id != holder_id
        or stage.current_release_sha != release_sha
        or not _is_manager_lease_provider(stage.lease_provider, lease_manager)
    ):
        _invalid("commands_binding")


def _assert_execution(
    stage: RunExecutionSupervisorV2,
    *,
    worker_api: SupabaseWorkerApi,
    account_id: str,
    holder_id: str,
    release_sha: str,
    persistence_authority: PersistenceAuthority,
) -> None:
    if type(stage) is not RunExecutionSupervisorV2:
        _invalid("execution_type")
    if type(stage.source) is not SupabasePaperExecutionCommandSource:
        _invalid("execution_source_type")
    source = stage.source
    if (
        source.account_id != account_id
        or source.release_sha != release_sha
        or source.persistence_authority != persistence_authority
        or source.base_url != worker_api.base_url
        or not source.transport_is_managed
    ):
        _invalid("execution_source_binding")
    if type(stage.execution) is not RunExecutionV2:
        _invalid("execution_runner_type")
    runner = stage.execution
    if (
        runner.kernel is not None
        or runner.durable_port is not worker_api
        or type(runner.simulator) is not DeterministicPaperExecutionSimulator
    ):
        _invalid("execution_runner_binding")
    if (
        type(stage.risk_service) is not RiskService
        or stage.worker_id != holder_id
        or stage.current_release_sha != release_sha
    ):
        _invalid("execution_binding")


def _assert_settlement(
    stage: MatureCashSettlements,
    *,
    worker_api: SupabaseWorkerApi,
    lease_manager: MaintainWorkerLease,
    account_id: str,
    holder_id: str,
    release_sha: str,
) -> None:
    if type(stage) is not MatureCashSettlements:
        _invalid("settlement_type")
    if (
        stage.port is not worker_api
        or stage.account_id != account_id
        or stage.environment != "paper"
        or stage.holder_id != holder_id
        or stage.release_sha != release_sha
        or not _is_manager_lease_provider(stage.lease_provider, lease_manager)
    ):
        _invalid("settlement_binding")


def _assert_reconciliation(
    stage: RunExecutionReconciliationStageV2,
    *,
    worker_api: SupabaseWorkerApi,
    lease_manager: MaintainWorkerLease,
    account_id: str,
    holder_id: str,
    release_sha: str,
) -> None:
    if type(stage) is not RunExecutionReconciliationStageV2:
        _invalid("reconciliation_type")
    if type(stage.unknown) is not ApplyUnknownExecutionResolutionsV2:
        _invalid("reconciliation_unknown_type")
    unknown = stage.unknown
    if (
        unknown.port is not worker_api
        or unknown.account_id != account_id
        or unknown.environment != "paper"
        or unknown.holder_id != holder_id
        or unknown.release_sha != release_sha
        or not _is_manager_lease_provider(unknown.lease_provider, lease_manager)
    ):
        _invalid("reconciliation_unknown_binding")
    if type(stage.generic) is not ReconcileExecutionV2:
        _invalid("reconciliation_generic_type")
    generic = stage.generic
    if (
        generic.port is not worker_api
        or generic.account_id != account_id
        or generic.worker_id != holder_id
        or generic.current_release_sha != release_sha
        or not _is_manager_lease_provider(generic.lease_provider, lease_manager)
    ):
        _invalid("reconciliation_generic_binding")
    if type(generic.handler) is not FailClosedExecutionReconciliationHandler:
        _invalid("reconciliation_handler_type")
    handler = generic.handler
    if (
        handler.recovery is not worker_api
        or handler.worker_id != holder_id
        or handler.current_release_sha != release_sha
    ):
        _invalid("reconciliation_handler_binding")


def _assert_outbox(
    stage: DispatchAlertOutbox,
    *,
    worker_api: SupabaseWorkerApi,
    holder_id: str,
) -> None:
    if type(stage) is not DispatchAlertOutbox:
        _invalid("outbox_type")
    if stage.outbox is not worker_api or stage.worker_id != holder_id:
        _invalid("outbox_binding")
    if type(stage.destination) not in {
        OutboxWebhookDestination,
        UnavailableOutboxDestination,
    }:
        _invalid("outbox_destination_type")


def _is_manager_lease_provider(
    provider: Callable[[], object],
    lease_manager: MaintainWorkerLease,
) -> bool:
    return (
        getattr(provider, "__self__", None) is lease_manager
        and getattr(provider, "__func__", None) is MaintainWorkerLease.current_lease
    )


def _require_runtime_identity(
    *,
    account_id: object,
    holder_id: object,
    release_sha: object,
    persistence_authority: object,
) -> None:
    try:
        holder_uuid = UUID(holder_id) if type(holder_id) is str else None
    except ValueError:
        holder_uuid = None
    if type(account_id) is not str or _ACCOUNT_RE.fullmatch(account_id) is None:
        _invalid("account_id")
    if holder_uuid is None or str(holder_uuid) != holder_id:
        _invalid("holder_id")
    if type(release_sha) is not str or _RELEASE_SHA_RE.fullmatch(release_sha) is None:
        _invalid("release_sha")
    if not is_persistence_authority(persistence_authority):
        _invalid("authority")


def _invalid(component: str) -> Never:
    raise SchedulerInvariantError(f"scheduler_runtime_{component}_is_invalid")
