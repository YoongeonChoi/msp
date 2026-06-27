from __future__ import annotations

from datetime import UTC, datetime

from app.application.ports.outcome_tracking_port import OutcomeTrackingRepositoryPort
from app.application.services.outcome_tracking_calculator import calculate_outcome
from app.application.services.outcome_tracking_models import (
    OutcomeRecord,
    OutcomeStatus,
    OutcomeTrackingSummary,
)
from app.application.services.outcome_tracking_parsing import (
    group_prices,
    paper_orders_by_decision,
    parse_decision,
)


class OutcomeTrackingService:
    def __init__(
        self,
        repository: OutcomeTrackingRepositoryPort,
        decision_limit: int = 500,
        price_limit: int = 5000,
    ) -> None:
        self.repository = repository
        self.decision_limit = decision_limit
        self.price_limit = price_limit

    async def update_once(self, now: datetime | None = None) -> OutcomeTrackingSummary:
        calculated_at = _utc_now(now)
        rows = await self.repository.load_outcome_tracking_rows(
            self.decision_limit, self.price_limit
        )
        prices_by_symbol = group_prices(rows.features_daily)
        orders_by_decision = paper_orders_by_decision(rows.orders)
        records = [
            calculate_outcome(
                parse_decision(row),
                orders_by_decision,
                prices_by_symbol,
                calculated_at,
            )
            for row in rows.decisions
        ]
        for record in records:
            await self.repository.upsert_outcome(record.to_row())
        return _summary(records)


def _summary(records: list[OutcomeRecord]) -> OutcomeTrackingSummary:
    return OutcomeTrackingSummary(
        processed_count=len(records),
        upserted_count=len(records),
        skipped_count=sum(1 for record in records if record.status == OutcomeStatus.SKIPPED),
        partial_count=sum(1 for record in records if record.status == OutcomeStatus.PARTIAL),
        complete_count=sum(1 for record in records if record.status == OutcomeStatus.COMPLETE),
    )


def _utc_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    if now.tzinfo is None:
        return now.replace(tzinfo=UTC)
    return now.astimezone(UTC)
