from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from time import perf_counter
from typing import cast
from uuid import uuid4

import httpx

from app.adapters.alerts.webhook_alert_notifier import WebhookAlertNotifier
from app.adapters.persistence.sql_repository import InMemoryRepository
from app.application.services.order_reconciliation_service import (
    OrderReconciliationService,
)
from app.config import load_settings
from app.domain.common.time import now_utc
from app.domain.trading.entities import BotSettings, Order
from app.tools.run_live_alert_drill_once import DrillAlertNotifier, DrillTimeoutBroker

AckReader = Callable[[str, float], Awaitable[bool]]
_DRILL_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


@dataclass(frozen=True, slots=True)
class IncidentResponseDrillResult:
    delivered: int
    max_latency_ms: int
    ack_required: bool
    acknowledged: bool
    ack_latency_ms: int | None
    drill_id: str


async def run_incident_response_drill(
    *,
    require_ack: bool,
    ack_timeout_sec: float = 300.0,
    ack_reader: AckReader | None = None,
    drill_id: str | None = None,
) -> IncidentResponseDrillResult:
    if ack_timeout_sec <= 0:
        raise ValueError("ack_timeout_sec_must_be_positive")
    settings = load_settings()
    active_drill_id = _normalize_drill_id(drill_id)
    if settings.alert_webhook_url is None:
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(_mock_alert_handler))
        webhook_notifier = WebhookAlertNotifier(
            "https://alerts.example.test/live-incident-drill",
            client=http_client,
            timeout_sec=settings.alert_webhook_timeout_sec,
        )
    else:
        webhook_notifier = WebhookAlertNotifier(
            settings.alert_webhook_url.get_secret_value(),
            timeout_sec=settings.alert_webhook_timeout_sec,
        )
    notifier = DrillAlertNotifier(webhook_notifier)
    try:
        await _send_incident_events(
            notifier,
            drill_id=active_drill_id,
            ack_required=require_ack,
        )
        delivered = sum(1 for item in notifier.deliveries if item.delivered)
        max_latency_ms = max((item.latency_ms for item in notifier.deliveries), default=0)
        if max_latency_ms > settings.alert_drill_max_latency_ms:
            raise RuntimeError(
                f"live_incident_response_drill_slow: max_latency_ms={max_latency_ms} "
                f"limit={settings.alert_drill_max_latency_ms}"
            )
        if delivered < 4:
            raise RuntimeError(
                f"live_incident_response_delivery_count_failed: delivered={delivered}"
            )
        acknowledged = False
        ack_latency_ms: int | None = None
        if require_ack:
            reader = ack_reader or _read_stdin_ack
            started = perf_counter()
            acknowledged = await reader(active_drill_id, ack_timeout_sec)
            ack_latency_ms = _elapsed_ms(started)
        return IncidentResponseDrillResult(
            delivered=delivered,
            max_latency_ms=max_latency_ms,
            ack_required=require_ack,
            acknowledged=acknowledged,
            ack_latency_ms=ack_latency_ms,
            drill_id=active_drill_id,
        )
    finally:
        await notifier.aclose()


async def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Send live incident alerts and optionally require human ACK."
    )
    parser.add_argument(
        "--require-ack",
        action="store_true",
        help="Wait for an operator to type ACK <drill_id> on stdin.",
    )
    parser.add_argument(
        "--ack-timeout-sec",
        type=float,
        default=300.0,
        help="Maximum seconds to wait for the operator ACK.",
    )
    parser.add_argument(
        "--drill-id",
        default=None,
        help="Optional deterministic drill id for controlled staging drills.",
    )
    args = parser.parse_args(argv)
    if args.ack_timeout_sec <= 0:
        parser.error("--ack-timeout-sec must be positive")

    result = await run_incident_response_drill(
        require_ack=args.require_ack,
        ack_timeout_sec=args.ack_timeout_sec,
        drill_id=args.drill_id,
    )
    if not result.ack_required:
        print(
            "FINAL=PASS live_incident_delivery_drill "
            f"delivered={result.delivered} max_latency_ms={result.max_latency_ms} "
            "ack_required=false"
        )
        return 0
    if result.acknowledged:
        print(
            "FINAL=PASS live_incident_response_drill "
            f"delivered={result.delivered} max_latency_ms={result.max_latency_ms} "
            f"acknowledged=true ack_latency_ms={result.ack_latency_ms} "
            f"drill_id={result.drill_id}"
        )
        return 0
    print(
        "FINAL=FAIL live_incident_response_drill "
        f"delivered={result.delivered} max_latency_ms={result.max_latency_ms} "
        f"acknowledged=false drill_id={result.drill_id}"
    )
    return 1


async def _send_incident_events(
    notifier: DrillAlertNotifier,
    *,
    drill_id: str,
    ack_required: bool,
) -> None:
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
            idempotency_key="live-incident-response-drill",
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
    await service.reconcile_live_orders()
    event = repository.engine_events[-1]
    if (
        event["level"] != "critical"
        or event["message"] != "live_order_manual_check_still_unknown"
    ):
        raise RuntimeError(f"live_incident_response_drill_failed: {event}")
    common_details = {
        "drill": True,
        "drill_id": drill_id,
        "ack_required": ack_required,
        "ack_phrase": f"ACK {drill_id}",
    }
    await notifier.notify_engine_event(
        "critical",
        "paper_health",
        "worker_heartbeat_stale",
        {"heartbeat_age_seconds": 999, **common_details},
    )
    await notifier.notify_engine_event(
        "critical",
        "live_account",
        "live_system_order_count_sync_failed",
        {"reason": "drill_order_count_timeout", **common_details},
    )
    await notifier.notify_engine_event(
        "critical",
        "live_account",
        "live_external_order_history_scope_not_accepted",
        {
            "required_env": "LIVE_SYSTEM_ORDER_COUNT_SCOPE_ACCEPTED",
            "accepted": False,
            "daily_order_count_scope": "system_created_live_orders_only",
            **common_details,
        },
    )


async def _read_stdin_ack(drill_id: str, timeout_sec: float) -> bool:
    expected = f"ACK {drill_id}"
    print(f"ACK_REQUIRED type exactly: {expected}", file=sys.stderr, flush=True)
    try:
        line = cast(
            str,
            await asyncio.wait_for(asyncio.to_thread(sys.stdin.readline), timeout_sec),
        )
    except TimeoutError:
        return False
    return line.strip() == expected


def _mock_alert_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(204, request=request)


def _elapsed_ms(started: float) -> int:
    return max(0, round((perf_counter() - started) * 1000))


def _normalize_drill_id(drill_id: str | None) -> str:
    active_drill_id = drill_id or str(uuid4())
    if not _DRILL_ID_RE.fullmatch(active_drill_id):
        raise ValueError("invalid_drill_id")
    return active_drill_id


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
