from __future__ import annotations

from typing import Protocol

from app.domain.risk.entities import RiskResult
from app.domain.strategy.entities import StrategyVersion
from app.domain.strategy.research import AIUpgradeCandidate, MonthlyResearchRows, MonthPeriod
from app.domain.trading.entities import BotSettings, DecisionSnapshot, Order


class RepositoryPort(Protocol):
    async def load_bot_settings(self) -> BotSettings:
        ...

    async def load_enabled_watchlist(self) -> list[str]:
        ...

    async def load_active_strategy_version(self) -> StrategyVersion | None:
        ...

    async def persist_decision_snapshot(self, snapshot: DecisionSnapshot) -> None:
        ...

    async def persist_order(self, order: Order, risk_result: RiskResult | None = None) -> None:
        ...

    async def record_heartbeat(self, status: str, details: dict[str, object]) -> None:
        ...

    async def record_engine_event(
        self, level: str, component: str, message: str, details: dict[str, object]
    ) -> None:
        ...

    async def record_api_health(
        self,
        provider: str,
        healthy: bool,
        details: dict[str, object],
    ) -> None:
        ...

    async def idempotency_key_exists(self, idempotency_key: str) -> bool:
        ...

    async def load_monthly_research_rows(self, period: MonthPeriod) -> MonthlyResearchRows:
        ...

    async def persist_ai_upgrade_candidate(self, candidate: AIUpgradeCandidate) -> None:
        ...
