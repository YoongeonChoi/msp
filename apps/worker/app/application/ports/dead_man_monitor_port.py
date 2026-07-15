from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from app.application.services.dead_man_service import DeadManSnapshot


class DeadManSnapshotSourcePort(Protocol):
    async def get_dead_man_snapshot(
        self,
        *,
        account_id: str,
        observed_at: datetime,
    ) -> DeadManSnapshot:
        ...


class DeadManAlertDestinationPort(Protocol):
    async def deliver_dead_man_alert(
        self,
        *,
        account_id: str,
        episode_id: str,
        event: Literal["unhealthy", "recovered"],
        reason_codes: tuple[str, ...],
        observed_at: datetime,
    ) -> None:
        ...
