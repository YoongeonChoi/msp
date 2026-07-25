from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from app.application.services.scheduler_invocation_deadline import (
        SchedulerInvocationEffectAuthorization,
    )

from app.domain.execution_v2.cash_settlement import (
    CashSettlementClaim,
    CashSettlementFailureCode,
    CashSettlementFailureReceipt,
    CashSettlementReceipt,
)


class CashSettlementPort(Protocol):
    async def claim_cash_settlement_batch(
        self,
        *,
        account_id: str,
        holder_id: str,
        release_sha: str,
        fencing_token: int,
        now: datetime,
        limit: int,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> tuple[CashSettlementClaim, ...]:
        ...

    async def complete_cash_settlement(
        self,
        claim: CashSettlementClaim,
        *,
        holder_id: str,
        release_sha: str,
        fencing_token: int,
        now: datetime,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> CashSettlementReceipt:
        ...

    async def fail_cash_settlement_attempt(
        self,
        claim: CashSettlementClaim,
        *,
        holder_id: str,
        release_sha: str,
        fencing_token: int,
        now: datetime,
        error_code: CashSettlementFailureCode,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> CashSettlementFailureReceipt:
        ...
