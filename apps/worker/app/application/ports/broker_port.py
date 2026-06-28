from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from app.domain.trading.entities import AccountState

BrokerOrderPlacementStatus = Literal["sent", "filled", "rejected", "unknown_requires_manual_check"]
BrokerOrderReconciliationStatus = Literal[
    "sent",
    "partial_filled",
    "filled",
    "canceled",
    "rejected",
    "unknown_requires_manual_check",
]


@dataclass(frozen=True, slots=True)
class BrokerOrderRequest:
    symbol: str
    side: Literal["buy", "sell"]
    amount_krw: int
    idempotency_key: str
    quantity: int
    limit_price_krw: int


@dataclass(frozen=True, slots=True)
class BrokerOrderResult:
    provider_order_id: str | None
    status: BrokerOrderPlacementStatus
    raw_summary: dict[str, object]


@dataclass(frozen=True, slots=True)
class BrokerOrderStatusResult:
    provider_order_id: str
    status: BrokerOrderReconciliationStatus
    raw_summary: dict[str, object]
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class BrokerCancelOrderResult:
    original_provider_order_id: str
    cancel_provider_order_id: str
    raw_summary: dict[str, object]


class BrokerPort(Protocol):
    async def place_order(self, request: BrokerOrderRequest) -> BrokerOrderResult:
        ...

    async def get_order_status(self, provider_order_id: str) -> BrokerOrderStatusResult:
        ...

    async def cancel_order(self, provider_order_id: str) -> BrokerCancelOrderResult:
        ...

    async def get_account_state(self, now: datetime) -> AccountState:
        ...

    async def provider_health(self) -> bool:
        ...
