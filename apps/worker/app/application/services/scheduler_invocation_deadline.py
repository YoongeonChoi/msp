from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from time import monotonic
from types import MappingProxyType
from typing import Literal, Never, SupportsIndex, cast

from app.application.ports.durable_scheduler_port import (
    DURABLE_SCHEDULER_TOTAL_RPC_TIMEOUT_SECONDS,
    DurableSchedulerPort,
    canonical_scheduler_outer_lease,
)
from app.application.ports.persistence_authority import (
    PersistenceAuthority,
    is_persistence_authority,
)
from app.application.use_cases.scheduler_runtime_capability import (
    SchedulerRuntimeCapability,
)
from app.domain.common.errors import KnownFailClosedError
from app.domain.execution_v2.models import WorkerLease
from app.domain.scheduler.models import (
    SCHEDULER_JOB_KEYS,
    SCHEDULER_RETRYABLE_REASONS,
    ScheduledJobClaimV1,
    ScheduledJobCompletionReceiptV1,
    ScheduledJobDefinitionV1,
    ScheduledJobFailureReceiptV1,
    SchedulerDefinitionConvergenceReceiptV1,
    SchedulerInvariantError,
    SchedulerJobKey,
    canonical_scheduler_claim,
    canonical_scheduler_claim_receipt,
    canonical_scheduler_convergence_receipt,
    canonical_scheduler_datetime,
    canonical_scheduler_definition,
)

MonotonicClock = Callable[[], float]
DeadlineWaiter = Callable[[float], Awaitable[None]]
FailStop = Callable[[str], Never]
type InvocationHandler[T] = Callable[["SchedulerInvocationPermit"], Awaitable[T]]
SchedulerInvocationTimeState = Literal["active", "deadline", "clock_corrupt"]
SchedulerInvocationSettlementState = Literal[
    "pending",
    "ready",
    "authorized",
    "dispatched_complete",
    "dispatched_fail",
    "expired",
]
SchedulerInvocationSettlementTransition = Literal["complete", "fail"]
TimerCancellationResult = Literal["cancelled", "fired", "failed", "stalled"]

SCHEDULER_INVOCATION_CANCELLATION_GRACE_SECONDS = 0.25
SCHEDULER_INVOCATION_VALIDATION_RESERVE_SECONDS = 1.75
SCHEDULER_INVOCATION_SETTLEMENT_START_RESERVE = timedelta(
    seconds=DURABLE_SCHEDULER_TOTAL_RPC_TIMEOUT_SECONDS
)
SCHEDULER_INVOCATION_SETTLEMENT_RESERVE = timedelta(
    seconds=(
        SCHEDULER_INVOCATION_CANCELLATION_GRACE_SECONDS
        + SCHEDULER_INVOCATION_VALIDATION_RESERVE_SECONDS
    )
) + SCHEDULER_INVOCATION_SETTLEMENT_START_RESERVE

_SCHEDULER_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SCHEDULER_REASON_RE = re.compile(r"[a-z][a-z0-9_]{0,127}")


class _SchedulerInvocationIssuance:
    __slots__ = ()


_SCHEDULER_INVOCATION_ISSUANCE = _SchedulerInvocationIssuance()


class _SchedulerTestMonotonicClock:
    """Exact, callback-free manual clock for deterministic scheduler tests."""

    __slots__ = ("value", "fail")
    value: float
    fail: bool

    def __init__(self, value: float) -> None:
        _require_exact_monotonic(value, "scheduler_test_clock_is_invalid")
        self.value = value
        self.fail = False

    def __call__(self) -> float:
        if self.fail:
            raise RuntimeError("scheduler_test_clock_failure")
        return self.value


class SchedulerClaimRpcStart:
    """Issued monotonic observation captured before a scheduler claim RPC."""

    __slots__ = ("_monotonic_clock", "_started_monotonic", "_issuance")
    _issuance: _SchedulerInvocationIssuance
    _monotonic_clock: MonotonicClock
    _started_monotonic: float

    def __init__(
        self,
        *,
        monotonic_clock: MonotonicClock,
        started_monotonic: float,
        _issuance: object,
    ) -> None:
        if _issuance is not _SCHEDULER_INVOCATION_ISSUANCE:
            raise SchedulerInvariantError("scheduler_claim_rpc_start_is_not_issued")
        if not callable(monotonic_clock):
            raise SchedulerInvariantError("scheduler_invocation_clock_is_invalid")
        _require_exact_monotonic(
            started_monotonic,
            "scheduler_invocation_rpc_started_monotonic_is_invalid",
        )
        object.__setattr__(self, "_monotonic_clock", monotonic_clock)
        object.__setattr__(self, "_started_monotonic", started_monotonic)
        object.__setattr__(self, "_issuance", _SCHEDULER_INVOCATION_ISSUANCE)

    def __setattr__(self, _name: str, _value: object) -> Never:
        raise SchedulerInvariantError("scheduler_claim_rpc_start_is_immutable")

    def __delattr__(self, _name: str) -> Never:
        raise SchedulerInvariantError("scheduler_claim_rpc_start_is_immutable")

    def __copy__(self) -> Never:
        raise SchedulerInvariantError("scheduler_claim_rpc_start_is_not_copyable")

    def __deepcopy__(self, _memo: object) -> Never:
        raise SchedulerInvariantError("scheduler_claim_rpc_start_is_not_copyable")

    def __reduce__(self) -> Never:
        raise SchedulerInvariantError("scheduler_claim_rpc_start_is_not_serializable")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> Never:
        raise SchedulerInvariantError("scheduler_claim_rpc_start_is_not_serializable")

    @property
    def started_monotonic(self) -> float:
        return self._started_monotonic

    def _assert_intact(self) -> None:
        try:
            if self._issuance is not _SCHEDULER_INVOCATION_ISSUANCE:
                raise SchedulerInvariantError("scheduler_claim_rpc_start_is_not_issued")
            if not callable(self._monotonic_clock):
                raise SchedulerInvariantError("scheduler_invocation_clock_is_invalid")
            _require_exact_monotonic(
                self._started_monotonic,
                "scheduler_invocation_rpc_started_monotonic_is_invalid",
            )
        except SchedulerInvariantError:
            raise
        except Exception:
            raise SchedulerInvariantError("scheduler_claim_rpc_start_is_invalid") from None


class SchedulerInvocationBinding:
    """Exact scheduler claim/outer-lease identity for one invocation."""

    __slots__ = ("_claim", "_outer_lease", "_issuance")
    _claim: ScheduledJobClaimV1
    _issuance: _SchedulerInvocationIssuance
    _outer_lease: WorkerLease

    def __init__(
        self,
        *,
        claim: ScheduledJobClaimV1,
        outer_lease: WorkerLease,
        _issuance: object,
    ) -> None:
        if _issuance is not _SCHEDULER_INVOCATION_ISSUANCE:
            raise SchedulerInvariantError("scheduler_invocation_binding_is_not_issued")
        canonical_claim = canonical_scheduler_claim(claim)
        canonical_outer_lease = canonical_scheduler_outer_lease(outer_lease)
        _validate_invocation_context(canonical_claim, canonical_outer_lease)
        object.__setattr__(self, "_claim", canonical_claim)
        object.__setattr__(self, "_outer_lease", canonical_outer_lease)
        object.__setattr__(self, "_issuance", _SCHEDULER_INVOCATION_ISSUANCE)

    def __setattr__(self, _name: str, _value: object) -> Never:
        raise SchedulerInvariantError("scheduler_invocation_binding_is_immutable")

    def __delattr__(self, _name: str) -> Never:
        raise SchedulerInvariantError("scheduler_invocation_binding_is_immutable")

    def __copy__(self) -> Never:
        raise SchedulerInvariantError("scheduler_invocation_binding_is_not_copyable")

    def __deepcopy__(self, _memo: object) -> Never:
        raise SchedulerInvariantError("scheduler_invocation_binding_is_not_copyable")

    def __reduce__(self) -> Never:
        raise SchedulerInvariantError("scheduler_invocation_binding_is_not_serializable")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> Never:
        raise SchedulerInvariantError("scheduler_invocation_binding_is_not_serializable")

    @property
    def account_id(self) -> str:
        return self._claim.run.account_id

    @property
    def holder_id(self) -> str:
        return self._claim.lease.holder_id

    @property
    def release_sha(self) -> str:
        return self._claim.lease.release_sha

    @property
    def job_key(self) -> SchedulerJobKey:
        return self._claim.job_key

    @property
    def run_id(self) -> str:
        return self._claim.run.run_id

    @property
    def run_revision(self) -> int:
        return self._claim.run.revision

    @property
    def lease_token(self) -> str:
        return self._claim.lease.lease_token

    @property
    def outer_fencing_token(self) -> int:
        return self._outer_lease.fencing_token

    @property
    def definition_sha256(self) -> str:
        return self._claim.definition.definition_sha256

    @property
    def attempt_number(self) -> int:
        return self._claim.lease.attempt_number

    def _assert_intact(self) -> None:
        try:
            if self._issuance is not _SCHEDULER_INVOCATION_ISSUANCE:
                raise SchedulerInvariantError("scheduler_invocation_binding_is_not_issued")
            canonical_claim = canonical_scheduler_claim(self._claim)
            canonical_outer_lease = canonical_scheduler_outer_lease(self._outer_lease)
            if canonical_claim != self._claim or canonical_outer_lease != self._outer_lease:
                raise SchedulerInvariantError("scheduler_invocation_binding_is_invalid")
            _validate_invocation_context(canonical_claim, canonical_outer_lease)
        except SchedulerInvariantError:
            raise
        except Exception:
            raise SchedulerInvariantError("scheduler_invocation_binding_is_invalid") from None


class SchedulerInvocationDeadline:
    """Immutable proof of the wall-clock/monotonic deadline conversion."""

    __slots__ = (
        "_binding",
        "_database_cutoff_at",
        "_rpc_start",
        "_monotonic_deadline",
        "_issuance",
    )
    _binding: SchedulerInvocationBinding
    _database_cutoff_at: datetime
    _issuance: _SchedulerInvocationIssuance
    _monotonic_deadline: float
    _rpc_start: SchedulerClaimRpcStart

    def __init__(
        self,
        *,
        binding: SchedulerInvocationBinding,
        database_cutoff_at: datetime,
        rpc_start: SchedulerClaimRpcStart,
        monotonic_deadline: float,
        _issuance: object,
    ) -> None:
        if _issuance is not _SCHEDULER_INVOCATION_ISSUANCE:
            raise SchedulerInvariantError("scheduler_invocation_deadline_is_not_issued")
        if type(binding) is not SchedulerInvocationBinding:
            raise SchedulerInvariantError("scheduler_invocation_binding_is_invalid")
        binding._assert_intact()
        if type(rpc_start) is not SchedulerClaimRpcStart:
            raise SchedulerInvariantError("scheduler_claim_rpc_start_is_invalid")
        rpc_start._assert_intact()
        cutoff_at = canonical_scheduler_datetime(
            database_cutoff_at,
            "scheduler_invocation_database_cutoff_at",
        )
        _require_exact_monotonic(
            monotonic_deadline,
            "scheduler_invocation_monotonic_deadline_is_invalid",
        )
        if cutoff_at != _database_cutoff_for_binding(binding):
            raise SchedulerInvariantError("scheduler_invocation_cutoff_binding_is_invalid")
        if monotonic_deadline != _convert_database_cutoff_to_monotonic(
            claim_observed_at=binding._claim.observed_at,
            database_cutoff_at=cutoff_at,
            rpc_started_monotonic=rpc_start.started_monotonic,
        ):
            raise SchedulerInvariantError("scheduler_invocation_deadline_binding_is_invalid")
        object.__setattr__(self, "_binding", binding)
        object.__setattr__(self, "_database_cutoff_at", cutoff_at)
        object.__setattr__(self, "_rpc_start", rpc_start)
        object.__setattr__(self, "_monotonic_deadline", monotonic_deadline)
        object.__setattr__(self, "_issuance", _SCHEDULER_INVOCATION_ISSUANCE)

    def __setattr__(self, _name: str, _value: object) -> Never:
        raise SchedulerInvariantError("scheduler_invocation_deadline_is_immutable")

    def __delattr__(self, _name: str) -> Never:
        raise SchedulerInvariantError("scheduler_invocation_deadline_is_immutable")

    def __copy__(self) -> Never:
        raise SchedulerInvariantError("scheduler_invocation_deadline_is_not_copyable")

    def __deepcopy__(self, _memo: object) -> Never:
        raise SchedulerInvariantError("scheduler_invocation_deadline_is_not_copyable")

    def __reduce__(self) -> Never:
        raise SchedulerInvariantError("scheduler_invocation_deadline_is_not_serializable")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> Never:
        raise SchedulerInvariantError("scheduler_invocation_deadline_is_not_serializable")

    @property
    def claim_observed_at(self) -> datetime:
        return self._binding._claim.observed_at

    @property
    def binding(self) -> SchedulerInvocationBinding:
        return self._binding

    @property
    def database_cutoff_at(self) -> datetime:
        return self._database_cutoff_at

    @property
    def rpc_started_monotonic(self) -> float:
        return self._rpc_start.started_monotonic

    @property
    def monotonic_deadline(self) -> float:
        return self._monotonic_deadline

    @property
    def database_settlement_start_cutoff_at(self) -> datetime:
        return _database_settlement_start_cutoff_for_binding(self._binding)

    @property
    def monotonic_settlement_start_deadline(self) -> float:
        return _monotonic_settlement_start_deadline(self)

    def _assert_intact(self) -> None:
        try:
            if self._issuance is not _SCHEDULER_INVOCATION_ISSUANCE:
                raise SchedulerInvariantError("scheduler_invocation_deadline_is_not_issued")
            if type(self._binding) is not SchedulerInvocationBinding:
                raise SchedulerInvariantError("scheduler_invocation_binding_is_invalid")
            self._binding._assert_intact()
            if type(self._rpc_start) is not SchedulerClaimRpcStart:
                raise SchedulerInvariantError("scheduler_claim_rpc_start_is_invalid")
            self._rpc_start._assert_intact()
            cutoff_at = canonical_scheduler_datetime(
                self._database_cutoff_at,
                "scheduler_invocation_database_cutoff_at",
            )
            _require_exact_monotonic(
                self._monotonic_deadline,
                "scheduler_invocation_monotonic_deadline_is_invalid",
            )
            if cutoff_at != _database_cutoff_for_binding(self._binding):
                raise SchedulerInvariantError("scheduler_invocation_cutoff_binding_is_invalid")
            expected = _convert_database_cutoff_to_monotonic(
                claim_observed_at=self._binding._claim.observed_at,
                database_cutoff_at=cutoff_at,
                rpc_started_monotonic=self._rpc_start.started_monotonic,
            )
            if expected != self._monotonic_deadline:
                raise SchedulerInvariantError("scheduler_invocation_deadline_binding_is_invalid")
        except SchedulerInvariantError:
            raise
        except Exception:
            raise SchedulerInvariantError("scheduler_invocation_deadline_is_invalid") from None


class SchedulerClaimedInvocation:
    """One-shot invocation envelope issued by one exact scheduler claim RPC."""

    __slots__ = (
        "_deadline",
        "_scheduler_port",
        "_persistence_authority",
        "_consumed",
        "_settlement_baseline_monotonic",
        "_settlement_state",
        "_issuance",
    )
    _consumed: bool
    _deadline: SchedulerInvocationDeadline
    _issuance: _SchedulerInvocationIssuance
    _persistence_authority: PersistenceAuthority
    _scheduler_port: DurableSchedulerPort
    _settlement_baseline_monotonic: float | None
    _settlement_state: SchedulerInvocationSettlementState

    def __init__(
        self,
        *,
        deadline: SchedulerInvocationDeadline,
        scheduler_port: DurableSchedulerPort,
        _issuance: object,
    ) -> None:
        if _issuance is not _SCHEDULER_INVOCATION_ISSUANCE:
            raise SchedulerInvariantError("scheduler_claimed_invocation_is_not_issued")
        if type(deadline) is not SchedulerInvocationDeadline:
            raise SchedulerInvariantError("scheduler_invocation_deadline_type_is_invalid")
        deadline._assert_intact()
        try:
            persistence_authority = scheduler_port.persistence_authority
            if (
                not callable(getattr(scheduler_port, "claim_due_job", None))
                or scheduler_port.release_sha != deadline.binding.release_sha
                or not is_persistence_authority(persistence_authority)
            ):
                raise SchedulerInvariantError("scheduler_claimed_invocation_port_is_invalid")
        except SchedulerInvariantError:
            raise
        except Exception:
            raise SchedulerInvariantError("scheduler_claimed_invocation_port_is_invalid") from None
        object.__setattr__(self, "_deadline", deadline)
        object.__setattr__(self, "_scheduler_port", scheduler_port)
        object.__setattr__(self, "_persistence_authority", persistence_authority)
        object.__setattr__(self, "_consumed", False)
        object.__setattr__(self, "_settlement_baseline_monotonic", None)
        object.__setattr__(self, "_settlement_state", "pending")
        object.__setattr__(self, "_issuance", _SCHEDULER_INVOCATION_ISSUANCE)

    def __setattr__(self, _name: str, _value: object) -> Never:
        raise SchedulerInvariantError("scheduler_claimed_invocation_is_immutable")

    def __delattr__(self, _name: str) -> Never:
        raise SchedulerInvariantError("scheduler_claimed_invocation_is_immutable")

    def __copy__(self) -> Never:
        raise SchedulerInvariantError("scheduler_claimed_invocation_is_not_copyable")

    def __deepcopy__(self, _memo: object) -> Never:
        raise SchedulerInvariantError("scheduler_claimed_invocation_is_not_copyable")

    def __reduce__(self) -> Never:
        raise SchedulerInvariantError("scheduler_claimed_invocation_is_not_serializable")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> Never:
        raise SchedulerInvariantError("scheduler_claimed_invocation_is_not_serializable")

    @property
    def claim(self) -> ScheduledJobClaimV1:
        return self._deadline.binding._claim

    @property
    def outer_lease(self) -> WorkerLease:
        return self._deadline.binding._outer_lease

    @property
    def deadline(self) -> SchedulerInvocationDeadline:
        return self._deadline

    @property
    def binding(self) -> SchedulerInvocationBinding:
        return self._deadline.binding

    @property
    def persistence_authority(self) -> PersistenceAuthority:
        return self._persistence_authority

    def _assert_intact(self) -> None:
        try:
            if self._issuance is not _SCHEDULER_INVOCATION_ISSUANCE:
                raise SchedulerInvariantError("scheduler_claimed_invocation_is_not_issued")
            if type(self._deadline) is not SchedulerInvocationDeadline:
                raise SchedulerInvariantError("scheduler_invocation_deadline_type_is_invalid")
            self._deadline._assert_intact()
            if type(self._consumed) is not bool:
                raise SchedulerInvariantError("scheduler_claimed_invocation_state_is_invalid")
            if self._settlement_state not in {
                "pending",
                "ready",
                "authorized",
                "dispatched_complete",
                "dispatched_fail",
                "expired",
            }:
                raise SchedulerInvariantError("scheduler_invocation_settlement_state_is_invalid")
            baseline = self._settlement_baseline_monotonic
            if self._settlement_state == "pending":
                if baseline is not None:
                    raise SchedulerInvariantError(
                        "scheduler_invocation_settlement_state_is_invalid"
                    )
            else:
                if baseline is None:
                    raise SchedulerInvariantError(
                        "scheduler_invocation_settlement_state_is_invalid"
                    )
                _require_exact_monotonic(
                    baseline,
                    "scheduler_invocation_settlement_baseline_is_invalid",
                )
                if not self._consumed or baseline < self.deadline.rpc_started_monotonic:
                    raise SchedulerInvariantError(
                        "scheduler_invocation_settlement_state_is_invalid"
                    )
            if (
                not callable(getattr(self._scheduler_port, "claim_due_job", None))
                or self._scheduler_port.release_sha != self.binding.release_sha
                or self._scheduler_port.persistence_authority != self._persistence_authority
                or not is_persistence_authority(self._persistence_authority)
            ):
                raise SchedulerInvariantError("scheduler_claimed_invocation_port_is_invalid")
        except SchedulerInvariantError:
            raise
        except Exception:
            raise SchedulerInvariantError("scheduler_claimed_invocation_is_invalid") from None

    def _consume(self) -> SchedulerInvocationDeadline:
        self._assert_intact()
        if self._consumed:
            raise SchedulerInvariantError("scheduler_claimed_invocation_is_consumed")
        object.__setattr__(self, "_consumed", True)
        return self._deadline

    def _authorize_settlement(self, baseline_monotonic: float) -> None:
        self._assert_intact()
        _require_exact_monotonic(
            baseline_monotonic,
            "scheduler_invocation_settlement_baseline_is_invalid",
        )
        if (
            not self._consumed
            or self._settlement_state != "pending"
            or baseline_monotonic < self.deadline.rpc_started_monotonic
        ):
            raise SchedulerInvariantError("scheduler_invocation_settlement_state_is_invalid")
        object.__setattr__(self, "_settlement_baseline_monotonic", baseline_monotonic)
        object.__setattr__(self, "_settlement_state", "ready")


class SchedulerConvergenceClaimResult:
    """Canonical convergence receipt plus its optional one-shot invocation."""

    __slots__ = ("_receipt", "_invocation", "_issuance")
    _invocation: SchedulerClaimedInvocation | None
    _issuance: _SchedulerInvocationIssuance
    _receipt: SchedulerDefinitionConvergenceReceiptV1

    def __init__(
        self,
        *,
        receipt: SchedulerDefinitionConvergenceReceiptV1,
        invocation: SchedulerClaimedInvocation | None,
        _issuance: object,
    ) -> None:
        if _issuance is not _SCHEDULER_INVOCATION_ISSUANCE:
            raise SchedulerInvariantError("scheduler_convergence_result_is_not_issued")
        canonical_receipt = canonical_scheduler_convergence_receipt(receipt)
        if canonical_receipt.status == "claimed":
            if (
                type(invocation) is not SchedulerClaimedInvocation
                or canonical_receipt.claim is None
            ):
                raise SchedulerInvariantError("scheduler_convergence_invocation_is_invalid")
            invocation._assert_intact()
            if invocation.claim != canonical_receipt.claim:
                raise SchedulerInvariantError("scheduler_convergence_invocation_is_invalid")
        elif invocation is not None:
            raise SchedulerInvariantError("scheduler_convergence_invocation_is_invalid")
        object.__setattr__(self, "_receipt", canonical_receipt)
        object.__setattr__(self, "_invocation", invocation)
        object.__setattr__(self, "_issuance", _SCHEDULER_INVOCATION_ISSUANCE)

    def __setattr__(self, _name: str, _value: object) -> Never:
        raise SchedulerInvariantError("scheduler_convergence_result_is_immutable")

    def __delattr__(self, _name: str) -> Never:
        raise SchedulerInvariantError("scheduler_convergence_result_is_immutable")

    def __copy__(self) -> Never:
        raise SchedulerInvariantError("scheduler_convergence_result_is_not_copyable")

    def __deepcopy__(self, _memo: object) -> Never:
        raise SchedulerInvariantError("scheduler_convergence_result_is_not_copyable")

    def __reduce__(self) -> Never:
        raise SchedulerInvariantError("scheduler_convergence_result_is_not_serializable")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> Never:
        raise SchedulerInvariantError("scheduler_convergence_result_is_not_serializable")

    @property
    def receipt(self) -> SchedulerDefinitionConvergenceReceiptV1:
        self._assert_intact()
        return self._receipt

    @property
    def invocation(self) -> SchedulerClaimedInvocation | None:
        self._assert_intact()
        return self._invocation

    def _assert_intact(self) -> None:
        try:
            if self._issuance is not _SCHEDULER_INVOCATION_ISSUANCE:
                raise SchedulerInvariantError("scheduler_convergence_result_is_not_issued")
            receipt = canonical_scheduler_convergence_receipt(self._receipt)
            invocation = self._invocation
            if receipt.status == "claimed":
                if type(invocation) is not SchedulerClaimedInvocation or receipt.claim is None:
                    raise SchedulerInvariantError("scheduler_convergence_invocation_is_invalid")
                invocation._assert_intact()
                if invocation.claim != receipt.claim:
                    raise SchedulerInvariantError("scheduler_convergence_invocation_is_invalid")
            elif invocation is not None:
                raise SchedulerInvariantError("scheduler_convergence_invocation_is_invalid")
        except SchedulerInvariantError:
            raise
        except Exception:
            raise SchedulerInvariantError("scheduler_convergence_result_is_invalid") from None


class SchedulerInvocationSettlementAuthorization:
    """One-shot settlement start bound to the latest same-generation lease."""

    __slots__ = (
        "_invocation",
        "_runtime",
        "_outer_lease",
        "_observed_at",
        "_issuance",
    )
    _invocation: SchedulerClaimedInvocation
    _issuance: _SchedulerInvocationIssuance
    _observed_at: datetime
    _outer_lease: WorkerLease
    _runtime: SchedulerRuntimeCapability

    def __init__(
        self,
        *,
        invocation: SchedulerClaimedInvocation,
        runtime: SchedulerRuntimeCapability,
        outer_lease: WorkerLease,
        observed_at: datetime,
        _issuance: object,
    ) -> None:
        if _issuance is not _SCHEDULER_INVOCATION_ISSUANCE:
            raise SchedulerInvariantError("scheduler_settlement_authorization_is_not_issued")
        if type(invocation) is not SchedulerClaimedInvocation:
            raise SchedulerInvariantError("scheduler_claimed_invocation_is_not_issued")
        canonical_outer_lease = canonical_scheduler_outer_lease(outer_lease)
        canonical_observed_at = canonical_scheduler_datetime(
            observed_at,
            "scheduler_settlement_authorized_at",
        )
        object.__setattr__(self, "_invocation", invocation)
        object.__setattr__(self, "_runtime", runtime)
        object.__setattr__(self, "_outer_lease", canonical_outer_lease)
        object.__setattr__(self, "_observed_at", canonical_observed_at)
        object.__setattr__(self, "_issuance", _SCHEDULER_INVOCATION_ISSUANCE)

    def __setattr__(self, _name: str, _value: object) -> Never:
        raise SchedulerInvariantError("scheduler_settlement_authorization_is_immutable")

    def __delattr__(self, _name: str) -> Never:
        raise SchedulerInvariantError("scheduler_settlement_authorization_is_immutable")

    def __copy__(self) -> Never:
        raise SchedulerInvariantError("scheduler_settlement_authorization_is_not_copyable")

    def __deepcopy__(self, _memo: object) -> Never:
        raise SchedulerInvariantError("scheduler_settlement_authorization_is_not_copyable")

    def __reduce__(self) -> Never:
        raise SchedulerInvariantError("scheduler_settlement_authorization_is_not_serializable")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> Never:
        raise SchedulerInvariantError("scheduler_settlement_authorization_is_not_serializable")

    def _prepare_dispatch(
        self,
        transition: SchedulerInvocationSettlementTransition,
        *,
        fail_stop: FailStop,
        failure_reason_code: str | None = None,
        retryable: bool | None = None,
    ) -> tuple[DurableSchedulerPort, ScheduledJobClaimV1, WorkerLease]:
        if not callable(fail_stop):
            raise SchedulerInvariantError("scheduler_invocation_callable_is_invalid")
        if transition not in {"complete", "fail"}:
            raise SchedulerInvariantError("scheduler_settlement_transition_is_invalid")
        try:
            if self._issuance is not _SCHEDULER_INVOCATION_ISSUANCE:
                raise SchedulerInvariantError(
                    "scheduler_settlement_authorization_is_not_issued"
                )
            if type(self._invocation) is not SchedulerClaimedInvocation:
                raise SchedulerInvariantError("scheduler_claimed_invocation_is_not_issued")
            self._invocation._assert_intact()
            _assert_settlement_clock_source(self._invocation)
        except SchedulerInvariantError:
            _invoke_fail_stop(fail_stop, "scheduler_settlement_provenance_invalid")
        except Exception:
            _invoke_fail_stop(fail_stop, "scheduler_settlement_provenance_invalid")
        if self._invocation._settlement_state != "authorized":
            raise SchedulerInvariantError("scheduler_invocation_settlement_is_consumed")
        if transition == "fail":
            if failure_reason_code is None or retryable is None:
                raise SchedulerInvariantError("scheduler_settlement_failure_is_invalid")
            if (
                retryable
                and failure_reason_code
                not in SCHEDULER_RETRYABLE_REASONS[self._invocation.binding.job_key]
            ):
                raise SchedulerInvariantError(
                    "scheduler_retry_classification_is_not_allowed"
                )
        elif failure_reason_code is not None or retryable is not None:
            raise SchedulerInvariantError("scheduler_settlement_completion_is_invalid")
        try:
            _assert_settlement_runtime_identity(self._runtime, self._invocation)
            canonical_observed_at = canonical_scheduler_datetime(
                self._observed_at,
                "scheduler_settlement_authorized_at",
            )
            canonical_outer_lease = canonical_scheduler_outer_lease(self._outer_lease)
            current_outer_lease = canonical_scheduler_outer_lease(
                self._runtime.current_outer_lease()
            )
        except SchedulerInvariantError:
            _invoke_fail_stop(fail_stop, "scheduler_settlement_provenance_invalid")
        except Exception:
            _invoke_fail_stop(fail_stop, "scheduler_settlement_provenance_invalid")
        try:
            current = _read_monotonic(
                self._invocation.deadline._rpc_start._monotonic_clock
            )
        except SchedulerInvariantError:
            _invoke_fail_stop(fail_stop, "scheduler_settlement_clock_corrupt")
        try:
            _assert_settlement_runtime_identity(self._runtime, self._invocation)
        except SchedulerInvariantError:
            _invoke_fail_stop(fail_stop, "scheduler_settlement_provenance_invalid")
        except Exception:
            _invoke_fail_stop(fail_stop, "scheduler_settlement_provenance_invalid")
        try:
            final_current = _read_monotonic(
                self._invocation.deadline._rpc_start._monotonic_clock
            )
        except SchedulerInvariantError:
            _invoke_fail_stop(fail_stop, "scheduler_settlement_clock_corrupt")
        if final_current < current:
            _invoke_fail_stop(fail_stop, "scheduler_settlement_clock_corrupt")
        try:
            baseline = self._invocation._settlement_baseline_monotonic
            if baseline is None or final_current < baseline:
                raise SchedulerInvariantError("scheduler_settlement_clock_corrupt")
            current_observed_at = self._invocation.deadline.claim_observed_at + timedelta(
                seconds=final_current
                - self._invocation.deadline.rpc_started_monotonic
            )
            if current_observed_at < canonical_observed_at:
                raise SchedulerInvariantError("scheduler_settlement_clock_corrupt")
        except SchedulerInvariantError:
            _invoke_fail_stop(fail_stop, "scheduler_settlement_clock_corrupt")
        except (OverflowError, TypeError, ValueError):
            _invoke_fail_stop(fail_stop, "scheduler_settlement_clock_corrupt")
        try:
            _refresh_same_generation_outer_lease(
                canonical_outer_lease,
                current_outer_lease,
                observed_at=current_observed_at,
            )
        except SchedulerInvariantError:
            _invoke_fail_stop(fail_stop, "scheduler_settlement_provenance_invalid")
        if final_current >= _monotonic_settlement_start_deadline(
            self._invocation.deadline
        ):
            object.__setattr__(self._invocation, "_settlement_state", "expired")
            raise SchedulerInvocationSettlementWindowExceeded(
                self._invocation.binding.job_key
            )
        object.__setattr__(
            self._invocation,
            "_settlement_state",
            "dispatched_complete" if transition == "complete" else "dispatched_fail",
        )
        return (
            self._invocation._scheduler_port,
            self._invocation.claim,
            current_outer_lease,
        )


@dataclass(frozen=True, slots=True)
class SchedulerDeadlineFailure:
    reason_code: str
    retryable: bool


SCHEDULER_DEADLINE_FAILURES: Mapping[SchedulerJobKey, SchedulerDeadlineFailure] = MappingProxyType(
    {
        "operations.commands": SchedulerDeadlineFailure(
            "command_poll_retryable",
            True,
        ),
        "operations.execution": SchedulerDeadlineFailure(
            "execution_deadline_effect_unknown",
            False,
        ),
        "operations.settlement": SchedulerDeadlineFailure(
            "settlement_deadline_effect_unknown",
            False,
        ),
        "operations.reconciliation": SchedulerDeadlineFailure(
            "reconciliation_poll_retryable",
            True,
        ),
        "operations.outbox": SchedulerDeadlineFailure(
            "outbox_poll_retryable",
            True,
        ),
    }
)
if frozenset(SCHEDULER_DEADLINE_FAILURES) != SCHEDULER_JOB_KEYS:
    raise RuntimeError("scheduler_deadline_failure_policy_is_incomplete")


class SchedulerInvocationPermitRevoked(BaseException):
    """Stop an effect even inside a broad ``except Exception`` handler."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class SchedulerInvocationDeadlineExceeded(KnownFailClosedError):
    def __init__(self, job_key: SchedulerJobKey) -> None:
        failure = SCHEDULER_DEADLINE_FAILURES[job_key]
        self.job_key = job_key
        self.reason_code = failure.reason_code
        self.retryable = failure.retryable
        super().__init__("durable_scheduler", failure.reason_code)


class SchedulerInvocationSettlementWindowExceeded(KnownFailClosedError):
    def __init__(self, job_key: SchedulerJobKey) -> None:
        self.job_key = job_key
        self.reason_code = "scheduler_settlement_window_expired"
        super().__init__("durable_scheduler", self.reason_code)


class SchedulerInvocationFailStopReturned(BaseException):
    """A configured fail-stop violated its non-returning contract."""


class _SchedulerDispatchRegistrySeal:
    """Opaque proof that one exact handler registry was factory-issued.

    Generic scheduler runners deliberately receive no seal.  The production
    infrastructure factory is the only allow-listed caller of the private
    issuance function below, so an arbitrary handler cannot make itself the
    effect issuer merely by supplying an otherwise valid runtime capability.
    """

    __slots__ = ("_runtime", "_runtime_provenance", "_entries", "_issuance")
    _entries: tuple[tuple[SchedulerJobKey, object, object], ...]
    _issuance: _SchedulerInvocationIssuance
    _runtime: SchedulerRuntimeCapability
    _runtime_provenance: object

    def __init__(
        self,
        runtime: SchedulerRuntimeCapability,
        entries: tuple[tuple[SchedulerJobKey, object, object], ...],
        *,
        runtime_provenance: object,
        _issuance: object,
    ) -> None:
        if _issuance is not _SCHEDULER_INVOCATION_ISSUANCE:
            raise SchedulerInvariantError("scheduler_dispatch_registry_seal_is_not_issued")
        if type(entries) is not tuple or len(entries) != len(SCHEDULER_JOB_KEYS):
            raise SchedulerInvariantError("scheduler_dispatch_registry_is_invalid")
        seen: set[SchedulerJobKey] = set()
        canonical: list[tuple[SchedulerJobKey, object, object]] = []
        for entry in entries:
            if type(entry) is not tuple or len(entry) != 3:
                raise SchedulerInvariantError("scheduler_dispatch_registry_is_invalid")
            job_key, handler, validator = entry
            if (
                job_key not in SCHEDULER_JOB_KEYS
                or job_key in seen
                or not callable(handler)
                or not callable(validator)
            ):
                raise SchedulerInvariantError("scheduler_dispatch_registry_is_invalid")
            seen.add(job_key)
            canonical.append((job_key, handler, validator))
        if seen != SCHEDULER_JOB_KEYS:
            raise SchedulerInvariantError("scheduler_dispatch_registry_is_invalid")
        _assert_scheduler_dispatch_runtime_provenance(runtime, runtime_provenance)
        object.__setattr__(self, "_runtime", runtime)
        object.__setattr__(self, "_runtime_provenance", runtime_provenance)
        object.__setattr__(self, "_entries", tuple(canonical))
        object.__setattr__(self, "_issuance", _SCHEDULER_INVOCATION_ISSUANCE)

    def __setattr__(self, _name: str, _value: object) -> Never:
        raise SchedulerInvariantError("scheduler_dispatch_registry_seal_is_immutable")

    def __delattr__(self, _name: str) -> Never:
        raise SchedulerInvariantError("scheduler_dispatch_registry_seal_is_immutable")

    def __copy__(self) -> Never:
        raise SchedulerInvariantError("scheduler_dispatch_registry_seal_is_not_copyable")

    def __deepcopy__(self, _memo: object) -> Never:
        raise SchedulerInvariantError("scheduler_dispatch_registry_seal_is_not_copyable")

    def __reduce__(self) -> Never:
        raise SchedulerInvariantError("scheduler_dispatch_registry_seal_is_not_serializable")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> Never:
        raise SchedulerInvariantError("scheduler_dispatch_registry_seal_is_not_serializable")

    def assert_authorized(
        self,
        runtime: SchedulerRuntimeCapability,
        *,
        job_key: SchedulerJobKey,
        handler: object,
        validator: object | None = None,
    ) -> None:
        try:
            if (
                self._issuance is not _SCHEDULER_INVOCATION_ISSUANCE
                or self._runtime is not runtime
                or type(self._entries) is not tuple
                or len(self._entries) != len(SCHEDULER_JOB_KEYS)
            ):
                raise SchedulerInvariantError("scheduler_dispatch_registry_seal_is_invalid")
            _assert_scheduler_dispatch_runtime_provenance(
                runtime,
                self._runtime_provenance,
            )
            matches = tuple(
                entry
                for entry in self._entries
                if entry[0] == job_key and entry[1] is handler
            )
            if len(matches) != 1 or (
                validator is not None and matches[0][2] is not validator
            ):
                raise SchedulerInvariantError("scheduler_dispatch_registry_binding_mismatch")
        except SchedulerInvariantError:
            raise
        except Exception:
            raise SchedulerInvariantError("scheduler_dispatch_registry_seal_is_invalid") from None


def _issue_scheduler_dispatch_registry_seal(
    runtime: SchedulerRuntimeCapability,
    entries: tuple[tuple[SchedulerJobKey, object, object], ...],
    *,
    runtime_provenance: object,
) -> _SchedulerDispatchRegistrySeal:
    """Private integration hook; production usage is guarded by an AST policy."""

    return _SchedulerDispatchRegistrySeal(
        runtime,
        entries,
        runtime_provenance=runtime_provenance,
        _issuance=_SCHEDULER_INVOCATION_ISSUANCE,
    )


def _assert_scheduler_dispatch_runtime_provenance(
    runtime: SchedulerRuntimeCapability,
    runtime_provenance: object,
) -> None:
    try:
        verifier = getattr(runtime, "_assert_dispatch_registry_provenance", None)
        if not callable(verifier):
            raise SchedulerInvariantError(
                "scheduler_dispatch_registry_runtime_provenance_is_invalid"
            )
        verifier(runtime_provenance)
    except SchedulerInvariantError:
        raise
    except Exception:
        raise SchedulerInvariantError(
            "scheduler_dispatch_registry_runtime_provenance_is_invalid"
        ) from None


class SchedulerInvocationPermit:
    """Single-invocation effect permit with a fixed monotonic cutoff."""

    __slots__ = (
        "_deadline",
        "_effect_issuer",
        "_dispatch_registry_seal",
        "_effect_runtime",
        "_monotonic_clock",
        "_last_monotonic",
        "_revocation_reason",
        "_issuance",
    )
    _deadline: SchedulerInvocationDeadline
    _effect_issuer: object
    _dispatch_registry_seal: _SchedulerDispatchRegistrySeal | None
    _effect_runtime: SchedulerRuntimeCapability | None
    _issuance: _SchedulerInvocationIssuance
    _last_monotonic: float
    _monotonic_clock: MonotonicClock
    _revocation_reason: str | None

    def __init__(
        self,
        deadline: SchedulerInvocationDeadline,
        *,
        effect_issuer: object,
        effect_runtime: SchedulerRuntimeCapability | None,
        dispatch_registry_seal: _SchedulerDispatchRegistrySeal | None,
        _issuance: object,
    ) -> None:
        if _issuance is not _SCHEDULER_INVOCATION_ISSUANCE:
            raise SchedulerInvariantError("scheduler_invocation_permit_is_not_issued")
        if type(deadline) is not SchedulerInvocationDeadline:
            raise SchedulerInvariantError("scheduler_invocation_deadline_type_is_invalid")
        if not callable(effect_issuer):
            raise SchedulerInvariantError("scheduler_invocation_effect_issuer_is_invalid")
        if (effect_runtime is None) != (dispatch_registry_seal is None):
            raise SchedulerInvariantError("scheduler_dispatch_registry_authority_is_invalid")
        if dispatch_registry_seal is not None:
            if type(dispatch_registry_seal) is not _SchedulerDispatchRegistrySeal:
                raise SchedulerInvariantError("scheduler_dispatch_registry_seal_is_invalid")
            dispatch_registry_seal.assert_authorized(
                cast(SchedulerRuntimeCapability, effect_runtime),
                job_key=deadline.binding.job_key,
                handler=effect_issuer,
            )
        deadline._assert_intact()
        monotonic_clock = deadline._rpc_start._monotonic_clock
        initial = _read_monotonic(monotonic_clock)
        if initial < deadline.rpc_started_monotonic:
            raise SchedulerInvariantError("scheduler_invocation_clock_moved_backwards")
        object.__setattr__(self, "_deadline", deadline)
        object.__setattr__(self, "_effect_issuer", effect_issuer)
        object.__setattr__(self, "_dispatch_registry_seal", dispatch_registry_seal)
        object.__setattr__(self, "_effect_runtime", effect_runtime)
        object.__setattr__(self, "_monotonic_clock", monotonic_clock)
        object.__setattr__(self, "_last_monotonic", initial)
        object.__setattr__(self, "_revocation_reason", None)
        object.__setattr__(self, "_issuance", _SCHEDULER_INVOCATION_ISSUANCE)

    def __setattr__(self, _name: str, _value: object) -> Never:
        raise SchedulerInvariantError("scheduler_invocation_permit_is_immutable")

    def __delattr__(self, _name: str) -> Never:
        raise SchedulerInvariantError("scheduler_invocation_permit_is_immutable")

    def __copy__(self) -> Never:
        raise SchedulerInvariantError("scheduler_invocation_permit_is_not_copyable")

    def __deepcopy__(self, _memo: object) -> Never:
        raise SchedulerInvariantError("scheduler_invocation_permit_is_not_copyable")

    def __reduce__(self) -> Never:
        raise SchedulerInvariantError("scheduler_invocation_permit_is_not_serializable")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> Never:
        raise SchedulerInvariantError("scheduler_invocation_permit_is_not_serializable")

    @property
    def revocation_reason(self) -> str | None:
        return self._revocation_reason

    def assert_effect_allowed(
        self,
        *,
        expected_binding: SchedulerInvocationBinding,
    ) -> None:
        try:
            self._assert_intact()
            if type(expected_binding) is not SchedulerInvocationBinding:
                raise SchedulerInvariantError("scheduler_invocation_binding_is_invalid")
            expected_binding._assert_intact()
            if self._deadline.binding is not expected_binding:
                raise SchedulerInvariantError("scheduler_invocation_permit_binding_mismatch")
        except SchedulerInvariantError:
            self._revoke_first_wins("binding_mismatch")
            raise SchedulerInvocationPermitRevoked("binding_mismatch") from None
        reason = self._revocation_reason
        if reason is not None:
            raise SchedulerInvocationPermitRevoked(reason)
        state = self._time_state()
        if state == "clock_corrupt":
            self._revoke_first_wins("clock_corrupt")
        elif state == "deadline":
            self._revoke_first_wins("deadline")
        reason = self._revocation_reason
        if reason is not None:
            raise SchedulerInvocationPermitRevoked(reason)

    def _time_state(self) -> SchedulerInvocationTimeState:
        reason = self._revocation_reason
        if reason == "clock_corrupt":
            return "clock_corrupt"
        if reason == "deadline":
            return "deadline"
        if reason not in {None, "handler_exit"}:
            return "clock_corrupt"
        try:
            current = _read_monotonic(self._monotonic_clock)
        except SchedulerInvariantError:
            return "clock_corrupt"
        if current < self._last_monotonic:
            return "clock_corrupt"
        object.__setattr__(self, "_last_monotonic", current)
        if current >= self._deadline.monotonic_deadline:
            return "deadline"
        return "active"

    def _revoke_first_wins(self, reason: str) -> None:
        if reason not in {
            "binding_mismatch",
            "clock_corrupt",
            "deadline",
            "external_cancel",
            "handler_exit",
        }:
            raise SchedulerInvariantError("scheduler_invocation_revocation_reason_is_invalid")
        if self._revocation_reason is None:
            object.__setattr__(self, "_revocation_reason", reason)

    def _assert_intact(self) -> None:
        try:
            if self._issuance is not _SCHEDULER_INVOCATION_ISSUANCE:
                raise SchedulerInvariantError("scheduler_invocation_permit_is_not_issued")
            if type(self._deadline) is not SchedulerInvocationDeadline:
                raise SchedulerInvariantError("scheduler_invocation_deadline_type_is_invalid")
            if not callable(self._effect_issuer):
                raise SchedulerInvariantError(
                    "scheduler_invocation_effect_issuer_is_invalid"
                )
            if (self._effect_runtime is None) != (
                self._dispatch_registry_seal is None
            ):
                raise SchedulerInvariantError(
                    "scheduler_dispatch_registry_authority_is_invalid"
                )
            if self._dispatch_registry_seal is not None:
                if type(self._dispatch_registry_seal) is not _SchedulerDispatchRegistrySeal:
                    raise SchedulerInvariantError(
                        "scheduler_dispatch_registry_seal_is_invalid"
                    )
                self._dispatch_registry_seal.assert_authorized(
                    cast(SchedulerRuntimeCapability, self._effect_runtime),
                    job_key=self._deadline.binding.job_key,
                    handler=self._effect_issuer,
                )
            self._deadline._assert_intact()
            if self._monotonic_clock is not self._deadline._rpc_start._monotonic_clock:
                raise SchedulerInvariantError("scheduler_invocation_permit_clock_mismatch")
            _require_exact_monotonic(
                self._last_monotonic,
                "scheduler_invocation_clock_is_invalid",
            )
            if self._last_monotonic < self._deadline.rpc_started_monotonic:
                raise SchedulerInvariantError("scheduler_invocation_clock_moved_backwards")
            if self._revocation_reason not in {
                None,
                "binding_mismatch",
                "clock_corrupt",
                "deadline",
                "external_cancel",
                "handler_exit",
            }:
                raise SchedulerInvariantError("scheduler_invocation_revocation_reason_is_invalid")
        except SchedulerInvariantError:
            raise
        except Exception:
            raise SchedulerInvariantError("scheduler_invocation_permit_is_invalid") from None


def require_scheduler_invocation_permit(
    value: object,
    *,
    expected_binding: SchedulerInvocationBinding,
) -> SchedulerInvocationPermit:
    """Validate one exact issued permit at the final effect dispatch boundary."""

    if type(value) is not SchedulerInvocationPermit:
        raise SchedulerInvocationPermitRevoked("permit_not_issued")
    permit = value
    permit.assert_effect_allowed(expected_binding=expected_binding)
    return permit


class SchedulerInvocationEffectAuthorization:
    """Sealed authority for one scheduler job's downstream effects.

    The authorization deliberately exposes no runtime, permit, or binding
    getters.  Every effect boundary must pass it back through
    ``require_scheduler_invocation_effect_authorization`` so the original
    runtime graph, outer fencing generation, binding identity, and monotonic
    deadline are all revalidated immediately before dispatch.
    """

    __slots__ = (
        "_runtime",
        "_permit",
        "_binding",
        "_expected_job_key",
        "_effect_issuer",
        "_dispatch_registry_seal",
        "_captured_outer_lease",
        "_scheduler_port",
        "_persistence_authority",
        "_issuance",
    )
    _binding: SchedulerInvocationBinding
    _captured_outer_lease: WorkerLease
    _expected_job_key: SchedulerJobKey
    _effect_issuer: object
    _dispatch_registry_seal: _SchedulerDispatchRegistrySeal
    _issuance: _SchedulerInvocationIssuance
    _permit: SchedulerInvocationPermit
    _persistence_authority: PersistenceAuthority
    _runtime: SchedulerRuntimeCapability
    _scheduler_port: DurableSchedulerPort

    def __init__(
        self,
        *,
        runtime: SchedulerRuntimeCapability,
        permit: SchedulerInvocationPermit,
        binding: SchedulerInvocationBinding,
        expected_job_key: SchedulerJobKey,
        effect_issuer: object,
        dispatch_registry_seal: _SchedulerDispatchRegistrySeal,
        _issuance: object,
    ) -> None:
        if _issuance is not _SCHEDULER_INVOCATION_ISSUANCE:
            raise SchedulerInvariantError(
                "scheduler_effect_authorization_is_not_issued"
            )
        if type(permit) is not SchedulerInvocationPermit:
            raise SchedulerInvariantError("scheduler_invocation_permit_is_not_issued")
        if type(binding) is not SchedulerInvocationBinding:
            raise SchedulerInvariantError("scheduler_invocation_binding_is_not_issued")
        if expected_job_key not in SCHEDULER_JOB_KEYS:
            raise SchedulerInvariantError("scheduler_effect_job_key_is_invalid")
        if permit._effect_issuer is not effect_issuer:
            permit._revoke_first_wins("binding_mismatch")
            raise SchedulerInvocationPermitRevoked("binding_mismatch")
        if permit._effect_runtime is not runtime:
            permit._revoke_first_wins("binding_mismatch")
            raise SchedulerInvocationPermitRevoked("binding_mismatch")
        if (
            type(dispatch_registry_seal) is not _SchedulerDispatchRegistrySeal
            or permit._dispatch_registry_seal is not dispatch_registry_seal
        ):
            permit._revoke_first_wins("binding_mismatch")
            raise SchedulerInvocationPermitRevoked("binding_mismatch")
        try:
            dispatch_registry_seal.assert_authorized(
                runtime,
                job_key=expected_job_key,
                handler=effect_issuer,
            )
        except SchedulerInvariantError:
            permit._revoke_first_wins("binding_mismatch")
            raise SchedulerInvocationPermitRevoked("binding_mismatch") from None
        object.__setattr__(self, "_runtime", runtime)
        object.__setattr__(self, "_permit", permit)
        object.__setattr__(self, "_binding", binding)
        object.__setattr__(self, "_expected_job_key", expected_job_key)
        object.__setattr__(self, "_effect_issuer", effect_issuer)
        object.__setattr__(self, "_dispatch_registry_seal", dispatch_registry_seal)
        object.__setattr__(
            self,
            "_captured_outer_lease",
            canonical_scheduler_outer_lease(binding._outer_lease),
        )
        object.__setattr__(self, "_scheduler_port", runtime.scheduler_port)
        object.__setattr__(
            self,
            "_persistence_authority",
            runtime.persistence_authority,
        )
        object.__setattr__(self, "_issuance", _SCHEDULER_INVOCATION_ISSUANCE)

    def __setattr__(self, _name: str, _value: object) -> Never:
        raise SchedulerInvariantError("scheduler_effect_authorization_is_immutable")

    def __delattr__(self, _name: str) -> Never:
        raise SchedulerInvariantError("scheduler_effect_authorization_is_immutable")

    def __copy__(self) -> Never:
        raise SchedulerInvariantError("scheduler_effect_authorization_is_not_copyable")

    def __deepcopy__(self, _memo: object) -> Never:
        raise SchedulerInvariantError("scheduler_effect_authorization_is_not_copyable")

    def __reduce__(self) -> Never:
        raise SchedulerInvariantError(
            "scheduler_effect_authorization_is_not_serializable"
        )

    def __reduce_ex__(self, _protocol: SupportsIndex) -> Never:
        raise SchedulerInvariantError(
            "scheduler_effect_authorization_is_not_serializable"
        )

    def _require_effect(self, expected_job_key: SchedulerJobKey) -> None:
        try:
            if self._issuance is not _SCHEDULER_INVOCATION_ISSUANCE:
                raise SchedulerInvariantError(
                    "scheduler_effect_authorization_is_not_issued"
                )
            if type(self._permit) is not SchedulerInvocationPermit:
                raise SchedulerInvariantError(
                    "scheduler_invocation_permit_is_not_issued"
                )
            if type(self._binding) is not SchedulerInvocationBinding:
                raise SchedulerInvariantError(
                    "scheduler_invocation_binding_is_not_issued"
                )
            if self._permit._effect_issuer is not self._effect_issuer:
                raise SchedulerInvariantError(
                    "scheduler_effect_issuer_identity_mismatch"
                )
            if self._permit._effect_runtime is not self._runtime:
                raise SchedulerInvariantError(
                    "scheduler_effect_runtime_identity_mismatch"
                )
            if (
                type(self._dispatch_registry_seal)
                is not _SchedulerDispatchRegistrySeal
                or self._permit._dispatch_registry_seal
                is not self._dispatch_registry_seal
            ):
                raise SchedulerInvariantError(
                    "scheduler_dispatch_registry_seal_identity_mismatch"
                )
            self._dispatch_registry_seal.assert_authorized(
                self._runtime,
                job_key=expected_job_key,
                handler=self._effect_issuer,
            )
            if (
                expected_job_key not in SCHEDULER_JOB_KEYS
                or self._expected_job_key != expected_job_key
                or self._binding.job_key != expected_job_key
            ):
                raise SchedulerInvariantError("scheduler_effect_job_binding_mismatch")
            self._runtime.assert_intact()
            scheduler_port = self._runtime.scheduler_port
            if (
                scheduler_port is not self._scheduler_port
                or self._runtime.persistence_authority != self._persistence_authority
                or self._runtime.account_id != self._binding.account_id
                or self._runtime.holder_id != self._binding.holder_id
                or self._runtime.release_sha != self._binding.release_sha
                or scheduler_port.release_sha != self._binding.release_sha
                or scheduler_port.persistence_authority
                != self._persistence_authority
            ):
                raise SchedulerInvariantError(
                    "scheduler_effect_runtime_identity_mismatch"
                )
            captured_outer_lease = canonical_scheduler_outer_lease(
                self._captured_outer_lease
            )
            if captured_outer_lease != canonical_scheduler_outer_lease(
                self._binding._outer_lease
            ):
                raise SchedulerInvariantError(
                    "scheduler_effect_outer_lease_binding_mismatch"
                )
            current_outer_lease = canonical_scheduler_outer_lease(
                self._runtime.current_outer_lease()
            )
            self._permit.assert_effect_allowed(expected_binding=self._binding)
            observed_at = self._binding._claim.observed_at + timedelta(
                seconds=(
                    self._permit._last_monotonic
                    - self._permit._deadline.rpc_started_monotonic
                )
            )
            _refresh_same_generation_outer_lease(
                captured_outer_lease,
                current_outer_lease,
                observed_at=observed_at,
            )
            self._permit.assert_effect_allowed(expected_binding=self._binding)
        except SchedulerInvocationPermitRevoked:
            raise
        except (SchedulerInvariantError, AttributeError, OverflowError, TypeError, ValueError):
            self._permit._revoke_first_wins("binding_mismatch")
            raise SchedulerInvocationPermitRevoked("binding_mismatch") from None


def issue_scheduler_invocation_effect_authorization(
    runtime: SchedulerRuntimeCapability,
    permit: SchedulerInvocationPermit,
    invocation_binding: SchedulerInvocationBinding,
    *,
    expected_job_key: SchedulerJobKey,
    effect_issuer: object,
    dispatch_registry_seal: _SchedulerDispatchRegistrySeal,
) -> SchedulerInvocationEffectAuthorization:
    """Bind one active invocation to one exact downstream job authority."""

    authorization = SchedulerInvocationEffectAuthorization(
        runtime=runtime,
        permit=permit,
        binding=invocation_binding,
        expected_job_key=expected_job_key,
        effect_issuer=effect_issuer,
        dispatch_registry_seal=dispatch_registry_seal,
        _issuance=_SCHEDULER_INVOCATION_ISSUANCE,
    )
    authorization._require_effect(expected_job_key)
    return authorization


def require_scheduler_invocation_effect_authorization(
    value: object,
    *,
    expected_job_key: SchedulerJobKey,
) -> SchedulerInvocationEffectAuthorization:
    """Revalidate a sealed authorization at an exact effect boundary."""

    if type(value) is not SchedulerInvocationEffectAuthorization:
        raise SchedulerInvocationPermitRevoked("permit_not_issued")
    authorization = value
    authorization._require_effect(expected_job_key)
    return authorization


def begin_scheduler_invocation_settlement(
    runtime: SchedulerRuntimeCapability,
    invocation: SchedulerClaimedInvocation,
    *,
    fail_stop: FailStop,
) -> SchedulerInvocationSettlementAuthorization:
    """Consume the one settlement start after handler termination is observed."""

    if not callable(fail_stop):
        raise SchedulerInvariantError("scheduler_invocation_callable_is_invalid")
    if type(invocation) is not SchedulerClaimedInvocation:
        _invoke_fail_stop(fail_stop, "scheduler_settlement_provenance_invalid")
    try:
        _assert_settlement_runtime_identity(runtime, invocation)
        invocation._assert_intact()
        _assert_settlement_clock_source(invocation)
    except (SchedulerInvariantError, AttributeError, TypeError):
        _invoke_fail_stop(fail_stop, "scheduler_settlement_provenance_invalid")
    if invocation._settlement_state == "pending":
        _invoke_fail_stop(fail_stop, "scheduler_settlement_provenance_invalid")
    if invocation._settlement_state != "ready":
        raise SchedulerInvariantError("scheduler_invocation_settlement_is_consumed")
    baseline = invocation._settlement_baseline_monotonic
    try:
        current = _read_monotonic(invocation.deadline._rpc_start._monotonic_clock)
    except SchedulerInvariantError:
        _invoke_fail_stop(fail_stop, "scheduler_settlement_clock_corrupt")
    if baseline is None or current < baseline:
        _invoke_fail_stop(fail_stop, "scheduler_settlement_clock_corrupt")
    try:
        current_outer_lease = canonical_scheduler_outer_lease(
            runtime.current_outer_lease()
        )
        _assert_settlement_runtime_identity(runtime, invocation)
    except (SchedulerInvariantError, AttributeError, OverflowError, TypeError, ValueError):
        _invoke_fail_stop(fail_stop, "scheduler_settlement_provenance_invalid")
    try:
        final_current = _read_monotonic(invocation.deadline._rpc_start._monotonic_clock)
    except SchedulerInvariantError:
        _invoke_fail_stop(fail_stop, "scheduler_settlement_clock_corrupt")
    if final_current < current or final_current < baseline:
        _invoke_fail_stop(fail_stop, "scheduler_settlement_clock_corrupt")
    try:
        _assert_settlement_runtime_identity(runtime, invocation)
    except SchedulerInvariantError:
        _invoke_fail_stop(fail_stop, "scheduler_settlement_provenance_invalid")
    try:
        sealed_current = _read_monotonic(
            invocation.deadline._rpc_start._monotonic_clock
        )
    except SchedulerInvariantError:
        _invoke_fail_stop(fail_stop, "scheduler_settlement_clock_corrupt")
    if sealed_current < final_current or sealed_current < baseline:
        _invoke_fail_stop(fail_stop, "scheduler_settlement_clock_corrupt")
    try:
        observed_at = invocation.deadline.claim_observed_at + timedelta(
            seconds=sealed_current - invocation.deadline.rpc_started_monotonic
        )
    except (OverflowError, TypeError, ValueError):
        _invoke_fail_stop(fail_stop, "scheduler_settlement_clock_corrupt")
    try:
        current_outer_lease = _refresh_same_generation_outer_lease(
            invocation.outer_lease,
            current_outer_lease,
            observed_at=observed_at,
        )
    except SchedulerInvariantError:
        _invoke_fail_stop(fail_stop, "scheduler_settlement_provenance_invalid")
    if sealed_current >= _monotonic_settlement_start_deadline(invocation.deadline):
        object.__setattr__(invocation, "_settlement_state", "expired")
        raise SchedulerInvocationSettlementWindowExceeded(invocation.binding.job_key)
    object.__setattr__(invocation, "_settlement_state", "authorized")
    try:
        return SchedulerInvocationSettlementAuthorization(
            invocation=invocation,
            runtime=runtime,
            outer_lease=current_outer_lease,
            observed_at=observed_at,
            _issuance=_SCHEDULER_INVOCATION_ISSUANCE,
        )
    except SchedulerInvariantError:
        _invoke_fail_stop(fail_stop, "scheduler_settlement_provenance_invalid")


async def complete_scheduler_invocation_settlement(
    authorization: SchedulerInvocationSettlementAuthorization,
    *,
    result_sha256: str,
    fail_stop: FailStop,
) -> ScheduledJobCompletionReceiptV1:
    """Start exactly one completion RPC through an unexpired authorization."""

    if not callable(fail_stop):
        raise SchedulerInvariantError("scheduler_invocation_callable_is_invalid")
    if type(authorization) is not SchedulerInvocationSettlementAuthorization:
        _invoke_fail_stop(fail_stop, "scheduler_settlement_provenance_invalid")
    _require_scheduler_sha256(result_sha256, "scheduler_completion_result_sha256")
    scheduler_port, claim, outer_lease = authorization._prepare_dispatch(
        "complete",
        fail_stop=fail_stop,
    )
    return await scheduler_port.complete_job_run(
        claim,
        outer_lease=outer_lease,
        result_sha256=result_sha256,
    )


async def fail_scheduler_invocation_settlement(
    authorization: SchedulerInvocationSettlementAuthorization,
    *,
    failure_reason_code: str,
    failure_sha256: str,
    retryable: bool,
    fail_stop: FailStop,
) -> ScheduledJobFailureReceiptV1:
    """Start exactly one failure RPC through an unexpired authorization."""

    if not callable(fail_stop):
        raise SchedulerInvariantError("scheduler_invocation_callable_is_invalid")
    if type(authorization) is not SchedulerInvocationSettlementAuthorization:
        _invoke_fail_stop(fail_stop, "scheduler_settlement_provenance_invalid")
    _require_scheduler_reason(
        failure_reason_code,
        "scheduler_failure_reason_code",
    )
    _require_scheduler_sha256(failure_sha256, "scheduler_failure_sha256")
    if type(retryable) is not bool:
        raise SchedulerInvariantError("scheduler_retryable_flag_is_invalid")
    scheduler_port, claim, outer_lease = authorization._prepare_dispatch(
        "fail",
        fail_stop=fail_stop,
        failure_reason_code=failure_reason_code,
        retryable=retryable,
    )
    return await scheduler_port.fail_job_run(
        claim,
        outer_lease=outer_lease,
        failure_reason_code=failure_reason_code,
        failure_sha256=failure_sha256,
        retryable=retryable,
    )


async def claim_scheduler_invocation(
    runtime: SchedulerRuntimeCapability,
) -> SchedulerClaimedInvocation | None:
    """Capture timing and claim a job through one attested runtime operation."""

    return await _claim_scheduler_invocation_with_clock(
        runtime,
        monotonic_clock=monotonic,
    )


async def _claim_scheduler_invocation_with_clock(
    runtime: SchedulerRuntimeCapability,
    *,
    monotonic_clock: MonotonicClock,
) -> SchedulerClaimedInvocation | None:
    if not callable(monotonic_clock):
        raise SchedulerInvariantError("scheduler_invocation_clock_is_invalid")
    runtime.assert_intact()
    scheduler_port = runtime.scheduler_port
    runtime_identity = (
        runtime.account_id,
        runtime.holder_id,
        runtime.release_sha,
        runtime.persistence_authority,
    )
    captured_outer_lease = canonical_scheduler_outer_lease(runtime.current_outer_lease())
    if (
        captured_outer_lease.account_id != runtime.account_id
        or captured_outer_lease.holder_id != runtime.holder_id
        or scheduler_port.release_sha != runtime.release_sha
        or scheduler_port.persistence_authority != runtime.persistence_authority
    ):
        raise SchedulerInvariantError("scheduler_invocation_runtime_identity_mismatch")
    rpc_start = SchedulerClaimRpcStart(
        monotonic_clock=monotonic_clock,
        started_monotonic=_read_monotonic(monotonic_clock),
        _issuance=_SCHEDULER_INVOCATION_ISSUANCE,
    )
    raw_receipt = await scheduler_port.claim_due_job(outer_lease=captured_outer_lease)
    receipt = canonical_scheduler_claim_receipt(raw_receipt)
    runtime.assert_intact()
    if runtime.scheduler_port is not scheduler_port:
        raise SchedulerInvariantError("scheduler_invocation_runtime_port_changed")
    if (
        runtime.account_id,
        runtime.holder_id,
        runtime.release_sha,
        runtime.persistence_authority,
    ) != runtime_identity:
        raise SchedulerInvariantError("scheduler_invocation_runtime_identity_changed")
    if (
        scheduler_port.release_sha != runtime_identity[2]
        or scheduler_port.persistence_authority != runtime_identity[3]
    ):
        raise SchedulerInvariantError("scheduler_invocation_runtime_identity_changed")
    current_outer_lease = _refresh_same_generation_outer_lease(
        captured_outer_lease,
        canonical_scheduler_outer_lease(runtime.current_outer_lease()),
        observed_at=receipt.observed_at,
    )
    claim = receipt.claim
    if claim is None:
        return None
    if claim.lease.release_sha != runtime.release_sha:
        raise SchedulerInvariantError("scheduler_invocation_release_binding_is_invalid")
    binding = SchedulerInvocationBinding(
        claim=claim,
        outer_lease=current_outer_lease,
        _issuance=_SCHEDULER_INVOCATION_ISSUANCE,
    )
    database_cutoff_at = _database_cutoff_for_binding(binding)
    monotonic_deadline = _convert_database_cutoff_to_monotonic(
        claim_observed_at=binding._claim.observed_at,
        database_cutoff_at=database_cutoff_at,
        rpc_started_monotonic=rpc_start.started_monotonic,
    )
    deadline = SchedulerInvocationDeadline(
        binding=binding,
        database_cutoff_at=database_cutoff_at,
        rpc_start=rpc_start,
        monotonic_deadline=monotonic_deadline,
        _issuance=_SCHEDULER_INVOCATION_ISSUANCE,
    )
    return SchedulerClaimedInvocation(
        deadline=deadline,
        scheduler_port=scheduler_port,
        _issuance=_SCHEDULER_INVOCATION_ISSUANCE,
    )


async def converge_scheduler_definition_invocation(
    runtime: SchedulerRuntimeCapability,
    definition: ScheduledJobDefinitionV1,
) -> SchedulerConvergenceClaimResult:
    """Converge one definition and bind any drained claim to the same RPC."""

    return await _converge_scheduler_definition_invocation_with_clock(
        runtime,
        definition,
        monotonic_clock=monotonic,
    )


async def _converge_scheduler_definition_invocation_with_clock(
    runtime: SchedulerRuntimeCapability,
    definition: ScheduledJobDefinitionV1,
    *,
    monotonic_clock: MonotonicClock,
) -> SchedulerConvergenceClaimResult:
    canonical_definition = canonical_scheduler_definition(definition)
    if not callable(monotonic_clock):
        raise SchedulerInvariantError("scheduler_invocation_clock_is_invalid")
    runtime.assert_intact()
    scheduler_port = runtime.scheduler_port
    runtime_identity = (
        runtime.account_id,
        runtime.holder_id,
        runtime.release_sha,
        runtime.persistence_authority,
    )
    captured_outer_lease = canonical_scheduler_outer_lease(runtime.current_outer_lease())
    if (
        captured_outer_lease.account_id != runtime.account_id
        or captured_outer_lease.holder_id != runtime.holder_id
        or scheduler_port.release_sha != runtime.release_sha
        or scheduler_port.persistence_authority != runtime.persistence_authority
    ):
        raise SchedulerInvariantError("scheduler_invocation_runtime_identity_mismatch")
    rpc_start = SchedulerClaimRpcStart(
        monotonic_clock=monotonic_clock,
        started_monotonic=_read_monotonic(monotonic_clock),
        _issuance=_SCHEDULER_INVOCATION_ISSUANCE,
    )
    raw_receipt = await scheduler_port.converge_job_definition(
        canonical_definition,
        outer_lease=captured_outer_lease,
    )
    receipt = canonical_scheduler_convergence_receipt(raw_receipt)
    runtime.assert_intact()
    if runtime.scheduler_port is not scheduler_port:
        raise SchedulerInvariantError("scheduler_invocation_runtime_port_changed")
    if (
        runtime.account_id,
        runtime.holder_id,
        runtime.release_sha,
        runtime.persistence_authority,
    ) != runtime_identity:
        raise SchedulerInvariantError("scheduler_invocation_runtime_identity_changed")
    if (
        scheduler_port.release_sha != runtime_identity[2]
        or scheduler_port.persistence_authority != runtime_identity[3]
    ):
        raise SchedulerInvariantError("scheduler_invocation_runtime_identity_changed")
    current_outer_lease = _refresh_same_generation_outer_lease(
        captured_outer_lease,
        canonical_scheduler_outer_lease(runtime.current_outer_lease()),
        observed_at=receipt.observed_at,
    )
    if (
        receipt.definition.account_id != runtime.account_id
        or receipt.definition.job_key != canonical_definition.job_key
        or (receipt.status == "converged" and receipt.definition.definition != canonical_definition)
    ):
        raise SchedulerInvariantError("scheduler_convergence_receipt_binding_mismatch")
    claim = receipt.claim
    if claim is None:
        return SchedulerConvergenceClaimResult(
            receipt=receipt,
            invocation=None,
            _issuance=_SCHEDULER_INVOCATION_ISSUANCE,
        )
    if claim.lease.release_sha != runtime.release_sha:
        raise SchedulerInvariantError("scheduler_invocation_release_binding_is_invalid")
    binding = SchedulerInvocationBinding(
        claim=claim,
        outer_lease=current_outer_lease,
        _issuance=_SCHEDULER_INVOCATION_ISSUANCE,
    )
    database_cutoff_at = _database_cutoff_for_binding(binding)
    monotonic_deadline = _convert_database_cutoff_to_monotonic(
        claim_observed_at=binding._claim.observed_at,
        database_cutoff_at=database_cutoff_at,
        rpc_started_monotonic=rpc_start.started_monotonic,
    )
    deadline = SchedulerInvocationDeadline(
        binding=binding,
        database_cutoff_at=database_cutoff_at,
        rpc_start=rpc_start,
        monotonic_deadline=monotonic_deadline,
        _issuance=_SCHEDULER_INVOCATION_ISSUANCE,
    )
    invocation = SchedulerClaimedInvocation(
        deadline=deadline,
        scheduler_port=scheduler_port,
        _issuance=_SCHEDULER_INVOCATION_ISSUANCE,
    )
    return SchedulerConvergenceClaimResult(
        receipt=receipt,
        invocation=invocation,
        _issuance=_SCHEDULER_INVOCATION_ISSUANCE,
    )


async def run_with_scheduler_deadline[T](
    handler: InvocationHandler[T],
    *,
    invocation: SchedulerClaimedInvocation,
    wait_until: DeadlineWaiter | None = None,
    fail_stop: FailStop,
    effect_issuer: object | None = None,
    effect_runtime: SchedulerRuntimeCapability | None = None,
    dispatch_registry_seal: _SchedulerDispatchRegistrySeal | None = None,
) -> T:
    if not callable(handler) or not callable(fail_stop):
        raise SchedulerInvariantError("scheduler_invocation_callable_is_invalid")
    if wait_until is not None and not callable(wait_until):
        raise SchedulerInvariantError("scheduler_invocation_waiter_is_invalid")
    if (effect_runtime is None) != (dispatch_registry_seal is None):
        _invoke_fail_stop(fail_stop, "scheduler_dispatch_registry_authority_invalid")
    resolved_effect_issuer = handler if effect_issuer is None else effect_issuer
    if not callable(resolved_effect_issuer):
        raise SchedulerInvariantError("scheduler_invocation_effect_issuer_is_invalid")
    if type(invocation) is not SchedulerClaimedInvocation:
        _invoke_fail_stop(fail_stop, "scheduler_deadline_provenance_invalid")
    try:
        deadline = invocation._consume()
    except SchedulerInvariantError:
        _invoke_fail_stop(fail_stop, "scheduler_deadline_provenance_invalid")
    job_key = deadline.binding.job_key
    monotonic_clock = deadline._rpc_start._monotonic_clock
    try:
        permit = SchedulerInvocationPermit(
            deadline,
            effect_issuer=resolved_effect_issuer,
            effect_runtime=effect_runtime,
            dispatch_registry_seal=dispatch_registry_seal,
            _issuance=_SCHEDULER_INVOCATION_ISSUANCE,
        )
    except SchedulerInvariantError as exc:
        reason = str(exc)
        if reason.startswith("scheduler_dispatch_registry_"):
            _invoke_fail_stop(
                fail_stop,
                "scheduler_dispatch_registry_authority_invalid",
            )
        _invoke_fail_stop(fail_stop, "scheduler_deadline_clock_corrupt")
    initial_state = permit._time_state()
    if initial_state == "clock_corrupt":
        permit._revoke_first_wins("clock_corrupt")
        _invoke_fail_stop(fail_stop, "scheduler_deadline_clock_corrupt")
    if initial_state == "deadline":
        permit._revoke_first_wins("deadline")
        _authorize_scheduler_settlement(invocation, permit)
        raise SchedulerInvocationDeadlineExceeded(job_key)

    resolved_waiter: DeadlineWaiter = wait_until or (
        lambda cutoff: wait_until_scheduler_deadline(
            cutoff,
            monotonic_clock=monotonic_clock,
        )
    )
    handler_task: asyncio.Task[T] = asyncio.create_task(_invoke_handler(handler, permit))

    async def run_waiter() -> None:
        await resolved_waiter(deadline.monotonic_deadline)

    timer_task: asyncio.Task[None] = asyncio.create_task(run_waiter())
    wait_set: set[asyncio.Future[object]] = {
        cast(asyncio.Future[object], handler_task),
        cast(asyncio.Future[object], timer_task),
    }
    try:
        done, _pending = await asyncio.wait(
            wait_set,
            return_when=asyncio.FIRST_COMPLETED,
        )
        timer_done = cast(asyncio.Future[object], timer_task) in done
        if timer_done and (timer_task.cancelled() or timer_task.exception() is not None):
            permit._revoke_first_wins("clock_corrupt")
            await _cancel_and_observe(
                handler_task,
                frozenset({"clock_corrupt"}),
            )
            _invoke_fail_stop(fail_stop, "scheduler_deadline_timer_failed")

        time_state = permit._time_state()
        if time_state == "clock_corrupt" or (timer_done and time_state != "deadline"):
            permit._revoke_first_wins("clock_corrupt")
            await _cancel_timer(timer_task)
            await _cancel_and_observe(
                handler_task,
                frozenset({"clock_corrupt"}),
            )
            _invoke_fail_stop(
                fail_stop,
                "scheduler_deadline_clock_corrupt_or_timer_early",
            )
        if timer_done or time_state == "deadline":
            permit._revoke_first_wins("deadline")
            timer_result = await _cancel_timer(timer_task)
            acknowledged = await _cancel_and_observe(
                handler_task,
                frozenset({"deadline"}),
            )
            if timer_result == "failed":
                _invoke_fail_stop(fail_stop, "scheduler_deadline_timer_failed")
            if timer_result == "stalled":
                _invoke_fail_stop(
                    fail_stop,
                    "scheduler_deadline_timer_cancellation_suppressed",
                )
            if not acknowledged:
                _invoke_fail_stop(
                    fail_stop,
                    "scheduler_handler_cancellation_suppressed",
                )
            _authorize_scheduler_settlement(invocation, permit)
            raise SchedulerInvocationDeadlineExceeded(job_key)

        timer_result = await _cancel_timer(timer_task)
        if timer_result == "failed":
            permit._revoke_first_wins("clock_corrupt")
            _invoke_fail_stop(fail_stop, "scheduler_deadline_timer_failed")
        if timer_result == "stalled":
            permit._revoke_first_wins("clock_corrupt")
            _invoke_fail_stop(
                fail_stop,
                "scheduler_deadline_timer_cancellation_suppressed",
            )
        if timer_result == "fired":
            time_state = permit._time_state()
            if time_state != "deadline":
                permit._revoke_first_wins("clock_corrupt")
                _invoke_fail_stop(
                    fail_stop,
                    "scheduler_deadline_clock_corrupt_or_timer_early",
                )
            permit._revoke_first_wins("deadline")
            _authorize_scheduler_settlement(invocation, permit)
            raise SchedulerInvocationDeadlineExceeded(job_key)
        if permit.revocation_reason != "handler_exit":
            _invoke_fail_stop(
                fail_stop,
                "scheduler_invocation_permit_state_invalid",
            )
        try:
            handler_result = await handler_task
        except asyncio.CancelledError:
            raise
        except Exception:
            _authorize_scheduler_settlement(invocation, permit)
            raise
        _authorize_scheduler_settlement(invocation, permit)
        return handler_result
    except asyncio.CancelledError:
        current_task = asyncio.current_task()
        if (
            current_task is not None
            and current_task.cancelling() == 0
            and handler_task.done()
            and handler_task.cancelled()
        ):
            raise
        permit._revoke_first_wins("external_cancel")
        timer_result = await _cancel_timer(timer_task)
        acknowledged = await _cancel_and_observe(
            handler_task,
            frozenset({"external_cancel", "deadline"}),
        )
        if not acknowledged:
            _invoke_fail_stop(
                fail_stop,
                "scheduler_handler_cancellation_suppressed",
            )
        if timer_result == "stalled":
            _invoke_fail_stop(
                fail_stop,
                "scheduler_deadline_timer_cancellation_suppressed",
            )
        raise


async def wait_until_scheduler_deadline(
    monotonic_deadline: float,
    *,
    monotonic_clock: MonotonicClock = monotonic,
) -> None:
    if type(monotonic_deadline) is not float or not math.isfinite(monotonic_deadline):
        raise SchedulerInvariantError("scheduler_invocation_monotonic_deadline_is_invalid")
    previous = _read_monotonic(monotonic_clock)
    while previous < monotonic_deadline:
        await asyncio.sleep(monotonic_deadline - previous)
        current = _read_monotonic(monotonic_clock)
        if current < previous:
            raise SchedulerInvariantError("scheduler_invocation_clock_moved_backwards")
        previous = current


def _authorize_scheduler_settlement(
    invocation: SchedulerClaimedInvocation,
    permit: SchedulerInvocationPermit,
) -> None:
    permit._assert_intact()
    if permit.revocation_reason not in {"handler_exit", "deadline"}:
        raise SchedulerInvariantError("scheduler_invocation_settlement_state_is_invalid")
    invocation._authorize_settlement(permit._last_monotonic)


async def _invoke_handler[T](
    handler: InvocationHandler[T],
    permit: SchedulerInvocationPermit,
) -> T:
    try:
        return await handler(permit)
    finally:
        permit._revoke_first_wins("handler_exit")


async def _cancel_and_observe[T](
    task: asyncio.Task[T],
    allowed_reasons: frozenset[str],
) -> bool:
    if task.done() or not task.cancel():
        await asyncio.gather(task, return_exceptions=True)
        return _completed_handler_allows_settlement(task, allowed_reasons)
    try:
        await asyncio.wait_for(
            asyncio.shield(task),
            timeout=SCHEDULER_INVOCATION_CANCELLATION_GRACE_SECONDS,
        )
    except asyncio.CancelledError:
        return task.cancelled()
    except SchedulerInvocationPermitRevoked as exc:
        return exc.reason in allowed_reasons
    except BaseException:
        return False
    return False


def _completed_handler_allows_settlement[T](
    task: asyncio.Task[T],
    allowed_reasons: frozenset[str],
) -> bool:
    if not task.done() or task.cancelled():
        return False
    try:
        error = task.exception()
    except BaseException:
        return False
    if error is None:
        return True
    if isinstance(error, SchedulerInvocationPermitRevoked):
        return error.reason in allowed_reasons
    if isinstance(error, Exception):
        return True
    raise error


async def _cancel_timer(
    timer_task: asyncio.Task[None],
) -> TimerCancellationResult:
    if timer_task.done() or not timer_task.cancel():
        result = await asyncio.gather(timer_task, return_exceptions=True)
        value = result[0]
        if isinstance(value, asyncio.CancelledError):
            return "cancelled"
        if value is None:
            return "fired"
        return "failed"
    try:
        await asyncio.wait_for(
            asyncio.shield(timer_task),
            timeout=SCHEDULER_INVOCATION_CANCELLATION_GRACE_SECONDS,
        )
    except TimeoutError:
        return "stalled"
    except asyncio.CancelledError:
        if timer_task.cancelled():
            return "cancelled"
        raise
    except BaseException:
        return "failed"
    return "fired"


def _read_monotonic(clock: MonotonicClock) -> float:
    try:
        value = clock()
    except BaseException:
        raise SchedulerInvariantError("scheduler_invocation_clock_failed") from None
    if type(value) is not float or not math.isfinite(value):
        raise SchedulerInvariantError("scheduler_invocation_clock_is_invalid")
    return value


def _require_exact_monotonic(value: object, reason: str) -> None:
    if type(value) is not float or not math.isfinite(value):
        raise SchedulerInvariantError(reason)


def _validate_invocation_context(
    claim: ScheduledJobClaimV1,
    outer_lease: WorkerLease,
) -> None:
    if (
        claim.run.account_id != outer_lease.account_id
        or claim.lease.account_id != outer_lease.account_id
        or claim.lease.holder_id != outer_lease.holder_id
        or claim.lease.outer_fencing_token != outer_lease.fencing_token
        or claim.lease.lease_expires_at > outer_lease.expires_at
        or not outer_lease.acquired_at <= claim.observed_at < outer_lease.expires_at
    ):
        raise SchedulerInvariantError("scheduler_invocation_context_binding_is_invalid")


def _assert_settlement_runtime_identity(
    runtime: SchedulerRuntimeCapability,
    invocation: SchedulerClaimedInvocation,
) -> None:
    try:
        runtime.assert_intact()
        binding = invocation.binding
        scheduler_port = runtime.scheduler_port
        if (
            scheduler_port is not invocation._scheduler_port
            or runtime.account_id != binding.account_id
            or runtime.holder_id != binding.holder_id
            or runtime.release_sha != binding.release_sha
            or runtime.persistence_authority != invocation.persistence_authority
            or scheduler_port.release_sha != binding.release_sha
            or scheduler_port.persistence_authority != invocation.persistence_authority
        ):
            raise SchedulerInvariantError("scheduler_settlement_runtime_identity_mismatch")
    except SchedulerInvariantError:
        raise
    except Exception:
        raise SchedulerInvariantError("scheduler_settlement_runtime_identity_mismatch") from None


def _assert_settlement_clock_source(invocation: SchedulerClaimedInvocation) -> None:
    monotonic_clock = invocation.deadline._rpc_start._monotonic_clock
    if (
        monotonic_clock is not monotonic
        and type(monotonic_clock) is not _SchedulerTestMonotonicClock
    ):
        raise SchedulerInvariantError("scheduler_settlement_clock_is_not_sealed")


def _refresh_same_generation_outer_lease(
    captured: WorkerLease,
    current: WorkerLease,
    *,
    observed_at: datetime,
) -> WorkerLease:
    observed = canonical_scheduler_datetime(
        observed_at,
        "scheduler_invocation_receipt_observed_at",
    )
    if (
        current.account_id != captured.account_id
        or current.holder_id != captured.holder_id
        or current.fencing_token != captured.fencing_token
        or current.acquired_at != captured.acquired_at
        or current.expires_at < captured.expires_at
        or not current.is_active(observed)
    ):
        raise SchedulerInvariantError("scheduler_invocation_outer_lease_changed")
    return current


def _database_cutoff_for_binding(
    binding: SchedulerInvocationBinding,
) -> datetime:
    binding._assert_intact()
    try:
        return (
            min(
                binding._claim.lease.lease_expires_at,
                binding._outer_lease.expires_at,
            )
            - SCHEDULER_INVOCATION_SETTLEMENT_RESERVE
        )
    except (OverflowError, ValueError):
        raise SchedulerInvariantError("scheduler_invocation_database_cutoff_is_invalid") from None


def _database_settlement_start_cutoff_for_binding(
    binding: SchedulerInvocationBinding,
) -> datetime:
    binding._assert_intact()
    try:
        return (
            min(
                binding._claim.lease.lease_expires_at,
                binding._outer_lease.expires_at,
            )
            - SCHEDULER_INVOCATION_SETTLEMENT_START_RESERVE
        )
    except (OverflowError, ValueError):
        raise SchedulerInvariantError(
            "scheduler_invocation_settlement_cutoff_is_invalid"
        ) from None


def _monotonic_settlement_start_deadline(
    deadline: SchedulerInvocationDeadline,
) -> float:
    deadline._assert_intact()
    return _convert_database_cutoff_to_monotonic(
        claim_observed_at=deadline.claim_observed_at,
        database_cutoff_at=_database_settlement_start_cutoff_for_binding(
            deadline.binding
        ),
        rpc_started_monotonic=deadline.rpc_started_monotonic,
    )


def _convert_database_cutoff_to_monotonic(
    *,
    claim_observed_at: datetime,
    database_cutoff_at: datetime,
    rpc_started_monotonic: float,
) -> float:
    observed_at = canonical_scheduler_datetime(
        claim_observed_at,
        "scheduler_invocation_claim_observed_at",
    )
    cutoff_at = canonical_scheduler_datetime(
        database_cutoff_at,
        "scheduler_invocation_database_cutoff_at",
    )
    _require_exact_monotonic(
        rpc_started_monotonic,
        "scheduler_invocation_rpc_started_monotonic_is_invalid",
    )
    monotonic_deadline = rpc_started_monotonic + (cutoff_at - observed_at).total_seconds()
    _require_exact_monotonic(
        monotonic_deadline,
        "scheduler_invocation_monotonic_deadline_is_invalid",
    )
    return monotonic_deadline


def _require_scheduler_sha256(value: object, field_name: str) -> None:
    if type(value) is not str or _SCHEDULER_SHA256_RE.fullmatch(value) is None:
        raise SchedulerInvariantError(f"{field_name}_is_invalid")


def _require_scheduler_reason(value: object, field_name: str) -> None:
    if type(value) is not str or _SCHEDULER_REASON_RE.fullmatch(value) is None:
        raise SchedulerInvariantError(f"{field_name}_is_invalid")


def _invoke_fail_stop(fail_stop: FailStop, reason: str) -> Never:
    fail_stop(reason)
    raise SchedulerInvocationFailStopReturned(reason)
