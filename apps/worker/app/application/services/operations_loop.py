from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import TypeVar

from app.application.use_cases.maintain_worker_lease import MaintainWorkerLease
from app.application.use_cases.run_operations_v2 import (
    OperationsV2RunError,
    OperationsV2RunResult,
    RunOperationsV2,
)
from app.config import Settings
from app.domain.common.json import JsonValue, to_json_value
from app.domain.common.time import now_utc
from app.infrastructure.graceful_shutdown import ShutdownFlag

ResultT = TypeVar("ResultT")


@dataclass(slots=True)
class _StageState:
    last_completed_at: datetime | None = None
    warning: bool = False
    error: bool = False


class OperationsLoop:
    """Run fenced V2 stages without letting slow delivery starve commands.

    Startup always drains commands once before execution can run.  Continuous
    mode then gives commands, the single execution scheduler, cash settlement,
    reconciliation, and the outbox dispatcher independent cadences.  A stage
    exception cancels the siblings and exits so the process supervisor can
    restart the runtime.
    Production broker writes remain outside this boundary.
    """

    def __init__(
        self,
        settings: Settings,
        shutdown: ShutdownFlag,
        run_operations: RunOperationsV2,
        lease_manager: MaintainWorkerLease,
    ) -> None:
        if not settings.execution_v2_worker_api_enabled:
            raise ValueError("operations_loop_requires_worker_api_enablement")
        self.settings = settings
        self.shutdown = shutdown
        self.run_operations = run_operations
        self.lease_manager = lease_manager

    async def run_once(self) -> OperationsV2RunResult:
        await self.lease_manager.acquire()
        renewal = asyncio.create_task(
            self._run_lease_renewal(),
            name="operations-v2-lease-renewal",
        )
        try:
            return await self.run_operations.run_once()
        finally:
            renewal.cancel()
            await asyncio.gather(renewal, return_exceptions=True)
            await self._record_shutdown_heartbeat()
            await self.lease_manager.release()

    async def run(self) -> None:
        if self.settings.run_once:
            await self.run_once()
            return

        await self.lease_manager.acquire()
        try:
            await self._run_continuous()
        finally:
            await self._record_shutdown_heartbeat()
            await self.lease_manager.release()

    async def _run_continuous(self) -> None:
        started_at = now_utc()
        states = {
            "commands": _StageState(),
            "execution": _StageState(),
            "settlement": _StageState(),
            "reconciliation": _StageState(),
            "outbox": _StageState(),
        }

        # A pending stop/control epoch change must be applied before the first
        # execution claim after process start.
        command_result = await self.run_operations.commands.run_once()
        states["commands"].last_completed_at = now_utc()
        command_error, command_warning = _stage_result_health_flags(command_result)
        states["commands"].error = command_error
        states["commands"].warning = command_warning
        _validate_command_result(command_result)
        if self.shutdown.requested:
            return

        await self.run_operations.record_scheduler_heartbeat(
            "warning",
            {
                "component": "operations_v2",
                "checkpoint": "independent_scheduler_started",
                "started_at": started_at.isoformat(),
                "command_interval_sec": self.settings.operations_command_interval_sec,
                "execution_interval_sec": self.settings.operations_execution_interval_sec,
                "settlement_interval_sec": (
                    self.settings.operations_settlement_interval_sec
                ),
                "reconciliation_interval_sec": (
                    self.settings.operations_reconciliation_interval_sec
                ),
                "outbox_interval_sec": self.settings.operations_outbox_interval_sec,
                "heartbeat_interval_sec": (
                    self.settings.operations_heartbeat_interval_sec
                ),
            },
        )

        tasks = (
            asyncio.create_task(
                self._run_stage(
                    "settlement",
                    self.run_operations.settlement.run_once,
                    self.settings.operations_settlement_interval_sec,
                    states["settlement"],
                ),
                name="operations-v2-settlement",
            ),
            asyncio.create_task(
                self._run_stage(
                    "commands",
                    self.run_operations.commands.run_once,
                    self.settings.operations_command_interval_sec,
                    states["commands"],
                    validate=_validate_command_result,
                    initial_delay=True,
                ),
                name="operations-v2-commands",
            ),
            asyncio.create_task(
                self._run_stage(
                    "execution",
                    self.run_operations.execution.run_once,
                    self.settings.operations_execution_interval_sec,
                    states["execution"],
                ),
                name="operations-v2-execution",
            ),
            asyncio.create_task(
                self._run_stage(
                    "reconciliation",
                    self.run_operations.reconciliation.run_once,
                    self.settings.operations_reconciliation_interval_sec,
                    states["reconciliation"],
                ),
                name="operations-v2-reconciliation",
            ),
            asyncio.create_task(
                self._run_stage(
                    "outbox",
                    self.run_operations.outbox.dispatch_once,
                    self.settings.operations_outbox_interval_sec,
                    states["outbox"],
                ),
                name="operations-v2-outbox",
            ),
            asyncio.create_task(
                self._run_heartbeat(started_at, states),
                name="operations-v2-heartbeat",
            ),
            asyncio.create_task(
                self._run_lease_renewal(),
                name="operations-v2-lease-renewal",
            ),
        )
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        failure: BaseException | None = None
        for task in done:
            if not task.cancelled() and task.exception() is not None:
                failure = task.exception()
                break
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if failure is not None:
            raise failure

    async def _run_lease_renewal(self) -> None:
        while not self.shutdown.requested:
            await asyncio.sleep(self.settings.worker_lease_renew_interval_sec)
            if self.shutdown.requested:
                return
            await self.lease_manager.renew()

    async def _run_stage(
        self,
        stage: str,
        runner: Callable[[], Awaitable[ResultT]],
        interval_sec: int,
        state: _StageState,
        *,
        validate: Callable[[ResultT], None] | None = None,
        initial_delay: bool = False,
    ) -> None:
        if initial_delay:
            await asyncio.sleep(interval_sec)
        while not self.shutdown.requested:
            try:
                result = await runner()
                if validate is not None:
                    validate(result)
                error, warning = _stage_result_health_flags(result)
            except Exception as exc:
                with suppress(Exception):
                    await self.run_operations.record_scheduler_heartbeat(
                        "error",
                        {
                            "component": "operations_v2",
                            "checkpoint": "independent_stage_failed",
                            "failed_stage": stage,
                            "error_type": type(exc).__name__,
                        },
                    )
                raise
            state.last_completed_at = now_utc()
            state.error = error
            state.warning = warning
            if self.shutdown.requested:
                return
            await asyncio.sleep(interval_sec)

    async def _run_heartbeat(
        self,
        started_at: datetime,
        states: dict[str, _StageState],
    ) -> None:
        while not self.shutdown.requested:
            await asyncio.sleep(self.settings.operations_heartbeat_interval_sec)
            if self.shutdown.requested:
                return
            completed_at = now_utc()
            incomplete_stages: list[JsonValue] = [
                to_json_value(name)
                for name, state in sorted(states.items())
                if state.last_completed_at is None
            ]
            warning_stages: list[JsonValue] = [
                to_json_value(name)
                for name, state in sorted(states.items())
                if state.warning
            ]
            error_stages: list[JsonValue] = [
                to_json_value(name)
                for name, state in sorted(states.items())
                if state.error
            ]
            await self.run_operations.record_scheduler_heartbeat(
                (
                    "error"
                    if error_stages
                    else "warning"
                    if incomplete_stages
                    else "ok"
                ),
                {
                    "component": "operations_v2",
                    "checkpoint": "independent_scheduler_running",
                    "started_at": started_at.isoformat(),
                    "completed_at": completed_at.isoformat(),
                    "stage_last_completed_at": {
                        name: (
                            state.last_completed_at.isoformat()
                            if state.last_completed_at is not None
                            else None
                        )
                        for name, state in states.items()
                    },
                    "error_stages": error_stages,
                    "incomplete_stages": incomplete_stages,
                    "warning_stages": warning_stages,
                },
            )

    async def _record_shutdown_heartbeat(self) -> None:
        with suppress(Exception):
            await self.run_operations.record_scheduler_heartbeat(
                "shutting_down",
                {
                    "component": "operations_v2",
                    "checkpoint": "independent_scheduler_stopping",
                    "completed_at": now_utc().isoformat(),
                },
            )


def _validate_command_result(result: object) -> None:
    failed = getattr(result, "failed", None)
    unacknowledged = getattr(result, "unacknowledged", None)
    if (
        isinstance(failed, bool)
        or not isinstance(failed, int)
        or failed < 0
        or isinstance(unacknowledged, bool)
        or not isinstance(unacknowledged, int)
        or unacknowledged < 0
    ):
        raise RuntimeError("operations_command_result_is_invalid")
    if failed:
        raise OperationsV2RunError(("commands_failed",))
    if unacknowledged:
        raise OperationsV2RunError(("commands_unacknowledged",))


def _stage_result_health_flags(result: object) -> tuple[bool, bool]:
    has_error = False
    has_business_warning = False
    for field in ("failed", "unacknowledged", "blocked", "manual"):
        value = getattr(result, field, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RuntimeError("operations_stage_result_is_invalid")
        if value:
            if field in {"failed", "unacknowledged"}:
                has_error = True
            else:
                has_business_warning = True
    return has_error, has_business_warning
