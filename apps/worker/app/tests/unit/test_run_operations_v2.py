from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.application.use_cases.apply_operation_commands import OperationCommandRunResult
from app.application.use_cases.dispatch_alert_outbox import AlertOutboxDispatchResult
from app.application.use_cases.mature_cash_settlements import CashSettlementRunResult
from app.application.use_cases.reconcile_execution_v2 import (
    ExecutionReconciliationRunResult,
)
from app.application.use_cases.run_execution_supervisor_v2 import (
    ExecutionSupervisorV2RunResult,
)
from app.application.use_cases.run_operations_v2 import (
    OperationsV2RunError,
    OperationsV2RunResult,
    RunOperationsV2,
)
from app.domain.common.json import JsonObject
from app.domain.execution_v2.models import ExecutionInvariantError
from app.domain.operations.models import RecordedWorkerHeartbeat, WorkerHeartbeatStatus


async def test_operations_boundary_orders_control_recovery_then_delivery() -> None:
    calls: list[str] = []
    commands = StubCommands(calls)
    execution = StubExecution(calls)
    settlement = StubSettlement(calls)
    reconciliation = StubReconciliation(calls)
    outbox = StubOutbox(calls)

    result = await RunOperationsV2(
        commands, execution, settlement, reconciliation, outbox
    ).run_once()

    assert calls == ["commands", "execution", "settlement", "reconciliation", "outbox"]
    assert result == OperationsV2RunResult(
        OperationCommandRunResult(1, 1),
        ExecutionSupervisorV2RunResult(2, 1, 1, 1, 1, 0, 0),
        CashSettlementRunResult(2, 1, 0, 1, 0, 1),
        ExecutionReconciliationRunResult(2, 1, 0, 1),
        AlertOutboxDispatchResult(3, 2, 1),
    )


async def test_operations_heartbeat_marks_only_completed_run_as_ok() -> None:
    calls: list[str] = []
    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    timestamps = iter((now, now + timedelta(seconds=1)))
    heartbeat = StubHeartbeat()

    await RunOperationsV2(
        StubCommands(calls),
        CleanExecution(calls),
        CleanSettlement(calls),
        CleanReconciliation(calls),
        CleanOutbox(calls),
        heartbeat=heartbeat,
        worker_id=str(uuid4()),
        clock=lambda: next(timestamps),
    ).run_once()

    assert heartbeat.records == [
        (
            "warning",
            {
                "component": "operations_v2",
                "checkpoint": "operations_started",
                "started_at": now.isoformat(),
            },
            now,
        ),
        (
            "ok",
            {
                "component": "operations_v2",
                "checkpoint": "operations_completed",
                "started_at": now.isoformat(),
                "completed_at": (now + timedelta(seconds=1)).isoformat(),
                "commands_applied": 1,
                "commands_failed": 0,
                "execution_claimed": 2,
                "execution_new_candidates": 1,
                "execution_resumed": 1,
                "execution_completed": 2,
                "execution_rescheduled": 0,
                "execution_blocked": 0,
                "execution_manual": 0,
                "execution_failed": 0,
                "settlement_claimed": 2,
                "settlement_completed": 2,
                "settlement_retried": 0,
                "settlement_dead_lettered": 0,
                "settlement_failed": 0,
                "reconciliation_claimed": 2,
                "reconciliation_manual": 0,
                "reconciliation_failed": 0,
                "unknown_resolution_listed": 0,
                "unknown_resolution_claimed": 0,
                "unknown_resolution_resumed": 0,
                "unknown_resolution_applied": 0,
                "unknown_resolution_replayed": 0,
                "unknown_resolution_failed": 0,
                "outbox_delivered": 3,
                "outbox_failed": 0,
            },
            now + timedelta(seconds=1),
        ),
    ]


async def test_operations_exception_records_error_without_false_ok() -> None:
    calls: list[str] = []
    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    timestamps = iter((now, now + timedelta(seconds=1)))
    heartbeat = StubHeartbeat()

    with pytest.raises(OperationsV2RunError) as raised:
        await RunOperationsV2(
            FailingCommands(calls),
            StubExecution(calls),
            StubSettlement(calls),
            StubReconciliation(calls),
            StubOutbox(calls),
            heartbeat=heartbeat,
            worker_id=str(uuid4()),
            clock=lambda: next(timestamps),
        ).run_once()

    assert raised.value.failed_stages == ("commands:RuntimeError",)
    assert calls == ["commands", "reconciliation", "outbox"]
    assert [record[0] for record in heartbeat.records] == ["warning", "error"]
    assert heartbeat.records[-1] == (
        "error",
        {
            "component": "operations_v2",
            "checkpoint": "operations_stage_failure",
            "started_at": now.isoformat(),
            "completed_at": (now + timedelta(seconds=1)).isoformat(),
            "failed_stages": ["commands:RuntimeError"],
            "commands_failed": 0,
            "commands_unacknowledged": 0,
            "execution_failed": 0,
            "settlement_failed": 0,
        },
        now + timedelta(seconds=1),
    )


async def test_terminal_command_failure_blocks_execution_but_does_not_starve_outbox() -> None:
    calls: list[str] = []
    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    timestamps = iter((now, now + timedelta(seconds=1)))
    heartbeat = StubHeartbeat()

    with pytest.raises(OperationsV2RunError) as raised:
        await RunOperationsV2(
            FailedCommandResult(calls),
            StubExecution(calls),
            StubSettlement(calls),
            StubReconciliation(calls),
            StubOutbox(calls),
            heartbeat=heartbeat,
            worker_id=str(uuid4()),
            clock=lambda: next(timestamps),
        ).run_once()

    assert raised.value.failed_stages == ("commands_failed",)
    assert calls == ["commands", "reconciliation", "outbox"]
    assert heartbeat.records[-1][0] == "error"


async def test_unavailable_execution_source_is_a_fail_closed_stage() -> None:
    calls: list[str] = []

    with pytest.raises(OperationsV2RunError) as raised:
        await RunOperationsV2(
            StubCommands(calls),
            UnavailableExecution(calls),
            StubSettlement(calls),
            StubReconciliation(calls),
            StubOutbox(calls),
        ).run_once()

    assert raised.value.failed_stages == (
        "execution:paper_execution_source_unavailable",
    )
    assert calls == ["commands", "execution", "settlement", "reconciliation", "outbox"]


class StubCommands:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def run_once(self) -> OperationCommandRunResult:
        self.calls.append("commands")
        return OperationCommandRunResult(1, 1)


class FailingCommands(StubCommands):
    async def run_once(self) -> OperationCommandRunResult:
        self.calls.append("commands")
        raise RuntimeError("command_failure")


class FailedCommandResult(StubCommands):
    async def run_once(self) -> OperationCommandRunResult:
        self.calls.append("commands")
        return OperationCommandRunResult(2, 1, 1, 0)


class StubExecution:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def run_once(self) -> ExecutionSupervisorV2RunResult:
        self.calls.append("execution")
        return ExecutionSupervisorV2RunResult(2, 1, 1, 1, 1, 0, 0)


class CleanExecution(StubExecution):
    async def run_once(self) -> ExecutionSupervisorV2RunResult:
        self.calls.append("execution")
        return ExecutionSupervisorV2RunResult(2, 1, 1, 2, 0, 0, 0)


class UnavailableExecution(StubExecution):
    async def run_once(self) -> ExecutionSupervisorV2RunResult:
        self.calls.append("execution")
        raise ExecutionInvariantError("paper_execution_source_unavailable")


class StubSettlement:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def run_once(self) -> CashSettlementRunResult:
        self.calls.append("settlement")
        return CashSettlementRunResult(2, 1, 0, 1, 0, 1)


class CleanSettlement(StubSettlement):
    async def run_once(self) -> CashSettlementRunResult:
        self.calls.append("settlement")
        return CashSettlementRunResult(2, 2, 0, 0, 0, 0)


class StubReconciliation:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def run_once(self) -> ExecutionReconciliationRunResult:
        self.calls.append("reconciliation")
        return ExecutionReconciliationRunResult(2, 1, 0, 1)


class CleanReconciliation(StubReconciliation):
    async def run_once(self) -> ExecutionReconciliationRunResult:
        self.calls.append("reconciliation")
        return ExecutionReconciliationRunResult(2, 2, 0, 0)


class StubOutbox:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def dispatch_once(self) -> AlertOutboxDispatchResult:
        self.calls.append("outbox")
        return AlertOutboxDispatchResult(3, 2, 1)


class CleanOutbox(StubOutbox):
    async def dispatch_once(self) -> AlertOutboxDispatchResult:
        self.calls.append("outbox")
        return AlertOutboxDispatchResult(3, 3, 0)


class StubHeartbeat:
    def __init__(self) -> None:
        self.records: list[tuple[WorkerHeartbeatStatus, JsonObject, datetime]] = []

    async def record_worker_heartbeat(
        self,
        *,
        worker_id: str,
        status: WorkerHeartbeatStatus,
        details: JsonObject,
        now: datetime,
    ) -> RecordedWorkerHeartbeat:
        del worker_id
        self.records.append((status, details, now))
        return RecordedWorkerHeartbeat(str(uuid4()), now)
