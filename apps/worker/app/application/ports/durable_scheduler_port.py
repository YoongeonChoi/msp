from __future__ import annotations

import re
from typing import Protocol
from uuid import UUID

from app.application.ports.persistence_authority import PersistenceAuthority
from app.domain.common.errors import KnownFailClosedError
from app.domain.execution_v2.models import WorkerLease
from app.domain.scheduler.models import (
    ScheduledJobClaimReceiptV1,
    ScheduledJobClaimV1,
    ScheduledJobCompletionReceiptV1,
    ScheduledJobDefinitionReceiptV1,
    ScheduledJobDefinitionV1,
    ScheduledJobFailureReceiptV1,
    SchedulerDeadLetterInspectionReceiptV1,
    SchedulerDefinitionConvergenceReceiptV1,
    SchedulerInvariantError,
    SchedulerReplayAssessmentV1,
    SchedulerReplayReceiptV1,
    canonical_scheduler_datetime,
)

_ACCOUNT_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
_MAX_DATABASE_BIGINT = 9_223_372_036_854_775_807
DURABLE_SCHEDULER_TOTAL_RPC_TIMEOUT_SECONDS = 4.0


class SchedulerMutationOutcomeUnknownError(KnownFailClosedError):
    """A scheduler RPC may have committed, but no valid receipt was observed."""

    def __init__(self, safe_message: str = "scheduler_mutation_outcome_unknown") -> None:
        super().__init__("durable_scheduler", safe_message)


class SchedulerTransitionRejectedError(KnownFailClosedError):
    """The database deterministically rejected a scheduler transition."""

    def __init__(self, safe_message: str = "scheduler_transition_rejected") -> None:
        super().__init__("durable_scheduler", safe_message)


def canonical_scheduler_outer_lease(value: object) -> WorkerLease:
    """Copy an untrusted outer lease into exact built-in scalar/time types."""

    try:
        if type(value) is not WorkerLease:
            raise ValueError
        lease = value
        if (
            type(lease.account_id) is not str
            or _ACCOUNT_RE.fullmatch(lease.account_id) is None
            or type(lease.holder_id) is not str
            or str(UUID(lease.holder_id)) != lease.holder_id
            or type(lease.fencing_token) is not int
            or not 0 < lease.fencing_token <= _MAX_DATABASE_BIGINT
        ):
            raise ValueError
        return WorkerLease(
            account_id=lease.account_id,
            holder_id=lease.holder_id,
            fencing_token=lease.fencing_token,
            acquired_at=canonical_scheduler_datetime(
                lease.acquired_at,
                "scheduler_outer_lease_acquired_at",
            ),
            expires_at=canonical_scheduler_datetime(
                lease.expires_at,
                "scheduler_outer_lease_expires_at",
            ),
        )
    except Exception:
        raise SchedulerInvariantError("scheduler_outer_lease_is_invalid") from None


class DurableSchedulerPort(Protocol):
    """DB-clock-owned scheduler transitions bound to the active worker lease."""

    @property
    def release_sha(self) -> str: ...

    @property
    def persistence_authority(self) -> PersistenceAuthority: ...

    async def ensure_job_definition(
        self,
        definition: ScheduledJobDefinitionV1,
        *,
        outer_lease: WorkerLease,
    ) -> ScheduledJobDefinitionReceiptV1: ...

    async def converge_job_definition(
        self,
        definition: ScheduledJobDefinitionV1,
        *,
        outer_lease: WorkerLease,
    ) -> SchedulerDefinitionConvergenceReceiptV1: ...

    async def claim_due_job(
        self,
        *,
        outer_lease: WorkerLease,
    ) -> ScheduledJobClaimReceiptV1: ...

    async def complete_job_run(
        self,
        claim: ScheduledJobClaimV1,
        *,
        outer_lease: WorkerLease,
        result_sha256: str,
    ) -> ScheduledJobCompletionReceiptV1: ...

    async def fail_job_run(
        self,
        claim: ScheduledJobClaimV1,
        *,
        outer_lease: WorkerLease,
        failure_reason_code: str,
        failure_sha256: str,
        retryable: bool,
    ) -> ScheduledJobFailureReceiptV1: ...

    async def inspect_dead_letter(
        self,
        source_run_id: str,
        *,
        outer_lease: WorkerLease,
    ) -> SchedulerDeadLetterInspectionReceiptV1: ...

    async def replay_dead_letter(
        self,
        assessment: SchedulerReplayAssessmentV1,
        *,
        replay_request_id: str,
        confirmed_reason_code: str,
        explicit_confirmation: bool,
        outer_lease: WorkerLease,
    ) -> SchedulerReplayReceiptV1: ...
