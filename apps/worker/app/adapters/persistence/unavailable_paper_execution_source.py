from __future__ import annotations

from datetime import datetime, timedelta

from app.application.ports.paper_execution_command_source_port import (
    ClaimedPaperExecutionCommand,
    PaperExecutionCommandBundle,
    PaperExecutionSourceCompletion,
    PaperExecutionSourceOutcome,
)
from app.domain.execution_v2.models import ExecutionInvariantError


class UnavailablePaperExecutionCommandSource:
    """Fail-closed placeholder until the durable source RPC contract is deployed."""

    async def claim_available_paper_execution(
        self,
        *,
        worker_id: str,
        release_sha: str,
        now: datetime,
        lease_ttl: timedelta,
    ) -> ClaimedPaperExecutionCommand | None:
        del worker_id, release_sha, now, lease_ttl
        raise ExecutionInvariantError("paper_execution_source_unavailable")

    async def load_claimed_paper_execution_bundle(
        self,
        claim: ClaimedPaperExecutionCommand,
        *,
        now: datetime,
    ) -> PaperExecutionCommandBundle:
        del claim, now
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
        )
        raise ExecutionInvariantError("paper_execution_source_unavailable")
