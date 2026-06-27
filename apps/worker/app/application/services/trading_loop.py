from __future__ import annotations

import asyncio

from app.application.use_cases.run_trading_cycle import RunTradingCycle
from app.config import Settings
from app.infrastructure.graceful_shutdown import ShutdownFlag


class TradingLoop:
    def __init__(
        self,
        settings: Settings,
        shutdown: ShutdownFlag,
        run_trading_cycle: RunTradingCycle,
    ) -> None:
        self.settings = settings
        self.shutdown = shutdown
        self.run_trading_cycle = run_trading_cycle

    async def run(self) -> None:
        if self.settings.run_once:
            await self.run_trading_cycle.execute()
            return
        while not self.shutdown.requested:
            await self.run_trading_cycle.execute()
            await asyncio.sleep(self.settings.loop_interval_sec)

