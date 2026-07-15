from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from app.domain.common.json import JsonObject
from app.domain.operations.models import (
    ClaimedOperationCommand,
    OperationCommandAcknowledgement,
)


class OperationCommandPort(Protocol):
    async def claim_operation_command_batch(
        self,
        *,
        holder_id: str,
        now: datetime,
        limit: int,
    ) -> tuple[ClaimedOperationCommand, ...]:
        ...

    async def acknowledge_operation_command(
        self,
        *,
        command_id: str,
        phase: Literal["claimed", "applied", "failed"],
        holder_id: str,
        now: datetime,
        result_summary: JsonObject,
        failure_code: str | None = None,
    ) -> OperationCommandAcknowledgement:
        ...
