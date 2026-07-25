from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import uuid4

import httpx
import pytest

import app.application.use_cases.dispatch_alert_outbox as outbox_module
from app.adapters.alerts.outbox_webhook_destination import OutboxWebhookDestination
from app.application.services.scheduler_invocation_deadline import (
    SchedulerInvocationEffectAuthorization,
    SchedulerInvocationPermitRevoked,
)
from app.application.use_cases.dispatch_alert_outbox import (
    AlertOutboxDispatchResult,
    DispatchAlertOutbox,
)
from app.domain.operations.models import (
    ClaimedDeliveryOutboxItem,
    CompletedOutboxDelivery,
    FailedOutboxDelivery,
    OperationsInvariantError,
    OutboxDeliveryReceipt,
)
from app.infrastructure.authenticated_webhook import ReceiverAckKeyRing
from app.tests.receiver_ack_fixture import (
    TEST_ACK_NOW,
    TEST_CURRENT_KEY,
    TEST_PREVIOUS_KEY,
    TEST_PREVIOUS_KEY_B64,
    json_response_body,
    receiver_key_ring_fixture,
    signed_receiver_response,
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
    assert outbox.completed == [(outbox.items[0].outbox_id, outbox.items[0].lease_token)]
    assert outbox.failed == []


async def test_scheduled_outbox_propagates_authorization_to_every_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    outbox = FakeOutbox([_item(now)])
    destination = FakeDestination()
    authorization = _authorization()
    gate = FakeSchedulerAuthorizationGate(
        authorization,
        expected_job_key="operations.outbox",
    )
    monkeypatch.setattr(
        outbox_module,
        "require_scheduler_invocation_effect_authorization",
        gate,
    )

    result = await DispatchAlertOutbox(
        outbox,
        destination,
        worker_id="worker-a",
        clock=lambda: now,
    ).dispatch_scheduled(authorization)

    assert result == AlertOutboxDispatchResult(1, 1, 0)
    assert gate.calls == 4
    assert outbox.scheduler_authorizations == [authorization, authorization]
    assert destination.scheduler_authorizations == [authorization]


async def test_scheduled_outbox_rejects_missing_authorization_before_claim() -> None:
    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    outbox = FakeOutbox([_item(now)])
    destination = FakeDestination()

    with pytest.raises(SchedulerInvocationPermitRevoked, match="permit_not_issued"):
        await DispatchAlertOutbox(
            outbox,
            destination,
            worker_id="worker-a",
            clock=lambda: now,
        ).dispatch_scheduled(cast(SchedulerInvocationEffectAuthorization, None))

    assert outbox.scheduler_authorizations == []
    assert destination.scheduler_authorizations == []


async def test_scheduled_outbox_revocation_before_claim_blocks_first_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    outbox = FakeOutbox([_item(now)])
    destination = FakeDestination()
    authorization = _authorization()
    gate = FakeSchedulerAuthorizationGate(
        authorization,
        expected_job_key="operations.outbox",
        revoke_at=1,
    )
    monkeypatch.setattr(
        outbox_module,
        "require_scheduler_invocation_effect_authorization",
        gate,
    )

    with pytest.raises(SchedulerInvocationPermitRevoked, match="deadline"):
        await DispatchAlertOutbox(
            outbox,
            destination,
            worker_id="worker-a",
            clock=lambda: now,
        ).dispatch_scheduled(authorization)

    assert outbox.scheduler_authorizations == []
    assert destination.scheduler_authorizations == []


async def test_scheduled_outbox_mid_run_revocation_escapes_broad_exception_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    outbox = FakeOutbox([_item(now)])
    destination = FakeDestination()
    authorization = _authorization()
    gate = FakeSchedulerAuthorizationGate(
        authorization,
        expected_job_key="operations.outbox",
        revoke_at=3,
    )
    monkeypatch.setattr(
        outbox_module,
        "require_scheduler_invocation_effect_authorization",
        gate,
    )

    with pytest.raises(SchedulerInvocationPermitRevoked, match="deadline"):
        await DispatchAlertOutbox(
            outbox,
            destination,
            worker_id="worker-a",
            clock=lambda: now,
        ).dispatch_scheduled(authorization)

    assert outbox.scheduler_authorizations == [authorization]
    assert outbox.completed == []
    assert outbox.failed == []
    assert destination.scheduler_authorizations == []


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


async def test_unsigned_receiver_ack_fails_once_without_completion() -> None:
    item = _item(TEST_ACK_NOW)
    outbox = FakeOutbox([item])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            202,
            content=_generic_receipt_body(item),
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await DispatchAlertOutbox(
            outbox,
            OutboxWebhookDestination(
                "https://alerts.example.test/events",
                key_ring=receiver_key_ring_fixture(),
                client=client,
                clock=lambda: TEST_ACK_NOW,
            ),
            worker_id="worker-a",
            clock=lambda: TEST_ACK_NOW,
        ).dispatch_once()

    assert result == AlertOutboxDispatchResult(1, 0, 1)
    assert outbox.completed == []
    assert len(outbox.failed) == 1
    assert outbox.failed[0][2] == ("destination_outbox_receiver_authentication_failed")


async def test_signed_receiver_ack_completes_once() -> None:
    item = _item(TEST_ACK_NOW)
    outbox = FakeOutbox([item])

    def handler(request: httpx.Request) -> httpx.Response:
        return _signed_generic_response(request, item)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await DispatchAlertOutbox(
            outbox,
            OutboxWebhookDestination(
                "https://alerts.example.test/events",
                key_ring=receiver_key_ring_fixture(),
                client=client,
                clock=lambda: TEST_ACK_NOW,
            ),
            worker_id="worker-a",
            clock=lambda: TEST_ACK_NOW,
        ).dispatch_once()

    assert result == AlertOutboxDispatchResult(1, 1, 0)
    assert outbox.completed == [(item.outbox_id, item.lease_token)]
    assert outbox.failed == []


async def test_arbitrary_destination_error_text_is_not_persisted() -> None:
    item = _item(TEST_ACK_NOW)
    outbox = FakeOutbox([item])
    sensitive_marker = "secret_derived_slug_must_not_escape"

    class UnsafeMessageDestination:
        async def deliver_outbox_item(
            self,
            claimed: ClaimedDeliveryOutboxItem,
            *,
            dedupe_key: str,
            scheduler_authorization: (
                SchedulerInvocationEffectAuthorization | None
            ) = None,
        ) -> OutboxDeliveryReceipt:
            del claimed, dedupe_key, scheduler_authorization
            raise OperationsInvariantError(sensitive_marker)

    result = await DispatchAlertOutbox(
        outbox,
        UnsafeMessageDestination(),
        worker_id="worker-a",
        clock=lambda: TEST_ACK_NOW,
    ).dispatch_once()

    assert result == AlertOutboxDispatchResult(1, 0, 1)
    assert outbox.failed[0][2] == "destination_operationsinvarianterror"
    assert sensitive_marker not in outbox.failed[0][2]


async def test_mixed_signed_and_unsigned_batch_settles_each_item_once() -> None:
    signed_item = _item(TEST_ACK_NOW)
    unsigned_item = _item(TEST_ACK_NOW)
    outbox = FakeOutbox([signed_item, unsigned_item])

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload["outbox_id"] == signed_item.outbox_id:
            return _signed_generic_response(request, signed_item)
        return httpx.Response(
            202,
            content=_generic_receipt_body(unsigned_item),
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await DispatchAlertOutbox(
            outbox,
            OutboxWebhookDestination(
                "https://alerts.example.test/events",
                key_ring=receiver_key_ring_fixture(),
                client=client,
                clock=lambda: TEST_ACK_NOW,
            ),
            worker_id="worker-a",
            clock=lambda: TEST_ACK_NOW,
        ).dispatch_once()

    assert result == AlertOutboxDispatchResult(2, 1, 1)
    assert outbox.completed == [(signed_item.outbox_id, signed_item.lease_token)]
    assert [failed[0] for failed in outbox.failed] == [unsigned_item.outbox_id]


async def test_completion_crash_accepts_same_cached_previous_key_ack_on_retry() -> None:
    active_now = TEST_ACK_NOW
    item = _item(active_now)
    cached_body: bytes | None = None
    cached_headers: list[tuple[bytes, bytes]] | None = None
    request_bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal cached_body, cached_headers
        request_bodies.append(request.content)
        if cached_headers is None or cached_body is None:
            response = _signed_generic_response(
                request,
                item,
                key_id="test-previous",
                key=TEST_PREVIOUS_KEY,
            )
            cached_body = response.content
            cached_headers = list(response.headers.raw)
        return httpx.Response(
            202,
            content=cached_body,
            headers=cached_headers,
            request=request,
        )

    old_ring = ReceiverAckKeyRing.from_base64(
        current_key_id="test-previous",
        current_key_b64=TEST_PREVIOUS_KEY_B64,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first_outbox = FakeOutbox(
            [item],
            completion_error=RuntimeError("completion write failed"),
        )
        with pytest.raises(RuntimeError, match="completion write failed"):
            await DispatchAlertOutbox(
                first_outbox,
                OutboxWebhookDestination(
                    "https://alerts.example.test/events",
                    key_ring=old_ring,
                    client=client,
                    clock=lambda: active_now,
                ),
                worker_id="worker-a",
                clock=lambda: active_now,
            ).dispatch_once()

        active_now = TEST_ACK_NOW + timedelta(minutes=10)
        retry_item = replace(
            item,
            attempt_count=2,
            lease_token=str(uuid4()),
            lease_expires_at=active_now + timedelta(seconds=30),
        )
        retry_outbox = FakeOutbox([retry_item])
        result = await DispatchAlertOutbox(
            retry_outbox,
            OutboxWebhookDestination(
                "https://alerts.example.test/events",
                key_ring=receiver_key_ring_fixture(),
                client=client,
                clock=lambda: active_now,
            ),
            worker_id="worker-a",
            clock=lambda: active_now,
        ).dispatch_once()

    assert result == AlertOutboxDispatchResult(1, 1, 0)
    assert first_outbox.failed == []
    assert retry_outbox.completed == [(retry_item.outbox_id, retry_item.lease_token)]
    assert request_bodies[0] == request_bodies[1]


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
        destination_type="incident_alert",
        attempt_count=attempt_count,
        lease_token=str(uuid4()),
        lease_expires_at=now + timedelta(seconds=30),
    )


class FakeDestination:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.dedupe_keys: list[str] = []
        self.scheduler_authorizations: list[
            SchedulerInvocationEffectAuthorization | None
        ] = []

    async def deliver_outbox_item(
        self,
        item: ClaimedDeliveryOutboxItem,
        *,
        dedupe_key: str,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> OutboxDeliveryReceipt:
        del item
        self.dedupe_keys.append(dedupe_key)
        self.scheduler_authorizations.append(scheduler_authorization)
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
        self.scheduler_authorizations: list[
            SchedulerInvocationEffectAuthorization | None
        ] = []

    async def claim_delivery_outbox(
        self,
        *,
        worker_id: str,
        now: datetime,
        limit: int,
        lease_ttl: timedelta,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> tuple[ClaimedDeliveryOutboxItem, ...]:
        del worker_id, now, limit, lease_ttl
        self.scheduler_authorizations.append(scheduler_authorization)
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
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> CompletedOutboxDelivery:
        del worker_id, external_receipt_id, external_receipt_sha256
        self.scheduler_authorizations.append(scheduler_authorization)
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
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> FailedOutboxDelivery:
        del worker_id
        self.scheduler_authorizations.append(scheduler_authorization)
        self.failed.append((outbox_id, lease_token, error_code))
        self.retry_delays.append(retry_after)
        return FailedOutboxDelivery(outbox_id, "pending", now)


def _generic_receipt_body(item: ClaimedDeliveryOutboxItem) -> bytes:
    receipt_id = f"receipt-{item.outbox_id}"
    material = json.dumps(
        {
            "accepted_dedupe_key": item.dedupe_key,
            "immutable_receipt_id": receipt_id,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return json_response_body(
        {
            "immutable_receipt_id": receipt_id,
            "accepted_dedupe_key": item.dedupe_key,
            "receipt_sha256": hashlib.sha256(material).hexdigest(),
        }
    )


def _signed_generic_response(
    request: httpx.Request,
    item: ClaimedDeliveryOutboxItem,
    *,
    key_id: str = "test-current",
    key: bytes = TEST_CURRENT_KEY,
) -> httpx.Response:
    return signed_receiver_response(
        request,
        context="outbox",
        binding={
            "outbox_id": item.outbox_id,
            "destination_type": item.destination_type,
            "dedupe_key": item.dedupe_key,
        },
        status_code=202,
        body=_generic_receipt_body(item),
        key_id=key_id,
        key=key,
        acknowledged_at=TEST_ACK_NOW,
    )


class FakeSchedulerAuthorizationGate:
    def __init__(
        self,
        authorization: SchedulerInvocationEffectAuthorization,
        *,
        expected_job_key: str,
        revoke_at: int | None = None,
    ) -> None:
        self.authorization = authorization
        self.expected_job_key = expected_job_key
        self.revoke_at = revoke_at
        self.calls = 0

    def __call__(
        self,
        value: object,
        *,
        expected_job_key: str,
    ) -> SchedulerInvocationEffectAuthorization:
        assert value is self.authorization
        assert expected_job_key == self.expected_job_key
        self.calls += 1
        if self.calls == self.revoke_at:
            raise SchedulerInvocationPermitRevoked("deadline")
        return self.authorization


def _authorization() -> SchedulerInvocationEffectAuthorization:
    return cast(SchedulerInvocationEffectAuthorization, object())
