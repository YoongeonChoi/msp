from __future__ import annotations

from dataclasses import dataclass

from app.domain.common.errors import KnownFailClosedError


@dataclass(frozen=True, slots=True)
class LiveOrderCancellationResult:
    order_id: str
    status: str
    reason: str | None


class LiveOrderCancellationService:
    """Historical live-row cancellation boundary, permanently quarantined.

    Contract-test cancellation belongs to the V2 contract state machine and
    never operates on a legacy ``mode=live`` order row.
    """

    def __init__(self, broker: object, repository: object) -> None:
        del broker, repository

    async def cancel_live_order(self, order_id: str) -> LiveOrderCancellationResult:
        del order_id
        raise KnownFailClosedError(
            "live_cancel",
            "legacy_live_order_cancel_is_quarantined",
        )
