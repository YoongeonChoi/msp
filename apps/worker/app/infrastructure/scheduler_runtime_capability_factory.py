from __future__ import annotations

import hashlib
import hmac
import re
import ssl
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Never, SupportsIndex
from uuid import UUID

import httpx

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
from app.application.services.scheduler_invocation_deadline import (
    SchedulerInvocationBinding,
    SchedulerInvocationPermit,
    issue_scheduler_invocation_effect_authorization,
)
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
from app.domain.execution_v2.models import WorkerLease
from app.domain.scheduler.models import (
    SCHEDULER_JOB_KEYS,
    SchedulerInvariantError,
    SchedulerJobKey,
)
from app.infrastructure.authenticated_webhook import (
    AuthenticatedWebhookTransport,
    ReceiverAckKeyRing,
    ValidatedWebhookTarget,
)

_ACCOUNT_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
_RELEASE_SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_SCHEDULER_HANDLER_ORDER: tuple[SchedulerJobKey, ...] = (
    "operations.commands",
    "operations.execution",
    "operations.settlement",
    "operations.reconciliation",
    "operations.outbox",
)


class _SupabaseSchedulerHandlerIssuance:
    __slots__ = ()


_SUPABASE_SCHEDULER_HANDLER_ISSUANCE = _SupabaseSchedulerHandlerIssuance()


@dataclass(frozen=True, slots=True)
class _ReceiverAckKeyRingPin:
    key_ring: ReceiverAckKeyRing
    current_key_id: str
    current_key_sha256: bytes
    previous_key_id: str | None
    previous_key_sha256: bytes | None


@dataclass(frozen=True, slots=True)
class _HttpxClientRuntimePin:
    client: httpx.AsyncClient
    transport: object
    mounts: object
    effective_transport: object
    event_hooks: object
    cookies: object
    header_sha256: bytes
    client_request_config: tuple[object, ...]
    client_methods: tuple[object, ...]
    transport_handler: object
    pool: object
    pool_type: type[object]
    pool_handler: object
    network_backend: object
    network_backend_type: type[object]
    network_backend_methods: tuple[object, ...]
    ssl_context: object
    ssl_context_state: tuple[object, ...]
    pool_route_config: tuple[object, ...]


@dataclass(frozen=True, slots=True)
class _OutboxRuntimePin:
    destination: OutboxWebhookDestination | UnavailableOutboxDestination
    transport: AuthenticatedWebhookTransport | None
    target: ValidatedWebhookTarget | None
    target_url: str | None
    client_pin: _HttpxClientRuntimePin | None
    key_ring_pin: _ReceiverAckKeyRingPin | None
    timeout_sec: float | None
    clock: object | None
    owns_client: bool | None


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
        "_outbox_pin",
        "_scheduler_client_pin",
        "_scheduler_headers_sha256",
        "_worker_api_client_pin",
        "_worker_api_headers_sha256",
        "_execution_source_client_pin",
        "_execution_source_headers_sha256",
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
    _outbox_pin: _OutboxRuntimePin
    _scheduler_client_pin: _HttpxClientRuntimePin
    _scheduler_headers_sha256: bytes
    _worker_api_client_pin: _HttpxClientRuntimePin
    _worker_api_headers_sha256: bytes
    _execution_source_client_pin: _HttpxClientRuntimePin
    _execution_source_headers_sha256: bytes
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
        if type(execution) is not RunExecutionSupervisorV2:
            _invalid("execution_type")
        if type(execution.source) is not SupabasePaperExecutionCommandSource:
            _invalid("execution_source_type")
        scheduler_client_pin = _capture_httpx_client_runtime_pin(
            scheduler._client,
            target_url=scheduler.base_url,
        )
        worker_api_client_pin = _capture_httpx_client_runtime_pin(
            worker_api._client,
            target_url=worker_api.base_url,
        )
        execution_source_client_pin = _capture_httpx_client_runtime_pin(
            execution.source._client,
            target_url=execution.source.base_url,
        )
        scheduler_headers_sha256 = _header_mapping_sha256(scheduler._headers)
        worker_api_headers_sha256 = _header_mapping_sha256(worker_api._headers)
        execution_source_headers_sha256 = _header_mapping_sha256(
            execution.source._headers
        )
        object.__setattr__(self, "_scheduler", scheduler)
        object.__setattr__(self, "_worker_api", worker_api)
        object.__setattr__(self, "_lease_manager", lease_manager)
        object.__setattr__(self, "_commands", commands)
        object.__setattr__(self, "_execution", execution)
        object.__setattr__(self, "_settlement", settlement)
        object.__setattr__(self, "_reconciliation", reconciliation)
        object.__setattr__(self, "_outbox", outbox)
        object.__setattr__(self, "_outbox_pin", _capture_outbox_runtime_pin(outbox))
        object.__setattr__(self, "_scheduler_client_pin", scheduler_client_pin)
        object.__setattr__(
            self,
            "_scheduler_headers_sha256",
            scheduler_headers_sha256,
        )
        object.__setattr__(self, "_worker_api_client_pin", worker_api_client_pin)
        object.__setattr__(
            self,
            "_worker_api_headers_sha256",
            worker_api_headers_sha256,
        )
        object.__setattr__(
            self,
            "_execution_source_client_pin",
            execution_source_client_pin,
        )
        object.__setattr__(
            self,
            "_execution_source_headers_sha256",
            execution_source_headers_sha256,
        )
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
            expected_outbox_pin=self._outbox_pin,
            expected_scheduler_client_pin=self._scheduler_client_pin,
            expected_scheduler_headers_sha256=self._scheduler_headers_sha256,
            expected_worker_api_client_pin=self._worker_api_client_pin,
            expected_worker_api_headers_sha256=self._worker_api_headers_sha256,
            expected_execution_source_client_pin=self._execution_source_client_pin,
            expected_execution_source_headers_sha256=(
                self._execution_source_headers_sha256
            ),
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


class _SupabaseSchedulerJobHandler:
    """One immutable handler bound to an attested runtime stage."""

    __slots__ = ("_runtime", "_job_key", "_issuance")
    _issuance: _SupabaseSchedulerHandlerIssuance
    _job_key: SchedulerJobKey
    _runtime: _SupabaseSchedulerRuntimeCapability

    def __init__(
        self,
        runtime: _SupabaseSchedulerRuntimeCapability,
        job_key: SchedulerJobKey,
        *,
        _issuance: object,
    ) -> None:
        if _issuance is not _SUPABASE_SCHEDULER_HANDLER_ISSUANCE:
            _invalid("handler_not_issued")
        if job_key not in SCHEDULER_JOB_KEYS:
            _invalid("handler_job_key")
        runtime.assert_intact()
        object.__setattr__(self, "_runtime", runtime)
        object.__setattr__(self, "_job_key", job_key)
        object.__setattr__(self, "_issuance", _SUPABASE_SCHEDULER_HANDLER_ISSUANCE)

    def __setattr__(self, _name: str, _value: object) -> Never:
        raise SchedulerInvariantError("scheduler_runtime_handler_is_immutable")

    def __delattr__(self, _name: str) -> Never:
        raise SchedulerInvariantError("scheduler_runtime_handler_is_immutable")

    def __reduce__(self) -> Never:
        raise TypeError("scheduler_runtime_handler_is_not_serializable")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> Never:
        raise TypeError("scheduler_runtime_handler_is_not_serializable")

    async def __call__(
        self,
        permit: SchedulerInvocationPermit,
        invocation_binding: SchedulerInvocationBinding,
    ) -> object:
        if self._issuance is not _SUPABASE_SCHEDULER_HANDLER_ISSUANCE:
            _invalid("handler_not_issued")
        runtime = self._runtime
        if require_supabase_scheduler_runtime_capability(runtime) is not runtime:
            _invalid("handler_runtime")
        authorization = issue_scheduler_invocation_effect_authorization(
            runtime,
            permit,
            invocation_binding,
            expected_job_key=self._job_key,
            effect_issuer=self,
        )
        if self._job_key == "operations.commands":
            return await runtime._commands.run_scheduled(authorization)
        if self._job_key == "operations.execution":
            return await runtime._execution.run_scheduled(authorization)
        if self._job_key == "operations.settlement":
            return await runtime._settlement.run_scheduled(authorization)
        if self._job_key == "operations.reconciliation":
            return await runtime._reconciliation.run_scheduled(authorization)
        if self._job_key == "operations.outbox":
            return await runtime._outbox.dispatch_scheduled(authorization)
        _invalid("handler_job_key")


def create_supabase_scheduler_job_bindings(
    runtime: SchedulerRuntimeCapability,
) -> Mapping[SchedulerJobKey, SchedulerJobBinding]:
    """Create the only production binding registry for the attested graph."""

    capability = require_supabase_scheduler_runtime_capability(runtime)
    assert type(capability) is _SupabaseSchedulerRuntimeCapability
    bindings = {
        job_key: SchedulerJobBinding(
            job_key=job_key,
            handler=_SupabaseSchedulerJobHandler(
                capability,
                job_key,
                _issuance=_SUPABASE_SCHEDULER_HANDLER_ISSUANCE,
            ),
            result_validator=SCHEDULER_RESULT_VALIDATORS[job_key],
        )
        for job_key in _SCHEDULER_HANDLER_ORDER
    }
    return MappingProxyType(bindings)


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
    expected_outbox_pin: _OutboxRuntimePin,
    expected_scheduler_client_pin: _HttpxClientRuntimePin,
    expected_scheduler_headers_sha256: bytes,
    expected_worker_api_client_pin: _HttpxClientRuntimePin,
    expected_worker_api_headers_sha256: bytes,
    expected_execution_source_client_pin: _HttpxClientRuntimePin,
    expected_execution_source_headers_sha256: bytes,
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
    _assert_httpx_client_runtime_pin(
        scheduler._client,
        target_url=scheduler.base_url,
        expected=expected_scheduler_client_pin,
    )
    _assert_httpx_client_runtime_pin(
        worker_api._client,
        target_url=worker_api.base_url,
        expected=expected_worker_api_client_pin,
    )
    _assert_header_mapping_pin(
        scheduler._headers,
        expected_sha256=expected_scheduler_headers_sha256,
    )
    _assert_header_mapping_pin(
        worker_api._headers,
        expected_sha256=expected_worker_api_headers_sha256,
    )

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
        expected_source_client_pin=expected_execution_source_client_pin,
        expected_source_headers_sha256=expected_execution_source_headers_sha256,
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
    _assert_outbox(
        outbox,
        worker_api=worker_api,
        holder_id=expected_holder_id,
        expected_pin=expected_outbox_pin,
    )


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
    expected_source_client_pin: _HttpxClientRuntimePin,
    expected_source_headers_sha256: bytes,
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
    _assert_httpx_client_runtime_pin(
        source._client,
        target_url=source.base_url,
        expected=expected_source_client_pin,
    )
    _assert_header_mapping_pin(
        source._headers,
        expected_sha256=expected_source_headers_sha256,
    )
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
    expected_pin: _OutboxRuntimePin,
) -> None:
    if type(stage) is not DispatchAlertOutbox:
        _invalid("outbox_type")
    if stage.outbox is not worker_api or stage.worker_id != holder_id:
        _invalid("outbox_binding")
    destination = stage.destination
    if destination is not expected_pin.destination:
        _invalid("outbox_destination_identity")
    if type(destination) is UnavailableOutboxDestination:
        if any(
            value is not None
            for value in (
                expected_pin.transport,
                expected_pin.target,
                expected_pin.target_url,
                expected_pin.client_pin,
                expected_pin.key_ring_pin,
                expected_pin.timeout_sec,
                expected_pin.clock,
                expected_pin.owns_client,
            )
        ):
            _invalid("outbox_destination_pin")
        return
    if type(destination) is not OutboxWebhookDestination:
        _invalid("outbox_destination_type")
    transport = destination._transport
    if (
        type(transport) is not AuthenticatedWebhookTransport
        or transport is not expected_pin.transport
        or transport._target is not expected_pin.target
        or transport._target.url != expected_pin.target_url
        or transport._timeout_sec != expected_pin.timeout_sec
        or transport._clock is not expected_pin.clock
        or transport._owns_client is not expected_pin.owns_client
        or transport._owns_client is not True
    ):
        _invalid("outbox_destination_pin")
    if expected_pin.target_url is None or expected_pin.client_pin is None:
        _invalid("outbox_destination_pin")
    if expected_pin.key_ring_pin is None:
        _invalid("outbox_destination_pin")
    _assert_httpx_client_runtime_pin(
        transport._client,
        target_url=expected_pin.target_url,
        expected=expected_pin.client_pin,
    )
    _assert_receiver_ack_key_ring_pin(
        transport._key_ring,
        expected=expected_pin.key_ring_pin,
    )


def _capture_outbox_runtime_pin(stage: DispatchAlertOutbox) -> _OutboxRuntimePin:
    if type(stage) is not DispatchAlertOutbox:
        _invalid("outbox_type")
    destination = stage.destination
    if type(destination) is UnavailableOutboxDestination:
        return _OutboxRuntimePin(
            destination=destination,
            transport=None,
            target=None,
            target_url=None,
            client_pin=None,
            key_ring_pin=None,
            timeout_sec=None,
            clock=None,
            owns_client=None,
        )
    if type(destination) is not OutboxWebhookDestination:
        _invalid("outbox_destination_type")
    transport = destination._transport
    if (
        type(transport) is not AuthenticatedWebhookTransport
        or type(transport._target) is not ValidatedWebhookTarget
        or type(transport._key_ring) is not ReceiverAckKeyRing
        or transport._owns_client is not True
    ):
        _invalid("outbox_destination_pin")
    return _OutboxRuntimePin(
        destination=destination,
        transport=transport,
        target=transport._target,
        target_url=transport._target.url,
        client_pin=_capture_httpx_client_runtime_pin(
            transport._client,
            target_url=transport._target.url,
        ),
        key_ring_pin=_capture_receiver_ack_key_ring_pin(transport._key_ring),
        timeout_sec=transport._timeout_sec,
        clock=transport._clock,
        owns_client=transport._owns_client,
    )


_HTTPX_CLIENT_DISPATCH_METHODS = (
    "build_request",
    "request",
    "post",
    "stream",
    "send",
    "_build_request_auth",
    "_send_handling_auth",
    "_send_handling_redirects",
    "_send_single_request",
    "_transport_for_url",
)
_HTTPX_NETWORK_BACKEND_METHODS = (
    "connect_tcp",
    "connect_unix_socket",
    "sleep",
)


def _header_mapping_sha256(value: object) -> bytes:
    if not isinstance(value, Mapping):
        _invalid("scheduler_http_headers")
    normalized: list[tuple[str, str]] = []
    for name, header_value in value.items():
        if type(name) is not str or type(header_value) is not str:
            _invalid("scheduler_http_headers")
        normalized.append((name.lower(), header_value))
    digest = hashlib.sha256()
    for name, header_value in sorted(normalized):
        try:
            name_bytes = name.encode("ascii", errors="strict")
            value_bytes = header_value.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            _invalid("scheduler_http_headers")
        digest.update(len(name_bytes).to_bytes(4, "big"))
        digest.update(name_bytes)
        digest.update(len(value_bytes).to_bytes(4, "big"))
        digest.update(value_bytes)
    return digest.digest()


def _assert_header_mapping_pin(
    value: object,
    *,
    expected_sha256: bytes,
) -> None:
    if (
        type(expected_sha256) is not bytes
        or len(expected_sha256) != hashlib.sha256().digest_size
        or not hmac.compare_digest(
            _header_mapping_sha256(value),
            expected_sha256,
        )
    ):
        _invalid("scheduler_http_headers")


def _capture_httpx_client_runtime_pin(
    client: object,
    *,
    target_url: str,
) -> _HttpxClientRuntimePin:
    if type(client) is not httpx.AsyncClient:
        _invalid("scheduler_http_client_type")
    client_state = vars(client)
    transport = client_state.get("_transport")
    mounts = client_state.get("_mounts")
    event_hooks = client_state.get("_event_hooks")
    cookies = client_state.get("_cookies")
    if (
        type(transport) is not httpx.AsyncHTTPTransport
        or type(mounts) is not dict
        or mounts
        or client_state.get("_trust_env") is not False
        or client_state.get("_auth") is not None
        or not isinstance(cookies, httpx.Cookies)
        or len(cookies) != 0
        or type(event_hooks) is not dict
        or frozenset(event_hooks) != frozenset({"request", "response"})
        or any(type(hooks) is not list or hooks for hooks in event_hooks.values())
        or any(name in client_state for name in _HTTPX_CLIENT_DISPATCH_METHODS)
    ):
        _invalid("scheduler_http_client_route")
    client_methods = tuple(
        getattr(httpx.AsyncClient, name, None)
        for name in _HTTPX_CLIENT_DISPATCH_METHODS
    )
    if any(not callable(method) for method in client_methods):
        _invalid("scheduler_http_client_route")
    try:
        effective_transport = client._transport_for_url(httpx.URL(target_url))
        header_sha256 = _httpx_headers_sha256(client.headers)
        timeout = client.timeout
        client_request_config = (
            str(client.base_url),
            str(client.params),
            timeout.connect,
            timeout.read,
            timeout.write,
            timeout.pool,
            client.follow_redirects,
            client.max_redirects,
        )
        transport_state = vars(transport)
        transport_handler = httpx.AsyncHTTPTransport.handle_async_request
        pool = transport_state["_pool"]
        pool_state = vars(pool)
        pool_type = type(pool)
        pool_handler = getattr(pool_type, "handle_async_request", None)
        network_backend = pool_state["_network_backend"]
        network_backend_type = type(network_backend)
        network_backend_state = vars(network_backend)
        network_backend_methods = tuple(
            getattr(network_backend_type, name, None)
            for name in _HTTPX_NETWORK_BACKEND_METHODS
        )
        ssl_context = pool_state["_ssl_context"]
        ssl_context_state = _ssl_context_state(ssl_context)
        pool_route_config = (
            pool_state.get("_proxy"),
            pool_state.get("_local_address"),
            pool_state.get("_uds"),
            pool_state.get("_socket_options"),
            pool_state.get("_http1"),
            pool_state.get("_http2"),
            pool_state.get("_retries"),
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        _invalid("scheduler_http_client_route")
    if (
        effective_transport is not transport
        or "handle_async_request" in transport_state
        or not callable(transport_handler)
        or "handle_async_request" in pool_state
        or not callable(pool_handler)
        or any(
            name in network_backend_state
            for name in _HTTPX_NETWORK_BACKEND_METHODS
        )
        or any(not callable(method) for method in network_backend_methods)
        or pool_state.get("_proxy") is not None
        or pool_state.get("_local_address") is not None
        or pool_state.get("_uds") is not None
        or pool_state.get("_socket_options") is not None
    ):
        _invalid("scheduler_http_client_route")
    return _HttpxClientRuntimePin(
        client=client,
        transport=transport,
        mounts=mounts,
        effective_transport=effective_transport,
        event_hooks=event_hooks,
        cookies=cookies,
        header_sha256=header_sha256,
        client_request_config=client_request_config,
        client_methods=client_methods,
        transport_handler=transport_handler,
        pool=pool,
        pool_type=pool_type,
        pool_handler=pool_handler,
        network_backend=network_backend,
        network_backend_type=network_backend_type,
        network_backend_methods=network_backend_methods,
        ssl_context=ssl_context,
        ssl_context_state=ssl_context_state,
        pool_route_config=pool_route_config,
    )


def _assert_httpx_client_runtime_pin(
    client: object,
    *,
    target_url: str,
    expected: _HttpxClientRuntimePin,
) -> None:
    current = _capture_httpx_client_runtime_pin(client, target_url=target_url)
    if (
        current.client is not expected.client
        or current.transport is not expected.transport
        or current.mounts is not expected.mounts
        or current.effective_transport is not expected.effective_transport
        or current.event_hooks is not expected.event_hooks
        or current.cookies is not expected.cookies
        or not hmac.compare_digest(
            current.header_sha256,
            expected.header_sha256,
        )
        or current.client_request_config != expected.client_request_config
        or len(current.client_methods) != len(expected.client_methods)
        or any(
            current_method is not expected_method
            for current_method, expected_method in zip(
                current.client_methods,
                expected.client_methods,
                strict=True,
            )
        )
        or current.transport_handler is not expected.transport_handler
        or current.pool is not expected.pool
        or current.pool_type is not expected.pool_type
        or current.pool_handler is not expected.pool_handler
        or current.network_backend is not expected.network_backend
        or current.network_backend_type is not expected.network_backend_type
        or len(current.network_backend_methods)
        != len(expected.network_backend_methods)
        or any(
            current_method is not expected_method
            for current_method, expected_method in zip(
                current.network_backend_methods,
                expected.network_backend_methods,
                strict=True,
            )
        )
        or current.ssl_context is not expected.ssl_context
        or current.ssl_context_state != expected.ssl_context_state
        or current.pool_route_config != expected.pool_route_config
    ):
        _invalid("scheduler_http_client_route")


def _httpx_headers_sha256(headers: httpx.Headers) -> bytes:
    digest = hashlib.sha256()
    for name, value in headers.raw:
        digest.update(len(name).to_bytes(4, "big"))
        digest.update(name)
        digest.update(len(value).to_bytes(4, "big"))
        digest.update(value)
    return digest.digest()


def _ssl_context_state(value: object) -> tuple[object, ...]:
    if type(value) is not ssl.SSLContext:
        _invalid("scheduler_http_client_tls")
    if (
        value.check_hostname is not True
        or value.verify_mode != ssl.CERT_REQUIRED
        or value.minimum_version < ssl.TLSVersion.TLSv1_2
    ):
        _invalid("scheduler_http_client_tls")
    try:
        ca_certificates = sorted(value.get_ca_certs(binary_form=True))
        if not ca_certificates:
            _invalid("scheduler_http_client_tls")
        ca_digest = hashlib.sha256()
        for certificate in ca_certificates:
            if type(certificate) is not bytes:
                _invalid("scheduler_http_client_tls")
            ca_digest.update(len(certificate).to_bytes(4, "big"))
            ca_digest.update(certificate)
        return (
            value.check_hostname,
            value.verify_mode,
            value.minimum_version,
            value.maximum_version,
            value.options,
            value.verify_flags,
            len(ca_certificates),
            ca_digest.digest(),
        )
    except (OverflowError, ssl.SSLError, TypeError, ValueError):
        _invalid("scheduler_http_client_tls")


def _capture_receiver_ack_key_ring_pin(
    key_ring: object,
) -> _ReceiverAckKeyRingPin:
    if type(key_ring) is not ReceiverAckKeyRing:
        _invalid("outbox_key_ring_type")
    current_key = key_ring._current_key
    previous_key = key_ring._previous_key
    if (
        type(key_ring.current_key_id) is not str
        or type(current_key) is not bytes
        or len(current_key) != 32
        or (key_ring.previous_key_id is None) != (previous_key is None)
        or (
            key_ring.previous_key_id is not None
            and (
                type(key_ring.previous_key_id) is not str
                or type(previous_key) is not bytes
                or len(previous_key) != 32
            )
        )
    ):
        _invalid("outbox_key_ring_state")
    return _ReceiverAckKeyRingPin(
        key_ring=key_ring,
        current_key_id=key_ring.current_key_id,
        current_key_sha256=hashlib.sha256(current_key).digest(),
        previous_key_id=key_ring.previous_key_id,
        previous_key_sha256=(
            hashlib.sha256(previous_key).digest()
            if previous_key is not None
            else None
        ),
    )


def _assert_receiver_ack_key_ring_pin(
    key_ring: object,
    *,
    expected: _ReceiverAckKeyRingPin,
) -> None:
    current = _capture_receiver_ack_key_ring_pin(key_ring)
    previous_digest_matches = (
        current.previous_key_sha256 is None
        and expected.previous_key_sha256 is None
    ) or (
        current.previous_key_sha256 is not None
        and expected.previous_key_sha256 is not None
        and hmac.compare_digest(
            current.previous_key_sha256,
            expected.previous_key_sha256,
        )
    )
    if (
        current.key_ring is not expected.key_ring
        or current.current_key_id != expected.current_key_id
        or current.previous_key_id != expected.previous_key_id
        or not hmac.compare_digest(
            current.current_key_sha256,
            expected.current_key_sha256,
        )
        or not previous_digest_matches
    ):
        _invalid("outbox_key_ring_state")


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
