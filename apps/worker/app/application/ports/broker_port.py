from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True, slots=True)
class BrokerOrderRequest:
    symbol: str
    side: Literal["buy", "sell"]
    amount_krw: int
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class BrokerOrderResult:
    provider_order_id: str | None
    status: Literal["sent", "filled", "rejected", "unknown_requires_manual_check"]
    raw_summary: dict[str, object]


class BrokerPort(Protocol):
    async def place_order(self, request: BrokerOrderRequest) -> BrokerOrderResult:
        ...

    async def provider_health(self) -> bool:
        ...

