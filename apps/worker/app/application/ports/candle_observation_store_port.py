from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.domain.common.errors import KnownFailClosedError
from app.domain.market_data.point_in_time import PointInTimeCandleV1


class CandleObservationStoreError(KnownFailClosedError):
    def __init__(self, safe_message: str) -> None:
        super().__init__("candle_observation_store", safe_message)


@dataclass(frozen=True, slots=True)
class CandleObservationWriteReceipt:
    idempotency_key: str
    canonical_observation_sha256: str
    revision: int
    inserted: bool
    stored_observed_at: datetime


class CandleObservationStorePort(Protocol):
    async def append_observation(
        self,
        candle: PointInTimeCandleV1,
    ) -> CandleObservationWriteReceipt:
        ...
