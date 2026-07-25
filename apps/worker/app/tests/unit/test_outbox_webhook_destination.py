import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import uuid4

import httpx
import pytest

from app.adapters.alerts.outbox_webhook_destination import OutboxWebhookDestination
from app.application.services.scheduler_invocation_deadline import (
    SchedulerInvocationEffectAuthorization,
)
from app.domain.operations.models import (
    ClaimedDeliveryOutboxItem,
    OperationsInvariantError,
)
from app.infrastructure.authenticated_webhook import (
    AuthenticatedWebhookResponse,
    AuthenticatedWebhookTransport,
)
from app.tests.receiver_ack_fixture import (
    TEST_ACK_NOW,
    json_response_body,
    receiver_key_ring_fixture,
    signed_receiver_response,
)


async def test_outbox_forwards_same_authorization_and_exact_scheduler_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = cast(SchedulerInvocationEffectAuthorization, object())
    captured: list[dict[str, object]] = []
    item = _audit_item("a" * 64)
    receipt_id = "archive-object-version-42"

    async def post_json(
        _transport: AuthenticatedWebhookTransport,
        **kwargs: object,
    ) -> AuthenticatedWebhookResponse:
        captured.append(kwargs)
        return AuthenticatedWebhookResponse(
            status_code=201,
            content=json_response_body(
                {
                    "immutable_receipt_id": receipt_id,
                    "archived_event_hash": "a" * 64,
                }
            ),
        )

    monkeypatch.setattr(AuthenticatedWebhookTransport, "post_json", post_json)
    receipt = await OutboxWebhookDestination(
        "https://archive.example.test/events",
        key_ring=receiver_key_ring_fixture(),
        clock=lambda: TEST_ACK_NOW,
    ).deliver_outbox_item(
        item,
        dedupe_key=item.dedupe_key,
        scheduler_authorization=authorization,
    )

    assert receipt.external_receipt_id == receipt_id
    assert captured[0]["scheduler_authorization"] is authorization
    assert "scheduler_job_key" not in captured[0]


async def test_webhook_destination_passes_receiver_dedupe_key_and_safe_payload() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        dedupe_key = f"execution-quarantine:{outbox_id}"
        body = json_response_body(
            {
                "immutable_receipt_id": "receiver-42",
                "accepted_dedupe_key": dedupe_key,
                "receipt_sha256": _receipt_sha256(dedupe_key, "receiver-42"),
            }
        )
        return signed_receiver_response(
            request,
            context="outbox",
            binding=_binding(item),
            status_code=202,
            body=body,
        )

    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    outbox_id = str(uuid4())
    item = ClaimedDeliveryOutboxItem(
        outbox_id=outbox_id,
        dedupe_key=f"execution-quarantine:{outbox_id}",
        event_type="execution_quarantined",
        payload_version=1,
        aggregate_type="order_intent",
        aggregate_id=str(uuid4()),
        payload={"reason_code": "provider_identity_mismatch"},
        destination_type="operations_metric",
        attempt_count=1,
        lease_token=str(uuid4()),
        lease_expires_at=now + timedelta(seconds=30),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        destination = OutboxWebhookDestination(
            "https://alerts.example.test/events",
            key_ring=receiver_key_ring_fixture(),
            client=client,
            clock=lambda: TEST_ACK_NOW,
        )
        receipt = await destination.deliver_outbox_item(
            item,
            dedupe_key=item.dedupe_key,
        )

    assert receipt.external_receipt_id == "receiver-42"
    assert len(receipt.external_receipt_sha256) == 64
    assert seen[0].headers["Idempotency-Key"] == item.dedupe_key
    assert json.loads(seen[0].content) == {
        "schema_version": 1,
        "outbox_id": item.outbox_id,
        "dedupe_key": item.dedupe_key,
        "event_type": "execution_quarantined",
        "aggregate_type": "order_intent",
        "aggregate_id": item.aggregate_id,
        "destination_type": "operations_metric",
        "payload": {"reason_code": "provider_identity_mismatch"},
    }


@pytest.mark.parametrize(
    ("body", "error"),
    [
        (None, "receipt_is_missing"),
        (
            {
                "immutable_receipt_id": "receiver-42",
                "accepted_dedupe_key": "wrong-key",
                "receipt_sha256": "a" * 64,
            },
            "dedupe_evidence_mismatch",
        ),
    ],
)
async def test_generic_webhook_refuses_2xx_without_receiver_dedupe_evidence(
    body: dict[str, str] | None,
    error: str,
) -> None:
    item = _audit_item("a" * 64)
    item = ClaimedDeliveryOutboxItem(
        outbox_id=item.outbox_id,
        dedupe_key=f"incident:{item.outbox_id}",
        event_type="incident_alert",
        payload_version=1,
        aggregate_type="incident",
        aggregate_id=item.aggregate_id,
        payload={},
        destination_type="incident_alert",
        attempt_count=1,
        lease_token=item.lease_token,
        lease_expires_at=item.lease_expires_at,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        response_body = json_response_body(body) if body is not None else b""
        return signed_receiver_response(
            request,
            context="outbox",
            binding=_binding(item),
            status_code=202,
            body=response_body,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(OperationsInvariantError, match=error):
            await OutboxWebhookDestination(
                "https://alerts.example.test/events",
                key_ring=receiver_key_ring_fixture(),
                client=client,
                clock=lambda: TEST_ACK_NOW,
            ).deliver_outbox_item(item, dedupe_key=item.dedupe_key)


async def test_audit_archive_requires_matching_external_hash_evidence() -> None:
    event_hash = "a" * 64

    item = _audit_item(event_hash)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json_response_body(
            {
                "immutable_receipt_id": "archive-object-version-42",
                "archived_event_hash": event_hash,
            }
        )
        return signed_receiver_response(
            request,
            context="outbox",
            binding=_binding(item),
            status_code=201,
            body=body,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        receipt = await OutboxWebhookDestination(
            "https://archive.example.test/events",
            key_ring=receiver_key_ring_fixture(),
            client=client,
            clock=lambda: TEST_ACK_NOW,
        ).deliver_outbox_item(item, dedupe_key=item.dedupe_key)

    assert receipt.external_receipt_id == "archive-object-version-42"
    assert receipt.external_receipt_sha256 == event_hash


async def test_audit_archive_refuses_success_without_hash_evidence() -> None:
    item = _audit_item("a" * 64)

    def handler(request: httpx.Request) -> httpx.Response:
        return signed_receiver_response(
            request,
            context="outbox",
            binding=_binding(item),
            status_code=201,
            body=json_response_body({"immutable_receipt_id": "archive-object-version-42"}),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(OperationsInvariantError, match="receipt_shape_is_invalid"):
            await OutboxWebhookDestination(
                "https://archive.example.test/events",
                key_ring=receiver_key_ring_fixture(),
                client=client,
                clock=lambda: TEST_ACK_NOW,
            ).deliver_outbox_item(item, dedupe_key=item.dedupe_key)


async def test_outbox_destination_rejects_unsigned_2xx_before_receipt_checks() -> None:
    item = _audit_item("a" * 64)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={
                "immutable_receipt_id": "archive-object-version-42",
                "archived_event_hash": "a" * 64,
            },
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(
            OperationsInvariantError,
            match="outbox_receiver_authentication_failed",
        ):
            await OutboxWebhookDestination(
                "https://archive.example.test/events",
                key_ring=receiver_key_ring_fixture(),
                client=client,
                clock=lambda: TEST_ACK_NOW,
            ).deliver_outbox_item(item, dedupe_key=item.dedupe_key)


async def test_outbox_destination_rejects_unknown_destination_before_network() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500, request=request)

    original = _audit_item("a" * 64)
    item = ClaimedDeliveryOutboxItem(
        outbox_id=original.outbox_id,
        dedupe_key=original.dedupe_key,
        event_type=original.event_type,
        payload_version=original.payload_version,
        aggregate_type=original.aggregate_type,
        aggregate_id=original.aggregate_id,
        payload=original.payload,
        destination_type="unregistered_destination",
        attempt_count=original.attempt_count,
        lease_token=original.lease_token,
        lease_expires_at=original.lease_expires_at,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(OperationsInvariantError, match="destination_type_is_invalid"):
            await OutboxWebhookDestination(
                "https://alerts.example.test/events",
                key_ring=receiver_key_ring_fixture(),
                client=client,
                clock=lambda: TEST_ACK_NOW,
            ).deliver_outbox_item(item, dedupe_key=item.dedupe_key)

    assert requests == 0


def _audit_item(event_hash: str) -> ClaimedDeliveryOutboxItem:
    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    outbox_id = str(uuid4())
    return ClaimedDeliveryOutboxItem(
        outbox_id=outbox_id,
        dedupe_key=f"audit-archive:{outbox_id}",
        event_type="audit_event_archived",
        payload_version=1,
        aggregate_type="audit_event",
        aggregate_id=str(uuid4()),
        payload={"event_hash": event_hash},
        destination_type="audit_archive",
        attempt_count=1,
        lease_token=str(uuid4()),
        lease_expires_at=now + timedelta(seconds=30),
    )


def _receipt_sha256(dedupe_key: str, receipt_id: str) -> str:
    material = json.dumps(
        {
            "accepted_dedupe_key": dedupe_key,
            "immutable_receipt_id": receipt_id,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _binding(item: ClaimedDeliveryOutboxItem) -> dict[str, str]:
    return {
        "outbox_id": item.outbox_id,
        "destination_type": item.destination_type,
        "dedupe_key": item.dedupe_key,
    }
