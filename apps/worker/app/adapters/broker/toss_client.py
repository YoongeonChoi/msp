from __future__ import annotations

from app.application.ports.broker_port import BrokerOrderRequest, BrokerOrderResult
from app.domain.common.errors import ProviderUnavailableError


class TossClient:
    async def provider_health(self) -> bool:
        return False

    async def place_order(self, request: BrokerOrderRequest) -> BrokerOrderResult:
        raise ProviderUnavailableError("toss", "toss_live_order_endpoint_not_implemented")

