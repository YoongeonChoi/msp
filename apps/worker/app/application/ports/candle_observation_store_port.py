from __future__ import annotations

import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from app.domain.common.errors import KnownFailClosedError
from app.domain.market_data.point_in_time import PointInTimeCandleV1


class CandleObservationStoreError(KnownFailClosedError):
    def __init__(self, safe_message: str) -> None:
        super().__init__("candle_observation_store", safe_message)


CandleObservationStorePersistenceKind = Literal["reference", "durable"]
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class CandleObservationWriteReceipt:
    idempotency_key: str
    canonical_observation_sha256: str
    revision: int
    inserted: bool
    stored_observed_at: datetime

    def __post_init__(self) -> None:
        valid = False
        with suppress(Exception):
            valid = (
                type(self.idempotency_key) is str
                and _SHA256_RE.fullmatch(self.idempotency_key) is not None
                and type(self.canonical_observation_sha256) is str
                and _SHA256_RE.fullmatch(self.canonical_observation_sha256) is not None
                and type(self.revision) is int
                and self.revision > 0
                and type(self.inserted) is bool
                and type(self.stored_observed_at) is datetime
                and self.stored_observed_at.tzinfo is not None
                and self.stored_observed_at.utcoffset() == timedelta(0)
            )
        if not valid:
            raise CandleObservationStoreError("candle_observation_store_receipt_invalid")
        object.__setattr__(
            self,
            "stored_observed_at",
            self.stored_observed_at.astimezone(UTC),
        )


class CandleObservationStorePort(Protocol):
    persistence_kind: CandleObservationStorePersistenceKind

    async def append_observation(
        self,
        candle: PointInTimeCandleV1,
    ) -> CandleObservationWriteReceipt: ...
