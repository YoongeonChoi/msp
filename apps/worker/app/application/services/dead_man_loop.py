from __future__ import annotations

import asyncio

from app.application.use_cases.run_dead_man_monitor import RunDeadManMonitor
from app.infrastructure.graceful_shutdown import ShutdownFlag


class DeadManMonitorLoop:
    def __init__(
        self,
        run_monitor: RunDeadManMonitor,
        shutdown: ShutdownFlag,
        *,
        interval_sec: int,
    ) -> None:
        if isinstance(interval_sec, bool) or not 5 <= interval_sec <= 300:
            raise ValueError("dead_man_interval_sec_is_invalid")
        self.run_monitor = run_monitor
        self.shutdown = shutdown
        self.interval_sec = interval_sec

    async def run(self) -> None:
        while not self.shutdown.requested:
            await self.run_monitor.run_once()
            if self.shutdown.requested:
                return
            await asyncio.sleep(self.interval_sec)
