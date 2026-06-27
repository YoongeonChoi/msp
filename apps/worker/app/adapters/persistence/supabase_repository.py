from __future__ import annotations

from uuid import UUID

import httpx

from app.adapters.persistence.models import decision_to_row, order_to_row
from app.config import Settings
from app.domain.risk.entities import RiskResult
from app.domain.trading.entities import BotSettings, DecisionSnapshot, Order


class SupabaseRepository:
    def __init__(self, settings: Settings) -> None:
        if not settings.supabase_url or not settings.supabase_secret_key:
            raise ValueError("Supabase repository requires SUPABASE_URL and SUPABASE_SECRET_KEY")
        self.base_url = settings.supabase_url.rstrip("/") + "/rest/v1"
        self.headers = {
            "apikey": settings.supabase_secret_key.get_secret_value(),
            "authorization": "Bearer " + settings.supabase_secret_key.get_secret_value(),
            "content-type": "application/json",
            "prefer": "return=minimal",
        }
        self.client = httpx.AsyncClient(timeout=10.0, headers=self.headers)

    async def _insert(self, table: str, row: dict[str, object]) -> None:
        response = await self.client.post(f"{self.base_url}/{table}", json=row)
        response.raise_for_status()

    async def load_bot_settings(self) -> BotSettings:
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

    async def load_active_strategy_version_id(self) -> UUID:
        response = await self.client.get(
            f"{self.base_url}/strategy_versions?select=id&status=eq.active&limit=1"
        )
        response.raise_for_status()
        rows = response.json()
        if not rows:
            raise ValueError("No active strategy version")
        return UUID(str(rows[0]["id"]))

    async def persist_decision_snapshot(self, snapshot: DecisionSnapshot) -> None:
        await self._insert("decision_snapshots", decision_to_row(snapshot))

    async def persist_order(self, order: Order, risk_result: RiskResult | None = None) -> None:
        await self._insert("orders", order_to_row(order, risk_result))

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
            f"{self.base_url}/orders?select=id&idempotency_key=eq.{idempotency_key}&limit=1"
        )
        response.raise_for_status()
        return bool(response.json())
