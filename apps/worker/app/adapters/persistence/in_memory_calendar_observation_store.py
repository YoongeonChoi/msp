from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.application.ports.calendar_observation_store_port import (
    CalendarObservationStatus,
    CalendarObservationStoreError,
    CalendarObservationWriteReceipt,
)
from app.domain.market_data.point_in_time_calendar import (
    PointInTimeCalendarError,
    PointInTimeKrDailySessionV1,
)


@dataclass(frozen=True, slots=True)
class StoredCalendarContentRevision:
    revision: int
    session: PointInTimeKrDailySessionV1


@dataclass(frozen=True, slots=True)
class StoredCalendarObservationOccurrence:
    occurrence_id: UUID
    revision: int
    session: PointInTimeKrDailySessionV1

    @property
    def observed_at(self) -> datetime:
        return self.session.observed_at


class InMemoryCalendarObservationStore:
    """Model durable calendar revision and occurrence semantics in memory.

    This adapter is intended for isolated tests. It is deliberately absent from
    the runtime container and does not make calendar evidence research-ready.
    """

    def __init__(self) -> None:
        self._revisions: dict[str, list[StoredCalendarContentRevision]] = {}
        self._occurrences: dict[
            str, list[StoredCalendarObservationOccurrence]
        ] = {}

    async def append_observation(
        self,
        session: PointInTimeKrDailySessionV1,
    ) -> CalendarObservationWriteReceipt:
        canonical = _canonical_session(session)
        identity = canonical.idempotency_key
        revisions = self._revisions.get(identity)
        occurrences = self._occurrences.get(identity)

        if revisions is None or occurrences is None:
            if revisions is not None or occurrences is not None:
                raise CalendarObservationStoreError(
                    "calendar_observation_store_internal_state_invalid"
                )
            revisions = []
            occurrences = []
            self._revisions[identity] = revisions
            self._occurrences[identity] = occurrences
            revision = self._append_revision(revisions, canonical)
            occurrence = self._append_occurrence(
                occurrences,
                revision,
                canonical,
            )
            return _receipt(
                revision,
                occurrence,
                status="stored",
                revision_inserted=True,
                occurrence_inserted=True,
            )

        if not revisions or not occurrences:
            raise CalendarObservationStoreError(
                "calendar_observation_store_internal_state_invalid"
            )

        exact_occurrence = next(
            (
                stored
                for stored in reversed(occurrences)
                if stored.observed_at == canonical.observed_at
            ),
            None,
        )
        if exact_occurrence is not None:
            if (
                exact_occurrence.session.to_payload()
                != canonical.to_payload()
            ):
                raise CalendarObservationStoreError(
                    "pit_calendar_revision_time_not_increasing"
                )
            revision = revisions[exact_occurrence.revision - 1]
            return _receipt(
                revision,
                exact_occurrence,
                status="replayed",
                revision_inserted=False,
                occurrence_inserted=False,
            )

        last_occurrence = occurrences[-1]
        if canonical.observed_at < last_occurrence.observed_at:
            raise CalendarObservationStoreError(
                "pit_calendar_observation_time_regressed"
            )

        latest_revision = revisions[-1]
        if (
            canonical.canonical_evidence_sha256
            == latest_revision.session.canonical_evidence_sha256
        ):
            occurrence = self._append_occurrence(
                occurrences,
                latest_revision,
                canonical,
            )
            return _receipt(
                latest_revision,
                occurrence,
                status="replayed",
                revision_inserted=False,
                occurrence_inserted=True,
            )

        if any(
            historical.session.canonical_evidence_sha256
            == canonical.canonical_evidence_sha256
            for historical in revisions[:-1]
        ):
            raise CalendarObservationStoreError(
                "pit_calendar_historical_hash_recurrence_ambiguous"
            )

        revision = self._append_revision(revisions, canonical)
        occurrence = self._append_occurrence(
            occurrences,
            revision,
            canonical,
        )
        return _receipt(
            revision,
            occurrence,
            status="stored",
            revision_inserted=True,
            occurrence_inserted=True,
        )

    def revisions_for(
        self,
        calendar_idempotency_key: str,
    ) -> tuple[StoredCalendarContentRevision, ...]:
        return tuple(
            StoredCalendarContentRevision(
                revision=item.revision,
                session=PointInTimeKrDailySessionV1.from_payload(
                    item.session.to_payload()
                ),
            )
            for item in self._revisions.get(calendar_idempotency_key, ())
        )

    def occurrences_for(
        self,
        calendar_idempotency_key: str,
    ) -> tuple[StoredCalendarObservationOccurrence, ...]:
        return tuple(
            StoredCalendarObservationOccurrence(
                occurrence_id=item.occurrence_id,
                revision=item.revision,
                session=PointInTimeKrDailySessionV1.from_payload(
                    item.session.to_payload()
                ),
            )
            for item in self._occurrences.get(calendar_idempotency_key, ())
        )

    @staticmethod
    def _append_revision(
        revisions: list[StoredCalendarContentRevision],
        session: PointInTimeKrDailySessionV1,
    ) -> StoredCalendarContentRevision:
        stored = StoredCalendarContentRevision(
            revision=len(revisions) + 1,
            session=session,
        )
        revisions.append(stored)
        return stored

    @staticmethod
    def _append_occurrence(
        occurrences: list[StoredCalendarObservationOccurrence],
        revision: StoredCalendarContentRevision,
        session: PointInTimeKrDailySessionV1,
    ) -> StoredCalendarObservationOccurrence:
        stored = StoredCalendarObservationOccurrence(
            occurrence_id=uuid4(),
            revision=revision.revision,
            session=session,
        )
        occurrences.append(stored)
        return stored


def _canonical_session(value: object) -> PointInTimeKrDailySessionV1:
    failed = False
    canonical: PointInTimeKrDailySessionV1 | None = None
    if type(value) is not PointInTimeKrDailySessionV1:
        failed = True
    else:
        try:
            canonical = PointInTimeKrDailySessionV1.from_payload(
                value.to_payload()
            )
        except (
            AttributeError,
            OverflowError,
            RuntimeError,
            TypeError,
            ValueError,
            PointInTimeCalendarError,
        ):
            failed = True
    if failed or canonical is None or canonical != value:
        raise CalendarObservationStoreError(
            "calendar_observation_store_item_invalid"
        )
    return canonical


def _receipt(
    revision: StoredCalendarContentRevision,
    occurrence: StoredCalendarObservationOccurrence,
    *,
    status: CalendarObservationStatus,
    revision_inserted: bool,
    occurrence_inserted: bool,
) -> CalendarObservationWriteReceipt:
    return CalendarObservationWriteReceipt(
        status=status,
        calendar_idempotency_key=revision.session.idempotency_key,
        canonical_evidence_sha256=(
            revision.session.canonical_evidence_sha256
        ),
        revision=revision.revision,
        revision_inserted=revision_inserted,
        occurrence_id=occurrence.occurrence_id,
        occurrence_inserted=occurrence_inserted,
        observed_at=occurrence.observed_at.astimezone(UTC),
    )
