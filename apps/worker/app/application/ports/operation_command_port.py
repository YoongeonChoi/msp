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
        account_id: str,
        holder_id: str,
        release_sha: str,
        fencing_token: int,
        now: datetime,
        limit: int,
    ) -> tuple[ClaimedOperationCommand, ...]:
        ...

    async def acknowledge_operation_command(
        self,
        *,
        command_id: str,
        phase: Literal["applied", "failed"],
        account_id: str,
        holder_id: str,
        release_sha: str,
        fencing_token: int,
        expected_revision: int,
        now: datetime,
        result_summary: JsonObject,
        failure_code: str | None = None,
    ) -> OperationCommandAcknowledgement:
        ...
