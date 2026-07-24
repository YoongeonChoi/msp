from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from app.domain.common.errors import KnownFailClosedError
from app.domain.market_data.daily_candle_timing import (
    PointInTimeDailyCandleTimingEvidenceV1,
)
from app.domain.market_data.point_in_time_calendar import (
    PointInTimeKrDailySessionV1,
)


class DailyCandleTimingStoreError(KnownFailClosedError):
    def __init__(self, safe_message: str) -> None:
        super().__init__("daily_candle_timing_store", safe_message)


type DailyCandleTimingWriteStatus = Literal["stored", "replayed"]


@dataclass(frozen=True, slots=True)
class DailyCandleTimingWriteReceipt:
    status: DailyCandleTimingWriteStatus
    request_idempotency_key: str
    timing_idempotency_key: str
    canonical_timing_evidence_sha256: str
    calendar_revision: int
    timing_revision: int
    calendar_inserted: bool
    timing_inserted: bool
    evidence_available_at: datetime
    quarantine_id: None
    reason_code: None


class DailyCandleTimingStorePort(Protocol):
    async def append_timing_evidence(
        self,
        request_idempotency_key: str,
        calendar: PointInTimeKrDailySessionV1,
        timing: PointInTimeDailyCandleTimingEvidenceV1,
    ) -> DailyCandleTimingWriteReceipt:
        ...
