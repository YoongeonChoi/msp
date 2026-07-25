from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from app.application.ports.paper_execution_command_source_port import (
    ClaimedPaperExecutionCommand,
    PaperExecutionCommandBundle,
    PaperExecutionSourceCompletion,
    PaperExecutionSourceOutcome,
)
from app.domain.execution_v2.models import ExecutionInvariantError

if TYPE_CHECKING:
    from app.application.services.scheduler_invocation_deadline import (
        SchedulerInvocationEffectAuthorization,
    )


class UnavailablePaperExecutionCommandSource:
    """Fail-closed placeholder until the durable source RPC contract is deployed."""

    async def claim_available_paper_execution(
        self,
        *,
        worker_id: str,
        release_sha: str,
        now: datetime,
        lease_ttl: timedelta,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> ClaimedPaperExecutionCommand | None:
        del worker_id, release_sha, now, lease_ttl, scheduler_authorization
        raise ExecutionInvariantError("paper_execution_source_unavailable")

    async def load_claimed_paper_execution_bundle(
        self,
        claim: ClaimedPaperExecutionCommand,
        *,
        now: datetime,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> PaperExecutionCommandBundle:
        del claim, now, scheduler_authorization
        raise ExecutionInvariantError("paper_execution_source_unavailable")

    async def complete_or_reschedule_paper_execution(
        self,
        *,
        command_id: str,
        claim_token: str,
        expected_revision: int,
        worker_id: str,
        release_sha: str,
        now: datetime,
        outcome: PaperExecutionSourceOutcome,
        next_available_at: datetime | None,
        reason_code: str,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> PaperExecutionSourceCompletion:
        del (
            command_id,
            claim_token,
            expected_revision,
            worker_id,
            release_sha,
            now,
            outcome,
            next_available_at,
            reason_code,
            scheduler_authorization,
        )
        raise ExecutionInvariantError("paper_execution_source_unavailable")
