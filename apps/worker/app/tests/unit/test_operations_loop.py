from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import cast

import pytest

from app.application.services.operations_loop import OperationsLoop
from app.application.use_cases.apply_operation_commands import OperationCommandRunResult
from app.application.use_cases.dispatch_alert_outbox import AlertOutboxDispatchResult
from app.application.use_cases.maintain_worker_lease import MaintainWorkerLease
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
from app.config import Settings
from app.infrastructure.graceful_shutdown import ShutdownFlag


async def test_run_once_mode_executes_exactly_one_ordered_operations_cycle() -> None:
    shutdown = ShutdownFlag()
    lifecycle: list[str] = []
    operations = RunOnceOperations(lifecycle)
    loop = OperationsLoop(
        _settings(run_once=True),
        shutdown,
        cast(RunOperationsV2, operations),
        cast(MaintainWorkerLease, FakeLeaseManager(lifecycle)),
    )

    await loop.run()

    assert operations.calls == 1
    assert lifecycle == ["lease_acquired", "operations", "lease_released"]


async def test_continuous_scheduler_applies_commands_before_first_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutdown = ShutdownFlag()
    calls: list[str] = []
    operations = IndependentOperations(shutdown, calls)
    _install_yielding_sleep(monkeypatch)
    loop = OperationsLoop(
        _settings(run_once=False),
        shutdown,
        cast(RunOperationsV2, operations),
        cast(MaintainWorkerLease, FakeLeaseManager()),
    )

    await loop.run()

    assert calls[0] == "commands"
    assert calls.count("commands") >= 1
    assert calls.count("execution") == 1
    assert operations.heartbeats[0]["checkpoint"] == "independent_scheduler_started"


async def test_slow_outbox_does_not_starve_command_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutdown = ShutdownFlag()
    calls: list[str] = []
    outbox_started = asyncio.Event()
    release_outbox = asyncio.Event()
    operations = IndependentOperations(
        shutdown,
        calls,
        stop_after_command_count=2,
        outbox_started=outbox_started,
        release_outbox=release_outbox,
    )
    _install_yielding_sleep(monkeypatch)
    loop = OperationsLoop(
        _settings(run_once=False),
        shutdown,
        cast(RunOperationsV2, operations),
        cast(MaintainWorkerLease, FakeLeaseManager()),
    )

    async def release_when_command_repolled() -> None:
        await outbox_started.wait()
        while operations.commands.calls < 2:
            await _ORIGINAL_SLEEP(0)
        release_outbox.set()

    release_task = asyncio.create_task(release_when_command_repolled())
    await loop.run()
    await release_task

    assert operations.commands.calls == 2
    assert calls.index("outbox_started") < calls.index("commands_repolled")
    assert calls[-1] == "outbox_released"


async def test_stage_failure_cancels_siblings_and_reaches_process_supervisor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutdown = ShutdownFlag()
    calls: list[str] = []
    operations = IndependentOperations(shutdown, calls, fail_execution=True)
    _install_yielding_sleep(monkeypatch)
    loop = OperationsLoop(
        _settings(run_once=False),
        shutdown,
        cast(RunOperationsV2, operations),
        cast(MaintainWorkerLease, FakeLeaseManager()),
    )

    with pytest.raises(RuntimeError, match="execution_failed"):
        await loop.run()


async def test_unacknowledged_repolled_command_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutdown = ShutdownFlag()
    calls: list[str] = []
    operations = IndependentOperations(
        shutdown,
        calls,
        unacknowledged_on_command_count=2,
    )
    _install_yielding_sleep(monkeypatch)
    loop = OperationsLoop(
        _settings(run_once=False),
        shutdown,
        cast(RunOperationsV2, operations),
        cast(MaintainWorkerLease, FakeLeaseManager()),
    )

    with pytest.raises(OperationsV2RunError) as raised:
        await loop.run()

    assert raised.value.failed_stages == ("commands_unacknowledged",)


class RunOnceOperations:
    def __init__(self, lifecycle: list[str]) -> None:
        self.calls = 0
        self.lifecycle = lifecycle

    async def run_once(self) -> OperationsV2RunResult:
        self.calls += 1
        self.lifecycle.append("operations")
        return _empty_cycle()


class FakeLeaseManager:
    def __init__(self, lifecycle: list[str] | None = None) -> None:
        self.acquired = False
        self.lifecycle = lifecycle

    async def acquire(self) -> object:
        assert not self.acquired
        self.acquired = True
        if self.lifecycle is not None:
            self.lifecycle.append("lease_acquired")
        return object()

    async def renew(self) -> object:
        assert self.acquired
        return object()

    async def release(self) -> object:
        assert self.acquired
        self.acquired = False
        if self.lifecycle is not None:
            self.lifecycle.append("lease_released")
        return object()


class IndependentOperations:
    def __init__(
        self,
        shutdown: ShutdownFlag,
        calls: list[str],
        *,
        stop_after_command_count: int | None = None,
        unacknowledged_on_command_count: int | None = None,
        fail_execution: bool = False,
        outbox_started: asyncio.Event | None = None,
        release_outbox: asyncio.Event | None = None,
    ) -> None:
        self.shutdown = shutdown
        self.calls = calls
        self.heartbeats: list[dict[str, object]] = []
        self.commands = CommandStage(
            shutdown,
            calls,
            stop_after=stop_after_command_count,
            unacknowledged_on=unacknowledged_on_command_count,
        )
        self.execution = ExecutionStage(
            shutdown,
            calls,
            fail=fail_execution,
            stop=stop_after_command_count is None
            and unacknowledged_on_command_count is None
            and not fail_execution,
        )
        self.settlement = SettlementStage(calls)
        self.reconciliation = ReconciliationStage(calls)
        self.outbox = OutboxStage(calls, outbox_started, release_outbox)

    async def record_scheduler_heartbeat(
        self,
        status: str,
        details: dict[str, object],
    ) -> None:
        del status
        self.heartbeats.append(details)


class CommandStage:
    def __init__(
        self,
        shutdown: ShutdownFlag,
        calls: list[str],
        *,
        stop_after: int | None,
        unacknowledged_on: int | None,
    ) -> None:
        self.shutdown = shutdown
        self.log = calls
        self.stop_after = stop_after
        self.unacknowledged_on = unacknowledged_on
        self.calls = 0

    async def run_once(self) -> OperationCommandRunResult:
        self.calls += 1
        self.log.append("commands" if self.calls == 1 else "commands_repolled")
        if self.stop_after == self.calls:
            self.shutdown.request()
        return OperationCommandRunResult(
            claimed=0,
            applied=0,
            unacknowledged=int(self.unacknowledged_on == self.calls),
        )


class ExecutionStage:
    def __init__(
        self,
        shutdown: ShutdownFlag,
        calls: list[str],
        *,
        fail: bool,
        stop: bool,
    ) -> None:
        self.shutdown = shutdown
        self.calls = calls
        self.fail = fail
        self.stop = stop

    async def run_once(self) -> ExecutionSupervisorV2RunResult:
        self.calls.append("execution")
        if self.fail:
            raise RuntimeError("execution_failed")
        if self.stop:
            self.shutdown.request()
        return ExecutionSupervisorV2RunResult(0, 0, 0, 0, 0, 0, 0)


class ReconciliationStage:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def run_once(self) -> ExecutionReconciliationRunResult:
        self.calls.append("reconciliation")
        return ExecutionReconciliationRunResult(0, 0, 0, 0)


class SettlementStage:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def run_once(self) -> CashSettlementRunResult:
        self.calls.append("settlement")
        return CashSettlementRunResult(0, 0, 0, 0, 0, 0)


class OutboxStage:
    def __init__(
        self,
        calls: list[str],
        started: asyncio.Event | None,
        release: asyncio.Event | None,
    ) -> None:
        self.calls = calls
        self.started = started
        self.release = release

    async def dispatch_once(self) -> AlertOutboxDispatchResult:
        if self.started is not None and self.release is not None:
            self.calls.append("outbox_started")
            self.started.set()
            await self.release.wait()
            self.calls.append("outbox_released")
        else:
            self.calls.append("outbox")
        return AlertOutboxDispatchResult(0, 0, 0)


_ORIGINAL_SLEEP: Callable[[float], Awaitable[None]] = asyncio.sleep


def _install_yielding_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def yielding_sleep(_: float) -> None:
        await _ORIGINAL_SLEEP(0)

    monkeypatch.setattr(
        "app.application.services.operations_loop.asyncio.sleep",
        yielding_sleep,
    )


def _empty_cycle() -> OperationsV2RunResult:
    return OperationsV2RunResult(
        OperationCommandRunResult(0, 0),
        ExecutionSupervisorV2RunResult(0, 0, 0, 0, 0, 0, 0),
        CashSettlementRunResult(0, 0, 0, 0, 0, 0),
        ExecutionReconciliationRunResult(0, 0, 0, 0),
        AlertOutboxDispatchResult(0, 0, 0),
    )


def _settings(*, run_once: bool) -> Settings:
    return Settings(
        RUN_ONCE=run_once,
        EXECUTION_V2_ENABLED=True,
        EXECUTION_V2_WORKER_API_ENABLED=True,
        EXECUTION_V2_WORKER_ID="00000000-0000-4000-8000-000000000001",
        EXECUTION_V2_ACCOUNT_ID="paper-primary",
        OPERATIONS_COMMAND_INTERVAL_SEC=1,
        OPERATIONS_EXECUTION_INTERVAL_SEC=1,
        OPERATIONS_SETTLEMENT_INTERVAL_SEC=1,
        OPERATIONS_RECONCILIATION_INTERVAL_SEC=1,
        OPERATIONS_OUTBOX_INTERVAL_SEC=1,
        OPERATIONS_HEARTBEAT_INTERVAL_SEC=1,
    )
