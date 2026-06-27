from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.domain.common.json import JsonObject


@dataclass(frozen=True, slots=True)
class OutcomeTrackingRows:
    decisions: list[JsonObject]
    orders: list[JsonObject]
    features_daily: list[JsonObject]
    existing_outcomes: list[JsonObject]


class OutcomeTrackingRepositoryPort(Protocol):
    async def load_outcome_tracking_rows(
        self, decision_limit: int, price_limit: int
    ) -> OutcomeTrackingRows:
        ...

    async def upsert_outcome(self, outcome: JsonObject) -> None:
        ...
