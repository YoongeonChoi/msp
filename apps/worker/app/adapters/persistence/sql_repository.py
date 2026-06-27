from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from app.adapters.persistence.models import decision_to_row, order_to_row
from app.application.ports.outcome_tracking_port import OutcomeTrackingRows
from app.application.ports.paper_health_port import PaperHealthRows
from app.domain.common.json import JsonObject, json_object, to_json_value
from app.domain.risk.entities import RiskResult
from app.domain.strategy.entities import StrategyVersion
from app.domain.strategy.research import AIUpgradeCandidate, MonthlyResearchRows, MonthPeriod
from app.domain.trading.entities import BotSettings, DecisionSnapshot, Order
from app.domain.trading.value_objects import StrategyWeights


class InMemoryRepository:
    def __init__(self, settings: BotSettings | None = None) -> None:
        self.settings = settings or BotSettings()
        self.watchlist = ["005930"]
        self.strategy_version: StrategyVersion | None = StrategyVersion(
            id=uuid4(),
            version="strategy_v1_weighted_factor",
            status="paper",
            strategy_type="WeightedFactorStrategyV1",
            weights=StrategyWeights(),
            buy_threshold=0.68,
            sell_threshold=0.25,
        )
        self.decisions: list[DecisionSnapshot] = []
        self.orders: list[Order] = []
        self.heartbeats: list[dict[str, object]] = []
        self.engine_events: list[dict[str, object]] = []
        self.api_health: list[dict[str, object]] = []
        self.outcomes: list[JsonObject] = []
        self.news_events: list[JsonObject] = []
        self.features_daily: list[JsonObject] = []
        self.backtest_runs: list[JsonObject] = []
        self.ai_upgrade_candidates: list[AIUpgradeCandidate] = []

    async def load_bot_settings(self) -> BotSettings:
        return self.settings

    async def load_enabled_watchlist(self) -> list[str]:
        return list(self.watchlist)

    async def load_active_strategy_version(self) -> StrategyVersion | None:
        return self.strategy_version

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

    async def load_paper_health_rows(self, since: datetime) -> PaperHealthRows:
        decision_rows = [decision_to_row(decision) for decision in self.decisions]
        order_rows = [order_to_row(order) for order in self.orders]
        return PaperHealthRows(
            latest_heartbeats=[json_object(item) for item in self.heartbeats],
            api_health=[json_object(item) for item in self.api_health],
            decisions_last_24h=decision_rows,
            orders_last_24h=order_rows,
            live_like_orders=[
                row
                for row in order_rows
                if row.get("status") in {"sent", "filled", "partial_filled"}
            ],
            order_key_rows=order_rows,
            recent_engine_events=[json_object(item) for item in self.engine_events],
            outcomes_last_24h=list(self.outcomes),
            db_size_bytes=None,
        )

    async def load_outcome_tracking_rows(
        self, decision_limit: int, price_limit: int
    ) -> OutcomeTrackingRows:
        return OutcomeTrackingRows(
            decisions=[decision_to_row(decision) for decision in self.decisions[:decision_limit]],
            orders=[order_to_row(order) for order in self.orders],
            features_daily=list(self.features_daily[:price_limit]),
            existing_outcomes=list(self.outcomes),
        )

    async def upsert_outcome(self, outcome: JsonObject) -> None:
        decision_id = outcome.get("decision_id")
        if not isinstance(decision_id, str):
            return
        self.outcomes = [
            existing
            for existing in self.outcomes
            if existing.get("decision_id") != decision_id
        ]
        self.outcomes.append(outcome)

    async def load_monthly_research_rows(self, period: MonthPeriod) -> MonthlyResearchRows:
        strategy = self.strategy_version
        return MonthlyResearchRows(
            base_strategy_version_id=strategy.id if strategy is not None else None,
            base_strategy_version=(
                strategy.version if strategy is not None else "strategy_v1_weighted_factor"
            ),
            decisions=[decision_to_row(decision) for decision in self.decisions],
            outcomes=list(self.outcomes),
            orders=[order_to_row(order) for order in self.orders],
            news_events=list(self.news_events),
            features_daily=list(self.features_daily),
            api_health=[
                {
                    "provider": to_json_value(item["provider"]),
                    "healthy": to_json_value(item["healthy"]),
                    "details": to_json_value(item["details"]),
                }
                for item in self.api_health
            ],
            backtest_runs=list(self.backtest_runs),
        )

    async def persist_ai_upgrade_candidate(self, candidate: AIUpgradeCandidate) -> None:
        self.ai_upgrade_candidates.append(candidate)
