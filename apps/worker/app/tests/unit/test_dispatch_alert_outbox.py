from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.application.use_cases.dispatch_alert_outbox import (
    AlertOutboxDispatchResult,
    DispatchAlertOutbox,
)
from app.domain.operations.models import (
    ClaimedDeliveryOutboxItem,
    CompletedOutboxDelivery,
    FailedOutboxDelivery,
    OutboxDeliveryReceipt,
)


async def test_dispatches_with_stable_receiver_dedupe_key_then_completes() -> None:
    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    outbox = FakeOutbox([_item(now)])
    destination = FakeDestination()
    dispatcher = DispatchAlertOutbox(
        outbox,
        destination,
        worker_id="worker-a",
        clock=lambda: now,
    )

    result = await dispatcher.dispatch_once()

    assert result == AlertOutboxDispatchResult(1, 1, 0)
    assert destination.dedupe_keys == [outbox.items[0].dedupe_key]
    assert outbox.completed == [
        (outbox.items[0].outbox_id, outbox.items[0].lease_token)
    ]
    assert outbox.failed == []


async def test_delivery_failure_is_safely_recorded_for_retry() -> None:
    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    outbox = FakeOutbox([_item(now)])
    destination = FakeDestination(error=TimeoutError("sensitive-message"))
    dispatcher = DispatchAlertOutbox(
        outbox,
        destination,
        worker_id="worker-a",
        clock=lambda: now,
    )

    result = await dispatcher.dispatch_once()

    assert result == AlertOutboxDispatchResult(1, 0, 1)
    assert outbox.completed == []
    assert outbox.failed == [
        (
            outbox.items[0].outbox_id,
            outbox.items[0].lease_token,
            "destination_timeouterror",
        )
    ]
    assert outbox.retry_delays == [timedelta(seconds=30)]


@pytest.mark.parametrize(
    ("attempt_count", "expected_delay"),
    [
        (1, timedelta(seconds=30)),
        (3, timedelta(minutes=2)),
        (100, timedelta(hours=1)),
    ],
)
async def test_delivery_retry_uses_capped_exponential_backoff(
    attempt_count: int,
    expected_delay: timedelta,
) -> None:
    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    item = _item(now, attempt_count=attempt_count)
    outbox = FakeOutbox([item])

    await DispatchAlertOutbox(
        outbox,
        FakeDestination(error=TimeoutError()),
        worker_id="worker-a",
        clock=lambda: now,
    ).dispatch_once()

    assert outbox.retry_delays == [expected_delay]


async def test_completion_crash_retries_same_dedupe_key_without_false_failure() -> None:
    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    item = _item(now)
    outbox = FakeOutbox([item], completion_error=RuntimeError("db unavailable"))
    destination = FakeDestination()
    dispatcher = DispatchAlertOutbox(
        outbox,
        destination,
        worker_id="worker-a",
        clock=lambda: now,
    )

    with pytest.raises(RuntimeError, match="db unavailable"):
        await dispatcher.dispatch_once()

    assert destination.dedupe_keys == [item.dedupe_key]
    assert outbox.failed == []
    assert item.dedupe_key == _item(now, outbox_id=item.outbox_id).dedupe_key


def _item(
    now: datetime,
    *,
    outbox_id: str | None = None,
    attempt_count: int = 1,
) -> ClaimedDeliveryOutboxItem:
    resolved_outbox_id = outbox_id or str(uuid4())
    return ClaimedDeliveryOutboxItem(
        outbox_id=resolved_outbox_id,
        dedupe_key=f"execution-quarantine:{resolved_outbox_id}",
        event_type="execution_quarantined",
        payload_version=1,
        aggregate_type="order_intent",
        aggregate_id=str(uuid4()),
        payload={"reason_code": "provider_observation_hash_mismatch"},
        destination_type="ops_webhook",
        attempt_count=attempt_count,
        lease_token=str(uuid4()),
        lease_expires_at=now + timedelta(seconds=30),
    )


class FakeDestination:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.dedupe_keys: list[str] = []

    async def deliver_outbox_item(
        self,
        item: ClaimedDeliveryOutboxItem,
        *,
        dedupe_key: str,
    ) -> OutboxDeliveryReceipt:
        del item
        self.dedupe_keys.append(dedupe_key)
        if self.error is not None:
            raise self.error
        return OutboxDeliveryReceipt(
            external_receipt_id="receiver-42",
            external_receipt_sha256="a" * 64,
        )


class FakeOutbox:
    def __init__(
        self,
        items: list[ClaimedDeliveryOutboxItem],
        *,
        completion_error: Exception | None = None,
    ) -> None:
        self.items = items
        self.completion_error = completion_error
        self.completed: list[tuple[str, str]] = []
        self.failed: list[tuple[str, str, str]] = []
        self.retry_delays: list[timedelta] = []

    async def claim_delivery_outbox(
        self,
        *,
        worker_id: str,
        now: datetime,
        limit: int,
        lease_ttl: timedelta,
    ) -> tuple[ClaimedDeliveryOutboxItem, ...]:
        del worker_id, now, limit, lease_ttl
        return tuple(self.items)

    async def complete_outbox_delivery(
        self,
        *,
        outbox_id: str,
        worker_id: str,
        lease_token: str,
        now: datetime,
        external_receipt_id: str,
        external_receipt_sha256: str,
    ) -> CompletedOutboxDelivery:
        del worker_id, external_receipt_id, external_receipt_sha256
        if self.completion_error is not None:
            raise self.completion_error
        self.completed.append((outbox_id, lease_token))
        return CompletedOutboxDelivery(outbox_id, "delivered", now)

    async def fail_outbox_delivery(
        self,
        *,
        outbox_id: str,
        worker_id: str,
        lease_token: str,
        now: datetime,
        error_code: str,
        retry_after: timedelta,
    ) -> FailedOutboxDelivery:
        del worker_id
        self.failed.append((outbox_id, lease_token, error_code))
        self.retry_delays.append(retry_after)
        return FailedOutboxDelivery(outbox_id, "pending", now)
