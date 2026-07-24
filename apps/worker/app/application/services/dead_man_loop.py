from __future__ import annotations

import asyncio

import structlog

from app.application.use_cases.run_dead_man_monitor import (
    DeadManAlertDeliveryError,
    RunDeadManMonitor,
)
from app.infrastructure.graceful_shutdown import ShutdownFlag

logger = structlog.get_logger()


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
            try:
                await self.run_monitor.run_once()
            except DeadManAlertDeliveryError:
                logger.warning("dead_man_alert_delivery_failed")
            if self.shutdown.requested:
                return
            await asyncio.sleep(self.interval_sec)
