from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Protocol
from uuid import UUID

from app.application.ports.calendar_observation_store_port import (
    CalendarObservationWriteReceipt,
)
from app.domain.common.errors import KnownFailClosedError
from app.domain.market_data.point_in_time_calendar import (
    PointInTimeKrDailySessionV1,
)


class KrDailySessionCollectionEvidenceError(KnownFailClosedError):
    def __init__(self, safe_message: str) -> None:
        super().__init__("kr_daily_session_collection_evidence", safe_message)


@dataclass(frozen=True, slots=True)
class CollectedKrDailySessionObservationV1:
    """Canonical evidence for one successfully persisted calendar observation."""

    session: PointInTimeKrDailySessionV1
    receipt: CalendarObservationWriteReceipt

    def __post_init__(self) -> None:
        canonical_session = _canonical_session(self.session)
        canonical_receipt = _canonical_receipt(self.receipt)
        if (
            canonical_receipt.calendar_idempotency_key != canonical_session.idempotency_key
            or canonical_receipt.canonical_evidence_sha256
            != canonical_session.canonical_evidence_sha256
            or canonical_receipt.observed_at != canonical_session.observed_at
        ):
            raise KrDailySessionCollectionEvidenceError(
                "kr_daily_session_collection_evidence_receipt_mismatch"
            )
        object.__setattr__(self, "session", canonical_session)
        object.__setattr__(self, "receipt", canonical_receipt)

    @property
    def target_date(self) -> date:
        return self.session.session_date


class KrDailySessionCollectorPort(Protocol):
    async def execute_with_evidence(
        self,
        target_date: date,
    ) -> CollectedKrDailySessionObservationV1: ...


def canonical_collected_kr_daily_session_observation(
    value: object,
) -> CollectedKrDailySessionObservationV1:
    if type(value) is not CollectedKrDailySessionObservationV1:
        raise KrDailySessionCollectionEvidenceError("kr_daily_session_collection_evidence_invalid")
    canonical: CollectedKrDailySessionObservationV1 | None = None
    with suppress(Exception):
        canonical = CollectedKrDailySessionObservationV1(
            session=value.session,
            receipt=value.receipt,
        )
    if canonical is None or canonical != value:
        raise KrDailySessionCollectionEvidenceError("kr_daily_session_collection_evidence_invalid")
    return canonical


def _canonical_session(value: object) -> PointInTimeKrDailySessionV1:
    if type(value) is not PointInTimeKrDailySessionV1:
        raise KrDailySessionCollectionEvidenceError(
            "kr_daily_session_collection_evidence_session_invalid"
        )
    canonical: PointInTimeKrDailySessionV1 | None = None
    with suppress(Exception):
        canonical = PointInTimeKrDailySessionV1.from_payload(value.to_payload())
    if canonical is None or canonical != value:
        raise KrDailySessionCollectionEvidenceError(
            "kr_daily_session_collection_evidence_session_invalid"
        )
    return canonical


def _canonical_receipt(value: object) -> CalendarObservationWriteReceipt:
    if type(value) is not CalendarObservationWriteReceipt:
        raise KrDailySessionCollectionEvidenceError(
            "kr_daily_session_collection_evidence_receipt_invalid"
        )
    canonical: CalendarObservationWriteReceipt | None = None
    with suppress(Exception):
        canonical = CalendarObservationWriteReceipt(
            status=value.status,
            calendar_idempotency_key=value.calendar_idempotency_key,
            canonical_evidence_sha256=value.canonical_evidence_sha256,
            revision=value.revision,
            revision_inserted=value.revision_inserted,
            occurrence_id=UUID(str(value.occurrence_id)),
            occurrence_inserted=value.occurrence_inserted,
            observed_at=_utc(value.observed_at),
        )
    if canonical is None or canonical != value:
        raise KrDailySessionCollectionEvidenceError(
            "kr_daily_session_collection_evidence_receipt_invalid"
        )
    return canonical


def _utc(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None:
        raise ValueError("datetime_invalid")
    if value.utcoffset() is None:
        raise ValueError("datetime_invalid")
    return value.astimezone(UTC)
