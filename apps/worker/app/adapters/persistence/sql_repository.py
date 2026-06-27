from __future__ import annotations

from uuid import UUID, uuid4

from app.domain.risk.entities import RiskResult
from app.domain.trading.entities import BotSettings, DecisionSnapshot, Order


class InMemoryRepository:
    def __init__(self, settings: BotSettings | None = None) -> None:
        self.settings = settings or BotSettings()
        self.watchlist = ["005930"]
        self.strategy_version_id = uuid4()
        self.decisions: list[DecisionSnapshot] = []
        self.orders: list[Order] = []
        self.heartbeats: list[dict[str, object]] = []
        self.engine_events: list[dict[str, object]] = []
        self.api_health: list[dict[str, object]] = []

    async def load_bot_settings(self) -> BotSettings:
        return self.settings

    async def load_enabled_watchlist(self) -> list[str]:
        return list(self.watchlist)

    async def load_active_strategy_version_id(self) -> UUID:
        return self.strategy_version_id

    async def persist_decision_snapshot(self, snapshot: DecisionSnapshot) -> None:
        self.decisions.append(snapshot)

    async def persist_order(self, order: Order, risk_result: RiskResult | None = None) -> None:
        self.orders.append(order)

    async def record_heartbeat(self, status: str, details: dict[str, object]) -> None:
        self.heartbeats.append({"status": status, "details": details})

    async def record_engine_event(
        self, level: str, component: str, message: str, details: dict[str, object]
    ) -> None:
        self.engine_events.append(
            {"level": level, "component": component, "message": message, "details": details}
        )

    async def record_api_health(
        self,
        provider: str,
        healthy: bool,
        details: dict[str, object],
    ) -> None:
        self.api_health.append({"provider": provider, "healthy": healthy, "details": details})

    async def idempotency_key_exists(self, idempotency_key: str) -> bool:
        return any(order.idempotency_key == idempotency_key for order in self.orders)
