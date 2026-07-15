from datetime import UTC, datetime

import pytest

from app.adapters.broker.contract_test_broker import ContractTestBroker
from app.adapters.persistence.sql_repository import InMemoryRepository
from app.application.ports.broker_port import (
    BrokerCancelOrderResult,
    BrokerOrderRequest,
    BrokerOrderResult,
    BrokerOrderStatusResult,
)
from app.application.services.live_order_cancellation_service import (
    LiveOrderCancellationService,
)
from app.domain.common.errors import KnownFailClosedError
from app.domain.trading.entities import AccountState, BotSettings


@pytest.mark.parametrize("broker", [ContractTestBroker(), None])
async def test_legacy_live_cancel_is_unconditionally_quarantined(broker: object) -> None:
    repository = InMemoryRepository(BotSettings())
    selected = broker or CountingProductionBroker()

    with pytest.raises(
        KnownFailClosedError,
        match="legacy_live_order_cancel_is_quarantined",
    ):
        await LiveOrderCancellationService(selected, repository).cancel_live_order(
            "00000000-0000-0000-0000-000000000001"
        )

    assert getattr(selected, "cancel_order_calls", 0) == 0
    assert getattr(selected, "get_order_status_calls", 0) == 0
    assert repository.orders == []
    assert repository.engine_events == []


class CountingProductionBroker:
    execution_environment = "production"
    network_enabled = True
    production_order_capable = True

    def __init__(self) -> None:
        self.cancel_order_calls = 0
        self.get_order_status_calls = 0

    async def provider_health(self) -> bool:
        return True

    async def place_order(self, request: BrokerOrderRequest) -> BrokerOrderResult:
        del request
        raise AssertionError("order network call is forbidden")

    async def get_order_status(self, provider_order_id: str) -> BrokerOrderStatusResult:
        del provider_order_id
        self.get_order_status_calls += 1
        raise AssertionError("order network call is forbidden")

    async def cancel_order(self, provider_order_id: str) -> BrokerCancelOrderResult:
        del provider_order_id
        self.cancel_order_calls += 1
        raise AssertionError("order network call is forbidden")

    async def get_account_state(self, now: datetime) -> AccountState:
        return AccountState(
            synced=True,
            cash_krw=1,
            equity_krw=1,
            daily_loss_pct=0,
            daily_order_count=0,
            synced_at=now.astimezone(UTC),
        )
