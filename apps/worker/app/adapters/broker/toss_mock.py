from __future__ import annotations

from datetime import datetime

from app.application.ports.broker_port import (
    BrokerCancelOrderResult,
    BrokerOrderRequest,
    BrokerOrderResult,
    BrokerOrderStatusResult,
)
from app.domain.common.errors import ProviderUnavailableError
from app.domain.trading.entities import AccountState


class TossMock:
    async def provider_health(self) -> bool:
        return True

    async def place_order(self, request: BrokerOrderRequest) -> BrokerOrderResult:
        raise ProviderUnavailableError("toss", "mock_broker_refuses_live_order")

    async def get_order_status(self, provider_order_id: str) -> BrokerOrderStatusResult:
        raise ProviderUnavailableError("toss", "mock_broker_refuses_live_order_status")

    async def cancel_order(self, provider_order_id: str) -> BrokerCancelOrderResult:
        raise ProviderUnavailableError("toss", "mock_broker_refuses_live_order_cancel")

    async def get_account_state(self, now: datetime) -> AccountState:
        raise ProviderUnavailableError("toss", "mock_broker_refuses_live_account_state")
