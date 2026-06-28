from __future__ import annotations

from app.application.ports.alert_port import AlertNotifierPort
from app.application.ports.broker_port import BrokerPort
from app.application.ports.repository_port import RepositoryPort
from app.domain.common.errors import (
    KnownFailClosedError,
    ProviderTimeoutError,
    ProviderUnknownError,
)
from app.domain.trading.entities import Order


class OrderReconciliationService:
    def __init__(
        self,
        broker: BrokerPort,
        repository: RepositoryPort,
        alert_notifier: AlertNotifierPort | None = None,
    ) -> None:
        self.broker = broker
        self.repository = repository
        self.alert_notifier = alert_notifier

    async def reconcile_live_orders(self, limit: int = 50) -> int:
        orders = await self.repository.load_live_orders_for_reconciliation(limit=limit)
        updated_count = 0
        for order in orders:
            updated = await self._reconcile_order(order)
            if updated:
                updated_count += 1
        return updated_count

    async def _reconcile_order(self, order: Order) -> bool:
        if order.provider_order_id is None:
            await self.repository.update_order_status(
                order_id=str(order.id),
                status="unknown_requires_manual_check",
                reason="missing_provider_order_id",
                provider_payload_summary=order.provider_payload_summary,
            )
            await self._record_engine_event(
                "critical",
                "live_reconciliation",
                "live_order_reconciliation_missing_provider_order_id",
                {"order_id": str(order.id), "symbol": order.symbol, "status": order.status},
            )
            return True

        try:
            broker_status = await self.broker.get_order_status(order.provider_order_id)
        except (ProviderTimeoutError, ProviderUnknownError) as exc:
            level = "critical" if order.status == "unknown_requires_manual_check" else "warning"
            message = (
                "live_order_manual_check_still_unknown"
                if order.status == "unknown_requires_manual_check"
                else "live_order_reconciliation_deferred"
            )
            await self._record_engine_event(
                level,
                "live_reconciliation",
                message,
                {
                    "order_id": str(order.id),
                    "symbol": order.symbol,
                    "status": order.status,
                    "provider_order_id_present": True,
                    "reason": exc.safe_message,
                },
            )
            return False
        except KnownFailClosedError as exc:
            await self.repository.update_order_status(
                order_id=str(order.id),
                status="unknown_requires_manual_check",
                reason=exc.safe_message,
                provider_payload_summary=order.provider_payload_summary,
            )
            await self._record_engine_event(
                "critical",
                "live_reconciliation",
                "live_order_reconciliation_failed_closed",
                {
                    "order_id": str(order.id),
                    "symbol": order.symbol,
                    "provider_order_id_present": True,
                    "reason": exc.safe_message,
                },
            )
            return True

        if broker_status.status == "unknown_requires_manual_check":
            await self.repository.update_order_status(
                order_id=str(order.id),
                status=broker_status.status,
                reason=broker_status.reason,
                provider_payload_summary=broker_status.raw_summary,
            )
            await self._record_engine_event(
                "critical",
                "live_reconciliation",
                "live_order_requires_manual_check_after_reconciliation",
                {
                    "order_id": str(order.id),
                    "symbol": order.symbol,
                    "previous_status": order.status,
                    "status": broker_status.status,
                    "provider_order_id_present": True,
                    "reason": broker_status.reason,
                },
            )
            return True
        if order.status == "unknown_requires_manual_check":
            reason = f"manual_check_required_provider_status_{broker_status.status}"
            await self.repository.update_order_status(
                order_id=str(order.id),
                status="unknown_requires_manual_check",
                reason=reason,
                provider_payload_summary=broker_status.raw_summary,
            )
            await self._record_engine_event(
                "critical",
                "live_reconciliation",
                "live_order_manual_check_provider_status_observed",
                {
                    "order_id": str(order.id),
                    "symbol": order.symbol,
                    "previous_status": order.status,
                    "provider_status": broker_status.status,
                    "provider_order_id_present": True,
                    "reason": reason,
                },
            )
            return True
        await self.repository.update_order_status(
            order_id=str(order.id),
            status=broker_status.status,
            reason=broker_status.reason,
            provider_payload_summary=broker_status.raw_summary,
        )
        await self.repository.record_engine_event(
            "info",
            "live_reconciliation",
            "live_order_reconciled",
            {
                "order_id": str(order.id),
                "symbol": order.symbol,
                "previous_status": order.status,
                "status": broker_status.status,
                "provider_order_id_present": True,
            },
        )
        return True

    async def _record_engine_event(
        self,
        level: str,
        component: str,
        message: str,
        details: dict[str, object],
    ) -> None:
        await self.repository.record_engine_event(level, component, message, details)
        if level == "critical" and self.alert_notifier is not None:
            await self.alert_notifier.notify_engine_event(level, component, message, details)
