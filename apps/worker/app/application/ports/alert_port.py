from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class AlertDeliveryResult:
    delivered: bool
    latency_ms: int
    error: str | None = None


class AlertNotifierPort(Protocol):
    async def notify_engine_event(
        self,
        level: str,
        component: str,
        message: str,
        details: dict[str, object],
    ) -> AlertDeliveryResult:
        ...
