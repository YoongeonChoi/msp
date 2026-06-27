from __future__ import annotations

from app.application.ports.broker_port import BrokerOrderRequest, BrokerOrderResult
from app.domain.common.errors import ProviderUnavailableError


class TossMock:
    async def provider_health(self) -> bool:
        return True

    async def place_order(self, request: BrokerOrderRequest) -> BrokerOrderResult:
        raise ProviderUnavailableError("toss", "mock_broker_refuses_live_order")

