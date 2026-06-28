from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from uuid import uuid4

import httpx

from app.adapters.alerts.webhook_alert_notifier import WebhookAlertNotifier
from app.adapters.persistence.sql_repository import InMemoryRepository
from app.application.ports.alert_port import AlertDeliveryResult
from app.application.ports.broker_port import (
    BrokerCancelOrderResult,
    BrokerOrderRequest,
    BrokerOrderResult,
    BrokerOrderStatusResult,
)
from app.application.services.order_reconciliation_service import (
    OrderReconciliationService,
)
from app.config import load_settings
from app.domain.common.errors import ProviderTimeoutError
from app.domain.common.time import now_utc
from app.domain.trading.entities import AccountState, BotSettings, Order


class DrillTimeoutBroker:
    async def provider_health(self) -> bool:
        return True

    async def place_order(self, request: BrokerOrderRequest) -> BrokerOrderResult:
        return BrokerOrderResult(
            provider_order_id="drill-unused",
            status="sent",
            raw_summary={"symbol": request.symbol},
        )

    async def get_order_status(self, provider_order_id: str) -> BrokerOrderStatusResult:
        raise ProviderTimeoutError("toss", "drill_provider_timeout")

    async def cancel_order(self, provider_order_id: str) -> BrokerCancelOrderResult:
        return BrokerCancelOrderResult(
            original_provider_order_id=provider_order_id,
            cancel_provider_order_id="drill-unused-cancel",
            raw_summary={"provider_order_id": provider_order_id},
        )

    async def get_account_state(self, now: datetime) -> AccountState:
        return AccountState(
            synced=True,
            cash_krw=1_000_000,
            equity_krw=1_000_000,
            daily_loss_pct=0.0,
            daily_order_count=0,
            synced_at=now,
        )


async def main() -> None:
    settings = load_settings()
    requests: list[httpx.Request] = []
    if settings.alert_webhook_url is None:
        http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(_mock_alert_handler(requests))
        )
        webhook_notifier = WebhookAlertNotifier(
            "https://alerts.example.test/live-drill",
            client=http_client,
            timeout_sec=settings.alert_webhook_timeout_sec,
        )
    else:
        http_client = None
        webhook_notifier = WebhookAlertNotifier(
            settings.alert_webhook_url.get_secret_value(),
            timeout_sec=settings.alert_webhook_timeout_sec,
        )
    notifier = DrillAlertNotifier(webhook_notifier)
    repository = InMemoryRepository(BotSettings(mode="live", live_order_allowed=False))
    repository.orders.append(
        Order(
            id=uuid4(),
            decision_id=uuid4(),
            symbol="005930",
            action="buy",
            mode="live",
            status="unknown_requires_manual_check",
            amount_krw=75_000,
            idempotency_key="live-alert-drill",
            reason="drill_unknown_status",
            created_at=now_utc(),
            provider_order_id="drill-provider-order",
        )
    )
    service = OrderReconciliationService(
        DrillTimeoutBroker(),
        repository,
        alert_notifier=notifier,
    )
    try:
        await service.reconcile_live_orders()
        event = repository.engine_events[-1]
        if (
            event["level"] != "critical"
            or event["message"] != "live_order_manual_check_still_unknown"
        ):
            raise RuntimeError(f"live_alert_drill_failed: {event}")
        await notifier.notify_engine_event(
            "critical",
            "paper_health",
            "worker_heartbeat_stale",
            {"heartbeat_age_seconds": 999, "drill": True},
        )
        await notifier.notify_engine_event(
            "critical",
            "live_account",
            "live_system_order_count_sync_failed",
            {"reason": "drill_order_count_timeout", "drill": True},
        )
        await notifier.notify_engine_event(
            "critical",
            "live_account",
            "live_external_order_history_scope_not_accepted",
            {
                "required_env": "LIVE_SYSTEM_ORDER_COUNT_SCOPE_ACCEPTED",
                "accepted": False,
                "daily_order_count_scope": "system_created_live_orders_only",
                "drill": True,
            },
        )
    finally:
        await notifier.aclose()
    delivered = sum(1 for item in notifier.deliveries if item.delivered)
    max_latency_ms = max((item.latency_ms for item in notifier.deliveries), default=0)
    if max_latency_ms > settings.alert_drill_max_latency_ms:
        raise RuntimeError(
            f"live_alert_drill_slow: max_latency_ms={max_latency_ms} "
            f"limit={settings.alert_drill_max_latency_ms}"
        )
    if delivered < 4:
        raise RuntimeError(f"live_alert_drill_delivery_count_failed: delivered={delivered}")
    print(
        "FINAL=PASS live_external_alert_drill "
        f"delivered={delivered} max_latency_ms={max_latency_ms}"
    )


class DrillAlertNotifier:
    def __init__(self, notifier: WebhookAlertNotifier) -> None:
        self.notifier = notifier
        self.deliveries: list[AlertDeliveryResult] = []

    async def notify_engine_event(
        self,
        level: str,
        component: str,
        message: str,
        details: dict[str, object],
    ) -> AlertDeliveryResult:
        result = await self.notifier.notify_engine_event(level, component, message, details)
        self.deliveries.append(result)
        return result

    async def aclose(self) -> None:
        await self.notifier.aclose()


def _mock_alert_handler(
    requests: list[httpx.Request],
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204, request=request)

    return handler


if __name__ == "__main__":
    asyncio.run(main())
