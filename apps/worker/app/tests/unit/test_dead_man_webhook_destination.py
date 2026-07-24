from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Literal

import httpx
import pytest

from app.adapters.alerts.dead_man_webhook_destination import (
    DeadManWebhookDestination,
)
from app.domain.operations.models import OperationsInvariantError
from app.tests.receiver_ack_fixture import (
    TEST_ACK_NOW,
    json_response_body,
    receiver_key_ring_fixture,
    signed_receiver_response,
)

EPISODE_ONE = "00000000-0000-4000-8000-000000000001"
EPISODE_TWO = "00000000-0000-4000-8000-000000000002"


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
        body = json_response_body(
            {
                "immutable_receipt_id": receipt_id,
                "accepted_dedupe_key": dedupe_key,
                "receipt_sha256": receipt_sha256,
            }
        )
        payload = json.loads(request.content)
        return signed_receiver_response(
            request,
            context="dead_man",
            binding={
                "episode_id": payload["episode_id"],
                "event": payload["event_type"].removeprefix("dead_man_monitor_"),
                "dedupe_key": dedupe_key,
            },
            status_code=200,
            body=body,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        destination = DeadManWebhookDestination(
            "https://alerts.example.invalid",
            key_ring=receiver_key_ring_fixture(),
            client=client,
            clock=lambda: TEST_ACK_NOW,
        )
        await destination.deliver_dead_man_alert(
            account_id="paper-primary",
            episode_id=EPISODE_ONE,
            event="unhealthy",
            reason_codes=("worker_heartbeat_stale",),
            observed_at=datetime(2026, 7, 14, 9, 0, tzinfo=UTC),
        )

    assert len(seen) == 1
    payload = json.loads(seen[0].content)
    assert payload["schema_version"] == 2
    assert payload["event_type"] == "dead_man_monitor_unhealthy"
    assert payload["episode_id"] == EPISODE_ONE
    assert payload["payload"]["severity"] == "critical"
    assert seen[0].headers["Idempotency-Key"].startswith("dead-man-v2:")


async def test_dead_man_dedupe_identity_tracks_episode_and_reason_transitions() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        dedupe_key = request.headers["Idempotency-Key"]
        receipt_id = f"receipt-{len(seen)}"
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
        body = json_response_body(
            {
                "immutable_receipt_id": receipt_id,
                "accepted_dedupe_key": dedupe_key,
                "receipt_sha256": receipt_sha256,
            }
        )
        payload = json.loads(request.content)
        return signed_receiver_response(
            request,
            context="dead_man",
            binding={
                "episode_id": payload["episode_id"],
                "event": payload["event_type"].removeprefix("dead_man_monitor_"),
                "dedupe_key": dedupe_key,
            },
            status_code=200,
            body=body,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        destination = DeadManWebhookDestination(
            "https://alerts.example.invalid",
            key_ring=receiver_key_ring_fixture(),
            client=client,
            clock=lambda: TEST_ACK_NOW,
        )
        cases: tuple[tuple[str, Literal["unhealthy", "recovered"], tuple[str, ...]], ...] = (
            (EPISODE_ONE, "unhealthy", ("worker_heartbeat_stale",)),
            (EPISODE_ONE, "unhealthy", ("worker_heartbeat_stale",)),
            (
                EPISODE_ONE,
                "unhealthy",
                ("worker_heartbeat_stale", "worker_lease_expired"),
            ),
            (
                EPISODE_ONE,
                "unhealthy",
                ("worker_lease_expired", "worker_heartbeat_stale"),
            ),
            (EPISODE_ONE, "recovered", ("worker_heartbeat_stale",)),
            (EPISODE_TWO, "unhealthy", ("worker_heartbeat_stale",)),
        )
        for episode_id, event, reasons in cases:
            await destination.deliver_dead_man_alert(
                account_id="paper-primary",
                episode_id=episode_id,
                event=event,
                reason_codes=reasons,
                observed_at=datetime(2026, 7, 14, 9, 0, tzinfo=UTC),
            )

    keys = [request.headers["Idempotency-Key"] for request in seen]
    assert keys[0] == keys[1]
    assert keys[2] != keys[1]
    assert keys[3] == keys[2]
    assert seen[3].content == seen[2].content
    assert keys[4] != keys[0]
    assert keys[5] != keys[0]
    assert [json.loads(request.content)["episode_id"] for request in seen] == [
        EPISODE_ONE,
        EPISODE_ONE,
        EPISODE_ONE,
        EPISODE_ONE,
        EPISODE_ONE,
        EPISODE_TWO,
    ]


async def test_dead_man_dedupe_changes_with_observation_but_retry_bytes_stay_exact() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        dedupe_key = request.headers["Idempotency-Key"]
        receipt_id = "receipt-observation"
        body = json_response_body(
            {
                "immutable_receipt_id": receipt_id,
                "accepted_dedupe_key": dedupe_key,
                "receipt_sha256": hashlib.sha256(
                    json.dumps(
                        {
                            "accepted_dedupe_key": dedupe_key,
                            "immutable_receipt_id": receipt_id,
                        },
                        ensure_ascii=True,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest(),
            }
        )
        payload = json.loads(request.content)
        return signed_receiver_response(
            request,
            context="dead_man",
            binding={
                "episode_id": payload["episode_id"],
                "event": "unhealthy",
                "dedupe_key": dedupe_key,
            },
            status_code=200,
            body=body,
        )

    first_observation = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        destination = DeadManWebhookDestination(
            "https://alerts.example.invalid",
            key_ring=receiver_key_ring_fixture(),
            client=client,
            clock=lambda: TEST_ACK_NOW,
        )
        for observed_at in (
            first_observation,
            first_observation,
            first_observation + timedelta(seconds=10),
        ):
            await destination.deliver_dead_man_alert(
                account_id="paper-primary",
                episode_id=EPISODE_ONE,
                event="unhealthy",
                reason_codes=("worker_heartbeat_stale",),
                observed_at=observed_at,
            )

    assert requests[0].content == requests[1].content
    assert requests[0].headers["Idempotency-Key"] == requests[1].headers["Idempotency-Key"]
    assert requests[2].content != requests[1].content
    assert requests[2].headers["Idempotency-Key"] != requests[1].headers["Idempotency-Key"]


async def test_dead_man_destination_rejects_unsigned_receipt() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        dedupe_key = request.headers["Idempotency-Key"]
        receipt_id = "receipt-1"
        return httpx.Response(
            200,
            json={
                "immutable_receipt_id": receipt_id,
                "accepted_dedupe_key": dedupe_key,
                "receipt_sha256": hashlib.sha256(
                    json.dumps(
                        {
                            "accepted_dedupe_key": dedupe_key,
                            "immutable_receipt_id": receipt_id,
                        },
                        ensure_ascii=True,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest(),
            },
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        destination = DeadManWebhookDestination(
            "https://alerts.example.invalid",
            key_ring=receiver_key_ring_fixture(),
            client=client,
            clock=lambda: TEST_ACK_NOW,
        )
        with pytest.raises(
            OperationsInvariantError,
            match="dead_man_receiver_authentication_failed",
        ):
            await destination.deliver_dead_man_alert(
                account_id="paper-primary",
                episode_id=EPISODE_ONE,
                event="unhealthy",
                reason_codes=("worker_heartbeat_stale",),
                observed_at=datetime(2026, 7, 14, 9, 0, tzinfo=UTC),
            )
