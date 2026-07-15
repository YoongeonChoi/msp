from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.application.ports.worker_heartbeat_port import WorkerHeartbeatPort
from app.application.use_cases.apply_operation_commands import OperationCommandRunResult
from app.application.use_cases.dispatch_alert_outbox import (
    AlertOutboxDispatchResult,
)
from app.application.use_cases.mature_cash_settlements import CashSettlementRunResult
from app.application.use_cases.reconcile_execution_v2 import (
    ExecutionReconciliationRunResult,
)
from app.application.use_cases.run_execution_supervisor_v2 import (
    ExecutionSupervisorV2RunResult,
)
from app.domain.common.json import JsonObject
from app.domain.common.time import now_utc
from app.domain.execution_v2.models import ExecutionInvariantError
from app.domain.operations.models import WorkerHeartbeatStatus


class OperationCommandRunner(Protocol):
    async def run_once(self) -> OperationCommandRunResult:
        ...


class ExecutionReconciliationRunner(Protocol):
    async def run_once(self) -> ExecutionReconciliationRunResult:
        ...


class ExecutionSupervisorRunner(Protocol):
    async def run_once(self) -> ExecutionSupervisorV2RunResult:
        ...


class CashSettlementRunner(Protocol):
    async def run_once(self) -> CashSettlementRunResult:
        ...


class AlertOutboxRunner(Protocol):
    async def dispatch_once(self) -> AlertOutboxDispatchResult:
        ...


@dataclass(frozen=True, slots=True)
class OperationsV2RunResult:
    commands: OperationCommandRunResult
    execution: ExecutionSupervisorV2RunResult
    settlement: CashSettlementRunResult
    reconciliation: ExecutionReconciliationRunResult
    outbox: AlertOutboxDispatchResult


class OperationsV2RunError(RuntimeError):
    def __init__(self, failed_stages: tuple[str, ...]) -> None:
        self.failed_stages = failed_stages
        super().__init__(f"operations_v2_stage_failure:{','.join(failed_stages)}")


class RunOperationsV2:
    """Explicit control/operations boundary; it never creates trading candidates."""

    def __init__(
        self,
        commands: OperationCommandRunner,
        execution: ExecutionSupervisorRunner,
        settlement: CashSettlementRunner,
        reconciliation: ExecutionReconciliationRunner,
        outbox: AlertOutboxRunner,
        *,
        heartbeat: WorkerHeartbeatPort | None = None,
        worker_id: str | None = None,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        if (heartbeat is None) != (worker_id is None):
            raise ExecutionInvariantError("operations_heartbeat_configuration_is_incomplete")
        if worker_id is not None and not worker_id.strip():
            raise ExecutionInvariantError("operations_worker_id_is_required")
        self.commands = commands
        self.execution = execution
        self.settlement = settlement
        self.reconciliation = reconciliation
        self.outbox = outbox
        self.heartbeat = heartbeat
        self.worker_id = worker_id
        self.clock = clock

    async def run_once(self) -> OperationsV2RunResult:
        started_at = self._now()
        failed_stages: list[str] = []
        try:
            await self._record_heartbeat(
                "warning",
                {
                    "component": "operations_v2",
                    "checkpoint": "operations_started",
                    "started_at": started_at.isoformat(),
                },
                started_at,
            )
        except Exception as exc:
            failed_stages.append(_stage_failure("heartbeat_start", exc))

        command_result = OperationCommandRunResult(0, 0)
        execution_result = ExecutionSupervisorV2RunResult(0, 0, 0, 0, 0, 0, 0)
        settlement_result = CashSettlementRunResult(0, 0, 0, 0, 0, 0)
        reconciliation_result = ExecutionReconciliationRunResult(0, 0, 0, 0)
        outbox_result = AlertOutboxDispatchResult(0, 0, 0)
        try:
            command_result = await self.commands.run_once()
            if command_result.unacknowledged:
                failed_stages.append("commands_unacknowledged")
            if command_result.failed:
                failed_stages.append("commands_failed")
        except Exception as exc:
            failed_stages.append(_stage_failure("commands", exc))
        if not failed_stages:
            try:
                execution_result = await self.execution.run_once()
            except Exception as exc:
                failed_stages.append(_stage_failure("execution", exc))
            try:
                settlement_result = await self.settlement.run_once()
            except Exception as exc:
                failed_stages.append(_stage_failure("settlement", exc))
        try:
            reconciliation_result = await self.reconciliation.run_once()
        except Exception as exc:
            failed_stages.append(_stage_failure("reconciliation", exc))
        try:
            outbox_result = await self.outbox.dispatch_once()
        except Exception as exc:
            failed_stages.append(_stage_failure("outbox", exc))

        result = OperationsV2RunResult(
            commands=command_result,
            execution=execution_result,
            settlement=settlement_result,
            reconciliation=reconciliation_result,
            outbox=outbox_result,
        )
        completed_at = self._now()
        if failed_stages:
            failure_details: JsonObject = {
                "component": "operations_v2",
                "checkpoint": "operations_stage_failure",
                "started_at": started_at.isoformat(),
                "completed_at": completed_at.isoformat(),
                "failed_stages": list(failed_stages),
                "commands_failed": result.commands.failed,
                "commands_unacknowledged": result.commands.unacknowledged,
                "execution_failed": result.execution.failed,
                "settlement_failed": result.settlement.failed,
            }
            with suppress(Exception):
                await self._record_heartbeat(
                    "error",
                    failure_details,
                    completed_at,
                )
            raise OperationsV2RunError(tuple(failed_stages))

        has_warnings = any(
            (
                result.commands.failed,
                result.execution.blocked,
                result.execution.manual,
                result.execution.failed,
                result.settlement.failed,
                result.settlement.dead_lettered,
                result.reconciliation.manual,
                result.reconciliation.failed,
                result.outbox.failed,
            )
        )
        await self._record_heartbeat(
            "warning" if has_warnings else "ok",
            {
                "component": "operations_v2",
                "checkpoint": (
                    "operations_completed_with_warnings"
                    if has_warnings
                    else "operations_completed"
                ),
                "started_at": started_at.isoformat(),
                "completed_at": completed_at.isoformat(),
                "commands_applied": result.commands.applied,
                "commands_failed": result.commands.failed,
                "execution_claimed": result.execution.claimed,
                "execution_new_candidates": result.execution.new_candidates,
                "execution_resumed": result.execution.resumed,
                "execution_completed": result.execution.completed,
                "execution_rescheduled": result.execution.rescheduled,
                "execution_blocked": result.execution.blocked,
                "execution_manual": result.execution.manual,
                "execution_failed": result.execution.failed,
                "settlement_claimed": result.settlement.claimed,
                "settlement_completed": result.settlement.completed,
                "settlement_retried": result.settlement.retried,
                "settlement_dead_lettered": result.settlement.dead_lettered,
                "settlement_failed": result.settlement.failed,
                "reconciliation_claimed": result.reconciliation.claimed,
                "reconciliation_manual": result.reconciliation.manual,
                "reconciliation_failed": result.reconciliation.failed,
                "unknown_resolution_listed": (
                    result.reconciliation.unknown_listed
                ),
                "unknown_resolution_claimed": (
                    result.reconciliation.unknown_claimed
                ),
                "unknown_resolution_resumed": (
                    result.reconciliation.unknown_resumed
                ),
                "unknown_resolution_applied": (
                    result.reconciliation.unknown_applied
                ),
                "unknown_resolution_replayed": (
                    result.reconciliation.unknown_replayed
                ),
                "unknown_resolution_failed": (
                    result.reconciliation.unknown_failed
                ),
                "outbox_delivered": result.outbox.delivered,
                "outbox_failed": result.outbox.failed,
            },
            completed_at,
        )
        return result

    async def _record_heartbeat(
        self,
        status: WorkerHeartbeatStatus,
        details: JsonObject,
        now: datetime,
    ) -> None:
        if self.heartbeat is None or self.worker_id is None:
            return
        await self.heartbeat.record_worker_heartbeat(
            worker_id=self.worker_id,
            status=status,
            details=details,
            now=now,
        )

    async def record_scheduler_heartbeat(
        self,
        status: WorkerHeartbeatStatus,
        details: JsonObject,
    ) -> None:
        """Record a scheduler heartbeat without exposing the persistence port.

        The continuous runtime uses independent stage cadences.  Keeping the
        heartbeat write on this boundary preserves the release/worker identity
        checks implemented by the durable adapter.
        """

        await self._record_heartbeat(status, details, self._now())

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ExecutionInvariantError("operations_clock_must_be_timezone_aware")
        return value


def _stage_failure(stage: str, exc: Exception) -> str:
    if isinstance(exc, ExecutionInvariantError):
        return f"{stage}:{exc.safe_message}"
    return f"{stage}:{type(exc).__name__}"
