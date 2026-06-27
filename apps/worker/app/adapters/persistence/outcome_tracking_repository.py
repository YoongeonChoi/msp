from __future__ import annotations

from urllib.parse import quote

import httpx

from app.application.ports.outcome_tracking_port import OutcomeTrackingRows
from app.config import Settings
from app.domain.common.json import JsonObject, JsonValue, json_object


class SupabaseOutcomeTrackingRepository:
    def __init__(self, settings: Settings) -> None:
        if not settings.supabase_url or not settings.supabase_secret_key:
            raise ValueError("Outcome tracking requires SUPABASE_URL and SUPABASE_SECRET_KEY")
        secret = settings.supabase_secret_key.get_secret_value()
        self.base_url = settings.supabase_url.rstrip("/") + "/rest/v1"
        self.headers = {
            "apikey": secret,
            "authorization": "Bearer " + secret,
            "content-type": "application/json",
            "prefer": "return=minimal",
        }
        self.client = httpx.AsyncClient(timeout=10.0, headers=self.headers)

    async def load_outcome_tracking_rows(
        self, decision_limit: int, price_limit: int
    ) -> OutcomeTrackingRows:
        decisions = await self._select_rows(
            "decision_snapshots",
            f"select=*&order=created_at.desc&limit={decision_limit}",
        )
        symbols = _symbols(decisions)
        earliest_date = _earliest_decision_date(decisions)
        return OutcomeTrackingRows(
            decisions=decisions,
            orders=await self._select_rows(
                "orders",
                f"select=*&order=created_at.desc&limit={decision_limit * 3}",
            ),
            features_daily=await self._select_features(symbols, earliest_date, price_limit),
            existing_outcomes=await self._select_rows(
                "outcomes",
                f"select=*&order=updated_at.desc&limit={decision_limit}",
            ),
        )

    async def upsert_outcome(self, outcome: JsonObject) -> None:
        headers = self.headers | {"prefer": "resolution=merge-duplicates,return=minimal"}
        response = await self.client.post(
            f"{self.base_url}/outcomes?on_conflict=decision_id",
            json=outcome,
            headers=headers,
        )
        response.raise_for_status()

    async def _select_features(
        self, symbols: list[str], earliest_date: str | None, price_limit: int
    ) -> list[JsonObject]:
        if not symbols or earliest_date is None:
            return []
        symbol_filter = ",".join(quote(symbol, safe="") for symbol in symbols)
        return await self._select_rows(
            "features_daily",
            f"select=*&symbol=in.({symbol_filter})"
            f"&trade_date=gte.{quote(earliest_date, safe='')}"
            f"&order=trade_date.asc"
            f"&limit={price_limit}",
        )

    async def _select_rows(self, table: str, query: str) -> list[JsonObject]:
        response = await self.client.get(f"{self.base_url}/{table}?{query}")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            return []
        return [json_object(row) for row in payload]

    async def aclose(self) -> None:
        await self.client.aclose()


def _symbols(rows: list[JsonObject]) -> list[str]:
    return sorted({value for row in rows if isinstance(value := row.get("symbol"), str)})


def _earliest_decision_date(rows: list[JsonObject]) -> str | None:
    dates = [_date_prefix(row.get("decided_at") or row.get("created_at")) for row in rows]
    clean_dates = [item for item in dates if item is not None]
    return min(clean_dates) if clean_dates else None


def _date_prefix(value: JsonValue | None) -> str | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    return value[:10]
