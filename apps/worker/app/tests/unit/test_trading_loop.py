from __future__ import annotations

from typing import cast

import pytest

from app.application.services.trading_loop import TradingLoop
from app.application.use_cases.run_trading_cycle import RunTradingCycle
from app.config import Settings
from app.domain.common.errors import KnownFailClosedError
from app.infrastructure.graceful_shutdown import ShutdownFlag


async def test_continuous_loop_records_known_fail_closed_and_keeps_running() -> None:
    shutdown = ShutdownFlag()
    cycle = FakeTradingCycle(shutdown)
    loop = TradingLoop(
        Settings(RUN_ONCE=False, LOOP_INTERVAL_SEC=0),
        shutdown,
        cast(RunTradingCycle, cycle),
    )

    await loop.run()

    assert cycle.calls == 2
    assert cycle.repository.events == [
        {
            "level": "warning",
            "component": "toss",
            "message": "toss_access_denied",
            "details": {"fail_closed": True, "loop_continues": True},
        }
    ]


async def test_run_once_preserves_known_fail_closed_for_operator_smoke() -> None:
    shutdown = ShutdownFlag()
    cycle = FakeTradingCycle(shutdown, always_fail=True)
    loop = TradingLoop(
        Settings(RUN_ONCE=True, LOOP_INTERVAL_SEC=0),
        shutdown,
        cast(RunTradingCycle, cycle),
    )

    with pytest.raises(KnownFailClosedError, match="toss_access_denied"):
        await loop.run()

    assert cycle.calls == 1
    assert cycle.repository.events == []


class FakeTradingCycle:
    def __init__(self, shutdown: ShutdownFlag, *, always_fail: bool = False) -> None:
        self.repository = FakeRepository()
        self.shutdown = shutdown
        self.always_fail = always_fail
        self.calls = 0

    async def execute(self) -> None:
        self.calls += 1
        if self.always_fail or self.calls == 1:
            raise KnownFailClosedError("toss", "toss_access_denied")
        self.shutdown.request()


class FakeRepository:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    async def record_engine_event(
        self,
        level: str,
        component: str,
        message: str,
        details: dict[str, object],
    ) -> None:
        self.events.append(
            {
                "level": level,
                "component": component,
                "message": message,
                "details": details,
            }
        )
