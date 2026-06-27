from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.domain.common.json import JsonObject
from app.domain.trading.entities import BotSettings


@dataclass(frozen=True, slots=True)
class PaperHealthRows:
    latest_heartbeats: list[JsonObject]
    api_health: list[JsonObject]
    decisions_last_24h: list[JsonObject]
    orders_last_24h: list[JsonObject]
    live_like_orders: list[JsonObject]
    order_key_rows: list[JsonObject]
    recent_engine_events: list[JsonObject]
    outcomes_last_24h: list[JsonObject]
    db_size_bytes: int | None


class PaperHealthRepositoryPort(Protocol):
    async def load_bot_settings(self) -> BotSettings:
        ...

    async def load_paper_health_rows(self, since: datetime) -> PaperHealthRows:
        ...

    async def record_engine_event(
        self, level: str, component: str, message: str, details: dict[str, object]
    ) -> None:
        ...
