from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.application.ports.alert_outbox_port import (
    AlertOutboxPort,
    DedupeAwareAlertDestinationPort,
)
from app.application.services.scheduler_invocation_deadline import (
    SchedulerInvocationEffectAuthorization,
    require_scheduler_invocation_effect_authorization,
)
from app.domain.common.time import now_utc
from app.domain.operations.models import OperationsInvariantError

_SAFE_DESTINATION_ERROR_CODES = frozenset({"outbox_receiver_authentication_failed"})


@dataclass(frozen=True, slots=True)
class AlertOutboxDispatchResult:
    claimed: int
    delivered: int
    failed: int


class DispatchAlertOutbox:
    """Lease, deliver, and settle the durable alert outbox.

    A delivery completion write can fail after the receiver accepted the event.
    The stable receiver dedupe key makes the resulting retry safe; this use case
    intentionally does not mark that case as a delivery failure.
    """

    def __init__(
        self,
        outbox: AlertOutboxPort,
        destination: DedupeAwareAlertDestinationPort,
        *,
        worker_id: str,
        clock: Callable[[], datetime] = now_utc,
        lease_ttl: timedelta = timedelta(seconds=30),
        retry_after: timedelta = timedelta(seconds=30),
        max_retry_after: timedelta = timedelta(hours=1),
    ) -> None:
        if not worker_id.strip():
            raise OperationsInvariantError("outbox_worker_id_is_required")
        if (
            lease_ttl <= timedelta(0)
            or retry_after <= timedelta(0)
            or max_retry_after < retry_after
        ):
            raise OperationsInvariantError("outbox_duration_must_be_positive")
        self.outbox = outbox
        self.destination = destination
        self.worker_id = worker_id
        self.clock = clock
        self.lease_ttl = lease_ttl
        self.retry_after = retry_after
        self.max_retry_after = max_retry_after

    async def dispatch_once(self, *, limit: int = 50) -> AlertOutboxDispatchResult:
        return await self._dispatch(
            limit=limit,
            scheduler_authorization=None,
        )

    async def dispatch_scheduled(
        self,
        scheduler_authorization: SchedulerInvocationEffectAuthorization,
        *,
        limit: int = 50,
    ) -> AlertOutboxDispatchResult:
        authorization = require_scheduler_invocation_effect_authorization(
            scheduler_authorization,
            expected_job_key="operations.outbox",
        )
        return await self._dispatch(
            limit=limit,
            scheduler_authorization=authorization,
        )

    async def _dispatch(
        self,
        *,
        limit: int,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None,
    ) -> AlertOutboxDispatchResult:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise OperationsInvariantError("outbox_dispatch_limit_is_invalid")
        claimed_at = self._now()
        if scheduler_authorization is None:
            items = await self.outbox.claim_delivery_outbox(
                worker_id=self.worker_id,
                now=claimed_at,
                limit=limit,
                lease_ttl=self.lease_ttl,
            )
        else:
            authorization = require_scheduler_invocation_effect_authorization(
                scheduler_authorization,
                expected_job_key="operations.outbox",
            )
            items = await self.outbox.claim_delivery_outbox(
                worker_id=self.worker_id,
                now=claimed_at,
                limit=limit,
                lease_ttl=self.lease_ttl,
                scheduler_authorization=authorization,
            )
        delivered = 0
        failed = 0
        for item in items:
            if item.lease_expires_at <= claimed_at:
                raise OperationsInvariantError("claimed_outbox_lease_is_expired")
            try:
                if scheduler_authorization is None:
                    receipt = await self.destination.deliver_outbox_item(
                        item,
                        dedupe_key=item.dedupe_key,
                    )
                else:
                    authorization = require_scheduler_invocation_effect_authorization(
                        scheduler_authorization,
                        expected_job_key="operations.outbox",
                    )
                    receipt = await self.destination.deliver_outbox_item(
                        item,
                        dedupe_key=item.dedupe_key,
                        scheduler_authorization=authorization,
                    )
            except Exception as exc:
                failed_at = self._now()
                if scheduler_authorization is None:
                    await self.outbox.fail_outbox_delivery(
                        outbox_id=item.outbox_id,
                        worker_id=self.worker_id,
                        lease_token=item.lease_token,
                        now=failed_at,
                        error_code=_safe_delivery_error_code(exc),
                        retry_after=self._retry_delay(item.attempt_count),
                    )
                else:
                    authorization = require_scheduler_invocation_effect_authorization(
                        scheduler_authorization,
                        expected_job_key="operations.outbox",
                    )
                    await self.outbox.fail_outbox_delivery(
                        outbox_id=item.outbox_id,
                        worker_id=self.worker_id,
                        lease_token=item.lease_token,
                        now=failed_at,
                        error_code=_safe_delivery_error_code(exc),
                        retry_after=self._retry_delay(item.attempt_count),
                        scheduler_authorization=authorization,
                    )
                failed += 1
                continue

            completed_at = self._now()
            if scheduler_authorization is None:
                await self.outbox.complete_outbox_delivery(
                    outbox_id=item.outbox_id,
                    worker_id=self.worker_id,
                    lease_token=item.lease_token,
                    now=completed_at,
                    external_receipt_id=receipt.external_receipt_id,
                    external_receipt_sha256=receipt.external_receipt_sha256,
                )
            else:
                authorization = require_scheduler_invocation_effect_authorization(
                    scheduler_authorization,
                    expected_job_key="operations.outbox",
                )
                await self.outbox.complete_outbox_delivery(
                    outbox_id=item.outbox_id,
                    worker_id=self.worker_id,
                    lease_token=item.lease_token,
                    now=completed_at,
                    external_receipt_id=receipt.external_receipt_id,
                    external_receipt_sha256=receipt.external_receipt_sha256,
                    scheduler_authorization=authorization,
                )
            delivered += 1
        return AlertOutboxDispatchResult(
            claimed=len(items),
            delivered=delivered,
            failed=failed,
        )

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise OperationsInvariantError("outbox_clock_must_be_timezone_aware")
        return value

    def _retry_delay(self, attempt_count: int) -> timedelta:
        exponent = min(attempt_count - 1, 20)
        delay_seconds = self.retry_after.total_seconds() * float(2**exponent)
        capped_seconds = min(delay_seconds, self.max_retry_after.total_seconds())
        return timedelta(seconds=capped_seconds)


def _safe_delivery_error_code(exc: Exception) -> str:
    if isinstance(exc, OperationsInvariantError) and len(exc.args) == 1:
        code = exc.args[0]
        if type(code) is str and code in _SAFE_DESTINATION_ERROR_CODES:
            return f"destination_{code}"
    return f"destination_{type(exc).__name__.lower()}"[:120]
