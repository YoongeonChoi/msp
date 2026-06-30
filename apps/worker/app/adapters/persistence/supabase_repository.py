from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from urllib.parse import quote

import httpx

from app.adapters.persistence.models import (
    ai_candidate_to_row,
    decision_to_row,
    fundamentals_row_from_decision,
    news_rows_from_decision,
    order_from_row,
    order_to_row,
    position_to_row,
)
from app.adapters.persistence.supabase_row_parsing import strategy_version_from_row
from app.config import Settings
from app.domain.common.json import JsonObject, json_object
from app.domain.common.time import now_utc
from app.domain.portfolio.entities import Position
from app.domain.risk.entities import RiskResult
from app.domain.strategy.entities import StrategyVersion
from app.domain.strategy.research import AIUpgradeCandidate, MonthlyResearchRows, MonthPeriod
from app.domain.trading.entities import BotSettings, DecisionSnapshot, Order, OrderStatus

PAPER_STRATEGY_VERSION = "strategy_v1_weighted_factor"


class SupabaseRepository:
    def __init__(self, settings: Settings, forced_settings: BotSettings | None = None) -> None:
        if not settings.supabase_url or not settings.supabase_secret_key:
            raise ValueError("Supabase repository requires SUPABASE_URL and SUPABASE_SECRET_KEY")
        self.base_url = settings.supabase_url.rstrip("/") + "/rest/v1"
        self.forced_settings = forced_settings
        self.headers = {
            "apikey": settings.supabase_secret_key.get_secret_value(),
            "authorization": "Bearer " + settings.supabase_secret_key.get_secret_value(),
            "content-type": "application/json",
            "prefer": "return=minimal",
        }
        self.client = httpx.AsyncClient(timeout=10.0, headers=self.headers)

    async def _insert(self, table: str, row: Mapping[str, object]) -> None:
        response = await self.client.post(f"{self.base_url}/{table}", json=row)
        response.raise_for_status()

    async def load_bot_settings(self) -> BotSettings:
        if self.forced_settings is not None:
            return self.forced_settings
        response = await self.client.get(f"{self.base_url}/bot_settings?select=*&id=eq.singleton")
        response.raise_for_status()
        rows = response.json()
        if not rows:
            return BotSettings()
        row = rows[0]
        return BotSettings(
            enabled=bool(row.get("enabled", False)),
            mode=row.get("mode", "paper"),
            live_order_allowed=bool(row.get("live_order_allowed", False)),
            max_order_amount_krw=int(row.get("max_order_amount_krw", 100_000)),
            max_daily_loss_pct=float(row.get("max_daily_loss_pct", 0.02)),
            max_daily_order_count=int(row.get("max_daily_order_count", 10)),
            max_position_pct=float(row.get("max_position_pct", 0.10)),
            max_sector_pct=float(row.get("max_sector_pct", 0.30)),
            loop_interval_sec=int(row.get("loop_interval_sec", 30)),
        )

    async def load_enabled_watchlist(self) -> list[str]:
        response = await self.client.get(f"{self.base_url}/watchlist?select=symbol&enabled=eq.true")
        response.raise_for_status()
        return [str(row["symbol"]) for row in response.json()]

    async def load_active_strategy_version(self) -> StrategyVersion | None:
        queries = (
            "version_name=eq.strategy_v1_weighted_factor&status=in.(paper,active)"
            "&order=created_at.desc&limit=1",
            "status=in.(paper,active)&order=created_at.desc&limit=1",
            "version=eq.strategy_v1_weighted_factor&status=in.(paper,active)"
            "&order=created_at.desc&limit=1",
        )
        for query in queries:
            rows = await self._select_strategy_versions(query)
            if rows:
                return strategy_version_from_row(rows[0])
        return None

    async def _select_strategy_versions(self, query: str) -> list[JsonObject]:
        return await self._select_optional_rows("strategy_versions", f"select=*&{query}")

    async def persist_decision_snapshot(self, snapshot: DecisionSnapshot) -> None:
        await self._insert("decision_snapshots", decision_to_row(snapshot))

    async def persist_feature_observations(self, snapshot: DecisionSnapshot) -> None:
        fundamentals = fundamentals_row_from_decision(snapshot)
        if fundamentals is not None:
            await self._upsert(
                "fundamentals_quarterly",
                fundamentals,
                "symbol,fiscal_year,fiscal_quarter",
            )
        for news_event in news_rows_from_decision(snapshot):
            await self._upsert("news_events", news_event, "symbol,title_hash")

    async def replace_positions(self, positions: list[Position], synced_at: datetime) -> None:
        seen_symbols = {position.symbol for position in positions}
        for position in positions:
            await self._upsert("positions", position_to_row(position, synced_at), "symbol")
        existing = await self._select_rows("positions", "select=symbol")
        for row in existing:
            symbol = row.get("symbol")
            if isinstance(symbol, str) and symbol not in seen_symbols:
                response = await self.client.delete(
                    f"{self.base_url}/positions?symbol=eq.{quote(symbol, safe='')}"
                )
                response.raise_for_status()

    async def persist_order(self, order: Order, risk_result: RiskResult | None = None) -> None:
        await self._insert("orders", order_to_row(order, risk_result))

    async def load_live_orders_for_reconciliation(self, limit: int = 50) -> list[Order]:
        bounded_limit = min(max(limit, 1), 200)
        query = (
            "select=*&mode=eq.live&status=in.(sent,partial_filled,unknown_requires_manual_check)"
            f"&order=created_at.asc&limit={bounded_limit}"
        )
        rows = await self._select_rows("orders", query)
        return [order_from_row(row) for row in rows]

    async def count_system_live_orders_created_between(
        self,
        start: datetime,
        end: datetime,
    ) -> int:
        start_time = quote(start.astimezone(UTC).isoformat(), safe="")
        end_time = quote(end.astimezone(UTC).isoformat(), safe="")
        query = (
            "select=id&mode=eq.live&status=neq.blocked"
            f"&created_at=gte.{start_time}&created_at=lt.{end_time}"
        )
        rows = await self._select_rows("orders", query)
        return len(rows)

    async def load_order_by_id(self, order_id: str) -> Order | None:
        rows = await self._select_rows(
            "orders",
            f"select=*&id=eq.{quote(order_id, safe='')}&limit=1",
        )
        if not rows:
            return None
        return order_from_row(rows[0])

    async def update_order_status(
        self,
        order_id: str,
        status: OrderStatus,
        reason: str | None,
        provider_payload_summary: dict[str, object] | None,
        provider_order_id: str | None = None,
    ) -> None:
        row: dict[str, object] = {
            "status": status,
            "reason": reason,
            "provider_payload_summary": provider_payload_summary,
            "updated_at": now_utc().astimezone(UTC).isoformat(),
        }
        if provider_order_id is not None:
            row["provider_order_id"] = provider_order_id
        response = await self.client.patch(
            f"{self.base_url}/orders?id=eq.{quote(order_id, safe='')}",
            json=row,
        )
        response.raise_for_status()

    async def record_heartbeat(self, status: str, details: dict[str, object]) -> None:
        await self._insert("worker_heartbeats", {"status": status, "details": details})

    async def record_engine_event(
        self, level: str, component: str, message: str, details: dict[str, object]
    ) -> None:
        await self._insert(
            "engine_events",
            {"level": level, "component": component, "message": message, "details": details},
        )

    async def record_api_health(
        self,
        provider: str,
        healthy: bool,
        details: dict[str, object],
    ) -> None:
        await self._insert(
            "api_health", {"provider": provider, "healthy": healthy, "details": details}
        )

    async def idempotency_key_exists(self, idempotency_key: str) -> bool:
        response = await self.client.get(
            f"{self.base_url}/orders?select=id&idempotency_key=eq."
            f"{quote(idempotency_key, safe='')}&limit=1"
        )
        response.raise_for_status()
        return bool(response.json())

    async def load_monthly_research_rows(self, period: MonthPeriod) -> MonthlyResearchRows:
        strategy = await self.load_active_strategy_version()
        start_time = quote(period.start_datetime.isoformat(), safe="")
        end_time = quote(period.end_datetime.isoformat(), safe="")
        start_date = period.start_date.isoformat()
        end_date = period.end_date.isoformat()
        decisions = await self._select_rows(
            "decision_snapshots",
            f"select=*&created_at=gte.{start_time}&created_at=lt.{end_time}&order=created_at.asc",
        )
        outcomes = await self._select_rows(
            "outcomes",
            f"select=*&created_at=gte.{start_time}&created_at=lt.{end_time}&order=created_at.asc",
        )
        orders = await self._select_rows(
            "orders",
            f"select=*&created_at=gte.{start_time}&created_at=lt.{end_time}&order=created_at.asc",
        )
        news_events = await self._select_rows(
            "news_events",
            f"select=symbol,title,source,published_at,sentiment,event_type,risk_level,"
            f"summary_short,trading_relevance,confidence,created_at"
            f"&created_at=gte.{start_time}&created_at=lt.{end_time}&order=created_at.asc",
        )
        features_daily = await self._select_rows(
            "features_daily",
            f"select=*&trade_date=gte.{start_date}&trade_date=lt.{end_date}&order=trade_date.asc",
        )
        api_health = await self._select_rows(
            "api_health",
            f"select=provider,healthy,status,latency_ms,checked_at,message,error_code"
            f"&checked_at=gte.{start_time}&checked_at=lt.{end_time}&order=checked_at.asc",
        )
        backtest_runs = await self._select_optional_rows(
            "backtest_runs",
            "select=strategy,strategy_version,period_start,period_end,total_return,cagr,"
            "max_drawdown,win_rate,turnover,blocked_reason_counts,assumptions,created_at"
            "&order=created_at.desc&limit=20",
        )
        return MonthlyResearchRows(
            base_strategy_version_id=strategy.id if strategy is not None else None,
            base_strategy_version=(
                strategy.version if strategy is not None else PAPER_STRATEGY_VERSION
            ),
            decisions=decisions,
            outcomes=outcomes,
            orders=orders,
            news_events=news_events,
            features_daily=features_daily,
            api_health=api_health,
            backtest_runs=backtest_runs,
        )

    async def persist_ai_upgrade_candidate(self, candidate: AIUpgradeCandidate) -> None:
        await self._insert("ai_upgrade_candidates", ai_candidate_to_row(candidate))

    async def _select_rows(self, table: str, query: str) -> list[JsonObject]:
        response = await self.client.get(f"{self.base_url}/{table}?{query}")
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list):
            return []
        return [json_object(row) for row in rows]

    async def _select_optional_rows(self, table: str, query: str) -> list[JsonObject]:
        try:
            return await self._select_rows(table, query)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {400, 404}:
                return []
            raise

    async def upsert_watchlist_demo(self) -> None:
        await self._upsert(
            "watchlist",
            {
                "symbol": "005930",
                "market": "KR",
                "name": "삼성전자",
                "sector": "반도체",
                "enabled": True,
                "notes": "paper trading demo seed",
            },
            "symbol,market",
        )

    async def upsert_strategy_v1(self) -> None:
        await self._upsert(
            "strategy_versions",
            {
                "version": PAPER_STRATEGY_VERSION,
                "version_name": PAPER_STRATEGY_VERSION,
                "status": "paper",
                "strategy_type": "WeightedFactorStrategyV1",
                "weights": {
                    "technical": 0.35,
                    "fundamental": 0.25,
                    "market_sector": 0.15,
                    "news_event": 0.15,
                    "portfolio": 0.10,
                },
                "params": {"buy_threshold": 0.68, "sell_threshold": 0.25},
            },
            "version",
        )

    async def _upsert(self, table: str, row: Mapping[str, object], on_conflict: str) -> None:
        headers = self.headers | {"prefer": "resolution=merge-duplicates,return=minimal"}
        response = await self.client.post(
            f"{self.base_url}/{table}?on_conflict={on_conflict}",
            json=row,
            headers=headers,
        )
        response.raise_for_status()

    async def aclose(self) -> None:
        await self.client.aclose()
