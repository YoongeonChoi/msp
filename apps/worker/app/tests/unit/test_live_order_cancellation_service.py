from datetime import datetime
from uuid import uuid4

from app.adapters.persistence.sql_repository import InMemoryRepository
from app.application.ports.broker_port import (
    BrokerCancelOrderResult,
    BrokerOrderReconciliationStatus,
    BrokerOrderRequest,
    BrokerOrderResult,
    BrokerOrderStatusResult,
)
from app.application.services.live_order_cancellation_service import (
    LiveOrderCancellationService,
)
from app.domain.common.errors import KnownFailClosedError, ProviderTimeoutError
from app.domain.common.time import now_utc
from app.domain.trading.entities import AccountState, BotSettings, Order, OrderStatus, TradingMode


class CancelBroker:
    def __init__(
        self,
        timeout: bool = False,
        status_timeout: bool = False,
        status_result: BrokerOrderReconciliationStatus = "canceled",
    ) -> None:
        self.timeout = timeout
        self.status_timeout = status_timeout
        self.status_result = status_result
        self.cancel_calls = 0
        self.status_calls = 0
        self.last_cancel_order_id: str | None = None
        self.last_status_order_id: str | None = None

    async def provider_health(self) -> bool:
        return True

    async def place_order(self, request: BrokerOrderRequest) -> BrokerOrderResult:
        return BrokerOrderResult(
            provider_order_id="unused",
            status="sent",
            raw_summary={"symbol": request.symbol},
        )

    async def get_order_status(self, provider_order_id: str) -> BrokerOrderStatusResult:
        self.status_calls += 1
        self.last_status_order_id = provider_order_id
        if self.status_timeout:
            raise ProviderTimeoutError("toss", "toss_read_timeout")
        return BrokerOrderStatusResult(
            provider_order_id=provider_order_id,
            status=self.status_result,
            raw_summary={
                "provider_order_id": provider_order_id,
                "status": self.status_result.upper(),
            },
        )

    async def cancel_order(self, provider_order_id: str) -> BrokerCancelOrderResult:
        self.cancel_calls += 1
        self.last_cancel_order_id = provider_order_id
        if self.timeout:
            raise ProviderTimeoutError("toss", "toss_write_timeout")
        return BrokerCancelOrderResult(
            original_provider_order_id=provider_order_id,
            cancel_provider_order_id="cancel-provider-order-1",
            raw_summary={
                "original_order_id": provider_order_id,
                "cancel_order_id": "cancel-provider-order-1",
            },
        )

    async def get_account_state(self, now: datetime) -> AccountState:
        return AccountState(
            synced=True,
            cash_krw=1_000_000,
            equity_krw=1_000_000,
            daily_loss_pct=0.0,
            daily_order_count=0,
            synced_at=now,
        )


async def test_cancel_live_order_marks_order_canceled_with_audit_payload() -> None:
    repository = InMemoryRepository(BotSettings())
    order = _order(mode="live", status="sent", provider_order_id="provider-order-1")
    repository.orders.append(order)
    broker = CancelBroker()
    service = LiveOrderCancellationService(broker, repository)

    result = await service.cancel_live_order(str(order.id))

    assert result.status == "canceled"
    assert broker.cancel_calls == 1
    assert broker.status_calls == 1
    assert broker.last_cancel_order_id == "provider-order-1"
    assert broker.last_status_order_id == "provider-order-1"
    assert repository.orders[0].status == "canceled"
    assert repository.orders[0].reason == "operator_cancel_confirmed"
    assert repository.orders[0].provider_payload_summary == {
        "previous_provider_payload_summary": None,
        "original_provider_order_id": "provider-order-1",
        "cancel": {
            "original_order_id": "provider-order-1",
            "cancel_order_id": "cancel-provider-order-1",
        },
        "confirmation": {
            "provider_order_id": "provider-order-1",
            "status": "CANCELED",
        },
    }
    assert repository.engine_events[-1]["message"] == "live_order_cancel_confirmed"


async def test_cancel_live_order_rejects_non_live_order_without_broker_call() -> None:
    repository = InMemoryRepository(BotSettings())
    order = _order(mode="paper", status="paper", provider_order_id=None)
    repository.orders.append(order)
    broker = CancelBroker()
    service = LiveOrderCancellationService(broker, repository)

    try:
        await service.cancel_live_order(str(order.id))
    except KnownFailClosedError as exc:
        assert exc.safe_message == "order_not_live"
    else:
        raise AssertionError("Expected KnownFailClosedError")

    assert broker.cancel_calls == 0
    assert broker.status_calls == 0
    assert repository.orders[0].status == "paper"
    assert repository.engine_events[-1]["message"] == "live_order_cancel_rejected"


async def test_cancel_live_order_timeout_requires_manual_check() -> None:
    repository = InMemoryRepository(BotSettings())
    order = _order(mode="live", status="sent", provider_order_id="provider-order-1")
    repository.orders.append(order)
    broker = CancelBroker(timeout=True)
    service = LiveOrderCancellationService(broker, repository)

    result = await service.cancel_live_order(str(order.id))

    assert result.status == "unknown_requires_manual_check"
    assert repository.orders[0].status == "unknown_requires_manual_check"
    assert repository.orders[0].reason == "toss_write_timeout"
    assert repository.orders[0].provider_payload_summary == {
        "previous_provider_payload_summary": None,
        "original_provider_order_id": "provider-order-1",
        "cancel_failure_reason": "toss_write_timeout",
    }
    assert repository.engine_events[-1]["message"] == (
        "live_order_cancel_unknown_requires_manual_check"
    )


async def test_cancel_live_order_requires_manual_check_when_confirmation_not_canceled() -> None:
    repository = InMemoryRepository(BotSettings())
    order = _order(mode="live", status="sent", provider_order_id="provider-order-1")
    repository.orders.append(order)
    broker = CancelBroker(status_result="sent")
    service = LiveOrderCancellationService(broker, repository)

    result = await service.cancel_live_order(str(order.id))

    assert result.status == "unknown_requires_manual_check"
    assert broker.cancel_calls == 1
    assert broker.status_calls == 1
    assert repository.orders[0].status == "unknown_requires_manual_check"
    assert repository.orders[0].reason == "cancel_confirmation_status_sent"
    assert repository.orders[0].provider_payload_summary == {
        "previous_provider_payload_summary": None,
        "original_provider_order_id": "provider-order-1",
        "cancel": {
            "original_order_id": "provider-order-1",
            "cancel_order_id": "cancel-provider-order-1",
        },
        "confirmation": {
            "provider_order_id": "provider-order-1",
            "status": "SENT",
        },
        "cancel_confirmation_failure_reason": "cancel_confirmation_status_sent",
    }
    assert repository.engine_events[-1]["message"] == "live_order_cancel_confirmation_failed"


async def test_cancel_live_order_confirmation_timeout_requires_manual_check() -> None:
    repository = InMemoryRepository(BotSettings())
    order = _order(mode="live", status="sent", provider_order_id="provider-order-1")
    repository.orders.append(order)
    broker = CancelBroker(status_timeout=True)
    service = LiveOrderCancellationService(broker, repository)

    result = await service.cancel_live_order(str(order.id))

    assert result.status == "unknown_requires_manual_check"
    assert broker.cancel_calls == 1
    assert broker.status_calls == 1
    assert repository.orders[0].status == "unknown_requires_manual_check"
    assert repository.orders[0].reason == "toss_read_timeout"
    assert repository.orders[0].provider_payload_summary == {
        "previous_provider_payload_summary": None,
        "original_provider_order_id": "provider-order-1",
        "cancel": {
            "original_order_id": "provider-order-1",
            "cancel_order_id": "cancel-provider-order-1",
        },
        "cancel_confirmation_failure_reason": "toss_read_timeout",
    }
    assert repository.engine_events[-1]["message"] == (
        "live_order_cancel_confirmation_unknown_requires_manual_check"
    )


def _order(
    *,
    mode: TradingMode,
    status: OrderStatus,
    provider_order_id: str | None,
) -> Order:
    return Order(
        id=uuid4(),
        decision_id=uuid4(),
        symbol="005930",
        action="buy",
        mode=mode,
        status=status,
        amount_krw=75_000,
        idempotency_key=str(uuid4()),
        provider_order_id=provider_order_id,
        reason=None,
        created_at=now_utc(),
    )
