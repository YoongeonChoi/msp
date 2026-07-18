from __future__ import annotations

from datetime import date
from typing import Protocol

from app.domain.market_data.point_in_time_calendar import (
    PointInTimeKrDailySessionV1,
)


class KrDailySessionSourcePort(Protocol):
    async def get_kr_daily_session_evidence(
        self,
        target_date: date,
    ) -> PointInTimeKrDailySessionV1: ...
