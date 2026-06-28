from __future__ import annotations

from time import perf_counter

import httpx

from app.application.ports.alert_port import AlertDeliveryResult
from app.domain.common.time import now_utc
from app.infrastructure.secrets_redaction import redact_mapping


class WebhookAlertNotifier:
    def __init__(
        self,
        webhook_url: str,
        client: httpx.AsyncClient | None = None,
        timeout_sec: float = 5.0,
    ) -> None:
        self.webhook_url = webhook_url
        self.client = client or httpx.AsyncClient(timeout=timeout_sec)

    async def notify_engine_event(
        self,
        level: str,
        component: str,
        message: str,
        details: dict[str, object],
    ) -> AlertDeliveryResult:
        started = perf_counter()
        try:
            response = await self.client.post(
                self.webhook_url,
                json={
                    "schema_version": 1,
                    "source": "kr-auto-trading-lab",
                    "level": level,
                    "component": component,
                    "message": message,
                    "details": redact_mapping(details),
                    "sent_at": now_utc().isoformat(),
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            return AlertDeliveryResult(
                delivered=False,
                latency_ms=_elapsed_ms(started),
                error=type(exc).__name__,
            )
        return AlertDeliveryResult(delivered=True, latency_ms=_elapsed_ms(started))

    async def aclose(self) -> None:
        await self.client.aclose()


def _elapsed_ms(started: float) -> int:
    return max(0, round((perf_counter() - started) * 1000))
