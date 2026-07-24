from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol
from uuid import UUID

from app.domain.common.errors import KnownFailClosedError
from app.domain.market_data.point_in_time_calendar import (
    PointInTimeKrDailySessionV1,
)

CalendarObservationStatus = Literal["stored", "replayed"]

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class CalendarObservationStoreError(KnownFailClosedError):
    def __init__(self, safe_message: str) -> None:
        super().__init__("calendar_observation_store", safe_message)


@dataclass(frozen=True, slots=True)
class CalendarObservationWriteReceipt:
    status: CalendarObservationStatus
    calendar_idempotency_key: str
    canonical_evidence_sha256: str
    revision: int
    revision_inserted: bool
    occurrence_id: UUID
    occurrence_inserted: bool
    observed_at: datetime

    def __post_init__(self) -> None:
        if type(self.status) is not str or self.status not in {
            "stored",
            "replayed",
        }:
            raise ValueError("calendar_observation_receipt_status_invalid")
        if (
            type(self.calendar_idempotency_key) is not str
            or _SHA256.fullmatch(self.calendar_idempotency_key) is None
        ):
            raise ValueError("calendar_observation_receipt_identity_invalid")
        if (
            type(self.canonical_evidence_sha256) is not str
            or _SHA256.fullmatch(self.canonical_evidence_sha256) is None
        ):
            raise ValueError("calendar_observation_receipt_evidence_hash_invalid")
        if (
            type(self.revision) is not int
            or self.revision <= 0
            or type(self.revision_inserted) is not bool
            or type(self.occurrence_inserted) is not bool
        ):
            raise ValueError("calendar_observation_receipt_revision_invalid")
        if type(self.occurrence_id) is not UUID or self.occurrence_id.version != 4:
            raise ValueError("calendar_observation_receipt_occurrence_id_invalid")
        if (
            type(self.observed_at) is not datetime
            or self.observed_at.tzinfo is None
            or self.observed_at.utcoffset() != timedelta(0)
        ):
            raise ValueError("calendar_observation_receipt_observed_at_invalid")
        if self.status == "stored" and not (
            self.revision_inserted and self.occurrence_inserted
        ):
            raise ValueError("calendar_observation_receipt_status_invalid")
        if self.status == "replayed" and self.revision_inserted:
            raise ValueError("calendar_observation_receipt_status_invalid")


class CalendarObservationStorePort(Protocol):
    async def append_observation(
        self,
        session: PointInTimeKrDailySessionV1,
    ) -> CalendarObservationWriteReceipt:
        ...
