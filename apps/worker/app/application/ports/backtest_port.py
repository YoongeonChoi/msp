from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol

from app.domain.common.json import JsonObject


@dataclass(frozen=True, slots=True)
class BacktestRows:
    strategy: JsonObject | None
    features_daily: list[JsonObject]
    fundamentals_quarterly: list[JsonObject]
    news_events: list[JsonObject]
    watchlist: list[JsonObject]


class BacktestRepositoryPort(Protocol):
    async def load_backtest_rows(self, strategy: str, start: date, end: date) -> BacktestRows:
        ...

    async def save_backtest_result(self, result: JsonObject) -> None:
        ...
