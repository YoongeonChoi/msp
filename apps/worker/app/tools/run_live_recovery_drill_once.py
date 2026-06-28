from __future__ import annotations

import asyncio
from datetime import datetime
from uuid import uuid4

from app.adapters.ai.openai_mock import OpenAIMock
from app.adapters.fundamentals.opendart_mock import OpenDartMock
from app.adapters.market_data.krx_mock import KrxMock
from app.adapters.news.naver_news_mock import NaverNewsMock
from app.adapters.persistence.sql_repository import InMemoryRepository
from app.application.ports.broker_port import (
    BrokerCancelOrderResult,
    BrokerOrderRequest,
    BrokerOrderResult,
    BrokerOrderStatusResult,
)
from app.application.services.execution_service import ExecutionService
from app.application.services.feature_service import FeatureService
from app.application.services.health_service import HealthService
from app.application.services.live_order_cancellation_service import (
    LiveOrderCancellationService,
)
from app.application.services.order_reconciliation_service import (
    OrderReconciliationService,
)
from app.application.services.risk_service import RiskService
from app.application.use_cases.run_trading_cycle import RunTradingCycle
from app.domain.common.errors import ProviderTimeoutError
from app.domain.common.time import now_utc
from app.domain.trading.entities import AccountState, BotSettings, Order, OrderStatus


class RecoveryDrillBroker:
    def __init__(
        self,
        statuses: dict[str, BrokerOrderStatusResult],
        timeout_provider_order_ids: set[str],
    ) -> None:
        self.statuses = statuses
        self.timeout_provider_order_ids = timeout_provider_order_ids
        self.status_calls = 0
        self.cancel_calls = 0

    async def provider_health(self) -> bool:
        return True

    async def place_order(self, request: BrokerOrderRequest) -> BrokerOrderResult:
        raise RuntimeError("live_recovery_drill_must_not_place_orders")

    async def get_order_status(self, provider_order_id: str) -> BrokerOrderStatusResult:
        self.status_calls += 1
        if provider_order_id in self.timeout_provider_order_ids:
            raise ProviderTimeoutError("toss", "drill_provider_timeout")
        return self.statuses[provider_order_id]

    async def cancel_order(self, provider_order_id: str) -> BrokerCancelOrderResult:
        self.cancel_calls += 1
        return BrokerCancelOrderResult(
            original_provider_order_id=provider_order_id,
            cancel_provider_order_id=f"drill-cancel-{provider_order_id}",
            raw_summary={
                "original_order_id": provider_order_id,
                "cancel_order_id": f"drill-cancel-{provider_order_id}",
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


async def main() -> None:
    repository = InMemoryRepository(
        BotSettings(enabled=True, mode="live", live_order_allowed=True)
    )
    broker = RecoveryDrillBroker(
        statuses={
            "drill-reconcile-filled": BrokerOrderStatusResult(
                provider_order_id="drill-reconcile-filled",
                status="filled",
                raw_summary={"status": "FILLED", "filled_quantity": "1"},
            ),
            "drill-cancel-confirm": BrokerOrderStatusResult(
                provider_order_id="drill-cancel-confirm",
                status="canceled",
                raw_summary={"status": "CANCELED"},
            ),
            "drill-cancel-still-sent": BrokerOrderStatusResult(
                provider_order_id="drill-cancel-still-sent",
                status="sent",
                raw_summary={"status": "SENT"},
            ),
        },
        timeout_provider_order_ids={"drill-manual-timeout"},
    )

    reconcile_order = _live_order(
        status="sent",
        provider_order_id="drill-reconcile-filled",
        idempotency_key="live-recovery-drill-reconcile",
    )
    manual_order = _live_order(
        status="unknown_requires_manual_check",
        provider_order_id="drill-manual-timeout",
        idempotency_key="live-recovery-drill-manual",
        reason="drill_unknown_status",
    )
    repository.orders.extend([reconcile_order, manual_order])

    reconciliation = OrderReconciliationService(broker, repository)
    reconciled_updates = await reconciliation.reconcile_live_orders(limit=10)

    cancel_confirm_order = _live_order(
        status="sent",
        provider_order_id="drill-cancel-confirm",
        idempotency_key="live-recovery-drill-cancel-confirm",
    )
    cancel_unknown_order = _live_order(
        status="partial_filled",
        provider_order_id="drill-cancel-still-sent",
        idempotency_key="live-recovery-drill-cancel-unknown",
    )
    repository.orders.extend([cancel_confirm_order, cancel_unknown_order])

    cancellation = LiveOrderCancellationService(broker, repository)
    cancel_confirmed = await cancellation.cancel_live_order(str(cancel_confirm_order.id))
    cancel_unknown = await cancellation.cancel_live_order(str(cancel_unknown_order.id))

    _assert_order_state(
        repository,
        order_id=str(reconcile_order.id),
        status="filled",
        reason=None,
    )
    _assert_order_state(
        repository,
        order_id=str(manual_order.id),
        status="unknown_requires_manual_check",
        reason="drill_unknown_status",
    )
    _assert_order_state(
        repository,
        order_id=str(cancel_confirm_order.id),
        status="canceled",
        reason="operator_cancel_confirmed",
    )
    _assert_order_state(
        repository,
        order_id=str(cancel_unknown_order.id),
        status="unknown_requires_manual_check",
        reason="cancel_confirmation_status_sent",
    )
    _assert_event_messages(
        repository,
        {
            "live_order_reconciled",
            "live_order_manual_check_still_unknown",
            "live_order_cancel_confirmed",
            "live_order_cancel_confirmation_failed",
        },
    )
    if reconciled_updates != 1:
        raise RuntimeError(f"live_recovery_drill_reconcile_count_failed: {reconciled_updates}")
    if cancel_confirmed.status != "canceled":
        raise RuntimeError(f"live_recovery_drill_cancel_confirm_failed: {cancel_confirmed}")
    if cancel_unknown.status != "unknown_requires_manual_check":
        raise RuntimeError(f"live_recovery_drill_cancel_unknown_failed: {cancel_unknown}")
    if broker.status_calls != 4:
        raise RuntimeError(f"live_recovery_drill_status_call_count_failed: {broker.status_calls}")
    if broker.cancel_calls != 2:
        raise RuntimeError(f"live_recovery_drill_cancel_call_count_failed: {broker.cancel_calls}")
    pending_order_blocked = await _pending_order_blocks_new_decisions(repository, broker)
    if not pending_order_blocked:
        raise RuntimeError("live_recovery_drill_pending_order_block_failed")
    manual_check_preserved = await _manual_check_status_preserved()
    if not manual_check_preserved:
        raise RuntimeError("live_recovery_drill_manual_check_preservation_failed")

    manual_check_events = sum(
        1
        for event in repository.engine_events
        if event["level"] == "critical"
        and event["message"]
        in {
            "live_order_manual_check_still_unknown",
            "live_order_cancel_confirmation_failed",
        }
    )
    print(
        "FINAL=PASS live_recovery_drill "
        f"reconciled_updates={reconciled_updates} "
        f"manual_check_events={manual_check_events} "
        "cancel_confirmed=1 cancel_unknown=1 "
        f"status_calls={broker.status_calls} cancel_calls={broker.cancel_calls} "
        "pending_order_blocked=1 "
        f"manual_check_preserved={int(manual_check_preserved)}"
    )


async def _manual_check_status_preserved() -> bool:
    repository = InMemoryRepository(
        BotSettings(enabled=True, mode="live", live_order_allowed=True)
    )
    broker = RecoveryDrillBroker(
        statuses={
            "drill-manual-filled": BrokerOrderStatusResult(
                provider_order_id="drill-manual-filled",
                status="filled",
                raw_summary={"status": "FILLED", "filled_quantity": "1"},
            )
        },
        timeout_provider_order_ids=set(),
    )
    order = _live_order(
        status="unknown_requires_manual_check",
        provider_order_id="drill-manual-filled",
        idempotency_key="live-recovery-drill-preserve-manual-check",
        reason="drill_manual_review_required",
    )
    repository.orders.append(order)

    updated = await OrderReconciliationService(broker, repository).reconcile_live_orders(
        limit=10
    )

    _assert_order_state(
        repository,
        order_id=str(order.id),
        status="unknown_requires_manual_check",
        reason="manual_check_required_provider_status_filled",
    )
    _assert_event_messages(
        repository,
        {"live_order_manual_check_provider_status_observed"},
    )
    return updated == 1 and broker.status_calls == 1 and broker.cancel_calls == 0


async def _pending_order_blocks_new_decisions(
    repository: InMemoryRepository,
    broker: RecoveryDrillBroker,
) -> bool:
    risk = RiskService()
    market_data = KrxMock()
    cycle = RunTradingCycle(
        repository=repository,
        broker=broker,
        market_data=market_data,
        health_service=HealthService(
            repository,
            broker,
            market_data,
            OpenDartMock(),
            NaverNewsMock(),
            OpenAIMock(),
        ),
        execution_service=ExecutionService(broker, repository, risk),
        risk_service=risk,
        feature_service=FeatureService(
            fundamentals=OpenDartMock(),
            news=NaverNewsMock(),
            fundamentals_provider_name="opendart_mock",
            news_provider_name="naver_mock",
        ),
        live_system_order_count_scope_accepted=True,
    )
    decisions_before = len(repository.decisions)
    orders_before = len(repository.orders)
    status_calls_before = broker.status_calls
    cancel_calls_before = broker.cancel_calls
    await cycle.execute()
    return (
        len(repository.decisions) == decisions_before
        and len(repository.orders) == orders_before
        and broker.status_calls == status_calls_before
        and broker.cancel_calls == cancel_calls_before
        and any(
            event["message"] == "live_pending_reconciliation_blocks_new_decisions"
            for event in repository.engine_events
        )
    )


def _live_order(
    *,
    status: OrderStatus,
    provider_order_id: str,
    idempotency_key: str,
    reason: str | None = None,
) -> Order:
    return Order(
        id=uuid4(),
        decision_id=uuid4(),
        symbol="005930",
        action="buy",
        mode="live",
        status=status,
        amount_krw=75_000,
        idempotency_key=idempotency_key,
        provider_order_id=provider_order_id,
        reason=reason,
        created_at=now_utc(),
    )


def _assert_order_state(
    repository: InMemoryRepository,
    *,
    order_id: str,
    status: OrderStatus,
    reason: str | None,
) -> None:
    order = next((item for item in repository.orders if str(item.id) == order_id), None)
    if order is None:
        raise RuntimeError(f"live_recovery_drill_order_missing: {order_id}")
    if order.status != status or order.reason != reason:
        raise RuntimeError(
            "live_recovery_drill_order_state_failed: "
            f"order_id={order_id} status={order.status} reason={order.reason}"
        )


def _assert_event_messages(
    repository: InMemoryRepository,
    expected_messages: set[str],
) -> None:
    messages = {str(event["message"]) for event in repository.engine_events}
    missing = expected_messages - messages
    if missing:
        raise RuntimeError(f"live_recovery_drill_events_missing: {sorted(missing)}")


if __name__ == "__main__":
    asyncio.run(main())
