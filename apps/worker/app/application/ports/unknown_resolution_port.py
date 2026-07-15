from __future__ import annotations

from datetime import datetime
from typing import Protocol

from app.domain.execution_v2.models import ExecutionEnvironment
from app.domain.execution_v2.unknown_resolution import (
    UnknownResolutionApplicationReceipt,
    UnknownResolutionCandidate,
    UnknownResolutionClaim,
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
    ) -> tuple[UnknownResolutionCandidate, ...]: ...

    async def claim_unknown_resolution(
        self,
        candidate: UnknownResolutionCandidate,
        *,
        now: datetime,
    ) -> UnknownResolutionClaim: ...

    async def apply_unknown_resolution(
        self,
        claim: UnknownResolutionClaim,
        *,
        now: datetime,
        replay: bool,
    ) -> UnknownResolutionApplicationReceipt: ...
