from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from app.domain.execution_v2.models import ExecutionEnvironment
from app.domain.execution_v2.unknown_resolution import (
    UnknownResolutionApplicationReceipt,
    UnknownResolutionCandidate,
    UnknownResolutionClaim,
)

if TYPE_CHECKING:
    from app.application.services.scheduler_invocation_deadline import (
        SchedulerInvocationEffectAuthorization,
    )


class UnknownResolutionPort(Protocol):
    async def list_unknown_resolution_candidates(
        self,
        *,
        account_id: str,
        environment: ExecutionEnvironment,
        holder_id: str,
        release_sha: str,
        fencing_token: int,
        now: datetime,
        limit: int,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> tuple[UnknownResolutionCandidate, ...]: ...

    async def claim_unknown_resolution(
        self,
        candidate: UnknownResolutionCandidate,
        *,
        now: datetime,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> UnknownResolutionClaim: ...

    async def apply_unknown_resolution(
        self,
        claim: UnknownResolutionClaim,
        *,
        now: datetime,
        replay: bool,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> UnknownResolutionApplicationReceipt: ...
