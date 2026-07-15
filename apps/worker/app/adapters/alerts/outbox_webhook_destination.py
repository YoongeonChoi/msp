from __future__ import annotations

import hashlib
import json
import re
from typing import cast

import httpx

from app.domain.operations.models import (
    ClaimedDeliveryOutboxItem,
    OperationsInvariantError,
    OutboxDeliveryReceipt,
)
from app.infrastructure.secrets_redaction import redact_mapping

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class OutboxWebhookDestination:
    """Dedupe-aware webhook adapter for durable operations alerts."""

    def __init__(
        self,
        webhook_url: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_sec: float = 5.0,
    ) -> None:
        if not webhook_url.strip():
            raise OperationsInvariantError("outbox_webhook_url_is_required")
        self.webhook_url = webhook_url
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=timeout_sec)

    async def deliver_outbox_item(
        self,
        item: ClaimedDeliveryOutboxItem,
        *,
        dedupe_key: str,
    ) -> OutboxDeliveryReceipt:
        if dedupe_key != item.dedupe_key:
            raise OperationsInvariantError("outbox_receiver_dedupe_key_mismatch")
        response = await self.client.post(
            self.webhook_url,
            headers={"Idempotency-Key": dedupe_key},
            json={
                "schema_version": item.payload_version,
                "dedupe_key": dedupe_key,
                "event_type": item.event_type,
                "aggregate_type": item.aggregate_type,
                "aggregate_id": item.aggregate_id,
                "payload": redact_mapping(cast(dict[str, object], item.payload)),
            },
        )
        response.raise_for_status()
        if item.destination_type == "audit_archive":
            return _audit_archive_receipt(item, response)
        return _dedupe_receipt(dedupe_key, response)

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()


def _audit_archive_receipt(
    item: ClaimedDeliveryOutboxItem,
    response: httpx.Response,
) -> OutboxDeliveryReceipt:
    expected_hash = item.payload.get("event_hash")
    if not isinstance(expected_hash, str) or _SHA256_RE.fullmatch(expected_hash) is None:
        raise OperationsInvariantError("audit_archive_payload_event_hash_is_invalid")
    try:
        body = response.json()
    except ValueError as exc:
        raise OperationsInvariantError("audit_archive_receipt_is_missing") from exc
    if not isinstance(body, dict) or set(body) != {
        "immutable_receipt_id",
        "archived_event_hash",
    }:
        raise OperationsInvariantError("audit_archive_receipt_shape_is_invalid")
    receipt_id = body.get("immutable_receipt_id")
    archived_hash = body.get("archived_event_hash")
    if not isinstance(receipt_id, str) or not receipt_id.strip():
        raise OperationsInvariantError("audit_archive_receipt_id_is_invalid")
    if (
        not isinstance(archived_hash, str)
        or _SHA256_RE.fullmatch(archived_hash) is None
        or archived_hash != expected_hash
    ):
        raise OperationsInvariantError("audit_archive_event_hash_mismatch")
    return OutboxDeliveryReceipt(
        external_receipt_id=receipt_id,
        external_receipt_sha256=archived_hash,
    )


def _dedupe_receipt(
    dedupe_key: str,
    response: httpx.Response,
) -> OutboxDeliveryReceipt:
    try:
        body = response.json()
    except ValueError as exc:
        raise OperationsInvariantError("outbox_receiver_receipt_is_missing") from exc
    if not isinstance(body, dict) or set(body) != {
        "immutable_receipt_id",
        "accepted_dedupe_key",
        "receipt_sha256",
    }:
        raise OperationsInvariantError("outbox_receiver_receipt_shape_is_invalid")
    receipt_id = body.get("immutable_receipt_id")
    accepted_dedupe_key = body.get("accepted_dedupe_key")
    receipt_sha256 = body.get("receipt_sha256")
    if not isinstance(receipt_id, str) or not receipt_id.strip():
        raise OperationsInvariantError("outbox_receiver_receipt_id_is_invalid")
    if accepted_dedupe_key != dedupe_key:
        raise OperationsInvariantError("outbox_receiver_dedupe_evidence_mismatch")
    expected_sha256 = hashlib.sha256(
        _receipt_material(dedupe_key, receipt_id)
    ).hexdigest()
    if (
        not isinstance(receipt_sha256, str)
        or _SHA256_RE.fullmatch(receipt_sha256) is None
        or receipt_sha256 != expected_sha256
    ):
        raise OperationsInvariantError("outbox_receiver_receipt_hash_mismatch")
    return OutboxDeliveryReceipt(
        external_receipt_id=receipt_id,
        external_receipt_sha256=receipt_sha256,
    )


def _receipt_material(dedupe_key: str, receipt_id: str) -> bytes:
    return json.dumps(
        {
            "accepted_dedupe_key": dedupe_key,
            "immutable_receipt_id": receipt_id,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class UnavailableOutboxDestination:
    """Records retryable failure when no outbound alert destination is configured."""

    async def deliver_outbox_item(
        self,
        item: ClaimedDeliveryOutboxItem,
        *,
        dedupe_key: str,
    ) -> OutboxDeliveryReceipt:
        del item, dedupe_key
        raise OperationsInvariantError("outbox_destination_is_not_configured")
