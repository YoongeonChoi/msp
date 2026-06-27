from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import quote

import httpx

from app.application.ports.backtest_port import BacktestRows
from app.config import Settings
from app.domain.common.json import JsonObject, json_object


class SupabaseBacktestRepository:
    def __init__(self, settings: Settings) -> None:
        if not settings.supabase_url or not settings.supabase_secret_key:
            raise ValueError("Backtest requires SUPABASE_URL and SUPABASE_SECRET_KEY")
        secret = settings.supabase_secret_key.get_secret_value()
        self.base_url = settings.supabase_url.rstrip("/") + "/rest/v1"
        self.headers = {
            "apikey": secret,
            "authorization": "Bearer " + secret,
            "content-type": "application/json",
            "prefer": "return=minimal",
        }
        self.client = httpx.AsyncClient(timeout=10.0, headers=self.headers)

    async def load_backtest_rows(self, strategy: str, start: date, end: date) -> BacktestRows:
        strategy_query = quote(strategy, safe="")
        start_date = quote(start.isoformat(), safe="")
        end_date = quote(end.isoformat(), safe="")
        end_exclusive = quote((end + timedelta(days=1)).isoformat(), safe="")
        return BacktestRows(
            strategy=_first_row(
                await self._select_rows(
                    "strategy_versions",
                    "select=*&version=eq."
                    f"{strategy_query}&status=in.(paper,active,draft)&order=created_at.desc&limit=1",
                )
            ),
            features_daily=await self._select_rows(
                "features_daily",
                f"select=*&trade_date=gte.{start_date}&trade_date=lte.{end_date}&order=trade_date.asc",
            ),
            fundamentals_quarterly=await self._select_rows(
                "fundamentals_quarterly",
                "select=*&order=updated_at.desc&limit=5000",
            ),
            news_events=await self._select_rows(
                "news_events",
                f"select=symbol,sentiment,risk_level,trading_relevance,confidence,created_at"
                f"&created_at=gte.{start_date}&created_at=lt.{end_exclusive}"
                f"&order=created_at.asc&limit=5000",
            ),
            watchlist=await self._select_rows(
                "watchlist", "select=symbol,sector,enabled,target_sell_krw,stop_loss_pct"
            ),
        )

    async def save_backtest_result(self, result: JsonObject) -> None:
        response = await self.client.post(f"{self.base_url}/backtest_runs", json=result)
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


def _first_row(rows: list[JsonObject]) -> JsonObject | None:
    return rows[0] if rows else None
