import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from app.adapters.alerts.outbox_webhook_destination import OutboxWebhookDestination
from app.domain.operations.models import (
    ClaimedDeliveryOutboxItem,
    OperationsInvariantError,
)


async def test_webhook_destination_passes_receiver_dedupe_key_and_safe_payload() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        dedupe_key = f"execution-quarantine:{outbox_id}"
        return httpx.Response(
            202,
            json={
                "immutable_receipt_id": "receiver-42",
                "accepted_dedupe_key": dedupe_key,
                "receipt_sha256": _receipt_sha256(dedupe_key, "receiver-42"),
            },
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
        destination_type="ops_webhook",
        attempt_count=1,
        lease_token=str(uuid4()),
        lease_expires_at=now + timedelta(seconds=30),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        destination = OutboxWebhookDestination(
            "https://alerts.example.test/events",
            client=client,
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
        "dedupe_key": item.dedupe_key,
        "event_type": "execution_quarantined",
        "aggregate_type": "order_intent",
        "aggregate_id": item.aggregate_id,
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
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, json=body) if body is not None else httpx.Response(202)

    item = _audit_item("a" * 64)
    item = ClaimedDeliveryOutboxItem(
        outbox_id=item.outbox_id,
        dedupe_key=f"incident:{item.outbox_id}",
        event_type="incident_alert",
        payload_version=1,
        aggregate_type="incident",
        aggregate_id=item.aggregate_id,
        payload={},
        destination_type="ops_webhook",
        attempt_count=1,
        lease_token=item.lease_token,
        lease_expires_at=item.lease_expires_at,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(OperationsInvariantError, match=error):
            await OutboxWebhookDestination(
                "https://alerts.example.test/events",
                client=client,
            ).deliver_outbox_item(item, dedupe_key=item.dedupe_key)


async def test_audit_archive_requires_matching_external_hash_evidence() -> None:
    event_hash = "a" * 64

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={
                "immutable_receipt_id": "archive-object-version-42",
                "archived_event_hash": event_hash,
            },
        )

    item = _audit_item(event_hash)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        receipt = await OutboxWebhookDestination(
            "https://archive.example.test/events",
            client=client,
        ).deliver_outbox_item(item, dedupe_key=item.dedupe_key)

    assert receipt.external_receipt_id == "archive-object-version-42"
    assert receipt.external_receipt_sha256 == event_hash


async def test_audit_archive_refuses_success_without_hash_evidence() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={"immutable_receipt_id": "archive-object-version-42"},
        )

    item = _audit_item("a" * 64)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(OperationsInvariantError, match="receipt_shape_is_invalid"):
            await OutboxWebhookDestination(
                "https://archive.example.test/events",
                client=client,
            ).deliver_outbox_item(item, dedupe_key=item.dedupe_key)


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
