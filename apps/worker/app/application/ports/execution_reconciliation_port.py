from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from app.domain.execution_v2.reconciliation import (
    ExecutionReconciliationClaim,
    ExecutionReconciliationCompletion,
    ExecutionReconciliationDecision,
    ExpiredPaperIntentResult,
    PreDispatchFailureResult,
    ReconciliationOutcome,
)

if TYPE_CHECKING:
    from app.application.services.scheduler_invocation_deadline import (
        SchedulerInvocationEffectAuthorization,
    )


class ExecutionReconciliationPort(Protocol):
    async def claim_execution_reconciliation_batch(
        self,
        *,
        account_id: str,
        worker_id: str,
        release_sha: str,
        fencing_token: int,
        now: datetime,
        limit: int,
        after_priority: int | None,
        after_intent_id: str | None,
        lease_ttl: timedelta,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> tuple[ExecutionReconciliationClaim, ...]:
        ...

    async def complete_execution_reconciliation(
        self,
        *,
        intent_id: str,
        worker_id: str,
        release_sha: str,
        fencing_token: int,
        now: datetime,
        outcome: ReconciliationOutcome,
        next_reconcile_at: datetime | None,
        reason_code: str,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> ExecutionReconciliationCompletion:
        ...


class ExecutionReconciliationHandlerPort(Protocol):
    async def reconcile_claim(
        self,
        claim: ExecutionReconciliationClaim,
        *,
        now: datetime,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> ExecutionReconciliationDecision:
        """Record any observation idempotently, then return claim disposition."""

        ...


class PreDispatchFailurePort(Protocol):
    async def fail_reserved_intent_pre_dispatch(
        self,
        *,
        intent_id: str,
        worker_id: str,
        fencing_token: int,
        control_epoch: int,
        release_sha: str,
        now: datetime,
        reason_code: str,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> PreDispatchFailureResult:
        ...

    async def expire_paper_intent_remainder(
        self,
        *,
        intent_id: str,
        worker_id: str,
        fencing_token: int,
        control_epoch: int,
        release_sha: str,
        now: datetime,
        reason_code: str,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> ExpiredPaperIntentResult:
        ...
