from __future__ import annotations

import httpx

from app.adapters.alerts.webhook_alert_notifier import WebhookAlertNotifier


async def test_webhook_alert_notifier_posts_redacted_engine_event_payload() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204, request=request)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = WebhookAlertNotifier(
        "https://alerts.example.test/live",
        client=http_client,
    )

    result = await notifier.notify_engine_event(
        "critical",
        "live_account",
        "live_system_order_count_sync_failed",
        {
            "reason": "RuntimeError",
            "SUPABASE_SECRET_KEY": "abcdef123456",
            "nested": {"authorization": "Bearer abcdef123456"},
            "attempts": [{"refresh_token": "nested-refresh-token"}],
        },
    )

    assert result.delivered is True
    assert result.error is None
    assert len(requests) == 1
    payload = requests[0].content.decode()
    assert "live_system_order_count_sync_failed" in payload
    assert "RuntimeError" in payload
    assert "abcdef123456" not in payload
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
        client=http_client,
    )

    result = await notifier.notify_engine_event(
        "critical",
        "live_reconciliation",
        "live_order_manual_check_still_unknown",
        {"token": "secret-token-value"},
    )

    assert result.delivered is False
    assert result.error == "HTTPStatusError"
    await http_client.aclose()
