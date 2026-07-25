from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from app.application.services.scheduler_invocation_deadline import (
        SchedulerInvocationEffectAuthorization,
    )

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
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
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
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> OperationCommandAcknowledgement:
        ...
