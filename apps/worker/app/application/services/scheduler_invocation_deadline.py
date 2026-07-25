from __future__ import annotations

import asyncio
import math
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
    ScheduledJobClaimV1,
    SchedulerInvariantError,
    SchedulerJobKey,
    canonical_scheduler_claim,
    canonical_scheduler_claim_receipt,
    canonical_scheduler_datetime,
)

MonotonicClock = Callable[[], float]
DeadlineWaiter = Callable[[float], Awaitable[None]]
FailStop = Callable[[str], Never]
type InvocationHandler[T] = Callable[["SchedulerInvocationPermit"], Awaitable[T]]
SchedulerInvocationTimeState = Literal["active", "deadline", "clock_corrupt"]
TimerCancellationResult = Literal["cancelled", "fired", "failed", "stalled"]

SCHEDULER_INVOCATION_CANCELLATION_GRACE_SECONDS = 0.25
SCHEDULER_INVOCATION_VALIDATION_RESERVE_SECONDS = 1.75
SCHEDULER_INVOCATION_SETTLEMENT_RESERVE = timedelta(
    seconds=(
        DURABLE_SCHEDULER_TOTAL_RPC_TIMEOUT_SECONDS
        + SCHEDULER_INVOCATION_CANCELLATION_GRACE_SECONDS
        + SCHEDULER_INVOCATION_VALIDATION_RESERVE_SECONDS
    )
)


class _SchedulerInvocationIssuance:
    __slots__ = ()


_SCHEDULER_INVOCATION_ISSUANCE = _SchedulerInvocationIssuance()


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
        "_issuance",
    )
    _consumed: bool
    _deadline: SchedulerInvocationDeadline
    _issuance: _SchedulerInvocationIssuance
    _persistence_authority: PersistenceAuthority
    _scheduler_port: DurableSchedulerPort

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


class SchedulerInvocationFailStopReturned(BaseException):
    """A configured fail-stop violated its non-returning contract."""


class SchedulerInvocationPermit:
    """Single-invocation effect permit with a fixed monotonic cutoff."""

    __slots__ = (
        "_deadline",
        "_monotonic_clock",
        "_last_monotonic",
        "_revocation_reason",
        "_issuance",
    )
    _deadline: SchedulerInvocationDeadline
    _issuance: _SchedulerInvocationIssuance
    _last_monotonic: float
    _monotonic_clock: MonotonicClock
    _revocation_reason: str | None

    def __init__(
        self,
        deadline: SchedulerInvocationDeadline,
        *,
        _issuance: object,
    ) -> None:
        if _issuance is not _SCHEDULER_INVOCATION_ISSUANCE:
            raise SchedulerInvariantError("scheduler_invocation_permit_is_not_issued")
        if type(deadline) is not SchedulerInvocationDeadline:
            raise SchedulerInvariantError("scheduler_invocation_deadline_type_is_invalid")
        deadline._assert_intact()
        monotonic_clock = deadline._rpc_start._monotonic_clock
        initial = _read_monotonic(monotonic_clock)
        if initial < deadline.rpc_started_monotonic:
            raise SchedulerInvariantError("scheduler_invocation_clock_moved_backwards")
        object.__setattr__(self, "_deadline", deadline)
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


async def run_with_scheduler_deadline[T](
    handler: InvocationHandler[T],
    *,
    invocation: SchedulerClaimedInvocation,
    wait_until: DeadlineWaiter | None = None,
    fail_stop: FailStop,
) -> T:
    if not callable(handler) or not callable(fail_stop):
        raise SchedulerInvariantError("scheduler_invocation_callable_is_invalid")
    if wait_until is not None and not callable(wait_until):
        raise SchedulerInvariantError("scheduler_invocation_waiter_is_invalid")
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
            _issuance=_SCHEDULER_INVOCATION_ISSUANCE,
        )
    except SchedulerInvariantError:
        _invoke_fail_stop(fail_stop, "scheduler_deadline_clock_corrupt")
    initial_state = permit._time_state()
    if initial_state == "clock_corrupt":
        permit._revoke_first_wins("clock_corrupt")
        _invoke_fail_stop(fail_stop, "scheduler_deadline_clock_corrupt")
    if initial_state == "deadline":
        permit._revoke_first_wins("deadline")
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
            raise SchedulerInvocationDeadlineExceeded(job_key)
        if permit.revocation_reason != "handler_exit":
            _invoke_fail_stop(
                fail_stop,
                "scheduler_invocation_permit_state_invalid",
            )
        return await handler_task
    except asyncio.CancelledError:
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
        return True
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


def _invoke_fail_stop(fail_stop: FailStop, reason: str) -> Never:
    fail_stop(reason)
    raise SchedulerInvocationFailStopReturned(reason)
