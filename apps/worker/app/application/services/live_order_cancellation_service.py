from __future__ import annotations

from dataclasses import dataclass, replace

from app.application.ports.broker_port import BrokerPort
from app.application.ports.repository_port import RepositoryPort
from app.domain.common.errors import (
    KnownFailClosedError,
    ProviderTimeoutError,
    ProviderUnknownError,
)
from app.domain.trading.entities import Order

OPEN_CANCELABLE_STATUSES = {"sent", "partial_filled"}


@dataclass(frozen=True, slots=True)
class LiveOrderCancellationResult:
    order_id: str
    status: str
    reason: str | None


class LiveOrderCancellationService:
    def __init__(self, broker: BrokerPort, repository: RepositoryPort) -> None:
        self.broker = broker
        self.repository = repository

    async def cancel_live_order(self, order_id: str) -> LiveOrderCancellationResult:
        order = await self.repository.load_order_by_id(order_id)
        if order is None:
            await self._record_rejection(order_id, "order_not_found", None)
            raise KnownFailClosedError("live_cancel", "order_not_found")
        if order.mode != "live":
            await self._record_rejection(order_id, "order_not_live", order)
            raise KnownFailClosedError("live_cancel", "order_not_live")
        if order.status not in OPEN_CANCELABLE_STATUSES:
            await self._record_rejection(order_id, "order_not_cancelable", order)
            raise KnownFailClosedError("live_cancel", "order_not_cancelable")
        if order.provider_order_id is None:
            await self.repository.update_order_status(
                order_id=str(order.id),
                status="unknown_requires_manual_check",
                reason="missing_provider_order_id_for_cancel",
                provider_payload_summary=order.provider_payload_summary,
                expected_statuses={order.status},
            )
            await self._record_rejection(order_id, "missing_provider_order_id_for_cancel", order)
            raise KnownFailClosedError("live_cancel", "missing_provider_order_id_for_cancel")

        reserved = await self.repository.update_order_status(
            order_id=str(order.id),
            status="unknown_requires_manual_check",
            reason="live_broker_cancel_result_pending",
            provider_payload_summary=_cancel_payload(
                order,
                cancel_failure_reason="live_broker_cancel_result_pending",
            ),
            expected_statuses={order.status},
        )
        if not reserved:
            current = await self.repository.load_order_by_id(str(order.id))
            if current is None:
                await self._record_rejection(order_id, "cancel_reservation_order_missing", order)
                raise KnownFailClosedError(
                    "live_cancel",
                    "cancel_reservation_order_missing",
                )
            await self.repository.record_engine_event(
                "warning",
                "live_cancel",
                "live_order_cancel_concurrent_transition_skipped",
                {
                    "order_id": str(order.id),
                    "symbol": order.symbol,
                    "observed_status": order.status,
                    "current_status": current.status,
                },
            )
            return LiveOrderCancellationResult(
                str(current.id),
                current.status,
                "concurrent_order_status_change",
            )
        order = replace(
            order,
            status="unknown_requires_manual_check",
            reason="live_broker_cancel_result_pending",
        )
        provider_order_id = order.provider_order_id
        assert provider_order_id is not None
        await self.repository.record_engine_event(
            "info",
            "live_cancel",
            "live_order_cancel_result_pending",
            {
                "order_id": str(order.id),
                "symbol": order.symbol,
                "provider_order_id_present": True,
            },
        )

        try:
            cancel_result = await self.broker.cancel_order(provider_order_id)
        except (ProviderTimeoutError, ProviderUnknownError) as exc:
            return await self._mark_unknown_requires_manual_check(
                order,
                reason=exc.safe_message,
                event_message="live_order_cancel_unknown_requires_manual_check",
                cancel_failure_reason=exc.safe_message,
            )
        except KnownFailClosedError as exc:
            await self._mark_unknown_requires_manual_check(
                order,
                reason=exc.safe_message,
                event_message="live_order_cancel_failed_closed",
                cancel_failure_reason=exc.safe_message,
            )
            raise

        if cancel_result.original_provider_order_id != provider_order_id:
            return await self._mark_unknown_requires_manual_check(
                order,
                reason="broker_cancel_identity_mismatch",
                event_message="live_order_cancel_identity_mismatch",
                cancel_summary=cancel_result.raw_summary,
                cancel_failure_reason="broker_cancel_identity_mismatch",
            )

        try:
            confirmation = await self.broker.get_order_status(provider_order_id)
        except (ProviderTimeoutError, ProviderUnknownError) as exc:
            return await self._mark_unknown_requires_manual_check(
                order,
                reason=exc.safe_message,
                event_message="live_order_cancel_confirmation_unknown_requires_manual_check",
                cancel_summary=cancel_result.raw_summary,
                confirmation_failure_reason=exc.safe_message,
            )
        except KnownFailClosedError as exc:
            await self._mark_unknown_requires_manual_check(
                order,
                reason=exc.safe_message,
                event_message="live_order_cancel_confirmation_failed_closed",
                cancel_summary=cancel_result.raw_summary,
                confirmation_failure_reason=exc.safe_message,
            )
            raise

        if confirmation.provider_order_id != provider_order_id:
            return await self._mark_unknown_requires_manual_check(
                order,
                reason="broker_cancel_confirmation_identity_mismatch",
                event_message="live_order_cancel_confirmation_identity_mismatch",
                cancel_summary=cancel_result.raw_summary,
                confirmation_summary=confirmation.raw_summary,
                confirmation_failure_reason="broker_cancel_confirmation_identity_mismatch",
            )

        if confirmation.status != "canceled":
            reason = f"cancel_confirmation_status_{confirmation.status}"
            return await self._mark_unknown_requires_manual_check(
                order,
                reason=reason,
                event_message="live_order_cancel_confirmation_failed",
                cancel_summary=cancel_result.raw_summary,
                confirmation_summary=confirmation.raw_summary,
                confirmation_failure_reason=reason,
            )

        updated = await self.repository.update_order_status(
            order_id=str(order.id),
            status="canceled",
            reason="operator_cancel_confirmed",
            provider_payload_summary=_cancel_payload(
                order,
                cancel_summary=cancel_result.raw_summary,
                confirmation_summary=confirmation.raw_summary,
            ),
            expected_statuses={order.status},
        )
        if not updated:
            current = await self.repository.load_order_by_id(str(order.id))
            if current is not None:
                await self.repository.record_engine_event(
                    "warning",
                    "live_cancel",
                    "live_order_cancel_concurrent_transition_skipped",
                    {
                        "order_id": str(order.id),
                        "symbol": order.symbol,
                        "observed_status": order.status,
                        "current_status": current.status,
                    },
                )
                return LiveOrderCancellationResult(
                    str(current.id),
                    current.status,
                    "concurrent_order_status_change",
                )
        await self.repository.record_engine_event(
            "info",
            "live_cancel",
            "live_order_cancel_confirmed",
            {
                "order_id": str(order.id),
                "symbol": order.symbol,
                "provider_order_id_present": True,
                "cancel_provider_order_id_present": True,
                "confirmation_status": confirmation.status,
            },
        )
        return LiveOrderCancellationResult(str(order.id), "canceled", "operator_cancel_confirmed")

    async def _mark_unknown_requires_manual_check(
        self,
        order: Order,
        *,
        reason: str,
        event_message: str,
        cancel_summary: dict[str, object] | None = None,
        confirmation_summary: dict[str, object] | None = None,
        cancel_failure_reason: str | None = None,
        confirmation_failure_reason: str | None = None,
    ) -> LiveOrderCancellationResult:
        updated = await self.repository.update_order_status(
            order_id=str(order.id),
            status="unknown_requires_manual_check",
            reason=reason,
            provider_payload_summary=_cancel_payload(
                order,
                cancel_summary=cancel_summary,
                confirmation_summary=confirmation_summary,
                cancel_failure_reason=cancel_failure_reason,
                confirmation_failure_reason=confirmation_failure_reason,
            ),
            expected_statuses={order.status},
        )
        if not updated:
            current = await self.repository.load_order_by_id(str(order.id))
            if current is not None:
                return LiveOrderCancellationResult(
                    str(current.id),
                    current.status,
                    "concurrent_order_status_change",
                )
        await self.repository.record_engine_event(
            "critical",
            "live_cancel",
            event_message,
            {
                "order_id": str(order.id),
                "symbol": order.symbol,
                "provider_order_id_present": order.provider_order_id is not None,
                "reason": reason,
            },
        )
        return LiveOrderCancellationResult(
            str(order.id),
            "unknown_requires_manual_check",
            reason,
        )

    async def _record_rejection(
        self,
        order_id: str,
        reason: str,
        order: Order | None,
    ) -> None:
        details: dict[str, object] = {"order_id": order_id, "reason": reason}
        if order is not None:
            details |= {"symbol": order.symbol, "status": order.status, "mode": order.mode}
        await self.repository.record_engine_event(
            "warning",
            "live_cancel",
            "live_order_cancel_rejected",
            details,
        )


def _cancel_payload(
    order: Order,
    *,
    cancel_summary: dict[str, object] | None = None,
    confirmation_summary: dict[str, object] | None = None,
    cancel_failure_reason: str | None = None,
    confirmation_failure_reason: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "previous_provider_payload_summary": order.provider_payload_summary,
        "original_provider_order_id": order.provider_order_id,
    }
    if cancel_summary is not None:
        payload["cancel"] = cancel_summary
    if confirmation_summary is not None:
        payload["confirmation"] = confirmation_summary
    if cancel_failure_reason is not None:
        payload["cancel_failure_reason"] = cancel_failure_reason
    if confirmation_failure_reason is not None:
        payload["cancel_confirmation_failure_reason"] = confirmation_failure_reason
    return payload
