from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

import pytest

from app.application.ports.durable_scheduler_runtime_port import (
    DurableSchedulerRuntimePort,
)
from app.application.ports.worker_heartbeat_port import WorkerHeartbeatPort
from app.application.services.durable_scheduler_loop import DurableSchedulerLoop
from app.application.use_cases.maintain_worker_lease import MaintainWorkerLease
from app.application.use_cases.run_durable_scheduler import (
    SCHEDULER_CONVERGENCE_ORDER,
    DurableSchedulerConvergenceResult,
    DurableSchedulerRunResult,
)
from app.domain.common.json import JsonObject
from app.domain.operations.models import RecordedWorkerHeartbeat, WorkerHeartbeatStatus
from app.domain.scheduler.models import (
    ScheduledJobClaimV1,
    ScheduledJobCompletionReceiptV1,
    ScheduledJobConvergenceDefinitionV1,
    ScheduledJobDefinitionV1,
    ScheduledJobLeaseV1,
    ScheduledJobRunV1,
    SchedulerDefinitionConvergenceReceiptV1,
    SchedulerInvariantError,
    SchedulerJobKey,
)

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)


class _StageHealthResult:
    def __init__(
        self,
        *,
        failed: int = 0,
        unacknowledged: int = 0,
        blocked: int = 0,
        manual: int = 0,
        dead_lettered: int = 0,
        unknown_failed: int = 0,
    ) -> None:
        self.failed = failed
        self.unacknowledged = unacknowledged
        self.blocked = blocked
        self.manual = manual
        self.dead_lettered = dead_lettered
        self.unknown_failed = unknown_failed
WORKER_ID = "44444444-4444-4444-8444-444444444444"
ACCOUNT_ID = "paper-primary"
RELEASE_SHA = "a" * 40
RESULT_SHA = "b" * 64
DEFINITION_IDS: Mapping[SchedulerJobKey, str] = {
    "operations.commands": "81111111-1111-4111-8111-111111111111",
    "operations.execution": "82222222-2222-4222-8222-222222222222",
    "operations.settlement": "83333333-3333-4333-8333-333333333333",
    "operations.reconciliation": "85555555-5555-4555-8555-555555555555",
    "operations.outbox": "86666666-6666-4666-8666-666666666666",
}
RUN_IDS: Mapping[SchedulerJobKey, str] = {
    "operations.commands": "11111111-1111-4111-8111-111111111111",
    "operations.execution": "22222222-2222-4222-8222-222222222222",
    "operations.settlement": "33333333-3333-4333-8333-333333333333",
    "operations.reconciliation": "55555555-5555-4555-8555-555555555555",
    "operations.outbox": "66666666-6666-4666-8666-666666666666",
}
LEASE_IDS: Mapping[SchedulerJobKey, str] = {
    "operations.commands": "71111111-1111-4111-8111-111111111111",
    "operations.execution": "72222222-2222-4222-8222-222222222222",
    "operations.settlement": "73333333-3333-4333-8333-333333333333",
    "operations.reconciliation": "75555555-5555-4555-8555-555555555555",
    "operations.outbox": "76666666-6666-4666-8666-666666666666",
}


async def test_run_once_drains_claimed_recovery_before_one_normal_claim() -> None:
    shutdown = FakeShutdown()
    lifecycle: list[str] = []
    recovery = _successful_result("operations.commands")
    normal = _successful_result("operations.execution")
    runtime = FakeRuntime(
        (
            _convergence({"operations.commands": "claimed"}, (recovery,)),
            _convergence(),
        ),
        (normal,),
        lifecycle,
    )
    lease = FakeLeaseManager(lifecycle)
    heartbeat = FakeHeartbeat(lifecycle)
    loop = _loop(
        runtime,
        lease,
        heartbeat,
        shutdown=shutdown,
        run_once=True,
    )

    cycle = await loop.run()

    assert cycle is not None
    assert cycle.convergence.outcome == "converged"
    assert cycle.run_result == normal
    assert runtime.convergence_calls == 2
    assert runtime.normal_calls == 1
    assert [item for item in lifecycle if item.startswith("scheduler:")] == [
        "scheduler:converge",
        "scheduler:converge",
        "scheduler:run_once",
    ]
    running = heartbeat.record_for("durable_scheduler_running")
    assert running[0] == "warning"
    success_times = cast(dict[str, object], running[1]["job_last_succeeded_at"])
    assert success_times["operations.commands"] == NOW.isoformat()
    assert success_times["operations.execution"] == NOW.isoformat()
    assert success_times["operations.settlement"] is None
    assert heartbeat.checkpoints == [
        "durable_scheduler_started",
        "durable_scheduler_running",
        "durable_scheduler_stopping",
    ]
    assert lifecycle[0] == "lease:acquire"
    assert lifecycle[-1] == "lease:release"


async def test_wait_blocks_normal_claim_without_spinning() -> None:
    runtime = FakeRuntime(
        (_convergence({"operations.commands": "wait"}),),
        (_idle_result(),),
    )
    lease = FakeLeaseManager()
    loop = _loop(
        runtime,
        lease,
        FakeHeartbeat(),
        shutdown=FakeShutdown(),
        run_once=True,
    )

    cycle = await loop.run()

    assert cycle is not None
    assert cycle.convergence.outcome == "wait"
    assert cycle.run_result is None
    assert runtime.convergence_calls == 1
    assert runtime.normal_calls == 0
    assert lease.renew_calls == 0
    assert lease.release_calls == 1


async def test_renewal_required_wait_renews_serially_then_retries() -> None:
    lifecycle: list[str] = []
    runtime = FakeRuntime(
        (
            _convergence(
                {"operations.commands": "wait"},
                wait_reason="scheduler_outer_lease_renewal_required",
            ),
            _convergence(),
        ),
        (_idle_result(),),
        lifecycle,
    )
    lease = FakeLeaseManager(lifecycle)
    loop = _loop(
        runtime,
        lease,
        FakeHeartbeat(lifecycle),
        shutdown=FakeShutdown(),
        run_once=True,
    )

    cycle = await loop.run()

    assert cycle is not None and cycle.convergence.outcome == "converged"
    assert runtime.normal_calls == 1
    assert lease.renew_calls == 1
    assert [item for item in lifecycle if item in {"scheduler:converge", "lease:renew"}] == [
        "scheduler:converge",
        "lease:renew",
        "scheduler:converge",
    ]


async def test_manual_resolution_fails_closed_and_cleans_up() -> None:
    runtime = FakeRuntime(
        (_convergence({"operations.commands": "manual_resolution"}),),
        (),
    )
    lease = FakeLeaseManager()
    heartbeat = FakeHeartbeat()
    loop = _loop(
        runtime,
        lease,
        heartbeat,
        shutdown=FakeShutdown(),
        run_once=True,
    )

    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_convergence_requires_manual_resolution",
    ):
        await loop.run()

    assert runtime.normal_calls == 0
    assert lease.release_calls == 1
    assert heartbeat.checkpoints[-1] == "durable_scheduler_stopping"


async def test_claimed_recovery_is_bounded() -> None:
    claimed = _convergence(
        {"operations.commands": "claimed"},
        (_successful_result("operations.commands"),),
    )
    runtime = FakeRuntime((claimed, claimed, claimed), ())
    lease = FakeLeaseManager()
    loop = _loop(
        runtime,
        lease,
        FakeHeartbeat(),
        shutdown=FakeShutdown(),
        run_once=True,
        max_convergence_steps=3,
    )

    with pytest.raises(
        SchedulerInvariantError,
        match="scheduler_convergence_step_budget_exhausted",
    ):
        await loop.run()

    assert runtime.convergence_calls == 3
    assert runtime.normal_calls == 0
    assert lease.release_calls == 1


async def test_shutdown_during_active_invocation_drains_then_stops_new_claims() -> None:
    shutdown = FakeShutdown()
    lifecycle: list[str] = []

    async def during_run(_call_number: int) -> None:
        lifecycle.append("invocation:started")
        shutdown.request()
        await asyncio.sleep(0)
        lifecycle.append("invocation:finished")

    runtime = FakeRuntime(
        (_convergence(),),
        (_successful_result("operations.commands"),),
        lifecycle,
        before_normal_return=during_run,
    )
    loop = _loop(
        runtime,
        FakeLeaseManager(lifecycle),
        FakeHeartbeat(lifecycle),
        shutdown=shutdown,
        run_once=False,
    )

    cycle = await loop.run()

    assert cycle is not None
    assert runtime.normal_calls == 1
    assert lifecycle.index("invocation:started") < lifecycle.index("invocation:finished")
    assert lifecycle.index("invocation:finished") < lifecycle.index("lease:release")


async def test_periodic_renewal_failure_cancels_active_scheduler_and_releases() -> None:
    scheduler_started = asyncio.Event()
    scheduler_cancelled = asyncio.Event()
    lease = FakeLeaseManager(renew_error=RuntimeError("lease_renewal_failed"))

    async def block_convergence(_call_number: int) -> None:
        scheduler_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            scheduler_cancelled.set()
            raise

    runtime = FakeRuntime(
        (_convergence(),),
        (),
        before_convergence_return=block_convergence,
    )

    async def renewal_first_sleep(seconds: float) -> None:
        if seconds == 1.0:
            await scheduler_started.wait()
            return
        await asyncio.Event().wait()

    loop = _loop(
        runtime,
        lease,
        FakeHeartbeat(),
        shutdown=FakeShutdown(),
        run_once=True,
        lease_renew_interval_sec=1.0,
        heartbeat_interval_sec=99.0,
        sleep=renewal_first_sleep,
    )

    with pytest.raises(RuntimeError, match="lease_renewal_failed"):
        await loop.run()

    assert scheduler_cancelled.is_set()
    assert lease.renew_calls == 1
    assert lease.release_calls == 1


async def test_base_exception_fail_stop_propagates_after_shutdown_cleanup() -> None:
    class ProcessFailStop(BaseException):
        pass

    lifecycle: list[str] = []

    async def fail_stop(_call_number: int) -> None:
        raise ProcessFailStop("sealed_runtime_changed")

    runtime = FakeRuntime(
        (_convergence(),),
        (),
        lifecycle,
        before_convergence_return=fail_stop,
    )
    lease = FakeLeaseManager(lifecycle)
    heartbeat = FakeHeartbeat(lifecycle)
    loop = _loop(
        runtime,
        lease,
        heartbeat,
        shutdown=FakeShutdown(),
        run_once=True,
    )

    with pytest.raises(ProcessFailStop, match="sealed_runtime_changed"):
        await loop.run()

    assert heartbeat.checkpoints[-1] == "durable_scheduler_stopping"
    assert lease.release_calls == 1
    assert lifecycle[-1] == "lease:release"


async def test_primary_fail_stop_is_not_masked_by_cleanup_failures() -> None:
    class ProcessFailStop(BaseException):
        pass

    class FailingRelease(FakeLeaseManager):
        async def release(self) -> object:
            await super().release()
            raise RuntimeError("lease_release_failed")

    class FailingStoppingHeartbeat(FakeHeartbeat):
        async def record_worker_heartbeat(
            self,
            *,
            worker_id: str,
            status: WorkerHeartbeatStatus,
            details: JsonObject,
            now: datetime,
        ) -> RecordedWorkerHeartbeat:
            recorded = await super().record_worker_heartbeat(
                worker_id=worker_id,
                status=status,
                details=details,
                now=now,
            )
            if details["checkpoint"] == "durable_scheduler_stopping":
                raise ValueError("shutdown_heartbeat_failed")
            return recorded

    async def fail_stop(_call_number: int) -> None:
        raise ProcessFailStop("sealed_runtime_changed")

    runtime = FakeRuntime(
        (_convergence(),),
        (),
        before_convergence_return=fail_stop,
    )
    loop = _loop(
        runtime,
        FailingRelease(),
        FailingStoppingHeartbeat(),
        shutdown=FakeShutdown(),
        run_once=True,
    )

    with pytest.raises(ProcessFailStop, match="sealed_runtime_changed") as raised:
        await loop.run()

    notes = getattr(raised.value, "__notes__", ())
    assert "scheduler cleanup also failed: ValueError" in notes
    assert "scheduler cleanup also failed: RuntimeError" in notes


async def test_successful_cycle_reports_all_cleanup_failures() -> None:
    class FailingRelease(FakeLeaseManager):
        async def release(self) -> object:
            await super().release()
            raise RuntimeError("lease_release_failed")

    class FailingStoppingHeartbeat(FakeHeartbeat):
        async def record_worker_heartbeat(
            self,
            *,
            worker_id: str,
            status: WorkerHeartbeatStatus,
            details: JsonObject,
            now: datetime,
        ) -> RecordedWorkerHeartbeat:
            recorded = await super().record_worker_heartbeat(
                worker_id=worker_id,
                status=status,
                details=details,
                now=now,
            )
            if details["checkpoint"] == "durable_scheduler_stopping":
                raise ValueError("shutdown_heartbeat_failed")
            return recorded

    loop = _loop(
        FakeRuntime((_convergence(),), (_idle_result(),)),
        FailingRelease(),
        FailingStoppingHeartbeat(),
        shutdown=FakeShutdown(),
        run_once=True,
    )

    with pytest.raises(BaseExceptionGroup) as raised:
        await loop.run()

    assert {type(error) for error in raised.value.exceptions} == {
        RuntimeError,
        ValueError,
    }


async def test_running_heartbeat_becomes_ok_only_after_all_jobs_succeed() -> None:
    shutdown = FakeShutdown()
    safe_jobs: tuple[SchedulerJobKey, ...] = (
        "operations.commands",
        "operations.reconciliation",
        "operations.outbox",
    )
    recovery_results = tuple(_successful_result(job_key) for job_key in safe_jobs)
    heartbeat = CoordinatedHeartbeat()
    runtime = FakeRuntime(
        (
            _convergence({job_key: "claimed" for job_key in safe_jobs}, recovery_results),
            _convergence(),
        ),
        (
            _successful_result("operations.execution"),
            _successful_result("operations.settlement"),
        ),
    )
    sleep = HeartbeatPulseSleep(heartbeat, shutdown)
    loop = _loop(
        runtime,
        FakeLeaseManager(),
        heartbeat,
        shutdown=shutdown,
        run_once=False,
        poll_interval_sec=2.0,
        heartbeat_interval_sec=1.0,
        lease_renew_interval_sec=99.0,
        sleep=sleep,
    )

    await loop.run()

    running_statuses = [
        status
        for status, details, _now in heartbeat.records
        if details["checkpoint"] == "durable_scheduler_running"
    ]
    assert running_statuses == ["warning", "ok"]
    assert runtime.normal_calls == 2


@pytest.mark.parametrize(
    ("execution_result", "expected_status", "expected_job_list"),
    (
        (
            _StageHealthResult(failed=1),
            "error",
            "error_jobs",
        ),
        (
            _StageHealthResult(blocked=1),
            "warning",
            "warning_jobs",
        ),
    ),
)
async def test_running_heartbeat_preserves_business_health_after_scheduler_success(
    execution_result: _StageHealthResult,
    expected_status: WorkerHeartbeatStatus,
    expected_job_list: str,
) -> None:
    shutdown = FakeShutdown()
    safe_jobs: tuple[SchedulerJobKey, ...] = (
        "operations.commands",
        "operations.reconciliation",
        "operations.outbox",
    )
    heartbeat = CoordinatedHeartbeat()
    runtime = FakeRuntime(
        (
            _convergence(
                {job_key: "claimed" for job_key in safe_jobs},
                tuple(_successful_result(job_key) for job_key in safe_jobs),
            ),
            _convergence(),
        ),
        (
            _successful_result(
                "operations.execution",
                handler_result=execution_result,
            ),
            _successful_result("operations.settlement"),
        ),
    )
    loop = _loop(
        runtime,
        FakeLeaseManager(),
        heartbeat,
        shutdown=shutdown,
        run_once=False,
        poll_interval_sec=2.0,
        heartbeat_interval_sec=1.0,
        lease_renew_interval_sec=99.0,
        sleep=HeartbeatPulseSleep(heartbeat, shutdown),
    )

    await loop.run()

    final_status, final_details, _now = heartbeat.records[-2]
    assert final_details["checkpoint"] == "durable_scheduler_running"
    assert final_status == expected_status
    assert final_details[expected_job_list] == ["operations.execution"]
    assert all(
        status != "ok"
        for status, details, _now in heartbeat.records
        if details["checkpoint"] == "durable_scheduler_running"
    )


class FakeShutdown:
    def __init__(self) -> None:
        self._requested = False

    @property
    def requested(self) -> bool:
        return self._requested

    def request(self) -> None:
        self._requested = True


class FakeLeaseManager:
    def __init__(
        self,
        lifecycle: list[str] | None = None,
        *,
        renew_error: Exception | None = None,
    ) -> None:
        self.lifecycle = lifecycle
        self.renew_error = renew_error
        self.acquired = False
        self.renew_calls = 0
        self.release_calls = 0

    async def acquire(self) -> object:
        assert not self.acquired
        self.acquired = True
        self._record("lease:acquire")
        return object()

    async def renew(self) -> object:
        assert self.acquired
        self.renew_calls += 1
        self._record("lease:renew")
        if self.renew_error is not None:
            raise self.renew_error
        return object()

    async def release(self) -> object:
        assert self.acquired
        self.acquired = False
        self.release_calls += 1
        self._record("lease:release")
        return object()

    def _record(self, value: str) -> None:
        if self.lifecycle is not None:
            self.lifecycle.append(value)


class FakeHeartbeat:
    def __init__(self, lifecycle: list[str] | None = None) -> None:
        self.lifecycle = lifecycle
        self.records: list[tuple[WorkerHeartbeatStatus, JsonObject, datetime]] = []

    @property
    def checkpoints(self) -> list[object]:
        return [details["checkpoint"] for _status, details, _now in self.records]

    async def record_worker_heartbeat(
        self,
        *,
        worker_id: str,
        status: WorkerHeartbeatStatus,
        details: JsonObject,
        now: datetime,
    ) -> RecordedWorkerHeartbeat:
        assert worker_id == WORKER_ID
        self.records.append((status, details, now))
        if self.lifecycle is not None:
            self.lifecycle.append(f"heartbeat:{details['checkpoint']}")
        return RecordedWorkerHeartbeat(
            heartbeat_id="99999999-9999-4999-8999-999999999999",
            created_at=now,
        )

    def record_for(self, checkpoint: str) -> tuple[WorkerHeartbeatStatus, JsonObject, datetime]:
        return next(
            record
            for record in self.records
            if record[1]["checkpoint"] == checkpoint
        )


class CoordinatedHeartbeat(FakeHeartbeat):
    def __init__(self) -> None:
        super().__init__()
        self.running_recorded = asyncio.Event()

    async def record_worker_heartbeat(
        self,
        *,
        worker_id: str,
        status: WorkerHeartbeatStatus,
        details: JsonObject,
        now: datetime,
    ) -> RecordedWorkerHeartbeat:
        recorded = await super().record_worker_heartbeat(
            worker_id=worker_id,
            status=status,
            details=details,
            now=now,
        )
        if details["checkpoint"] == "durable_scheduler_running":
            self.running_recorded.set()
        return recorded


class HeartbeatPulseSleep:
    def __init__(self, heartbeat: CoordinatedHeartbeat, shutdown: FakeShutdown) -> None:
        self.heartbeat = heartbeat
        self.shutdown = shutdown
        self.pulse = asyncio.Event()
        self.polls = 0

    async def __call__(self, seconds: float) -> None:
        if seconds == 1.0:
            await self.pulse.wait()
            self.pulse.clear()
            return
        if seconds == 2.0:
            self.polls += 1
            self.heartbeat.running_recorded.clear()
            self.pulse.set()
            await self.heartbeat.running_recorded.wait()
            if self.polls == 2:
                self.shutdown.request()
            return
        await asyncio.Event().wait()


class FakeRuntime:
    def __init__(
        self,
        convergence_results: tuple[DurableSchedulerConvergenceResult, ...],
        normal_results: tuple[DurableSchedulerRunResult, ...],
        lifecycle: list[str] | None = None,
        *,
        before_convergence_return: Callable[[int], Awaitable[None]] | None = None,
        before_normal_return: Callable[[int], Awaitable[None]] | None = None,
    ) -> None:
        self.convergence_results = list(convergence_results)
        self.normal_results = list(normal_results)
        self.lifecycle = lifecycle
        self.before_convergence_return = before_convergence_return
        self.before_normal_return = before_normal_return
        self.convergence_calls = 0
        self.normal_calls = 0
        self.intact_calls = 0

    def assert_intact(self) -> None:
        self.intact_calls += 1

    async def converge_step(self) -> DurableSchedulerConvergenceResult:
        self.convergence_calls += 1
        self._record("scheduler:converge")
        if self.before_convergence_return is not None:
            await self.before_convergence_return(self.convergence_calls)
        return self.convergence_results.pop(0)

    async def run_once(self) -> DurableSchedulerRunResult:
        self.normal_calls += 1
        self._record("scheduler:run_once")
        if self.before_normal_return is not None:
            await self.before_normal_return(self.normal_calls)
        return self.normal_results.pop(0)

    def _record(self, value: str) -> None:
        if self.lifecycle is not None:
            self.lifecycle.append(value)


def _loop(
    runtime: FakeRuntime,
    lease: FakeLeaseManager,
    heartbeat: FakeHeartbeat,
    *,
    shutdown: FakeShutdown,
    run_once: bool,
    poll_interval_sec: float = 10.0,
    heartbeat_interval_sec: float = 20.0,
    lease_renew_interval_sec: float = 30.0,
    max_convergence_steps: int = 8,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> DurableSchedulerLoop:
    return DurableSchedulerLoop(
        cast(DurableSchedulerRuntimePort, runtime),
        cast(MaintainWorkerLease, lease),
        cast(WorkerHeartbeatPort, heartbeat),
        worker_id=WORKER_ID,
        shutdown=shutdown,
        run_once=run_once,
        poll_interval_sec=poll_interval_sec,
        heartbeat_interval_sec=heartbeat_interval_sec,
        lease_renew_interval_sec=lease_renew_interval_sec,
        max_convergence_steps=max_convergence_steps,
        clock=lambda: NOW,
        sleep=sleep or _never_sleep,
    )


async def _never_sleep(_seconds: float) -> None:
    await asyncio.Event().wait()


def _definition(job_key: SchedulerJobKey) -> ScheduledJobDefinitionV1:
    return ScheduledJobDefinitionV1(
        job_key=job_key,
        interval_seconds=2,
        lease_ttl_seconds=30,
        max_attempts=1 if job_key in {"operations.execution", "operations.settlement"} else 3,
        retry_base_seconds=2,
        retry_max_seconds=8,
        max_manual_replays=(
            0 if job_key in {"operations.execution", "operations.settlement"} else 1
        ),
    )


def _claim(job_key: SchedulerJobKey) -> ScheduledJobClaimV1:
    definition = _definition(job_key)
    run = ScheduledJobRunV1(
        run_id=RUN_IDS[job_key],
        account_id=ACCOUNT_ID,
        job_key=job_key,
        definition_sha256=definition.definition_sha256,
        state="leased",
        revision=1,
        attempt_count=1,
        replay_generation=0,
        replay_of_run_id=None,
        scheduled_for=NOW - timedelta(seconds=1),
        available_at=NOW - timedelta(seconds=1),
        created_at=NOW - timedelta(seconds=1),
        updated_at=NOW,
    )
    lease = ScheduledJobLeaseV1(
        lease_token=LEASE_IDS[job_key],
        run_id=run.run_id,
        account_id=ACCOUNT_ID,
        holder_id=WORKER_ID,
        release_sha=RELEASE_SHA,
        outer_fencing_token=1,
        attempt_number=1,
        run_revision=1,
        leased_at=NOW,
        lease_expires_at=NOW + timedelta(seconds=20),
    )
    return ScheduledJobClaimV1(
        definition=definition,
        run=run,
        lease=lease,
        observed_at=NOW,
    )


def _convergence(
    statuses: Mapping[
        SchedulerJobKey,
        Literal["converged", "claimed", "wait", "manual_resolution"],
    ] | None = None,
    run_results: tuple[DurableSchedulerRunResult, ...] = (),
    *,
    wait_reason: str = "active_run_wait",
) -> DurableSchedulerConvergenceResult:
    selected = statuses or {}
    receipts: list[SchedulerDefinitionConvergenceReceiptV1] = []
    for job_key in SCHEDULER_CONVERGENCE_ORDER:
        status = selected.get(job_key, "converged")
        definition = _definition(job_key)
        claim = _claim(job_key) if status == "claimed" else None
        active_run_id = (
            RUN_IDS[job_key]
            if status in {"claimed", "wait", "manual_resolution"}
            else None
        )
        receipts.append(
            SchedulerDefinitionConvergenceReceiptV1(
                status=status,
                definition=ScheduledJobConvergenceDefinitionV1(
                    definition_id=DEFINITION_IDS[job_key],
                    account_id=ACCOUNT_ID,
                    definition=claim.definition if claim is not None else definition,
                    revision=1,
                    next_due_at=NOW + timedelta(seconds=2),
                    scheduler_state="blocked" if status == "manual_resolution" else "ready",
                ),
                claim=claim,
                active_run_id=active_run_id,
                next_eligible_at=NOW + timedelta(seconds=3) if status == "wait" else None,
                reason_code=(
                    wait_reason
                    if status == "wait"
                    else "manual_resolution_required"
                    if status == "manual_resolution"
                    else None
                ),
                observed_at=NOW,
            )
        )
    return DurableSchedulerConvergenceResult(
        outcome=(
            "manual_resolution"
            if "manual_resolution" in selected.values()
            else "wait"
            if "wait" in selected.values()
            else "claimed"
            if "claimed" in selected.values()
            else "converged"
        ),
        receipts=tuple(receipts),
        run_results=run_results,
    )


def _successful_result(
    job_key: SchedulerJobKey,
    *,
    handler_result: object | None = None,
) -> DurableSchedulerRunResult:
    completion = ScheduledJobCompletionReceiptV1(
        run_id=RUN_IDS[job_key],
        run_revision=2,
        attempt_count=1,
        next_attempt_at=None,
        failure_reason_code=None,
        result_sha256=RESULT_SHA,
        observed_at=NOW,
    )
    return DurableSchedulerRunResult(
        outcome="succeeded",
        run_id=RUN_IDS[job_key],
        job_key=job_key,
        result_sha256=RESULT_SHA,
        completion=completion,
        failure=None,
        handler_result=handler_result or _StageHealthResult(),
    )


def _idle_result() -> DurableSchedulerRunResult:
    return DurableSchedulerRunResult(
        outcome="idle",
        run_id=None,
        job_key=None,
        result_sha256=None,
        completion=None,
        failure=None,
        handler_result=None,
    )
