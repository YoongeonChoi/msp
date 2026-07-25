from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from app.application.services.scheduler_invocation_deadline import (
        SchedulerInvocationEffectAuthorization,
    )

from app.domain.operations.models import (
    ClaimedDeliveryOutboxItem,
    CompletedOutboxDelivery,
    FailedOutboxDelivery,
    OutboxDeliveryReceipt,
)


class AlertOutboxPort(Protocol):
    async def claim_delivery_outbox(
        self,
        *,
        worker_id: str,
        now: datetime,
        limit: int,
        lease_ttl: timedelta,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> tuple[ClaimedDeliveryOutboxItem, ...]:
        ...

    async def complete_outbox_delivery(
        self,
        *,
        outbox_id: str,
        worker_id: str,
        lease_token: str,
        now: datetime,
        external_receipt_id: str,
        external_receipt_sha256: str,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> CompletedOutboxDelivery:
        ...

    async def fail_outbox_delivery(
        self,
        *,
        outbox_id: str,
        worker_id: str,
        lease_token: str,
        now: datetime,
        error_code: str,
        retry_after: timedelta,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> FailedOutboxDelivery:
        ...


class DedupeAwareAlertDestinationPort(Protocol):
    async def deliver_outbox_item(
        self,
        item: ClaimedDeliveryOutboxItem,
        *,
        dedupe_key: str,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> OutboxDeliveryReceipt:
        ...
