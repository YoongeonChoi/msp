from __future__ import annotations

import asyncio
from typing import cast

import pytest

from app.application.services.dead_man_loop import DeadManMonitorLoop
from app.application.use_cases.run_dead_man_monitor import (
    DeadManAlertDeliveryError,
    RunDeadManMonitor,
)
from app.infrastructure.graceful_shutdown import ShutdownFlag


async def test_dead_man_loop_retries_retryable_delivery_without_exiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutdown = ShutdownFlag()
    runner = RetryThenStopRunner(shutdown)
    sleeps: list[int] = []

    async def fake_sleep(seconds: int) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await DeadManMonitorLoop(
        cast(RunDeadManMonitor, runner),
        shutdown,
        interval_sec=5,
    ).run()

    assert runner.calls == 2
    assert sleeps == [5]


class RetryThenStopRunner:
    def __init__(self, shutdown: ShutdownFlag) -> None:
        self.shutdown = shutdown
        self.calls = 0

    async def run_once(self) -> None:
        self.calls += 1
        if self.calls == 1:
            raise DeadManAlertDeliveryError("dead_man_alert_delivery_failed")
        self.shutdown.request()
