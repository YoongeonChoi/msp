from __future__ import annotations

import asyncio
import copy
import pickle
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from time import monotonic
from typing import Any, Literal, Never, cast

import pytest

from app.application.ports.durable_scheduler_port import SchedulerMutationOutcomeUnknownError
from app.application.services import scheduler_invocation_deadline as deadline_module
from app.application.services.scheduler_invocation_deadline import (
    SCHEDULER_DEADLINE_FAILURES,
    SCHEDULER_INVOCATION_SETTLEMENT_RESERVE,
    SCHEDULER_INVOCATION_SETTLEMENT_START_RESERVE,
    FailStop,
    SchedulerClaimedInvocation,
    SchedulerConvergenceClaimResult,
    SchedulerInvocationBinding,
    SchedulerInvocationDeadline,
    SchedulerInvocationDeadlineExceeded,
    SchedulerInvocationEffectAuthorization,
    SchedulerInvocationFailStopReturned,
    SchedulerInvocationPermit,
    SchedulerInvocationPermitRevoked,
    SchedulerInvocationSettlementAuthorization,
    SchedulerInvocationSettlementWindowExceeded,
    _claim_scheduler_invocation_with_clock,
    _converge_scheduler_definition_invocation_with_clock,
    begin_scheduler_invocation_settlement,
    complete_scheduler_invocation_settlement,
    fail_scheduler_invocation_settlement,
    issue_scheduler_invocation_effect_authorization,
    require_scheduler_invocation_effect_authorization,
    require_scheduler_invocation_permit,
    run_with_scheduler_deadline,
)
from app.domain.execution_v2.models import WorkerLease
from app.domain.scheduler.models import (
    SCHEDULER_JOB_KEYS,
    SCHEDULER_RETRYABLE_REASONS,
    ScheduledJobClaimReceiptV1,
    ScheduledJobClaimV1,
    ScheduledJobCompletionReceiptV1,
    ScheduledJobConvergenceDefinitionV1,
    ScheduledJobDefinitionV1,
    ScheduledJobFailureReceiptV1,
    ScheduledJobLeaseV1,
    ScheduledJobRunV1,
    SchedulerDefinitionConvergenceReceiptV1,
    SchedulerInvariantError,
    SchedulerJobKey,
)

_OBSERVED_AT = datetime(2026, 7, 25, 1, 2, 3, tzinfo=UTC)
_ACCOUNT_ID = "paper-primary"
_RUN_ID = "11111111-1111-4111-8111-111111111111"
_LEASE_TOKEN = "22222222-2222-4222-8222-222222222222"
_HOLDER_ID = "33333333-3333-4333-8333-333333333333"
_RELEASE_SHA = "a" * 40
_PERSISTENCE_AUTHORITY = "supabase-worker-api:" + "b" * 64
_OTHER_PERSISTENCE_AUTHORITY = "supabase-worker-api:" + "c" * 64


_Clock = deadline_module._SchedulerTestMonotonicClock


class _ManualWaiter:
    cutoffs: list[float]
    cancelled: bool

    def __init__(self) -> None:
        self.called = asyncio.Event()
        self.release = asyncio.Event()
        self.cutoffs = []
        self.cancelled = False

    async def __call__(self, cutoff: float) -> None:
        self.cutoffs.append(cutoff)
        self.called.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class _FailStopTriggered(BaseException):
    reason: str

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _raise_fail_stop(reason: str) -> Never:
    raise _FailStopTriggered(reason)


def _must_not_fail_stop(reason: str) -> Never:
    raise AssertionError(f"unexpected fail-stop: {reason}")


class _SchedulerPortStub:
    after_claim: Callable[[], None] | None
    completion_calls: list[tuple[ScheduledJobClaimV1, WorkerLease, str]]
    completion_error: BaseException | None
    convergence_receipt: SchedulerDefinitionConvergenceReceiptV1 | None
    converged_definitions: list[ScheduledJobDefinitionV1]
    failure_calls: list[tuple[ScheduledJobClaimV1, WorkerLease, str, str, bool]]
    receipt: ScheduledJobClaimReceiptV1
    release_sha: str
    persistence_authority: str
    received_outer_leases: list[WorkerLease]

    def __init__(
        self,
        receipt: ScheduledJobClaimReceiptV1,
        *,
        after_claim: Callable[[], None] | None = None,
        convergence_receipt: SchedulerDefinitionConvergenceReceiptV1 | None = None,
    ) -> None:
        self.release_sha = _RELEASE_SHA
        self.persistence_authority = _PERSISTENCE_AUTHORITY
        self.receipt = receipt
        self.after_claim = after_claim
        self.convergence_receipt = convergence_receipt
        self.completion_calls = []
        self.completion_error = None
        self.converged_definitions = []
        self.failure_calls = []
        self.received_outer_leases = []

    async def claim_due_job(
        self,
        *,
        outer_lease: WorkerLease,
    ) -> ScheduledJobClaimReceiptV1:
        self.received_outer_leases.append(outer_lease)
        if self.after_claim is not None:
            self.after_claim()
        return self.receipt

    async def converge_job_definition(
        self,
        definition: ScheduledJobDefinitionV1,
        *,
        outer_lease: WorkerLease,
    ) -> SchedulerDefinitionConvergenceReceiptV1:
        self.converged_definitions.append(definition)
        self.received_outer_leases.append(outer_lease)
        if self.after_claim is not None:
            self.after_claim()
        if self.convergence_receipt is None:
            raise AssertionError("unexpected convergence")
        return self.convergence_receipt

    async def complete_job_run(
        self,
        claim: ScheduledJobClaimV1,
        *,
        outer_lease: WorkerLease,
        result_sha256: str,
    ) -> ScheduledJobCompletionReceiptV1:
        self.completion_calls.append((claim, outer_lease, result_sha256))
        if self.completion_error is not None:
            raise self.completion_error
        return ScheduledJobCompletionReceiptV1(
            run_id=claim.run.run_id,
            run_revision=claim.run.revision + 1,
            attempt_count=claim.lease.attempt_number,
            next_attempt_at=None,
            failure_reason_code=None,
            result_sha256=result_sha256,
            observed_at=claim.observed_at,
        )

    async def fail_job_run(
        self,
        claim: ScheduledJobClaimV1,
        *,
        outer_lease: WorkerLease,
        failure_reason_code: str,
        failure_sha256: str,
        retryable: bool,
    ) -> ScheduledJobFailureReceiptV1:
        self.failure_calls.append(
            (
                claim,
                outer_lease,
                failure_reason_code,
                failure_sha256,
                retryable,
            )
        )
        return ScheduledJobFailureReceiptV1(
            run_id=claim.run.run_id,
            run_revision=claim.run.revision + 1,
            attempt_count=claim.lease.attempt_number,
            state="retry_wait" if retryable else "dead_letter",
            failure_reason_code=failure_reason_code,
            result_sha256=failure_sha256,
            next_attempt_at=(claim.observed_at + timedelta(seconds=1) if retryable else None),
            observed_at=claim.observed_at,
        )


class _RuntimeStub:
    account_id: str
    holder_id: str
    release_sha: str
    persistence_authority: str
    integrity_checks: int
    outer_lease: WorkerLease
    scheduler_port: Any

    def __init__(self, port: _SchedulerPortStub, outer_lease: WorkerLease) -> None:
        self.scheduler_port = cast(Any, port)
        self.outer_lease = outer_lease
        self.account_id = _ACCOUNT_ID
        self.holder_id = _HOLDER_ID
        self.release_sha = _RELEASE_SHA
        self.persistence_authority = _PERSISTENCE_AUTHORITY
        self.integrity_checks = 0

    def assert_intact(self) -> None:
        self.integrity_checks += 1

    def current_outer_lease(self) -> WorkerLease:
        return self.outer_lease


def _definition(job_key: SchedulerJobKey) -> ScheduledJobDefinitionV1:
    effectful = job_key in {"operations.execution", "operations.settlement"}
    return ScheduledJobDefinitionV1(
        job_key=job_key,
        interval_seconds=2,
        lease_ttl_seconds=60,
        max_attempts=1 if effectful else 4,
        retry_base_seconds=2,
        retry_max_seconds=8,
        max_manual_replays=0 if effectful else 1,
    )


def _claim(
    *,
    job_key: SchedulerJobKey = "operations.commands",
    observed_at: datetime = _OBSERVED_AT,
    lease_seconds: float = 20.0,
) -> ScheduledJobClaimV1:
    definition = _definition(job_key)
    run = ScheduledJobRunV1(
        run_id=_RUN_ID,
        account_id=_ACCOUNT_ID,
        job_key=job_key,
        definition_sha256=definition.definition_sha256,
        state="leased",
        revision=2,
        attempt_count=1,
        replay_generation=0,
        replay_of_run_id=None,
        scheduled_for=observed_at - timedelta(seconds=2),
        available_at=observed_at - timedelta(seconds=2),
        created_at=observed_at - timedelta(seconds=2),
        updated_at=observed_at,
    )
    lease = ScheduledJobLeaseV1(
        lease_token=_LEASE_TOKEN,
        run_id=_RUN_ID,
        account_id=_ACCOUNT_ID,
        holder_id=_HOLDER_ID,
        release_sha=_RELEASE_SHA,
        outer_fencing_token=7,
        attempt_number=1,
        run_revision=2,
        leased_at=observed_at,
        lease_expires_at=observed_at + timedelta(seconds=lease_seconds),
    )
    return ScheduledJobClaimV1(
        definition=definition,
        run=run,
        lease=lease,
        observed_at=observed_at,
    )


def _convergence_receipt(
    *,
    definition: ScheduledJobDefinitionV1,
    claim: ScheduledJobClaimV1 | None = None,
) -> SchedulerDefinitionConvergenceReceiptV1:
    status: Literal["claimed", "converged"] = "claimed" if claim is not None else "converged"
    return SchedulerDefinitionConvergenceReceiptV1(
        status=status,
        definition=ScheduledJobConvergenceDefinitionV1(
            definition_id="55555555-5555-4555-8555-555555555555",
            account_id=_ACCOUNT_ID,
            definition=definition,
            revision=1,
            next_due_at=_OBSERVED_AT + timedelta(seconds=definition.interval_seconds),
            scheduler_state="ready",
        ),
        claim=claim,
        active_run_id=claim.run.run_id if claim is not None else None,
        next_eligible_at=None,
        reason_code=None,
        observed_at=_OBSERVED_AT,
    )


def _outer_lease(
    *,
    observed_at: datetime = _OBSERVED_AT,
    lease_seconds: float = 30.0,
) -> WorkerLease:
    return WorkerLease(
        account_id=_ACCOUNT_ID,
        holder_id=_HOLDER_ID,
        fencing_token=7,
        acquired_at=observed_at - timedelta(seconds=1),
        expires_at=observed_at + timedelta(seconds=lease_seconds),
    )


async def _claim_once(
    clock: Callable[[], float],
    *,
    claim: ScheduledJobClaimV1 | None,
    outer_lease: WorkerLease,
    observed_at: datetime = _OBSERVED_AT,
    after_claim: Callable[[], None] | None = None,
) -> tuple[SchedulerClaimedInvocation | None, _SchedulerPortStub, _RuntimeStub]:
    port = _SchedulerPortStub(
        ScheduledJobClaimReceiptV1(
            claim=claim,
            observed_at=observed_at,
        ),
        after_claim=after_claim,
    )
    runtime = _RuntimeStub(port, outer_lease)
    invocation = await _claim_scheduler_invocation_with_clock(
        cast(Any, runtime),
        monotonic_clock=clock,
    )
    return invocation, port, runtime


async def _deadline(
    clock: Callable[[], float],
    *,
    job_key: SchedulerJobKey = "operations.commands",
    remaining_seconds: float = 10.0,
) -> SchedulerClaimedInvocation:
    invocation, _runtime = await _deadline_context(
        clock,
        job_key=job_key,
        remaining_seconds=remaining_seconds,
    )
    return invocation


async def _deadline_context(
    clock: Callable[[], float],
    *,
    job_key: SchedulerJobKey = "operations.commands",
    remaining_seconds: float = 10.0,
) -> tuple[SchedulerClaimedInvocation, _RuntimeStub]:
    lease_seconds = remaining_seconds + SCHEDULER_INVOCATION_SETTLEMENT_RESERVE.total_seconds()
    claim = _claim(job_key=job_key, lease_seconds=lease_seconds)
    invocation, _port, runtime = await _claim_once(
        clock,
        claim=claim,
        outer_lease=_outer_lease(lease_seconds=lease_seconds),
        observed_at=claim.observed_at,
    )
    assert invocation is not None
    return invocation, runtime


async def _capture_active_permit(
    clock: _Clock,
    *,
    job_key: SchedulerJobKey = "operations.commands",
) -> tuple[
    SchedulerInvocationPermit,
    SchedulerInvocationBinding,
    asyncio.Task[None],
]:
    invocation = await _deadline(clock, job_key=job_key)
    captured: list[SchedulerInvocationPermit] = []
    started = asyncio.Event()

    async def handler(permit: SchedulerInvocationPermit) -> None:
        captured.append(permit)
        started.set()
        await asyncio.Event().wait()

    run_task = asyncio.create_task(
        run_with_scheduler_deadline(
            handler,
            invocation=invocation,
            wait_until=_ManualWaiter(),
            fail_stop=_must_not_fail_stop,
        )
    )
    await started.wait()
    return captured[0], invocation.binding, run_task


async def _capture_active_effect_authorization(
    clock: _Clock,
    *,
    job_key: SchedulerJobKey = "operations.commands",
) -> tuple[
    SchedulerInvocationEffectAuthorization,
    SchedulerInvocationPermit,
    SchedulerInvocationBinding,
    _RuntimeStub,
    asyncio.Task[None],
]:
    invocation, runtime = await _deadline_context(clock, job_key=job_key)
    captured: list[
        tuple[SchedulerInvocationEffectAuthorization, SchedulerInvocationPermit]
    ] = []
    started = asyncio.Event()

    async def handler(permit: SchedulerInvocationPermit) -> None:
        authorization = issue_scheduler_invocation_effect_authorization(
            cast(Any, runtime),
            permit,
            invocation.binding,
            expected_job_key=job_key,
            effect_issuer=handler,
        )
        captured.append((authorization, permit))
        started.set()
        await asyncio.Event().wait()

    run_task = asyncio.create_task(
        run_with_scheduler_deadline(
            handler,
            invocation=invocation,
            wait_until=_ManualWaiter(),
            fail_stop=_must_not_fail_stop,
            effect_runtime=cast(Any, runtime),
        )
    )
    await started.wait()
    authorization, permit = captured[0]
    return authorization, permit, invocation.binding, runtime, run_task


async def _cancel_run(task: asyncio.Task[None]) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_deadline_uses_earliest_lease_and_reserves_full_settlement_budget() -> None:
    clock = _Clock(100.0)
    invocation, port, runtime = await _claim_once(
        clock,
        claim=_claim(lease_seconds=20),
        outer_lease=_outer_lease(lease_seconds=30),
        after_claim=lambda: setattr(clock, "value", 103.0),
    )
    assert invocation is not None
    deadline = invocation.deadline

    assert port.received_outer_leases == [_outer_lease(lease_seconds=30)]
    assert runtime.integrity_checks == 2
    assert SCHEDULER_INVOCATION_SETTLEMENT_RESERVE.total_seconds() == 6.0
    assert SCHEDULER_INVOCATION_SETTLEMENT_START_RESERVE.total_seconds() == 4.0
    assert deadline.database_cutoff_at == _OBSERVED_AT + timedelta(seconds=14)
    assert deadline.database_settlement_start_cutoff_at == (
        _OBSERVED_AT + timedelta(seconds=16)
    )
    assert deadline.rpc_started_monotonic == 100.0
    assert deadline.monotonic_deadline == 114.0
    assert deadline.monotonic_settlement_start_deadline == 116.0
    assert deadline.monotonic_deadline - 103.0 == 11.0


async def test_claim_accepts_inner_lease_that_fits_refreshed_outer_lease() -> None:
    clock = _Clock(200.0)
    claim = _claim(lease_seconds=20)
    captured = _outer_lease(lease_seconds=12)
    port = _SchedulerPortStub(
        ScheduledJobClaimReceiptV1(claim=claim, observed_at=claim.observed_at)
    )
    runtime = _RuntimeStub(port, captured)
    renewed = replace(captured, expires_at=_OBSERVED_AT + timedelta(seconds=30))
    port.after_claim = lambda: setattr(runtime, "outer_lease", renewed)

    invocation = await _claim_scheduler_invocation_with_clock(
        cast(Any, runtime),
        monotonic_clock=clock,
    )

    assert invocation is not None
    deadline = invocation.deadline

    assert invocation.outer_lease == renewed
    assert deadline.database_cutoff_at == _OBSERVED_AT + timedelta(seconds=14)
    assert deadline.monotonic_deadline == 214.0


async def test_deadline_canonicalizes_the_database_cutoff_to_utc() -> None:
    seoul = timezone(timedelta(hours=9))
    observed_at = datetime(2026, 7, 25, 10, 2, 3, tzinfo=seoul)
    invocation, _port, _runtime = await _claim_once(
        _Clock(100.0),
        claim=_claim(observed_at=observed_at, lease_seconds=16),
        outer_lease=_outer_lease(observed_at=observed_at, lease_seconds=16),
        observed_at=observed_at,
    )
    assert invocation is not None
    deadline = invocation.deadline

    assert deadline.database_cutoff_at == _OBSERVED_AT + timedelta(seconds=10)
    assert deadline.database_cutoff_at.tzinfo is UTC


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), True, 1],
)
async def test_deadline_rejects_non_finite_or_non_exact_rpc_start_values(
    value: object,
) -> None:
    claim = _claim()
    port = _SchedulerPortStub(
        ScheduledJobClaimReceiptV1(claim=claim, observed_at=claim.observed_at)
    )
    runtime = _RuntimeStub(port, _outer_lease())
    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_invocation_clock_is_invalid",
    ):
        await _claim_scheduler_invocation_with_clock(
            cast(Any, runtime),
            monotonic_clock=lambda: cast(float, value),
        )
    assert port.received_outer_leases == []


async def test_rpc_start_maps_clock_failure_to_a_scheduler_invariant() -> None:
    def failed_clock() -> float:
        raise RuntimeError("clock_failed")

    claim = _claim()
    port = _SchedulerPortStub(
        ScheduledJobClaimReceiptV1(claim=claim, observed_at=claim.observed_at)
    )
    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_invocation_clock_failed",
    ):
        await _claim_scheduler_invocation_with_clock(
            cast(Any, _RuntimeStub(port, _outer_lease())),
            monotonic_clock=failed_clock,
        )
    assert port.received_outer_leases == []


async def test_deadline_rejects_claim_and_outer_lease_binding_mismatch() -> None:
    clock = _Clock(100.0)

    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_invocation_context_binding_is_invalid",
    ):
        await _claim_once(
            clock,
            claim=_claim(),
            outer_lease=replace(_outer_lease(), fencing_token=8),
        )


async def test_claim_returns_no_invocation_when_database_has_no_due_job() -> None:
    invocation, port, runtime = await _claim_once(
        _Clock(100.0),
        claim=None,
        outer_lease=_outer_lease(),
    )

    assert invocation is None
    assert port.received_outer_leases == [_outer_lease()]
    assert runtime.integrity_checks == 2


async def test_no_claim_receipt_must_still_be_inside_the_outer_lease() -> None:
    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_invocation_outer_lease_changed",
    ):
        await _claim_once(
            _Clock(100.0),
            claim=None,
            outer_lease=_outer_lease(lease_seconds=0.0),
        )


async def test_claim_accepts_only_a_same_generation_outer_lease_renewal() -> None:
    claim = _claim(lease_seconds=20)
    captured = _outer_lease(lease_seconds=25)
    port = _SchedulerPortStub(
        ScheduledJobClaimReceiptV1(claim=claim, observed_at=claim.observed_at)
    )
    runtime = _RuntimeStub(port, captured)
    renewed = replace(captured, expires_at=captured.expires_at + timedelta(seconds=5))
    port.after_claim = lambda: setattr(runtime, "outer_lease", renewed)

    invocation = await _claim_scheduler_invocation_with_clock(
        cast(Any, runtime),
        monotonic_clock=_Clock(100.0),
    )

    assert invocation is not None
    assert invocation.outer_lease == renewed
    assert invocation.deadline.database_cutoff_at == _OBSERVED_AT + timedelta(seconds=14)


async def test_claim_rejects_an_inner_lease_beyond_the_current_outer_lease() -> None:
    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_invocation_context_binding_is_invalid",
    ):
        await _claim_once(
            _Clock(100.0),
            claim=_claim(lease_seconds=20),
            outer_lease=_outer_lease(lease_seconds=12),
        )


@pytest.mark.parametrize(
    "current_outer_lease",
    (
        replace(_outer_lease(), holder_id="44444444-4444-4444-8444-444444444444"),
        replace(_outer_lease(), fencing_token=8),
        replace(_outer_lease(), acquired_at=_OBSERVED_AT - timedelta(seconds=2)),
        replace(_outer_lease(), expires_at=_OBSERVED_AT + timedelta(seconds=29)),
    ),
)
async def test_claim_rejects_outer_lease_generation_changes(
    current_outer_lease: WorkerLease,
) -> None:
    claim = _claim()
    port = _SchedulerPortStub(
        ScheduledJobClaimReceiptV1(claim=claim, observed_at=claim.observed_at)
    )
    runtime = _RuntimeStub(port, _outer_lease())
    port.after_claim = lambda: setattr(
        runtime,
        "outer_lease",
        current_outer_lease,
    )

    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_invocation_outer_lease_changed",
    ):
        await _claim_scheduler_invocation_with_clock(
            cast(Any, runtime),
            monotonic_clock=_Clock(100.0),
        )


async def test_claim_rejects_runtime_port_or_identity_changes_after_rpc() -> None:
    claim = _claim()
    receipt = ScheduledJobClaimReceiptV1(
        claim=claim,
        observed_at=claim.observed_at,
    )
    original_port = _SchedulerPortStub(receipt)
    runtime = _RuntimeStub(original_port, _outer_lease())
    replacement_port = _SchedulerPortStub(receipt)
    original_port.after_claim = lambda: setattr(
        runtime,
        "scheduler_port",
        cast(Any, replacement_port),
    )

    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_invocation_runtime_port_changed",
    ):
        await _claim_scheduler_invocation_with_clock(
            cast(Any, runtime),
            monotonic_clock=_Clock(100.0),
        )

    authority_port = _SchedulerPortStub(receipt)
    runtime = _RuntimeStub(authority_port, _outer_lease())
    authority_port.after_claim = lambda: setattr(
        authority_port,
        "persistence_authority",
        _OTHER_PERSISTENCE_AUTHORITY,
    )
    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_invocation_runtime_identity_changed",
    ):
        await _claim_scheduler_invocation_with_clock(
            cast(Any, runtime),
            monotonic_clock=_Clock(100.0),
        )

    identity_port = _SchedulerPortStub(receipt)
    runtime = _RuntimeStub(identity_port, _outer_lease())
    identity_port.after_claim = lambda: setattr(runtime, "release_sha", "c" * 40)
    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_invocation_runtime_identity_changed",
    ):
        await _claim_scheduler_invocation_with_clock(
            cast(Any, runtime),
            monotonic_clock=_Clock(100.0),
        )


async def test_claim_rejects_release_identity_mismatch() -> None:
    claim = _claim()
    mismatched_claim = replace(
        claim,
        lease=replace(claim.lease, release_sha="c" * 40),
    )

    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_invocation_release_binding_is_invalid",
    ):
        await _claim_once(
            _Clock(100.0),
            claim=mismatched_claim,
            outer_lease=_outer_lease(),
        )


def test_raw_claim_and_deadline_issuance_helpers_are_not_exposed() -> None:
    assert not hasattr(deadline_module, "_begin_scheduler_claim_rpc")
    assert not hasattr(deadline_module, "_issue_scheduler_invocation")


async def test_convergence_claim_is_bound_to_its_rpc_start_and_receipt() -> None:
    clock = _Clock(100.0)
    claim = _claim(lease_seconds=20)
    receipt = _convergence_receipt(
        definition=claim.definition,
        claim=claim,
    )
    port = _SchedulerPortStub(
        ScheduledJobClaimReceiptV1(claim=None, observed_at=_OBSERVED_AT),
        after_claim=lambda: setattr(clock, "value", 103.0),
        convergence_receipt=receipt,
    )
    runtime = _RuntimeStub(port, _outer_lease(lease_seconds=30))

    result = await _converge_scheduler_definition_invocation_with_clock(
        cast(Any, runtime),
        claim.definition,
        monotonic_clock=clock,
    )

    assert result.receipt == receipt
    assert result.invocation is not None
    assert result.invocation.claim == claim
    assert result.invocation.deadline.rpc_started_monotonic == 100.0
    assert result.invocation.deadline.monotonic_deadline == 114.0
    assert port.converged_definitions == [claim.definition]
    assert port.received_outer_leases == [_outer_lease(lease_seconds=30)]
    assert runtime.integrity_checks == 2
    assert not hasattr(result, "__dict__")
    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_convergence_result_is_immutable",
    ):
        result._invocation = None
    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_convergence_result_is_not_copyable",
    ):
        copy.copy(result)
    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_convergence_result_is_not_serializable",
    ):
        pickle.dumps(result)


async def test_convergence_clock_failure_prevents_the_rpc() -> None:
    def failed_clock() -> float:
        raise RuntimeError("clock_failed")

    definition = _definition("operations.commands")
    port = _SchedulerPortStub(
        ScheduledJobClaimReceiptV1(claim=None, observed_at=_OBSERVED_AT),
        convergence_receipt=_convergence_receipt(definition=definition),
    )

    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_invocation_clock_failed",
    ):
        await _converge_scheduler_definition_invocation_with_clock(
            cast(Any, _RuntimeStub(port, _outer_lease())),
            definition,
            monotonic_clock=failed_clock,
        )

    assert port.converged_definitions == []
    assert port.received_outer_leases == []


async def test_converged_definition_returns_no_invocation() -> None:
    definition = _definition("operations.commands")
    receipt = _convergence_receipt(definition=definition)
    port = _SchedulerPortStub(
        ScheduledJobClaimReceiptV1(claim=None, observed_at=_OBSERVED_AT),
        convergence_receipt=receipt,
    )

    result = await _converge_scheduler_definition_invocation_with_clock(
        cast(Any, _RuntimeStub(port, _outer_lease())),
        definition,
        monotonic_clock=_Clock(100.0),
    )

    assert result.receipt == receipt
    assert result.invocation is None


async def test_convergence_accepts_only_same_generation_outer_lease_renewal() -> None:
    claim = _claim(lease_seconds=20)
    captured = _outer_lease(lease_seconds=25)
    renewed = replace(captured, expires_at=captured.expires_at + timedelta(seconds=5))
    port = _SchedulerPortStub(
        ScheduledJobClaimReceiptV1(claim=None, observed_at=_OBSERVED_AT),
        convergence_receipt=_convergence_receipt(
            definition=claim.definition,
            claim=claim,
        ),
    )
    runtime = _RuntimeStub(port, captured)
    port.after_claim = lambda: setattr(runtime, "outer_lease", renewed)

    result = await _converge_scheduler_definition_invocation_with_clock(
        cast(Any, runtime),
        claim.definition,
        monotonic_clock=_Clock(100.0),
    )

    assert result.invocation is not None
    assert result.invocation.outer_lease == renewed
    assert result.invocation.deadline.database_cutoff_at == (
        _OBSERVED_AT + timedelta(seconds=14)
    )


async def test_convergence_rejects_a_receipt_outside_the_outer_lease() -> None:
    definition = _definition("operations.commands")
    port = _SchedulerPortStub(
        ScheduledJobClaimReceiptV1(claim=None, observed_at=_OBSERVED_AT),
        convergence_receipt=_convergence_receipt(definition=definition),
    )

    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_invocation_outer_lease_changed",
    ):
        await _converge_scheduler_definition_invocation_with_clock(
            cast(Any, _RuntimeStub(port, _outer_lease(lease_seconds=0.0))),
            definition,
            monotonic_clock=_Clock(100.0),
        )


async def test_convergence_rejects_definition_or_outer_generation_drift() -> None:
    requested = _definition("operations.commands")
    mismatched_receipt = _convergence_receipt(
        definition=replace(requested, interval_seconds=3)
    )
    mismatched_port = _SchedulerPortStub(
        ScheduledJobClaimReceiptV1(claim=None, observed_at=_OBSERVED_AT),
        convergence_receipt=mismatched_receipt,
    )
    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_convergence_receipt_binding_mismatch",
    ):
        await _converge_scheduler_definition_invocation_with_clock(
            cast(Any, _RuntimeStub(mismatched_port, _outer_lease())),
            requested,
            monotonic_clock=_Clock(100.0),
        )

    receipt = _convergence_receipt(definition=requested)
    port = _SchedulerPortStub(
        ScheduledJobClaimReceiptV1(claim=None, observed_at=_OBSERVED_AT),
        convergence_receipt=receipt,
    )
    runtime = _RuntimeStub(port, _outer_lease())
    port.after_claim = lambda: setattr(
        runtime,
        "outer_lease",
        replace(_outer_lease(), fencing_token=8),
    )
    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_invocation_outer_lease_changed",
    ):
        await _converge_scheduler_definition_invocation_with_clock(
            cast(Any, runtime),
            requested,
            monotonic_clock=_Clock(100.0),
        )


async def test_convergence_rejects_runtime_port_or_identity_changes_after_rpc() -> None:
    definition = _definition("operations.commands")
    receipt = _convergence_receipt(definition=definition)
    original_port = _SchedulerPortStub(
        ScheduledJobClaimReceiptV1(claim=None, observed_at=_OBSERVED_AT),
        convergence_receipt=receipt,
    )
    replacement_port = _SchedulerPortStub(
        ScheduledJobClaimReceiptV1(claim=None, observed_at=_OBSERVED_AT),
        convergence_receipt=receipt,
    )
    runtime = _RuntimeStub(original_port, _outer_lease())
    original_port.after_claim = lambda: setattr(
        runtime,
        "scheduler_port",
        cast(Any, replacement_port),
    )

    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_invocation_runtime_port_changed",
    ):
        await _converge_scheduler_definition_invocation_with_clock(
            cast(Any, runtime),
            definition,
            monotonic_clock=_Clock(100.0),
        )

    authority_port = _SchedulerPortStub(
        ScheduledJobClaimReceiptV1(claim=None, observed_at=_OBSERVED_AT),
        convergence_receipt=receipt,
    )
    runtime = _RuntimeStub(authority_port, _outer_lease())
    authority_port.after_claim = lambda: setattr(
        authority_port,
        "persistence_authority",
        _OTHER_PERSISTENCE_AUTHORITY,
    )

    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_invocation_runtime_identity_changed",
    ):
        await _converge_scheduler_definition_invocation_with_clock(
            cast(Any, runtime),
            definition,
            monotonic_clock=_Clock(100.0),
        )

    release_port = _SchedulerPortStub(
        ScheduledJobClaimReceiptV1(claim=None, observed_at=_OBSERVED_AT),
        convergence_receipt=receipt,
    )
    runtime = _RuntimeStub(release_port, _outer_lease())
    release_port.after_claim = lambda: setattr(release_port, "release_sha", "c" * 40)

    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_invocation_runtime_identity_changed",
    ):
        await _converge_scheduler_definition_invocation_with_clock(
            cast(Any, runtime),
            definition,
            monotonic_clock=_Clock(100.0),
        )

    identity_port = _SchedulerPortStub(
        ScheduledJobClaimReceiptV1(claim=None, observed_at=_OBSERVED_AT),
        convergence_receipt=receipt,
    )
    runtime = _RuntimeStub(identity_port, _outer_lease())
    identity_port.after_claim = lambda: setattr(runtime, "release_sha", "c" * 40)

    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_invocation_runtime_identity_changed",
    ):
        await _converge_scheduler_definition_invocation_with_clock(
            cast(Any, runtime),
            definition,
            monotonic_clock=_Clock(100.0),
        )


async def test_convergence_rejects_claim_release_identity_drift() -> None:
    claim = _claim()
    mismatched_claim = replace(
        claim,
        lease=replace(claim.lease, release_sha="c" * 40),
    )
    receipt = _convergence_receipt(
        definition=mismatched_claim.definition,
        claim=mismatched_claim,
    )
    port = _SchedulerPortStub(
        ScheduledJobClaimReceiptV1(claim=None, observed_at=_OBSERVED_AT),
        convergence_receipt=receipt,
    )

    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_invocation_release_binding_is_invalid",
    ):
        await _converge_scheduler_definition_invocation_with_clock(
            cast(Any, _RuntimeStub(port, _outer_lease())),
            mismatched_claim.definition,
            monotonic_clock=_Clock(100.0),
        )


async def test_convergence_result_rejects_public_self_minting() -> None:
    claim = _claim()
    claimed_receipt = _convergence_receipt(
        definition=claim.definition,
        claim=claim,
    )
    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_convergence_result_is_not_issued",
    ):
        SchedulerConvergenceClaimResult(
            receipt=claimed_receipt,
            invocation=None,
            _issuance=object(),
        )

    invocation = await _deadline(_Clock(100.0))
    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_convergence_result_is_not_issued",
    ):
        SchedulerConvergenceClaimResult(
            receipt=_convergence_receipt(definition=claim.definition),
            invocation=invocation,
            _issuance=object(),
        )


async def test_convergence_result_detects_object_level_tampering() -> None:
    claim = _claim()
    port = _SchedulerPortStub(
        ScheduledJobClaimReceiptV1(claim=None, observed_at=_OBSERVED_AT),
        convergence_receipt=_convergence_receipt(
            definition=claim.definition,
            claim=claim,
        ),
    )
    result = await _converge_scheduler_definition_invocation_with_clock(
        cast(Any, _RuntimeStub(port, _outer_lease())),
        claim.definition,
        monotonic_clock=_Clock(100.0),
    )

    object.__setattr__(result, "_invocation", None)

    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_convergence_invocation_is_invalid",
    ):
        _ = result.receipt


async def test_deadline_binds_the_complete_scheduler_invocation_identity() -> None:
    deadline = await _deadline(_Clock(100.0))
    binding = deadline.binding

    assert binding.account_id == _ACCOUNT_ID
    assert binding.holder_id == _HOLDER_ID
    assert binding.release_sha == _RELEASE_SHA
    assert binding.job_key == "operations.commands"
    assert binding.run_id == _RUN_ID
    assert binding.run_revision == 2
    assert binding.lease_token == _LEASE_TOKEN
    assert binding.outer_fencing_token == 7
    assert binding.definition_sha256 == _definition("operations.commands").definition_sha256
    assert binding.attempt_number == 1


async def test_deadline_and_binding_are_immutable_and_non_transferable() -> None:
    invocation = await _deadline(_Clock(100.0))
    deadline = invocation.deadline

    assert not hasattr(invocation, "__dict__")
    assert not hasattr(deadline, "__dict__")
    assert not hasattr(deadline.binding, "__dict__")
    with pytest.raises(SchedulerInvariantError, match="deadline_is_immutable"):
        deadline._monotonic_deadline = 999.0
    with pytest.raises(SchedulerInvariantError, match="binding_is_immutable"):
        deadline.binding._claim = cast(Any, object())
    with pytest.raises(SchedulerInvariantError, match="deadline_is_not_copyable"):
        copy.copy(deadline)
    with pytest.raises(
        SchedulerInvariantError,
        match="claimed_invocation_is_not_copyable",
    ):
        copy.copy(invocation)
    with pytest.raises(SchedulerInvariantError, match="binding_is_not_copyable"):
        copy.deepcopy(deadline.binding)
    with pytest.raises(
        SchedulerInvariantError,
        match="deadline_is_not_serializable",
    ):
        pickle.dumps(deadline)
    with pytest.raises(
        SchedulerInvariantError,
        match="claimed_invocation_is_not_serializable",
    ):
        pickle.dumps(invocation)


async def test_deadline_and_permit_reject_public_self_minting() -> None:
    clock = _Clock(100.0)
    invocation = await _deadline(clock)
    deadline = invocation.deadline
    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_claimed_invocation_is_not_issued",
    ):
        SchedulerClaimedInvocation(
            deadline=deadline,
            scheduler_port=cast(Any, object()),
            _issuance=object(),
        )
    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_invocation_deadline_is_not_issued",
    ):
        SchedulerInvocationDeadline(
            binding=deadline.binding,
            database_cutoff_at=deadline.database_cutoff_at,
            rpc_start=deadline._rpc_start,
            monotonic_deadline=110.0,
            _issuance=object(),
        )
    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_invocation_permit_is_not_issued",
    ):
        SchedulerInvocationPermit(
            deadline,
            effect_issuer=lambda _permit: None,
            effect_runtime=None,
            _issuance=object(),
        )


def test_deadline_failure_table_is_complete_and_conservative() -> None:
    assert frozenset(SCHEDULER_DEADLINE_FAILURES) == SCHEDULER_JOB_KEYS
    assert {
        job_key: (failure.reason_code, failure.retryable)
        for job_key, failure in SCHEDULER_DEADLINE_FAILURES.items()
    } == {
        "operations.commands": ("command_poll_retryable", True),
        "operations.execution": ("execution_deadline_effect_unknown", False),
        "operations.settlement": ("settlement_deadline_effect_unknown", False),
        "operations.reconciliation": ("reconciliation_poll_retryable", True),
        "operations.outbox": ("outbox_poll_retryable", True),
    }
    assert all(
        not failure.retryable or failure.reason_code in SCHEDULER_RETRYABLE_REASONS[job_key]
        for job_key, failure in SCHEDULER_DEADLINE_FAILURES.items()
    )


async def test_permit_is_immutable_non_copyable_and_non_serializable() -> None:
    permit, _binding, run_task = await _capture_active_permit(_Clock(100.0))

    assert not hasattr(permit, "__dict__")
    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_invocation_permit_is_immutable",
    ):
        permit._deadline = cast(Any, object())
    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_invocation_permit_is_immutable",
    ):
        del permit._last_monotonic
    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_invocation_permit_is_not_copyable",
    ):
        copy.copy(permit)
    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_invocation_permit_is_not_copyable",
    ):
        copy.deepcopy(permit)
    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_invocation_permit_is_not_serializable",
    ):
        pickle.dumps(permit)
    await _cancel_run(run_task)


async def test_effect_authorization_is_exact_immutable_and_non_transferable() -> None:
    authorization, _permit, _binding, _runtime, run_task = (
        await _capture_active_effect_authorization(_Clock(100.0))
    )

    assert (
        require_scheduler_invocation_effect_authorization(
            authorization,
            expected_job_key="operations.commands",
        )
        is authorization
    )
    assert not hasattr(authorization, "__dict__")
    assert not hasattr(authorization, "binding")
    assert not hasattr(authorization, "permit")
    assert not hasattr(authorization, "runtime")
    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_effect_authorization_is_immutable",
    ):
        authorization._expected_job_key = "operations.outbox"
    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_effect_authorization_is_immutable",
    ):
        del authorization._captured_outer_lease
    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_effect_authorization_is_not_copyable",
    ):
        copy.copy(authorization)
    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_effect_authorization_is_not_copyable",
    ):
        copy.deepcopy(authorization)
    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_effect_authorization_is_not_serializable",
    ):
        pickle.dumps(authorization)
    with pytest.raises(SchedulerInvocationPermitRevoked, match="permit_not_issued"):
        require_scheduler_invocation_effect_authorization(
            object(),
            expected_job_key="operations.commands",
        )

    await _cancel_run(run_task)


async def test_effect_authorization_rejects_wrong_job_and_cross_run_binding() -> None:
    authorization, _permit, _binding, _runtime, first_task = (
        await _capture_active_effect_authorization(_Clock(100.0))
    )
    with pytest.raises(SchedulerInvocationPermitRevoked) as wrong_job:
        require_scheduler_invocation_effect_authorization(
            authorization,
            expected_job_key="operations.outbox",
        )
    assert wrong_job.value.reason == "binding_mismatch"
    await _cancel_run(first_task)

    (
        _first_authorization,
        first_permit,
        _first_binding,
        first_runtime,
        first_task,
    ) = await _capture_active_effect_authorization(_Clock(200.0))
    (
        _second_authorization,
        _second_permit,
        second_binding,
        _second_runtime,
        second_task,
    ) = await _capture_active_effect_authorization(_Clock(300.0))

    with pytest.raises(SchedulerInvocationPermitRevoked) as cross_run:
        issue_scheduler_invocation_effect_authorization(
            cast(Any, first_runtime),
            first_permit,
            second_binding,
            expected_job_key="operations.commands",
            effect_issuer=cast(Any, first_permit)._effect_issuer,
        )
    assert cross_run.value.reason == "binding_mismatch"

    await _cancel_run(first_task)
    await _cancel_run(second_task)


async def test_effect_authorization_rejects_a_different_issuer_identity() -> None:
    authorization, permit, binding, runtime, run_task = (
        await _capture_active_effect_authorization(_Clock(100.0))
    )

    async def cloned_handler(_permit: SchedulerInvocationPermit) -> None:
        return None

    with pytest.raises(SchedulerInvocationPermitRevoked) as wrong_issuer:
        issue_scheduler_invocation_effect_authorization(
            cast(Any, runtime),
            permit,
            binding,
            expected_job_key="operations.commands",
            effect_issuer=cloned_handler,
        )
    assert wrong_issuer.value.reason == "binding_mismatch"
    with pytest.raises(SchedulerInvocationPermitRevoked) as revoked:
        require_scheduler_invocation_effect_authorization(
            authorization,
            expected_job_key="operations.commands",
        )
    assert revoked.value.reason == "binding_mismatch"

    await _cancel_run(run_task)


async def test_effect_authorization_is_revoked_after_handler_exit() -> None:
    clock = _Clock(100.0)
    invocation, runtime = await _deadline_context(clock)
    captured: list[SchedulerInvocationEffectAuthorization] = []

    async def handler(permit: SchedulerInvocationPermit) -> str:
        captured.append(
            issue_scheduler_invocation_effect_authorization(
                cast(Any, runtime),
                permit,
                invocation.binding,
                expected_job_key="operations.commands",
                effect_issuer=handler,
            )
        )
        return "completed"

    assert (
        await run_with_scheduler_deadline(
            handler,
            invocation=invocation,
            wait_until=_ManualWaiter(),
            fail_stop=_must_not_fail_stop,
            effect_runtime=cast(Any, runtime),
        )
        == "completed"
    )
    with pytest.raises(SchedulerInvocationPermitRevoked) as exited:
        require_scheduler_invocation_effect_authorization(
            captured[0],
            expected_job_key="operations.commands",
        )
    assert exited.value.reason == "handler_exit"


async def test_effect_authorization_is_revoked_at_deadline() -> None:
    clock = _Clock(100.0)
    authorization, permit, _binding, _runtime, run_task = (
        await _capture_active_effect_authorization(clock)
    )
    clock.value = 110.0

    with pytest.raises(SchedulerInvocationPermitRevoked) as expired:
        require_scheduler_invocation_effect_authorization(
            authorization,
            expected_job_key="operations.commands",
        )

    assert expired.value.reason == "deadline"
    assert permit.revocation_reason == "deadline"
    await _cancel_run(run_task)


_EffectAuthorizationMutation = Callable[[_RuntimeStub], None]


def _replace_effect_scheduler_port(runtime: _RuntimeStub) -> None:
    runtime.scheduler_port = object()


def _change_effect_persistence_authority(runtime: _RuntimeStub) -> None:
    runtime.persistence_authority = _OTHER_PERSISTENCE_AUTHORITY


def _change_effect_outer_fencing_token(runtime: _RuntimeStub) -> None:
    runtime.outer_lease = replace(runtime.outer_lease, fencing_token=8)


@pytest.mark.parametrize(
    "mutate_runtime",
    (
        _replace_effect_scheduler_port,
        _change_effect_persistence_authority,
        _change_effect_outer_fencing_token,
    ),
)
async def test_effect_authorization_blocks_runtime_drift_before_dispatch(
    mutate_runtime: _EffectAuthorizationMutation,
) -> None:
    authorization, permit, _binding, runtime, run_task = (
        await _capture_active_effect_authorization(_Clock(100.0))
    )
    mutate_runtime(runtime)

    with pytest.raises(SchedulerInvocationPermitRevoked) as revoked:
        require_scheduler_invocation_effect_authorization(
            authorization,
            expected_job_key="operations.commands",
        )

    assert revoked.value.reason == "binding_mismatch"
    assert permit.revocation_reason == "binding_mismatch"
    await _cancel_run(run_task)


async def test_permit_deadline_revocation_bypasses_broad_exception_handlers() -> None:
    clock = _Clock(100.0)
    permit, binding, run_task = await _capture_active_permit(clock)
    clock.value = 110.0
    caught_by_exception = False

    try:
        permit.assert_effect_allowed(expected_binding=binding)
    except Exception:
        caught_by_exception = True
    except SchedulerInvocationPermitRevoked as exc:
        assert exc.reason == "deadline"

    assert not caught_by_exception
    assert permit.revocation_reason == "deadline"
    await _cancel_run(run_task)


async def test_permit_fails_closed_when_the_monotonic_clock_moves_backwards() -> None:
    clock = _Clock(100.0)
    permit, binding, run_task = await _capture_active_permit(clock)
    clock.value = 105.0
    permit.assert_effect_allowed(expected_binding=binding)
    clock.value = 104.0

    with pytest.raises(SchedulerInvocationPermitRevoked) as exc_info:
        permit.assert_effect_allowed(expected_binding=binding)

    assert exc_info.value.reason == "clock_corrupt"
    assert permit.revocation_reason == "clock_corrupt"
    await _cancel_run(run_task)


@pytest.mark.parametrize("failure_mode", ["non_finite", "exception"])
async def test_permit_fails_closed_when_the_monotonic_clock_is_invalid(
    failure_mode: str,
) -> None:
    clock = _Clock(100.0)
    permit, binding, run_task = await _capture_active_permit(clock)
    if failure_mode == "non_finite":
        clock.value = float("nan")
    else:
        clock.fail = True

    with pytest.raises(SchedulerInvocationPermitRevoked) as exc_info:
        permit.assert_effect_allowed(expected_binding=binding)

    assert exc_info.value.reason == "clock_corrupt"
    await _cancel_run(run_task)


async def test_effect_boundary_rejects_fake_and_cross_run_permits() -> None:
    first_clock = _Clock(100.0)
    permit, binding, run_task = await _capture_active_permit(first_clock)
    other_deadline = await _deadline(
        _Clock(200.0),
        job_key="operations.execution",
    )

    with pytest.raises(SchedulerInvocationPermitRevoked, match="permit_not_issued"):
        require_scheduler_invocation_permit(
            object(),
            expected_binding=binding,
        )
    with pytest.raises(SchedulerInvocationPermitRevoked, match="binding_mismatch"):
        require_scheduler_invocation_permit(
            permit,
            expected_binding=other_deadline.binding,
        )

    await _cancel_run(run_task)


async def test_normal_handler_return_revokes_the_permit() -> None:
    clock = _Clock(100.0)
    waiter = _ManualWaiter()
    deadline = await _deadline(clock)
    captured: list[SchedulerInvocationPermit] = []

    async def handler(permit: SchedulerInvocationPermit) -> str:
        captured.append(permit)
        permit.assert_effect_allowed(expected_binding=deadline.binding)
        return "completed"

    result = await run_with_scheduler_deadline(
        handler,
        invocation=deadline,
        wait_until=waiter,
        fail_stop=_must_not_fail_stop,
    )

    assert result == "completed"
    assert captured[0].revocation_reason == "handler_exit"
    assert waiter.cancelled
    with pytest.raises(SchedulerInvocationPermitRevoked, match="handler_exit"):
        captured[0].assert_effect_allowed(expected_binding=deadline.binding)


async def test_handler_completion_authorizes_exactly_one_settlement_start() -> None:
    clock = _Clock(100.0)
    invocation, runtime = await _deadline_context(clock)

    async def handler(permit: SchedulerInvocationPermit) -> str:
        clock.value = 105.0
        require_scheduler_invocation_permit(
            permit,
            expected_binding=invocation.binding,
        )
        return "completed"

    assert (
        await run_with_scheduler_deadline(
            handler,
            invocation=invocation,
            wait_until=_ManualWaiter(),
            fail_stop=_must_not_fail_stop,
        )
        == "completed"
    )
    clock.value = 111.999

    authorization = begin_scheduler_invocation_settlement(
        cast(Any, runtime),
        invocation,
        fail_stop=_must_not_fail_stop,
    )
    assert not hasattr(authorization, "binding")
    assert not hasattr(authorization, "outer_lease")
    assert not hasattr(authorization, "__dict__")
    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_settlement_authorization_is_immutable",
    ):
        authorization._outer_lease = _outer_lease()
    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_settlement_authorization_is_not_copyable",
    ):
        copy.copy(authorization)
    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_settlement_authorization_is_not_serializable",
    ):
        pickle.dumps(authorization)
    result_sha256 = "d" * 64
    receipt = await complete_scheduler_invocation_settlement(
        authorization,
        result_sha256=result_sha256,
        fail_stop=_must_not_fail_stop,
    )
    scheduler_port = cast(_SchedulerPortStub, runtime.scheduler_port)
    assert receipt.result_sha256 == result_sha256
    assert scheduler_port.completion_calls == [
        (invocation.claim, runtime.outer_lease, result_sha256)
    ]
    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_invocation_settlement_is_consumed",
    ):
        await fail_scheduler_invocation_settlement(
            authorization,
            failure_reason_code="handler_error",
            failure_sha256="e" * 64,
            retryable=False,
            fail_stop=_must_not_fail_stop,
        )
    assert scheduler_port.failure_calls == []
    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_invocation_settlement_is_consumed",
    ):
        begin_scheduler_invocation_settlement(
            cast(Any, runtime),
            invocation,
            fail_stop=_must_not_fail_stop,
        )


async def test_unknown_completion_outcome_never_opens_failure_dispatch() -> None:
    invocation, runtime = await _deadline_context(_Clock(100.0))

    async def handler(_permit: SchedulerInvocationPermit) -> None:
        return None

    await run_with_scheduler_deadline(
        handler,
        invocation=invocation,
        wait_until=_ManualWaiter(),
        fail_stop=_must_not_fail_stop,
    )
    authorization = begin_scheduler_invocation_settlement(
        cast(Any, runtime),
        invocation,
        fail_stop=_must_not_fail_stop,
    )
    scheduler_port = cast(_SchedulerPortStub, runtime.scheduler_port)
    scheduler_port.completion_error = SchedulerMutationOutcomeUnknownError()

    with pytest.raises(SchedulerMutationOutcomeUnknownError):
        await complete_scheduler_invocation_settlement(
            authorization,
            result_sha256="d" * 64,
            fail_stop=_must_not_fail_stop,
        )
    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_invocation_settlement_is_consumed",
    ):
        await fail_scheduler_invocation_settlement(
            authorization,
            failure_reason_code="handler_error",
            failure_sha256="e" * 64,
            retryable=False,
            fail_stop=_must_not_fail_stop,
        )

    assert len(scheduler_port.completion_calls) == 1
    assert scheduler_port.failure_calls == []


async def test_in_flight_completion_blocks_concurrent_failure_dispatch() -> None:
    invocation, runtime = await _deadline_context(_Clock(100.0))

    async def handler(_permit: SchedulerInvocationPermit) -> None:
        return None

    await run_with_scheduler_deadline(
        handler,
        invocation=invocation,
        wait_until=_ManualWaiter(),
        fail_stop=_must_not_fail_stop,
    )
    authorization = begin_scheduler_invocation_settlement(
        cast(Any, runtime),
        invocation,
        fail_stop=_must_not_fail_stop,
    )
    scheduler_port = cast(_SchedulerPortStub, runtime.scheduler_port)
    complete_job_run = scheduler_port.complete_job_run
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocking_complete_job_run(
        claim: ScheduledJobClaimV1,
        *,
        outer_lease: WorkerLease,
        result_sha256: str,
    ) -> ScheduledJobCompletionReceiptV1:
        entered.set()
        await release.wait()
        return await complete_job_run(
            claim,
            outer_lease=outer_lease,
            result_sha256=result_sha256,
        )

    cast(Any, scheduler_port).complete_job_run = blocking_complete_job_run
    completion_task = asyncio.create_task(
        complete_scheduler_invocation_settlement(
            authorization,
            result_sha256="d" * 64,
            fail_stop=_must_not_fail_stop,
        )
    )
    await entered.wait()

    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_invocation_settlement_is_consumed",
    ):
        await fail_scheduler_invocation_settlement(
            authorization,
            failure_reason_code="handler_error",
            failure_sha256="e" * 64,
            retryable=False,
            fail_stop=_must_not_fail_stop,
        )

    release.set()
    await completion_task
    assert len(scheduler_port.completion_calls) == 1
    assert scheduler_port.failure_calls == []


async def test_settlement_start_rejects_the_exact_rpc_reserve_cutoff() -> None:
    clock = _Clock(100.0)
    invocation, runtime = await _deadline_context(clock)

    async def handler(_permit: SchedulerInvocationPermit) -> str:
        return "completed"

    await run_with_scheduler_deadline(
        handler,
        invocation=invocation,
        wait_until=_ManualWaiter(),
        fail_stop=_must_not_fail_stop,
    )
    clock.value = 112.0

    with pytest.raises(SchedulerInvocationSettlementWindowExceeded) as exc_info:
        begin_scheduler_invocation_settlement(
            cast(Any, runtime),
            invocation,
            fail_stop=_must_not_fail_stop,
        )

    assert exc_info.value.job_key == "operations.commands"
    assert exc_info.value.reason_code == "scheduler_settlement_window_expired"
    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_invocation_settlement_is_consumed",
    ):
        begin_scheduler_invocation_settlement(
            cast(Any, runtime),
            invocation,
            fail_stop=_must_not_fail_stop,
        )


async def test_settlement_start_rechecks_time_after_runtime_provenance() -> None:
    clock = _Clock(100.0)
    invocation, runtime = await _deadline_context(clock)

    async def handler(_permit: SchedulerInvocationPermit) -> None:
        return None

    await run_with_scheduler_deadline(
        handler,
        invocation=invocation,
        wait_until=_ManualWaiter(),
        fail_stop=_must_not_fail_stop,
    )
    clock.value = 111.999
    current_outer_lease = runtime.current_outer_lease

    def advance_clock_at_provenance_check() -> WorkerLease:
        clock.value = 112.0
        return current_outer_lease()

    cast(Any, runtime).current_outer_lease = advance_clock_at_provenance_check

    with pytest.raises(SchedulerInvocationSettlementWindowExceeded):
        begin_scheduler_invocation_settlement(
            cast(Any, runtime),
            invocation,
            fail_stop=_must_not_fail_stop,
        )


async def test_settlement_start_rechecks_cutoff_after_final_attestation() -> None:
    clock = _Clock(100.0)
    invocation, runtime = await _deadline_context(clock)

    async def handler(_permit: SchedulerInvocationPermit) -> None:
        return None

    await run_with_scheduler_deadline(
        handler,
        invocation=invocation,
        wait_until=_ManualWaiter(),
        fail_stop=_must_not_fail_stop,
    )
    clock.value = 111.999
    assert_intact = runtime.assert_intact
    attestation_count = 0

    def cross_cutoff_during_final_attestation() -> None:
        nonlocal attestation_count
        attestation_count += 1
        assert_intact()
        if attestation_count == 3:
            clock.value = 112.0

    cast(Any, runtime).assert_intact = cross_cutoff_during_final_attestation

    with pytest.raises(SchedulerInvocationSettlementWindowExceeded):
        begin_scheduler_invocation_settlement(
            cast(Any, runtime),
            invocation,
            fail_stop=_must_not_fail_stop,
        )


async def test_settlement_authorization_expires_at_the_start_cutoff() -> None:
    clock = _Clock(100.0)
    invocation, runtime = await _deadline_context(clock)

    async def handler(_permit: SchedulerInvocationPermit) -> None:
        return None

    await run_with_scheduler_deadline(
        handler,
        invocation=invocation,
        wait_until=_ManualWaiter(),
        fail_stop=_must_not_fail_stop,
    )
    clock.value = 111.999
    authorization = begin_scheduler_invocation_settlement(
        cast(Any, runtime),
        invocation,
        fail_stop=_must_not_fail_stop,
    )
    clock.value = 112.0

    with pytest.raises(SchedulerInvocationSettlementWindowExceeded):
        await complete_scheduler_invocation_settlement(
            authorization,
            result_sha256="d" * 64,
            fail_stop=_must_not_fail_stop,
        )
    assert cast(_SchedulerPortStub, runtime.scheduler_port).completion_calls == []


async def test_settlement_start_fail_stops_clock_regression() -> None:
    clock = _Clock(100.0)
    invocation, runtime = await _deadline_context(clock)

    async def handler(permit: SchedulerInvocationPermit) -> None:
        clock.value = 105.0
        require_scheduler_invocation_permit(
            permit,
            expected_binding=invocation.binding,
        )

    await run_with_scheduler_deadline(
        handler,
        invocation=invocation,
        wait_until=_ManualWaiter(),
        fail_stop=_must_not_fail_stop,
    )
    clock.value = 104.0

    with pytest.raises(_FailStopTriggered) as exc_info:
        begin_scheduler_invocation_settlement(
            cast(Any, runtime),
            invocation,
            fail_stop=_raise_fail_stop,
        )

    assert exc_info.value.reason == "scheduler_settlement_clock_corrupt"


async def test_settlement_start_fail_stops_clock_failure() -> None:
    clock = _Clock(100.0)
    invocation, runtime = await _deadline_context(clock)

    async def handler(_permit: SchedulerInvocationPermit) -> None:
        return None

    await run_with_scheduler_deadline(
        handler,
        invocation=invocation,
        wait_until=_ManualWaiter(),
        fail_stop=_must_not_fail_stop,
    )
    clock.fail = True

    with pytest.raises(_FailStopTriggered) as exc_info:
        begin_scheduler_invocation_settlement(
            cast(Any, runtime),
            invocation,
            fail_stop=_raise_fail_stop,
        )

    assert exc_info.value.reason == "scheduler_settlement_clock_corrupt"


async def test_settlement_requires_observed_handler_termination() -> None:
    invocation, runtime = await _deadline_context(_Clock(100.0))

    with pytest.raises(_FailStopTriggered) as exc_info:
        begin_scheduler_invocation_settlement(
            cast(Any, runtime),
            invocation,
            fail_stop=_raise_fail_stop,
        )

    assert exc_info.value.reason == "scheduler_settlement_provenance_invalid"


async def test_settlement_rejects_an_unsealed_monotonic_callback() -> None:
    def unsealed_clock() -> float:
        return 100.0

    invocation, runtime = await _deadline_context(unsealed_clock)

    async def handler(_permit: SchedulerInvocationPermit) -> None:
        return None

    await run_with_scheduler_deadline(
        handler,
        invocation=invocation,
        wait_until=_ManualWaiter(),
        fail_stop=_must_not_fail_stop,
    )

    with pytest.raises(_FailStopTriggered) as exc_info:
        begin_scheduler_invocation_settlement(
            cast(Any, runtime),
            invocation,
            fail_stop=_raise_fail_stop,
        )

    assert exc_info.value.reason == "scheduler_settlement_provenance_invalid"


async def test_settlement_authorization_rejects_public_self_minting() -> None:
    invocation, runtime = await _deadline_context(_Clock(100.0))

    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_settlement_authorization_is_not_issued",
    ):
        SchedulerInvocationSettlementAuthorization(
            invocation=invocation,
            runtime=cast(Any, runtime),
            outer_lease=runtime.outer_lease,
            observed_at=_OBSERVED_AT,
            _issuance=object(),
        )


async def test_settlement_uses_a_same_generation_outer_lease_renewal() -> None:
    clock = _Clock(100.0)
    invocation, runtime = await _deadline_context(clock)

    async def handler(_permit: SchedulerInvocationPermit) -> None:
        return None

    await run_with_scheduler_deadline(
        handler,
        invocation=invocation,
        wait_until=_ManualWaiter(),
        fail_stop=_must_not_fail_stop,
    )
    renewed = replace(
        runtime.outer_lease,
        expires_at=runtime.outer_lease.expires_at + timedelta(seconds=5),
    )
    runtime.outer_lease = renewed

    authorization = begin_scheduler_invocation_settlement(
        cast(Any, runtime),
        invocation,
        fail_stop=_must_not_fail_stop,
    )
    result_sha256 = "d" * 64
    await complete_scheduler_invocation_settlement(
        authorization,
        result_sha256=result_sha256,
        fail_stop=_must_not_fail_stop,
    )
    assert cast(_SchedulerPortStub, runtime.scheduler_port).completion_calls == [
        (invocation.claim, renewed, result_sha256)
    ]


@pytest.mark.parametrize(
    "drift",
    ("runtime_port", "runtime_release", "port_authority", "outer_generation"),
)
async def test_settlement_fail_stops_runtime_or_lease_drift(drift: str) -> None:
    invocation, runtime = await _deadline_context(_Clock(100.0))

    async def handler(_permit: SchedulerInvocationPermit) -> None:
        return None

    await run_with_scheduler_deadline(
        handler,
        invocation=invocation,
        wait_until=_ManualWaiter(),
        fail_stop=_must_not_fail_stop,
    )
    if drift == "runtime_port":
        runtime.scheduler_port = cast(
            Any,
            _SchedulerPortStub(
                ScheduledJobClaimReceiptV1(claim=None, observed_at=_OBSERVED_AT)
            ),
        )
    elif drift == "runtime_release":
        runtime.release_sha = "c" * 40
    elif drift == "port_authority":
        scheduler_port = cast(Any, invocation._scheduler_port)
        scheduler_port.persistence_authority = _OTHER_PERSISTENCE_AUTHORITY
    else:
        runtime.outer_lease = replace(runtime.outer_lease, fencing_token=8)

    with pytest.raises(_FailStopTriggered) as exc_info:
        begin_scheduler_invocation_settlement(
            cast(Any, runtime),
            invocation,
            fail_stop=_raise_fail_stop,
        )

    assert exc_info.value.reason == "scheduler_settlement_provenance_invalid"


async def test_settlement_dispatch_rejects_outer_lease_callback_runtime_drift() -> None:
    clock = _Clock(100.0)
    invocation, runtime = await _deadline_context(clock)

    async def handler(_permit: SchedulerInvocationPermit) -> None:
        return None

    await run_with_scheduler_deadline(
        handler,
        invocation=invocation,
        wait_until=_ManualWaiter(),
        fail_stop=_must_not_fail_stop,
    )
    authorization = begin_scheduler_invocation_settlement(
        cast(Any, runtime),
        invocation,
        fail_stop=_must_not_fail_stop,
    )

    current_outer_lease = runtime.current_outer_lease

    def mutate_during_outer_lease_read() -> WorkerLease:
        runtime.release_sha = "c" * 40
        return current_outer_lease()

    cast(Any, runtime).current_outer_lease = mutate_during_outer_lease_read

    with pytest.raises(_FailStopTriggered) as exc_info:
        await complete_scheduler_invocation_settlement(
            authorization,
            result_sha256="d" * 64,
            fail_stop=_raise_fail_stop,
        )

    assert exc_info.value.reason == "scheduler_settlement_provenance_invalid"
    assert cast(_SchedulerPortStub, invocation._scheduler_port).completion_calls == []


async def test_settlement_dispatch_rechecks_cutoff_after_runtime_attestation() -> None:
    clock = _Clock(100.0)
    invocation, runtime = await _deadline_context(clock)

    async def handler(_permit: SchedulerInvocationPermit) -> None:
        return None

    await run_with_scheduler_deadline(
        handler,
        invocation=invocation,
        wait_until=_ManualWaiter(),
        fail_stop=_must_not_fail_stop,
    )
    clock.value = 111.999
    authorization = begin_scheduler_invocation_settlement(
        cast(Any, runtime),
        invocation,
        fail_stop=_must_not_fail_stop,
    )
    assert_intact = runtime.assert_intact
    attestation_count = 0

    def cross_cutoff_during_second_attestation() -> None:
        nonlocal attestation_count
        attestation_count += 1
        assert_intact()
        if attestation_count == 2:
            clock.value = 112.0

    cast(Any, runtime).assert_intact = cross_cutoff_during_second_attestation

    with pytest.raises(SchedulerInvocationSettlementWindowExceeded):
        await complete_scheduler_invocation_settlement(
            authorization,
            result_sha256="d" * 64,
            fail_stop=_must_not_fail_stop,
        )

    assert cast(_SchedulerPortStub, runtime.scheduler_port).completion_calls == []


async def test_claimed_invocation_can_run_only_once() -> None:
    invocation = await _deadline(_Clock(100.0))

    async def handler(_permit: SchedulerInvocationPermit) -> str:
        return "completed"

    assert (
        await run_with_scheduler_deadline(
            handler,
            invocation=invocation,
            wait_until=_ManualWaiter(),
            fail_stop=_must_not_fail_stop,
        )
        == "completed"
    )
    with pytest.raises(_FailStopTriggered) as exc_info:
        await run_with_scheduler_deadline(
            handler,
            invocation=invocation,
            wait_until=_ManualWaiter(),
            fail_stop=_raise_fail_stop,
        )
    assert exc_info.value.reason == "scheduler_deadline_provenance_invalid"


async def test_claimed_invocation_rejects_port_identity_tampering() -> None:
    claim = _claim(lease_seconds=16)
    invocation, port, _runtime = await _claim_once(
        _Clock(100.0),
        claim=claim,
        outer_lease=_outer_lease(lease_seconds=16),
    )
    assert invocation is not None
    port.release_sha = "c" * 40

    async def handler(_permit: SchedulerInvocationPermit) -> None:
        raise AssertionError("tampered invocation must not start")

    with pytest.raises(_FailStopTriggered) as exc_info:
        await run_with_scheduler_deadline(
            handler,
            invocation=invocation,
            wait_until=_ManualWaiter(),
            fail_stop=_raise_fail_stop,
        )
    assert exc_info.value.reason == "scheduler_deadline_provenance_invalid"


async def test_handler_error_is_preserved_and_revokes_the_permit() -> None:
    waiter = _ManualWaiter()
    invocation, runtime = await _deadline_context(_Clock(100.0))
    captured: list[SchedulerInvocationPermit] = []

    async def handler(permit: SchedulerInvocationPermit) -> None:
        captured.append(permit)
        raise RuntimeError("handler_failed")

    with pytest.raises(RuntimeError, match="handler_failed"):
        await run_with_scheduler_deadline(
            handler,
            invocation=invocation,
            wait_until=waiter,
            fail_stop=_must_not_fail_stop,
        )

    assert captured[0].revocation_reason == "handler_exit"
    authorization = begin_scheduler_invocation_settlement(
        cast(Any, runtime),
        invocation,
        fail_stop=_must_not_fail_stop,
    )
    failure_sha256 = "e" * 64
    receipt = await fail_scheduler_invocation_settlement(
        authorization,
        failure_reason_code="handler_error",
        failure_sha256=failure_sha256,
        retryable=False,
        fail_stop=_must_not_fail_stop,
    )
    assert receipt.failure_reason_code == "handler_error"
    assert cast(_SchedulerPortStub, runtime.scheduler_port).failure_calls == [
        (
            invocation.claim,
            runtime.outer_lease,
            "handler_error",
            failure_sha256,
            False,
        )
    ]


async def test_handler_cannot_hide_a_transient_clock_failure() -> None:
    clock = _Clock(100.0)
    deadline = await _deadline(clock)

    async def handler(permit: SchedulerInvocationPermit) -> str:
        clock.fail = True
        try:
            require_scheduler_invocation_permit(
                permit,
                expected_binding=deadline.binding,
            )
        except SchedulerInvocationPermitRevoked as exc:
            assert exc.reason == "clock_corrupt"
        finally:
            clock.fail = False
        return "must_not_escape"

    with pytest.raises(_FailStopTriggered) as exc_info:
        await run_with_scheduler_deadline(
            handler,
            invocation=deadline,
            wait_until=_ManualWaiter(),
            fail_stop=_raise_fail_stop,
        )

    assert exc_info.value.reason == "scheduler_deadline_clock_corrupt_or_timer_early"


async def test_handler_cannot_convert_deadline_revocation_to_success() -> None:
    clock = _Clock(100.0)
    deadline = await _deadline(clock)

    async def handler(permit: SchedulerInvocationPermit) -> str:
        clock.value = 110.0
        try:
            require_scheduler_invocation_permit(
                permit,
                expected_binding=deadline.binding,
            )
        except SchedulerInvocationPermitRevoked as exc:
            assert exc.reason == "deadline"
        return "must_not_escape"

    with pytest.raises(SchedulerInvocationDeadlineExceeded) as exc_info:
        await run_with_scheduler_deadline(
            handler,
            invocation=deadline,
            wait_until=_ManualWaiter(),
            fail_stop=_must_not_fail_stop,
        )

    assert exc_info.value.reason_code == "command_poll_retryable"


async def test_background_effect_is_rejected_after_handler_return() -> None:
    clock = _Clock(100.0)
    deadline = await _deadline(clock)
    release = asyncio.Event()
    background_tasks: list[asyncio.Task[None]] = []

    async def handler(permit: SchedulerInvocationPermit) -> str:
        async def late_effect() -> None:
            await release.wait()
            permit.assert_effect_allowed(expected_binding=deadline.binding)

        background_tasks.append(asyncio.create_task(late_effect()))
        return "completed"

    assert (
        await run_with_scheduler_deadline(
            handler,
            invocation=deadline,
            wait_until=_ManualWaiter(),
            fail_stop=_must_not_fail_stop,
        )
        == "completed"
    )
    release.set()

    with pytest.raises(SchedulerInvocationPermitRevoked, match="handler_exit"):
        await background_tasks[0]


@pytest.mark.parametrize(
    ("job_key", "reason_code", "retryable"),
    [
        ("operations.commands", "command_poll_retryable", True),
        ("operations.execution", "execution_deadline_effect_unknown", False),
        ("operations.settlement", "settlement_deadline_effect_unknown", False),
        (
            "operations.reconciliation",
            "reconciliation_poll_retryable",
            True,
        ),
        ("operations.outbox", "outbox_poll_retryable", True),
    ],
)
async def test_deadline_cancels_the_handler_and_maps_job_failure_policy(
    job_key: SchedulerJobKey,
    reason_code: str,
    retryable: bool,
) -> None:
    clock = _Clock(100.0)
    waiter = _ManualWaiter()
    started = asyncio.Event()
    captured: list[SchedulerInvocationPermit] = []
    deadline, runtime = await _deadline_context(clock, job_key=job_key)

    async def handler(permit: SchedulerInvocationPermit) -> None:
        captured.append(permit)
        started.set()
        await asyncio.Event().wait()

    run_task = asyncio.create_task(
        run_with_scheduler_deadline(
            handler,
            invocation=deadline,
            wait_until=waiter,
            fail_stop=_must_not_fail_stop,
        )
    )
    await started.wait()
    await waiter.called.wait()
    clock.value = 110.0
    waiter.release.set()

    with pytest.raises(SchedulerInvocationDeadlineExceeded) as exc_info:
        await run_task

    assert exc_info.value.reason_code == reason_code
    assert exc_info.value.retryable is retryable
    assert captured[0].revocation_reason == "deadline"
    clock.value = 111.0
    authorization = begin_scheduler_invocation_settlement(
        cast(Any, runtime),
        deadline,
        fail_stop=_must_not_fail_stop,
    )
    failure_sha256 = "e" * 64
    await fail_scheduler_invocation_settlement(
        authorization,
        failure_reason_code=reason_code,
        failure_sha256=failure_sha256,
        retryable=retryable,
        fail_stop=_must_not_fail_stop,
    )
    assert cast(_SchedulerPortStub, runtime.scheduler_port).failure_calls == [
        (
            deadline.claim,
            runtime.outer_lease,
            reason_code,
            failure_sha256,
            retryable,
        )
    ]


async def test_deadline_wins_when_handler_and_timer_complete_together() -> None:
    clock = _Clock(100.0)
    gate = asyncio.Event()
    waiter_started = asyncio.Event()
    deadline = await _deadline(clock, job_key="operations.execution")

    async def handler(_permit: SchedulerInvocationPermit) -> str:
        await gate.wait()
        return "must_not_escape"

    async def waiter(_cutoff: float) -> None:
        waiter_started.set()
        await gate.wait()

    run_task = asyncio.create_task(
        run_with_scheduler_deadline(
            handler,
            invocation=deadline,
            wait_until=waiter,
            fail_stop=_must_not_fail_stop,
        )
    )
    await waiter_started.wait()
    clock.value = 110.0
    gate.set()

    with pytest.raises(SchedulerInvocationDeadlineExceeded) as exc_info:
        await run_task

    assert exc_info.value.reason_code == "execution_deadline_effect_unknown"
    assert not exc_info.value.retryable


@pytest.mark.parametrize("outcome", ["fail_stop", "self_cancel"])
async def test_deadline_same_tick_unsafe_handler_never_authorizes_settlement(
    outcome: str,
) -> None:
    clock = _Clock(100.0)
    gate = asyncio.Event()
    handler_started = asyncio.Event()
    waiter_started = asyncio.Event()
    invocation, runtime = await _deadline_context(
        clock,
        job_key="operations.execution",
    )

    async def handler(_permit: SchedulerInvocationPermit) -> None:
        handler_started.set()
        await gate.wait()
        if outcome == "fail_stop":
            raise _FailStopTriggered("handler_fail_stop")
        raise asyncio.CancelledError("handler_self_cancelled")

    async def waiter(_cutoff: float) -> None:
        waiter_started.set()
        await gate.wait()

    run_task = asyncio.create_task(
        run_with_scheduler_deadline(
            handler,
            invocation=invocation,
            wait_until=waiter,
            fail_stop=_raise_fail_stop,
        )
    )
    await handler_started.wait()
    await waiter_started.wait()
    clock.value = 110.0
    gate.set()

    with pytest.raises(_FailStopTriggered) as exc_info:
        await run_task

    expected_reason = (
        "handler_fail_stop"
        if outcome == "fail_stop"
        else "scheduler_handler_cancellation_suppressed"
    )
    assert exc_info.value.reason == expected_reason
    with pytest.raises(_FailStopTriggered) as settlement_exc:
        begin_scheduler_invocation_settlement(
            cast(Any, runtime),
            invocation,
            fail_stop=_raise_fail_stop,
        )
    assert settlement_exc.value.reason == "scheduler_settlement_provenance_invalid"


async def test_deadline_path_cancels_a_timer_that_has_not_fired() -> None:
    clock = _Clock(100.0)
    waiter = _ManualWaiter()
    handler_started = asyncio.Event()
    handler_release = asyncio.Event()
    deadline = await _deadline(clock, job_key="operations.execution")

    async def handler(_permit: SchedulerInvocationPermit) -> str:
        handler_started.set()
        await handler_release.wait()
        return "must_not_escape"

    run_task = asyncio.create_task(
        run_with_scheduler_deadline(
            handler,
            invocation=deadline,
            wait_until=waiter,
            fail_stop=_must_not_fail_stop,
        )
    )
    await handler_started.wait()
    await waiter.called.wait()
    clock.value = 110.0
    handler_release.set()

    with pytest.raises(SchedulerInvocationDeadlineExceeded):
        await run_task

    assert waiter.cancelled


@pytest.mark.parametrize(
    ("waiter_kind", "reason"),
    [
        ("early", "scheduler_deadline_clock_corrupt_or_timer_early"),
        ("error", "scheduler_deadline_timer_failed"),
        ("cancel", "scheduler_deadline_timer_failed"),
    ],
)
async def test_timer_anomalies_trigger_fail_stop(
    waiter_kind: str,
    reason: str,
) -> None:
    started = asyncio.Event()
    clock = _Clock(100.0)
    deadline = await _deadline(clock)

    async def handler(_permit: SchedulerInvocationPermit) -> None:
        started.set()
        await asyncio.Event().wait()

    async def waiter(_cutoff: float) -> None:
        if waiter_kind == "error":
            raise RuntimeError("timer_failed")
        if waiter_kind == "cancel":
            raise asyncio.CancelledError

    run_task = asyncio.create_task(
        run_with_scheduler_deadline(
            handler,
            invocation=deadline,
            wait_until=waiter,
            fail_stop=_raise_fail_stop,
        )
    )
    await started.wait()

    with pytest.raises(_FailStopTriggered) as exc_info:
        await run_task

    assert exc_info.value.reason == reason


@pytest.mark.parametrize(
    ("timer_outcome", "reason"),
    [
        ("return", "scheduler_deadline_clock_corrupt_or_timer_early"),
        ("error", "scheduler_deadline_timer_failed"),
    ],
)
async def test_handler_success_cannot_hide_timer_cancellation_suppression(
    timer_outcome: str,
    reason: str,
) -> None:
    waiter_started = asyncio.Event()
    handler_release = asyncio.Event()
    deadline = await _deadline(_Clock(100.0))

    async def handler(_permit: SchedulerInvocationPermit) -> str:
        await handler_release.wait()
        return "completed"

    async def waiter(_cutoff: float) -> None:
        waiter_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if timer_outcome == "error":
                raise RuntimeError("timer_cancel_suppressed") from None
            return

    run_task = asyncio.create_task(
        run_with_scheduler_deadline(
            handler,
            invocation=deadline,
            wait_until=waiter,
            fail_stop=_raise_fail_stop,
        )
    )
    await waiter_started.wait()
    handler_release.set()

    with pytest.raises(_FailStopTriggered) as exc_info:
        await run_task

    assert exc_info.value.reason == reason


async def test_stalled_timer_cancellation_triggers_fail_stop_without_task_leak() -> None:
    waiter_started = asyncio.Event()
    handler_release = asyncio.Event()
    timer_release = asyncio.Event()
    timer_tasks: list[asyncio.Task[None]] = []
    deadline = await _deadline(_Clock(100.0))

    async def handler(_permit: SchedulerInvocationPermit) -> str:
        await handler_release.wait()
        return "completed"

    async def waiter(_cutoff: float) -> None:
        current = asyncio.current_task()
        assert current is not None
        timer_tasks.append(current)
        waiter_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await timer_release.wait()

    run_task = asyncio.create_task(
        run_with_scheduler_deadline(
            handler,
            invocation=deadline,
            wait_until=waiter,
            fail_stop=_raise_fail_stop,
        )
    )
    await waiter_started.wait()
    handler_release.set()

    with pytest.raises(_FailStopTriggered) as exc_info:
        await run_task

    assert exc_info.value.reason == "scheduler_deadline_timer_cancellation_suppressed"
    timer_release.set()
    await timer_tasks[0]


async def test_external_cancellation_is_preserved_after_children_acknowledge() -> None:
    started = asyncio.Event()
    captured: list[SchedulerInvocationPermit] = []
    deadline, runtime = await _deadline_context(_Clock(100.0))

    async def handler(permit: SchedulerInvocationPermit) -> None:
        captured.append(permit)
        started.set()
        await asyncio.Event().wait()

    run_task = asyncio.create_task(
        run_with_scheduler_deadline(
            handler,
            invocation=deadline,
            wait_until=_ManualWaiter(),
            fail_stop=_must_not_fail_stop,
        )
    )
    await started.wait()
    run_task.cancel("outer_stop")

    with pytest.raises(asyncio.CancelledError) as exc_info:
        await run_task

    assert exc_info.value.args == ("outer_stop",)
    assert captured[0].revocation_reason == "external_cancel"
    with pytest.raises(_FailStopTriggered) as settlement_exc:
        begin_scheduler_invocation_settlement(
            cast(Any, runtime),
            deadline,
            fail_stop=_raise_fail_stop,
        )
    assert settlement_exc.value.reason == "scheduler_settlement_provenance_invalid"


async def test_handler_self_cancellation_never_authorizes_settlement() -> None:
    invocation, runtime = await _deadline_context(_Clock(100.0))

    async def handler(_permit: SchedulerInvocationPermit) -> None:
        raise asyncio.CancelledError("handler_cancelled")

    with pytest.raises(asyncio.CancelledError) as exc_info:
        await run_with_scheduler_deadline(
            handler,
            invocation=invocation,
            wait_until=_ManualWaiter(),
            fail_stop=_must_not_fail_stop,
        )

    assert exc_info.value.args == ("handler_cancelled",)
    with pytest.raises(_FailStopTriggered) as settlement_exc:
        begin_scheduler_invocation_settlement(
            cast(Any, runtime),
            invocation,
            fail_stop=_raise_fail_stop,
        )
    assert settlement_exc.value.reason == "scheduler_settlement_provenance_invalid"


async def test_handler_fail_stop_base_exception_never_authorizes_settlement() -> None:
    invocation, runtime = await _deadline_context(_Clock(100.0))

    async def handler(_permit: SchedulerInvocationPermit) -> None:
        raise _FailStopTriggered("handler_fail_stop")

    with pytest.raises(_FailStopTriggered, match="handler_fail_stop"):
        await run_with_scheduler_deadline(
            handler,
            invocation=invocation,
            wait_until=_ManualWaiter(),
            fail_stop=_must_not_fail_stop,
        )

    with pytest.raises(_FailStopTriggered) as settlement_exc:
        begin_scheduler_invocation_settlement(
            cast(Any, runtime),
            invocation,
            fail_stop=_raise_fail_stop,
        )
    assert settlement_exc.value.reason == "scheduler_settlement_provenance_invalid"


@pytest.mark.parametrize("suppression", ["return", "error", "wrong_revoke"])
async def test_handler_cancellation_suppression_triggers_fail_stop(
    suppression: str,
) -> None:
    clock = _Clock(100.0)
    waiter = _ManualWaiter()
    started = asyncio.Event()
    deadline = await _deadline(clock, job_key="operations.execution")

    async def handler(_permit: SchedulerInvocationPermit) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if suppression == "error":
                raise RuntimeError("cancel_suppressed") from None
            if suppression == "wrong_revoke":
                raise SchedulerInvocationPermitRevoked("forged_reason") from None
            return

    run_task = asyncio.create_task(
        run_with_scheduler_deadline(
            handler,
            invocation=deadline,
            wait_until=waiter,
            fail_stop=_raise_fail_stop,
        )
    )
    await started.wait()
    await waiter.called.wait()
    clock.value = 110.0
    waiter.release.set()

    with pytest.raises(_FailStopTriggered) as exc_info:
        await run_task

    assert exc_info.value.reason == "scheduler_handler_cancellation_suppressed"


async def test_handler_may_acknowledge_cancellation_by_observing_revoked_permit() -> None:
    clock = _Clock(100.0)
    waiter = _ManualWaiter()
    started = asyncio.Event()
    deadline = await _deadline(clock, job_key="operations.execution")

    async def handler(permit: SchedulerInvocationPermit) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            permit.assert_effect_allowed(expected_binding=deadline.binding)

    run_task = asyncio.create_task(
        run_with_scheduler_deadline(
            handler,
            invocation=deadline,
            wait_until=waiter,
            fail_stop=_must_not_fail_stop,
        )
    )
    await started.wait()
    await waiter.called.wait()
    clock.value = 110.0
    waiter.release.set()

    with pytest.raises(SchedulerInvocationDeadlineExceeded):
        await run_task


async def test_handler_that_ignores_cancellation_grace_triggers_fail_stop() -> None:
    clock = _Clock(100.0)
    waiter = _ManualWaiter()
    started = asyncio.Event()
    release = asyncio.Event()
    handler_tasks: list[asyncio.Task[None]] = []
    deadline = await _deadline(clock, job_key="operations.execution")

    async def handler(_permit: SchedulerInvocationPermit) -> None:
        current = asyncio.current_task()
        assert current is not None
        handler_tasks.append(current)
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()

    run_task = asyncio.create_task(
        run_with_scheduler_deadline(
            handler,
            invocation=deadline,
            wait_until=waiter,
            fail_stop=_raise_fail_stop,
        )
    )
    await started.wait()
    await waiter.called.wait()
    clock.value = 110.0
    waiter.release.set()

    with pytest.raises(_FailStopTriggered) as exc_info:
        await run_task

    assert exc_info.value.reason == "scheduler_handler_cancellation_suppressed"
    release.set()
    await handler_tasks[0]


async def test_invalid_initial_clock_fails_before_starting_the_handler() -> None:
    called = False
    clock = _Clock(100.0)
    deadline = await _deadline(clock)

    async def handler(_permit: SchedulerInvocationPermit) -> None:
        nonlocal called
        called = True

    clock.fail = True

    with pytest.raises(_FailStopTriggered) as exc_info:
        await run_with_scheduler_deadline(
            handler,
            invocation=deadline,
            fail_stop=_raise_fail_stop,
        )

    assert exc_info.value.reason == "scheduler_deadline_clock_corrupt"
    assert not called


async def test_clock_regression_since_rpc_start_fails_before_handler_start() -> None:
    called = False
    clock = _Clock(100.0)
    deadline = await _deadline(clock)
    clock.value = 99.0

    async def handler(_permit: SchedulerInvocationPermit) -> None:
        nonlocal called
        called = True

    with pytest.raises(_FailStopTriggered) as exc_info:
        await run_with_scheduler_deadline(
            handler,
            invocation=deadline,
            fail_stop=_raise_fail_stop,
        )

    assert exc_info.value.reason == "scheduler_deadline_clock_corrupt"
    assert not called


async def test_already_expired_deadline_does_not_start_the_handler() -> None:
    called = False
    clock = _Clock(100.0)
    deadline = await _deadline(
        clock,
        job_key="operations.settlement",
        remaining_seconds=0.0,
    )

    async def handler(_permit: SchedulerInvocationPermit) -> None:
        nonlocal called
        called = True

    with pytest.raises(SchedulerInvocationDeadlineExceeded) as exc_info:
        await run_with_scheduler_deadline(
            handler,
            invocation=deadline,
            fail_stop=_must_not_fail_stop,
        )

    assert exc_info.value.reason_code == "settlement_deadline_effect_unknown"
    assert not called


async def test_returning_fail_stop_is_rejected_as_a_contract_violation() -> None:
    async def handler(_permit: SchedulerInvocationPermit) -> None:
        raise AssertionError("handler_must_not_start")

    returning_fail_stop = cast(FailStop, lambda _reason: None)
    clock = _Clock(100.0)
    deadline = await _deadline(clock)
    clock.fail = True

    with pytest.raises(SchedulerInvocationFailStopReturned):
        await run_with_scheduler_deadline(
            handler,
            invocation=deadline,
            fail_stop=returning_fail_stop,
        )


async def test_default_waiter_enforces_a_real_monotonic_deadline() -> None:
    deadline = await _deadline(
        monotonic,
        job_key="operations.outbox",
        remaining_seconds=0.01,
    )

    async def handler(_permit: SchedulerInvocationPermit) -> None:
        await asyncio.Event().wait()

    with pytest.raises(SchedulerInvocationDeadlineExceeded):
        await run_with_scheduler_deadline(
            handler,
            invocation=deadline,
            fail_stop=_must_not_fail_stop,
        )
