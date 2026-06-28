from __future__ import annotations

from datetime import datetime
from typing import Protocol

from app.domain.risk.entities import RiskResult
from app.domain.strategy.entities import StrategyVersion
from app.domain.strategy.research import AIUpgradeCandidate, MonthlyResearchRows, MonthPeriod
from app.domain.trading.entities import BotSettings, DecisionSnapshot, Order, OrderStatus


class RepositoryPort(Protocol):
    async def load_bot_settings(self) -> BotSettings: ...

    async def load_enabled_watchlist(self) -> list[str]: ...

    async def load_active_strategy_version(self) -> StrategyVersion | None: ...

    async def persist_decision_snapshot(self, snapshot: DecisionSnapshot) -> None: ...

    async def persist_order(self, order: Order, risk_result: RiskResult | None = None) -> None: ...

    async def load_live_orders_for_reconciliation(self, limit: int = 50) -> list[Order]: ...

    async def count_system_live_orders_created_between(
        self,
        start: datetime,
        end: datetime,
    ) -> int: ...

    async def load_order_by_id(self, order_id: str) -> Order | None: ...

    async def update_order_status(
        self,
        order_id: str,
        status: OrderStatus,
        reason: str | None,
        provider_payload_summary: dict[str, object] | None,
        provider_order_id: str | None = None,
    ) -> None: ...

    async def record_heartbeat(self, status: str, details: dict[str, object]) -> None: ...

    async def record_engine_event(
        self, level: str, component: str, message: str, details: dict[str, object]
    ) -> None: ...

    async def record_api_health(
        self,
        provider: str,
        healthy: bool,
        details: dict[str, object],
    ) -> None: ...

    async def idempotency_key_exists(self, idempotency_key: str) -> bool: ...

    async def load_monthly_research_rows(self, period: MonthPeriod) -> MonthlyResearchRows: ...

    async def persist_ai_upgrade_candidate(self, candidate: AIUpgradeCandidate) -> None: ...
