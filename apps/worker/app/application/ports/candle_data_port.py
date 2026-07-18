from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.domain.market_data.point_in_time import PointInTimeCandleV1


@dataclass(frozen=True, slots=True)
class DailyCandleReadRequest:
    symbol: str
    before: datetime
    count: int = 100
    adjusted: bool = True


@dataclass(frozen=True, slots=True)
class DailyCandleReadPage:
    candles: tuple[PointInTimeCandleV1, ...]
    next_before: datetime | None
    observed_at: datetime


class DailyCandleSourcePort(Protocol):
    async def read_daily_candle_page(
        self,
        request: DailyCandleReadRequest,
    ) -> DailyCandleReadPage:
        ...
