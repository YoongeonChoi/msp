from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import httpx

from app.adapters.alerts.dead_man_webhook_destination import (
    DeadManWebhookDestination,
)


async def test_dead_man_destination_requires_dedupe_receipt() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        dedupe_key = request.headers["Idempotency-Key"]
        receipt_id = "receipt-1"
        receipt_sha256 = hashlib.sha256(
            json.dumps(
                {
                    "accepted_dedupe_key": dedupe_key,
                    "immutable_receipt_id": receipt_id,
                },
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        return httpx.Response(
            200,
            json={
                "immutable_receipt_id": receipt_id,
                "accepted_dedupe_key": dedupe_key,
                "receipt_sha256": receipt_sha256,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        destination = DeadManWebhookDestination(
            "https://alerts.example.invalid",
            client=client,
        )
        await destination.deliver_dead_man_alert(
            account_id="paper-primary",
            event="unhealthy",
            reason_codes=("worker_heartbeat_stale",),
            observed_at=datetime(2026, 7, 14, 9, 0, tzinfo=UTC),
        )

    assert len(seen) == 1
    payload = json.loads(seen[0].content)
    assert payload["event_type"] == "dead_man_monitor_unhealthy"
    assert payload["payload"]["severity"] == "critical"
    assert seen[0].headers["Idempotency-Key"].startswith("dead-man-v1:")
