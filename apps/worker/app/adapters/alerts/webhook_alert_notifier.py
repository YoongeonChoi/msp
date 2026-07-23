from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from time import perf_counter

import httpx

from app.application.ports.alert_port import AlertDeliveryResult
from app.domain.common.time import now_utc
from app.infrastructure.authenticated_webhook import (
    AuthenticatedWebhookError,
    AuthenticatedWebhookTransport,
    ReceiverAckKeyRing,
)
from app.infrastructure.secrets_redaction import redact_mapping


class WebhookAlertNotifier:
    def __init__(
        self,
        webhook_url: str,
        *,
        key_ring: ReceiverAckKeyRing,
        client: httpx.AsyncClient | None = None,
        timeout_sec: float = 5.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._transport = AuthenticatedWebhookTransport(
            webhook_url,
            key_ring=key_ring,
            client=client,
            timeout_sec=timeout_sec,
            clock=clock,
        )

    async def notify_engine_event(
        self,
        level: str,
        component: str,
        message: str,
        details: dict[str, object],
    ) -> AlertDeliveryResult:
        started = perf_counter()
        payload = {
            "schema_version": 1,
            "source": "kr-auto-trading-lab",
            "level": level,
            "component": component,
            "message": message,
            "details": redact_mapping(details),
            "sent_at": now_utc().isoformat(),
        }
        try:
            await self._transport.post_json(
                context="legacy_alert",
                payload=payload,
                binding={
                    "level": level,
                    "component": component,
                    "message": message,
                },
            )
        except AuthenticatedWebhookError:
            return AlertDeliveryResult(
                delivered=False,
                latency_ms=_elapsed_ms(started),
                error="receiver_acknowledgement_failed",
            )
        return AlertDeliveryResult(delivered=True, latency_ms=_elapsed_ms(started))

    async def aclose(self) -> None:
        await self._transport.close()


def _elapsed_ms(started: float) -> int:
    return max(0, round((perf_counter() - started) * 1000))
