from datetime import datetime
from uuid import uuid4

from app.adapters.persistence.sql_repository import InMemoryRepository
from app.application.ports.alert_port import AlertDeliveryResult
from app.application.ports.broker_port import (
    BrokerCancelOrderResult,
    BrokerOrderRequest,
    BrokerOrderResult,
    BrokerOrderStatusResult,
)
from app.application.services.order_reconciliation_service import OrderReconciliationService
from app.domain.common.errors import ProviderTimeoutError
from app.domain.common.time import now_utc
from app.domain.trading.entities import AccountState, BotSettings, Order, OrderStatus


class StatusBroker:
    def __init__(
        self,
        result: BrokerOrderStatusResult | None = None,
        timeout: bool = False,
    ) -> None:
        self.result = result
        self.timeout = timeout
        self.status_calls = 0

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
        if self.timeout:
            raise ProviderTimeoutError("toss", "toss_read_timeout")
        if self.result is None:
            return BrokerOrderStatusResult(
                provider_order_id=provider_order_id,
                status="sent",
                raw_summary={"provider_order_id": provider_order_id},
            )
        return self.result

    async def cancel_order(self, provider_order_id: str) -> BrokerCancelOrderResult:
        return BrokerCancelOrderResult(
            original_provider_order_id=provider_order_id,
            cancel_provider_order_id="unused-cancel-order",
            raw_summary={"provider_order_id": provider_order_id},
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


async def test_reconcile_live_order_updates_final_broker_status() -> None:
    repository = InMemoryRepository(BotSettings())
    order = _live_order(provider_order_id="provider-order-1")
    repository.orders.append(order)
    broker = StatusBroker(
        BrokerOrderStatusResult(
            provider_order_id="provider-order-1",
            status="filled",
            raw_summary={"status": "FILLED", "filled_quantity": "1"},
        )
    )
    service = OrderReconciliationService(broker, repository)

    updated = await service.reconcile_live_orders()

    assert updated == 1
    assert broker.status_calls == 1
    assert repository.orders[0].status == "filled"
    assert repository.orders[0].provider_payload_summary == {
        "status": "FILLED",
        "filled_quantity": "1",
    }
    assert repository.engine_events[-1]["message"] == "live_order_reconciled"


async def test_reconcile_live_order_without_provider_id_requires_manual_check() -> None:
    repository = InMemoryRepository(BotSettings())
    repository.orders.append(_live_order(provider_order_id=None))
    broker = StatusBroker()
    service = OrderReconciliationService(broker, repository)

    updated = await service.reconcile_live_orders()

    assert updated == 1
    assert broker.status_calls == 0
    assert repository.orders[0].status == "unknown_requires_manual_check"
    assert repository.orders[0].reason == "missing_provider_order_id"
    assert repository.engine_events[-1]["message"] == (
        "live_order_reconciliation_missing_provider_order_id"
    )


async def test_reconcile_live_order_timeout_defers_without_status_change() -> None:
    repository = InMemoryRepository(BotSettings())
    repository.orders.append(_live_order(provider_order_id="provider-order-1"))
    service = OrderReconciliationService(StatusBroker(timeout=True), repository)

    updated = await service.reconcile_live_orders()

    assert updated == 0
    assert repository.orders[0].status == "sent"
    assert repository.engine_events[-1]["message"] == "live_order_reconciliation_deferred"
    assert repository.engine_events[-1]["level"] == "warning"


async def test_unknown_live_order_timeout_emits_critical_manual_check_alert() -> None:
    repository = InMemoryRepository(BotSettings())
    repository.orders.append(
        _live_order(
            provider_order_id="provider-order-1",
            status="unknown_requires_manual_check",
        )
    )
    notifier = CapturingAlertNotifier()
    service = OrderReconciliationService(
        StatusBroker(timeout=True),
        repository,
        alert_notifier=notifier,
    )

    updated = await service.reconcile_live_orders()

    assert updated == 0
    assert repository.orders[0].status == "unknown_requires_manual_check"
    assert repository.engine_events[-1]["level"] == "critical"
    assert repository.engine_events[-1]["message"] == "live_order_manual_check_still_unknown"
    details = repository.engine_events[-1]["details"]
    assert isinstance(details, dict)
    assert details["status"] == "unknown_requires_manual_check"
    assert notifier.messages == ["live_order_manual_check_still_unknown"]


async def test_broker_unknown_status_emits_critical_manual_check_alert() -> None:
    repository = InMemoryRepository(BotSettings())
    repository.orders.append(_live_order(provider_order_id="provider-order-1"))
    service = OrderReconciliationService(
        StatusBroker(
            BrokerOrderStatusResult(
                provider_order_id="provider-order-1",
                status="unknown_requires_manual_check",
                raw_summary={"status": "UNKNOWN"},
                reason="provider_status_unknown",
            )
        ),
        repository,
    )

    updated = await service.reconcile_live_orders()

    assert updated == 1
    assert repository.orders[0].status == "unknown_requires_manual_check"
    assert repository.orders[0].reason == "provider_status_unknown"
    assert repository.engine_events[-1]["level"] == "critical"
    assert repository.engine_events[-1]["message"] == (
        "live_order_requires_manual_check_after_reconciliation"
    )


async def test_unknown_manual_check_order_does_not_auto_clear_on_terminal_provider_status() -> (
    None
):
    repository = InMemoryRepository(BotSettings())
    repository.orders.append(
        _live_order(
            provider_order_id="provider-order-1",
            status="unknown_requires_manual_check",
        )
    )
    service = OrderReconciliationService(
        StatusBroker(
            BrokerOrderStatusResult(
                provider_order_id="provider-order-1",
                status="filled",
                raw_summary={"status": "FILLED", "filled_quantity": "1"},
            )
        ),
        repository,
    )

    updated = await service.reconcile_live_orders()

    assert updated == 1
    assert repository.orders[0].status == "unknown_requires_manual_check"
    assert repository.orders[0].reason == "manual_check_required_provider_status_filled"
    assert repository.orders[0].provider_payload_summary == {
        "status": "FILLED",
        "filled_quantity": "1",
    }
    assert repository.engine_events[-1]["level"] == "critical"
    assert repository.engine_events[-1]["message"] == (
        "live_order_manual_check_provider_status_observed"
    )
    details = repository.engine_events[-1]["details"]
    assert isinstance(details, dict)
    assert details["provider_status"] == "filled"


def _live_order(
    provider_order_id: str | None,
    status: OrderStatus = "sent",
) -> Order:
    return Order(
        id=uuid4(),
        decision_id=uuid4(),
        symbol="005930",
        action="buy",
        mode="live",
        status=status,
        amount_krw=75_000,
        idempotency_key="live-key-1",
        provider_order_id=provider_order_id,
        reason=None,
        created_at=now_utc(),
    )


class CapturingAlertNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def notify_engine_event(
        self,
        level: str,
        component: str,
        message: str,
        details: dict[str, object],
    ) -> AlertDeliveryResult:
        self.messages.append(message)
        return AlertDeliveryResult(delivered=True, latency_ms=1)
