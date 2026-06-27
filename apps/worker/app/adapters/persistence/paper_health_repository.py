from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from urllib.parse import quote

import httpx

from app.application.ports.paper_health_port import PaperHealthRows
from app.config import Settings
from app.domain.common.json import JsonObject, JsonValue, json_object
from app.domain.trading.entities import BotSettings


class SupabasePaperHealthRepository:
    def __init__(self, settings: Settings) -> None:
        if not settings.supabase_url or not settings.supabase_secret_key:
            raise ValueError("Supabase paper health requires SUPABASE_URL and SUPABASE_SECRET_KEY")
        secret = settings.supabase_secret_key.get_secret_value()
        self.base_url = settings.supabase_url.rstrip("/") + "/rest/v1"
        self.headers = {
            "apikey": secret,
            "authorization": "Bearer " + secret,
            "content-type": "application/json",
            "prefer": "return=minimal",
        }
        self.client = httpx.AsyncClient(timeout=10.0, headers=self.headers)

    async def load_bot_settings(self) -> BotSettings:
        rows = await self._select_rows("bot_settings", "select=*&id=eq.singleton&limit=1")
        if not rows:
            return BotSettings()
        row = rows[0]
        return BotSettings(
            enabled=_bool(row.get("enabled")),
            mode="live" if row.get("mode") == "live" else "paper",
            live_order_allowed=_bool(row.get("live_order_allowed")),
            max_order_amount_krw=_int(row, "max_order_amount_krw", 100_000),
            max_daily_loss_pct=_float(row, "max_daily_loss_pct", 0.02),
            max_daily_order_count=_int(row, "max_daily_order_count", 10),
            max_position_pct=_float(row, "max_position_pct", 0.10),
            max_sector_pct=_float(row, "max_sector_pct", 0.30),
            loop_interval_sec=_int(row, "loop_interval_sec", 30),
        )

    async def load_paper_health_rows(self, since: datetime) -> PaperHealthRows:
        since_time = quote(since.isoformat(), safe="")
        return PaperHealthRows(
            latest_heartbeats=await self._select_rows(
                "worker_heartbeats", "select=*&order=created_at.desc&limit=1"
            ),
            api_health=await self._select_rows(
                "api_health", "select=*&order=checked_at.desc&limit=100"
            ),
            decisions_last_24h=await self._select_rows(
                "decision_snapshots",
                f"select=*&created_at=gte.{since_time}&order=created_at.desc",
            ),
            orders_last_24h=await self._select_rows(
                "orders", f"select=*&created_at=gte.{since_time}&order=created_at.desc"
            ),
            live_like_orders=await self._select_rows(
                "orders",
                "select=id,symbol,mode,status,created_at"
                "&status=in.(sent,filled,partial_filled)"
                "&order=created_at.desc"
                "&limit=1000",
            ),
            order_key_rows=await self._select_rows(
                "orders",
                "select=id,idempotency_key,status,created_at&order=created_at.desc&limit=5000",
            ),
            recent_engine_events=await self._select_rows(
                "engine_events",
                f"select=level,component,message,created_at"
                f"&level=in.(error,critical)"
                f"&created_at=gte.{since_time}"
                f"&order=created_at.desc"
                f"&limit=20",
            ),
            outcomes_last_24h=await self._select_rows(
                "outcomes",
                f"select=id,order_id,decision_id,created_at"
                f"&created_at=gte.{since_time}"
                f"&order=created_at.desc"
                f"&limit=1000",
            ),
            db_size_bytes=_first_db_size(
                await self._select_rows(
                    "retention_runs", "select=db_size_bytes&order=started_at.desc&limit=10"
                )
            ),
        )

    async def record_engine_event(
        self, level: str, component: str, message: str, details: dict[str, object]
    ) -> None:
        response = await self.client.post(
            f"{self.base_url}/engine_events",
            json={"level": level, "component": component, "message": message, "details": details},
        )
        response.raise_for_status()

    async def _select_rows(self, table: str, query: str) -> list[JsonObject]:
        response = await self.client.get(f"{self.base_url}/{table}?{query}")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            return []
        return [json_object(row) for row in payload]

    async def aclose(self) -> None:
        await self.client.aclose()


def _first_db_size(rows: list[JsonObject]) -> int | None:
    for row in rows:
        value = row.get("db_size_bytes")
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdecimal():
            return int(value)
    return None


def _bool(value: JsonValue | None) -> bool:
    return value is True


def _int(row: Mapping[str, JsonValue], key: str, default: int) -> int:
    value = row.get(key)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return default


def _float(row: Mapping[str, JsonValue], key: str, default: float) -> float:
    value = row.get(key)
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default
