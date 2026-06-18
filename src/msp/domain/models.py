from __future__ import annotations

from dataclasses import dataclass

from msp.domain.enums import OrderSide


@dataclass(frozen=True)
class BrokerOrderCommand:
    account_id: str
    order_intent_id: str
    broker_client_order_id: str
    symbol: str
    side: OrderSide
    quantity: float
    limit_price: float
    currency: str


@dataclass(frozen=True)
class BrokerOrderResult:
    broker_order_id: str
    status: str
    filled_quantity: float
    average_price: float
    raw_response: dict
