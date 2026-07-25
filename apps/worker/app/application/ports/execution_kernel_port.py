from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from app.domain.execution_v2.models import (
    AccountingTransaction,
    ExecutionCostSchedule,
    ExecutionGate,
    ExecutionIntent,
    ExecutionObservation,
    MinuteBar,
    ObservationRecordResult,
    OrderIntentReservationResult,
    PaperAccountSnapshot,
    PaperExecutionCheckpoint,
    PaperExecutionEvidence,
    PaperPositionCostBasis,
    PaperSimulationResult,
    WorkerLease,
    WorkerLeaseRelease,
)

if TYPE_CHECKING:
    from app.application.services.scheduler_invocation_deadline import (
        SchedulerInvocationEffectAuthorization,
    )


class WorkerLeasePort(Protocol):
    async def acquire_worker_lease(
        self,
        *,
        account_id: str,
        holder_id: str,
        now: datetime,
        ttl: timedelta,
    ) -> WorkerLease:
        ...

    async def renew_worker_lease(
        self,
        lease: WorkerLease,
        *,
        now: datetime,
        ttl: timedelta,
    ) -> WorkerLease:
        ...

    async def release_worker_lease(
        self,
        lease: WorkerLease,
        *,
        now: datetime,
    ) -> WorkerLeaseRelease:
        ...


class ExecutionKernelPort(Protocol):
    """Persistence and coordination boundary for the V2 execution kernel."""

    async def configure_account(
        self,
        account_id: str,
        cash_krw: int,
        positions: Sequence[PaperPositionCostBasis] | None = None,
    ) -> None:
        ...

    async def replace_gate(self, gate: ExecutionGate) -> None:
        ...

    async def acquire_lease(
        self,
        *,
        account_id: str,
        holder_id: str,
        now: datetime,
        ttl: timedelta,
    ) -> WorkerLease:
        ...

    async def reserve_intent(
        self,
        intent: ExecutionIntent,
        *,
        now: datetime,
    ) -> bool:
        ...

    async def execute_paper(
        self,
        intent: ExecutionIntent,
        bars: Sequence[MinuteBar],
        *,
        cost_schedule: ExecutionCostSchedule | None,
        execution_evidence: PaperExecutionEvidence | None,
        now: datetime,
        position_cost_basis: PaperPositionCostBasis | None = None,
    ) -> PaperSimulationResult:
        ...

    async def mark_dispatch_started(
        self,
        intent: ExecutionIntent,
        *,
        now: datetime,
    ) -> None:
        ...

    async def record_execution_observation(
        self,
        intent: ExecutionIntent,
        observation: ExecutionObservation,
        *,
        accounting_transaction: AccountingTransaction | None = None,
        now: datetime,
    ) -> None:
        ...

    async def account_snapshot(self, account_id: str) -> PaperAccountSnapshot:
        ...


class QuiescentPaperAccountRestorePort(Protocol):
    async def restore_quiescent_account(
        self,
        snapshot: PaperAccountSnapshot,
    ) -> None:
        ...


class ContractDispatchPort(Protocol):
    async def mark_dispatch_started(
        self,
        intent: ExecutionIntent,
        *,
        now: datetime,
    ) -> None:
        ...

    async def record_execution_observation(
        self,
        intent: ExecutionIntent,
        observation: ExecutionObservation,
        *,
        accounting_transaction: AccountingTransaction | None = None,
        now: datetime,
    ) -> None:
        ...


class DurableExecutionV2Port(Protocol):
    @property
    def current_release_sha(self) -> str:
        ...

    async def reserve_order_intent(
        self,
        intent: ExecutionIntent,
        *,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> OrderIntentReservationResult:
        ...

    async def mark_dispatch_started(
        self,
        intent: ExecutionIntent,
        *,
        now: datetime,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> None:
        ...

    async def load_paper_execution_checkpoint(
        self,
        intent: ExecutionIntent,
        *,
        now: datetime,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> PaperExecutionCheckpoint:
        ...

    async def record_execution_observation(
        self,
        intent: ExecutionIntent,
        observation: ExecutionObservation,
        *,
        accounting_transaction: AccountingTransaction | None = None,
        intent_release_sha: str,
        now: datetime,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> ObservationRecordResult:
        ...
