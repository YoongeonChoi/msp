from __future__ import annotations

import json

import httpx

from app.adapters.alerts.webhook_alert_notifier import WebhookAlertNotifier
from app.tests.receiver_ack_fixture import (
    TEST_ACK_NOW,
    receiver_key_ring_fixture,
    signed_receiver_response,
)


async def test_webhook_alert_notifier_posts_redacted_engine_event_payload() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content)
        return signed_receiver_response(
            request,
            context="legacy_alert",
            binding={
                "level": payload["level"],
                "component": payload["component"],
                "message": payload["message"],
            },
            status_code=204,
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = WebhookAlertNotifier(
        "https://alerts.example.test/live",
        key_ring=receiver_key_ring_fixture(),
        client=http_client,
        clock=lambda: TEST_ACK_NOW,
    )

    result = await notifier.notify_engine_event(
        "critical",
        "live_account",
        "live_system_order_count_sync_failed",
        {
            "reason": "RuntimeError",
            "SUPABASE_SECRET_KEY": "not-a-" + "secret-fixture",
            "nested": {"authorization": "Bearer not-a-secret-fixture"},
            "attempts": [{"refresh_token": "nested-refresh-token"}],
        },
    )

    assert result.delivered is True
    assert result.error is None
    assert len(requests) == 1
    payload = requests[0].content.decode()
    assert "live_system_order_count_sync_failed" in payload
    assert "RuntimeError" in payload
    assert "not-a-secret-fixture" not in payload
    assert "abcdef" not in payload
    assert "nested-refresh-token" not in payload
    assert "<redacted>" in payload
    await http_client.aclose()


async def test_webhook_alert_notifier_reports_delivery_failure_without_secret_leak() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, request=request)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = WebhookAlertNotifier(
        "https://alerts.example.test/live",
        key_ring=receiver_key_ring_fixture(),
        client=http_client,
        clock=lambda: TEST_ACK_NOW,
    )

    result = await notifier.notify_engine_event(
        "critical",
        "live_reconciliation",
        "live_order_manual_check_still_unknown",
        {"token": "secret-token-value"},
    )

    assert result.delivered is False
    assert result.error == "receiver_acknowledgement_failed"
    await http_client.aclose()


async def test_webhook_alert_notifier_rejects_unsigned_2xx() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204, request=request)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = WebhookAlertNotifier(
        "https://alerts.example.test/live",
        key_ring=receiver_key_ring_fixture(),
        client=http_client,
        clock=lambda: TEST_ACK_NOW,
    )

    result = await notifier.notify_engine_event(
        "critical",
        "live_account",
        "live_system_order_count_sync_failed",
        {},
    )

    assert result.delivered is False
    assert result.error == "receiver_acknowledgement_failed"
    await http_client.aclose()
