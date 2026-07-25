from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, TypeVar, cast
from uuid import UUID

from app.application.ports.durable_scheduler_runtime_port import (
    DurableSchedulerRuntimePort,
)
from app.application.ports.worker_heartbeat_port import WorkerHeartbeatPort
from app.application.services.scheduler_job_health import (
    SchedulerJobHealth,
    classify_successful_scheduler_job,
)
from app.application.use_cases.maintain_worker_lease import MaintainWorkerLease
from app.application.use_cases.run_durable_scheduler import (
    SCHEDULER_CONVERGENCE_ORDER,
    DurableSchedulerConvergenceResult,
    DurableSchedulerRunResult,
)
from app.domain.common.json import JsonObject, JsonValue, to_json_value
from app.domain.common.time import now_utc
from app.domain.operations.models import WorkerHeartbeatStatus
from app.domain.scheduler.models import SchedulerInvariantError, SchedulerJobKey

_OUTER_LEASE_RENEWAL_REQUIRED = "scheduler_outer_lease_renewal_required"
_T = TypeVar("_T")


class ShutdownSignal(Protocol):
    @property
    def requested(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class DurableSchedulerCycleResult:
    convergence: DurableSchedulerConvergenceResult
    run_result: DurableSchedulerRunResult | None

    def __post_init__(self) -> None:
        if type(self.convergence) is not DurableSchedulerConvergenceResult:
            raise SchedulerInvariantError("scheduler_cycle_convergence_is_invalid")
        if self.run_result is not None and type(self.run_result) is not DurableSchedulerRunResult:
            raise SchedulerInvariantError("scheduler_cycle_run_result_is_invalid")
        if self.convergence.outcome != "converged" and self.run_result is not None:
            raise SchedulerInvariantError("scheduler_cycle_claim_preceded_convergence")


@dataclass(slots=True)
class _HeartbeatState:
    started_at: datetime
    last_succeeded_at: dict[SchedulerJobKey, datetime | None] = field(
        default_factory=lambda: {job_key: None for job_key in SCHEDULER_CONVERGENCE_ORDER}
    )
    retry_wait_jobs: set[SchedulerJobKey] = field(default_factory=set)
    dead_letter_jobs: set[SchedulerJobKey] = field(default_factory=set)
    job_health: dict[SchedulerJobKey, SchedulerJobHealth | None] = field(
        default_factory=lambda: {
            job_key: None for job_key in SCHEDULER_CONVERGENCE_ORDER
        }
    )


class DurableSchedulerLoop:
    """Own one outer lease and drive the durable scheduler serially.

    Definition convergence and normal claims never overlap.  Only the lease
    renewal and liveness-heartbeat supervisors run concurrently with a single
    active scheduler operation.  A failing supervisor cancels that operation
    so it cannot continue under an unmaintained process lease.
    """

    def __init__(
        self,
        runtime: DurableSchedulerRuntimePort,
        lease_manager: MaintainWorkerLease,
        heartbeat: WorkerHeartbeatPort,
        *,
        worker_id: str,
        shutdown: ShutdownSignal,
        run_once: bool,
        poll_interval_sec: float,
        heartbeat_interval_sec: float,
        lease_renew_interval_sec: float,
        max_convergence_steps: int,
        clock: Callable[[], datetime] = now_utc,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if any(
            not callable(getattr(runtime, operation, None))
            for operation in ("assert_intact", "converge_step", "run_once")
        ):
            raise SchedulerInvariantError("scheduler_runtime_facade_is_invalid")
        if not callable(getattr(lease_manager, "acquire", None)) or any(
            not callable(getattr(lease_manager, operation, None))
            for operation in ("renew", "release")
        ):
            raise SchedulerInvariantError("scheduler_lease_manager_is_invalid")
        if not callable(getattr(heartbeat, "record_worker_heartbeat", None)):
            raise SchedulerInvariantError("scheduler_heartbeat_port_is_invalid")
        if not _is_uuid(worker_id):
            raise SchedulerInvariantError("scheduler_worker_id_is_invalid")
        if not hasattr(shutdown, "requested"):
            raise SchedulerInvariantError("scheduler_shutdown_signal_is_invalid")
        if type(run_once) is not bool:
            raise SchedulerInvariantError("scheduler_run_once_mode_is_invalid")
        for value, field_name in (
            (poll_interval_sec, "poll_interval"),
            (heartbeat_interval_sec, "heartbeat_interval"),
            (lease_renew_interval_sec, "lease_renew_interval"),
        ):
            _require_positive_interval(value, field_name)
        if (
            type(max_convergence_steps) is not int
            or not 1 <= max_convergence_steps <= 1_000
        ):
            raise SchedulerInvariantError("scheduler_convergence_step_budget_is_invalid")
        if not callable(clock) or not callable(sleep):
            raise SchedulerInvariantError("scheduler_loop_dependency_is_invalid")

        self._runtime = runtime
        self.lease_manager = lease_manager
        self.heartbeat = heartbeat
        self.worker_id = worker_id
        self.shutdown = shutdown
        self.run_once_mode = run_once
        self.poll_interval_sec = float(poll_interval_sec)
        self.heartbeat_interval_sec = float(heartbeat_interval_sec)
        self.lease_renew_interval_sec = float(lease_renew_interval_sec)
        self.max_convergence_steps = max_convergence_steps
        self.clock = clock
        self.sleep = sleep
        self._renew_lock = asyncio.Lock()
        self._heartbeat_state: _HeartbeatState | None = None

    async def run(self) -> DurableSchedulerCycleResult | None:
        if self.run_once_mode:
            return await self.run_once()
        return await self._run_with_outer_lease(self._run_continuous)

    async def run_once(self) -> DurableSchedulerCycleResult | None:
        return await self._run_with_outer_lease(self._run_single_cycle)

    async def _run_with_outer_lease(
        self,
        work: Callable[[], Awaitable[_T]],
    ) -> _T:
        acquired = False
        result: _T | None = None
        failure: BaseException | None = None
        cleanup_failures: list[BaseException] = []
        try:
            await self.lease_manager.acquire()
            acquired = True
            self._heartbeat_state = _HeartbeatState(started_at=self._now())
            await self._record_heartbeat(
                "warning",
                checkpoint="durable_scheduler_started",
            )
            result = await self._supervise(work)
        except BaseException as exc:
            failure = exc
        if acquired:
            try:
                await self._record_heartbeat(
                    "shutting_down",
                    checkpoint="durable_scheduler_stopping",
                )
            except BaseException as exc:
                cleanup_failures.append(exc)
            try:
                await self.lease_manager.release()
            except BaseException as exc:
                cleanup_failures.append(exc)
        if failure is not None:
            for cleanup_failure in cleanup_failures:
                failure.add_note(
                    "scheduler cleanup also failed: "
                    f"{type(cleanup_failure).__name__}"
                )
            raise failure
        if len(cleanup_failures) == 1:
            raise cleanup_failures[0]
        if cleanup_failures:
            raise BaseExceptionGroup(
                "durable_scheduler_cleanup_failed",
                cleanup_failures,
            )
        return cast(_T, result)

    async def _supervise(self, work: Callable[[], Awaitable[_T]]) -> _T:
        stopped = asyncio.Event()

        async def invoke_work() -> _T:
            return await work()

        work_task: asyncio.Task[_T] = asyncio.create_task(
            invoke_work(),
            name="durable-scheduler-serial-runner",
        )
        renewal_task = asyncio.create_task(
            self._run_lease_renewal(stopped),
            name="durable-scheduler-lease-renewal",
        )
        heartbeat_task = asyncio.create_task(
            self._run_heartbeat(stopped),
            name="durable-scheduler-heartbeat",
        )
        supervisors = (renewal_task, heartbeat_task)
        try:
            done, _pending = await asyncio.wait(
                (work_task, *supervisors),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if work_task in done:
                completed_supervisors = tuple(task for task in supervisors if task in done)
                if not completed_supervisors:
                    return work_task.result()

            failed_supervisor = next(task for task in supervisors if task in done)
            failure = failed_supervisor.exception()
            work_task.cancel()
            work_outcome = (await asyncio.gather(work_task, return_exceptions=True))[0]
            if isinstance(work_outcome, BaseException) and not isinstance(
                work_outcome,
                asyncio.CancelledError,
            ):
                raise work_outcome
            if failure is None:
                raise SchedulerInvariantError("scheduler_supervisor_stopped_unexpectedly")
            raise failure
        finally:
            stopped.set()
            for task in (work_task, *supervisors):
                if not task.done():
                    task.cancel()
            await asyncio.gather(work_task, *supervisors, return_exceptions=True)

    async def _run_single_cycle(self) -> DurableSchedulerCycleResult | None:
        convergence = await self._converge_until_boundary()
        if convergence is None:
            return None
        run_result: DurableSchedulerRunResult | None = None
        if convergence.outcome == "converged" and not self.shutdown.requested:
            run_result = await self._run_normal_claim()
        cycle = DurableSchedulerCycleResult(
            convergence=convergence,
            run_result=run_result,
        )
        await self._record_running_heartbeat()
        return cycle

    async def _run_continuous(self) -> DurableSchedulerCycleResult | None:
        convergence: DurableSchedulerConvergenceResult | None = None
        while not self.shutdown.requested:
            convergence = await self._converge_until_boundary()
            if convergence is None or self.shutdown.requested:
                break
            if convergence.outcome == "converged":
                break
            await self.sleep(self.poll_interval_sec)

        if convergence is None:
            return None
        last_cycle = DurableSchedulerCycleResult(convergence=convergence, run_result=None)
        if convergence.outcome != "converged" or self.shutdown.requested:
            return last_cycle

        while not self.shutdown.requested:
            run_result = await self._run_normal_claim()
            last_cycle = DurableSchedulerCycleResult(
                convergence=convergence,
                run_result=run_result,
            )
            if self.shutdown.requested:
                break
            await self.sleep(self.poll_interval_sec)
        return last_cycle

    async def _converge_until_boundary(
        self,
    ) -> DurableSchedulerConvergenceResult | None:
        for _step in range(self.max_convergence_steps):
            if self.shutdown.requested:
                return None
            self._runtime.assert_intact()
            result = await self._runtime.converge_step()
            if type(result) is not DurableSchedulerConvergenceResult:
                raise SchedulerInvariantError("scheduler_convergence_result_is_invalid")
            self._runtime.assert_intact()
            self._observe_results(result.run_results)

            if result.outcome == "manual_resolution":
                raise SchedulerInvariantError("scheduler_convergence_requires_manual_resolution")
            if self.shutdown.requested:
                return result
            if result.outcome == "converged":
                return result
            if result.outcome == "wait":
                if self._requires_immediate_outer_lease_renewal(result):
                    await self._renew_outer_lease()
                    continue
                return result
            if result.outcome != "claimed":
                raise SchedulerInvariantError("scheduler_convergence_outcome_is_invalid")
        raise SchedulerInvariantError("scheduler_convergence_step_budget_exhausted")

    async def _run_normal_claim(self) -> DurableSchedulerRunResult:
        if self.shutdown.requested:
            raise SchedulerInvariantError("scheduler_claim_started_after_shutdown")
        self._runtime.assert_intact()
        result = await self._runtime.run_once()
        if type(result) is not DurableSchedulerRunResult:
            raise SchedulerInvariantError("scheduler_run_result_is_invalid")
        self._runtime.assert_intact()
        self._observe_results((result,))
        return result

    async def _run_lease_renewal(self, stopped: asyncio.Event) -> None:
        while not stopped.is_set():
            await self.sleep(self.lease_renew_interval_sec)
            if stopped.is_set():
                return
            await self._renew_outer_lease()

    async def _renew_outer_lease(self) -> None:
        async with self._renew_lock:
            await self.lease_manager.renew()
            self._runtime.assert_intact()

    async def _run_heartbeat(self, stopped: asyncio.Event) -> None:
        while not stopped.is_set():
            await self.sleep(self.heartbeat_interval_sec)
            if stopped.is_set():
                return
            await self._record_running_heartbeat()

    async def _record_running_heartbeat(self) -> None:
        state = self._require_heartbeat_state()
        incomplete = tuple(
            job_key
            for job_key in SCHEDULER_CONVERGENCE_ORDER
            if state.last_succeeded_at[job_key] is None
        )
        status: WorkerHeartbeatStatus = (
            "error"
            if any(health == "error" for health in state.job_health.values())
            else "warning"
            if incomplete
            or any(health == "warning" for health in state.job_health.values())
            else "ok"
        )
        await self._record_heartbeat(status, checkpoint="durable_scheduler_running")

    async def _record_heartbeat(
        self,
        status: WorkerHeartbeatStatus,
        *,
        checkpoint: str,
    ) -> None:
        state = self._require_heartbeat_state()
        completed_at = self._now()
        succeeded_times: dict[str, JsonValue] = {
            job_key: (
                timestamp.isoformat()
                if (timestamp := state.last_succeeded_at[job_key]) is not None
                else None
            )
            for job_key in SCHEDULER_CONVERGENCE_ORDER
        }
        incomplete_jobs: list[JsonValue] = [
            to_json_value(job_key)
            for job_key in SCHEDULER_CONVERGENCE_ORDER
            if state.last_succeeded_at[job_key] is None
        ]
        retry_wait_jobs: list[JsonValue] = [
            to_json_value(job_key) for job_key in sorted(state.retry_wait_jobs)
        ]
        dead_letter_jobs: list[JsonValue] = [
            to_json_value(job_key) for job_key in sorted(state.dead_letter_jobs)
        ]
        warning_jobs: list[JsonValue] = [
            to_json_value(job_key)
            for job_key in SCHEDULER_CONVERGENCE_ORDER
            if state.job_health[job_key] == "warning"
        ]
        error_jobs: list[JsonValue] = [
            to_json_value(job_key)
            for job_key in SCHEDULER_CONVERGENCE_ORDER
            if state.job_health[job_key] == "error"
        ]
        details: JsonObject = {
            "component": "operations_v2",
            "checkpoint": checkpoint,
            "started_at": state.started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "job_last_succeeded_at": succeeded_times,
            "incomplete_jobs": incomplete_jobs,
            "retry_wait_jobs": retry_wait_jobs,
            "dead_letter_jobs": dead_letter_jobs,
            "warning_jobs": warning_jobs,
            "error_jobs": error_jobs,
        }
        await self.heartbeat.record_worker_heartbeat(
            worker_id=self.worker_id,
            status=status,
            details=details,
            now=completed_at,
        )

    def _observe_results(self, results: tuple[DurableSchedulerRunResult, ...]) -> None:
        state = self._require_heartbeat_state()
        for result in results:
            if type(result) is not DurableSchedulerRunResult:
                raise SchedulerInvariantError("scheduler_run_result_is_invalid")
            job_key = result.job_key
            if result.outcome == "idle":
                continue
            if job_key is None:
                raise SchedulerInvariantError("scheduler_run_result_job_key_is_invalid")
            if result.outcome == "succeeded":
                completion = result.completion
                if completion is None:
                    raise SchedulerInvariantError("scheduler_completion_result_is_invalid")
                state.last_succeeded_at[job_key] = completion.observed_at
                state.retry_wait_jobs.discard(job_key)
                state.dead_letter_jobs.discard(job_key)
                state.job_health[job_key] = classify_successful_scheduler_job(
                    job_key,
                    result.handler_result,
                )
            elif result.outcome == "retry_wait":
                state.retry_wait_jobs.add(job_key)
                state.dead_letter_jobs.discard(job_key)
                state.job_health[job_key] = "warning"
            elif result.outcome == "dead_letter":
                state.dead_letter_jobs.add(job_key)
                state.retry_wait_jobs.discard(job_key)
                state.job_health[job_key] = "error"
            else:
                raise SchedulerInvariantError("scheduler_run_result_outcome_is_invalid")

    @staticmethod
    def _requires_immediate_outer_lease_renewal(
        result: DurableSchedulerConvergenceResult,
    ) -> bool:
        return any(
            receipt.status == "wait"
            and receipt.reason_code == _OUTER_LEASE_RENEWAL_REQUIRED
            for receipt in result.receipts
        )

    def _require_heartbeat_state(self) -> _HeartbeatState:
        if self._heartbeat_state is None:
            raise SchedulerInvariantError("scheduler_heartbeat_state_is_not_initialized")
        return self._heartbeat_state

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise SchedulerInvariantError("scheduler_loop_clock_must_be_timezone_aware")
        return value


def _require_positive_interval(value: object, field_name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise SchedulerInvariantError(f"scheduler_{field_name}_is_invalid")


def _is_uuid(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False




__all__ = (
    "DurableSchedulerCycleResult",
    "DurableSchedulerLoop",
    "ShutdownSignal",
)
