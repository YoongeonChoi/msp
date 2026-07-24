from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Literal

from app.application.ports.calendar_observation_store_port import (
    CalendarObservationStorePort,
    CalendarObservationWriteReceipt,
)
from app.application.ports.kr_daily_session_collection_port import (
    CollectedKrDailySessionObservationV1,
)
from app.application.ports.kr_daily_session_source_port import (
    KrDailySessionSourcePort,
)
from app.domain.common.errors import KnownFailClosedError
from app.domain.common.time import now_utc
from app.domain.market_data.point_in_time_calendar import (
    PointInTimeKrDailySessionV1,
)

KrDailySessionCollectionWriteOutcome = Literal["not_attempted", "unknown"]


class KrDailySessionCollectionError(KnownFailClosedError):
    write_outcome: KrDailySessionCollectionWriteOutcome

    def __init__(
        self,
        safe_message: str,
        *,
        write_outcome: KrDailySessionCollectionWriteOutcome = "not_attempted",
    ) -> None:
        super().__init__("kr_daily_session_collection", safe_message)
        self.write_outcome = write_outcome


class CollectKrDailySessionObservation:
    def __init__(
        self,
        source: KrDailySessionSourcePort,
        store: CalendarObservationStorePort,
        *,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        self.source = source
        self.store = store
        self.clock = clock

    async def execute(
        self,
        target_date: date,
    ) -> CalendarObservationWriteReceipt:
        collected = await self.execute_with_evidence(target_date)
        return collected.receipt

    async def execute_with_evidence(
        self,
        target_date: date,
    ) -> CollectedKrDailySessionObservationV1:
        valid_target_date = _target_date(target_date)
        started_at = _read_clock(self.clock)

        try:
            source_session = await self.source.get_kr_daily_session_evidence(valid_target_date)
        except Exception:
            pass
        else:
            completed_at = _read_clock(self.clock)
            if completed_at < started_at:
                raise KrDailySessionCollectionError(
                    "kr_daily_session_collection_clock_moved_backwards"
                )
            session = _source_session(
                source_session,
                target_date=valid_target_date,
                started_at=started_at,
                completed_at=completed_at,
            )
            receipt = await self._append(session)
            try:
                collected = CollectedKrDailySessionObservationV1(
                    session=session,
                    receipt=receipt,
                )
            except Exception:
                pass
            else:
                return collected

            raise KrDailySessionCollectionError(
                "kr_daily_session_collection_store_outcome_unknown",
                write_outcome="unknown",
            )

        raise KrDailySessionCollectionError("kr_daily_session_collection_source_failed")

    async def _append(
        self,
        session: PointInTimeKrDailySessionV1,
    ) -> CalendarObservationWriteReceipt:
        try:
            source_receipt = await self.store.append_observation(session)
            receipt = _store_receipt(source_receipt, session=session)
        except Exception:
            pass
        else:
            return receipt

        raise KrDailySessionCollectionError(
            "kr_daily_session_collection_store_outcome_unknown",
            write_outcome="unknown",
        )


def _target_date(value: object) -> date:
    if type(value) is not date:
        raise KrDailySessionCollectionError("kr_daily_session_collection_target_date_invalid")
    return value


def _read_clock(clock: Callable[[], datetime]) -> datetime:
    try:
        value = clock()
        if type(value) is datetime and value.tzinfo is not None and value.utcoffset() is not None:
            return value.astimezone(UTC)
    except Exception:
        pass
    raise KrDailySessionCollectionError("kr_daily_session_collection_clock_invalid")


def _source_session(
    value: object,
    *,
    target_date: date,
    started_at: datetime,
    completed_at: datetime,
) -> PointInTimeKrDailySessionV1:
    try:
        if type(value) is PointInTimeKrDailySessionV1:
            canonical = PointInTimeKrDailySessionV1.from_payload(value.to_payload())
            observed_at = canonical.observed_at.astimezone(UTC)
            if canonical == value:
                if canonical.session_date != target_date:
                    raise KrDailySessionCollectionError(
                        "kr_daily_session_collection_session_date_mismatch"
                    )
                if not started_at <= observed_at <= completed_at:
                    raise KrDailySessionCollectionError(
                        "kr_daily_session_collection_observed_at_outside_read"
                    )
                return canonical
    except KrDailySessionCollectionError:
        raise
    except Exception:
        pass
    raise KrDailySessionCollectionError("kr_daily_session_collection_source_evidence_invalid")


def _store_receipt(
    value: object,
    *,
    session: PointInTimeKrDailySessionV1,
) -> CalendarObservationWriteReceipt:
    try:
        if type(value) is CalendarObservationWriteReceipt:
            canonical = CalendarObservationWriteReceipt(
                status=value.status,
                calendar_idempotency_key=value.calendar_idempotency_key,
                canonical_evidence_sha256=value.canonical_evidence_sha256,
                revision=value.revision,
                revision_inserted=value.revision_inserted,
                occurrence_id=value.occurrence_id,
                occurrence_inserted=value.occurrence_inserted,
                observed_at=value.observed_at,
            )
            if canonical == value:
                if (
                    canonical.calendar_idempotency_key != session.idempotency_key
                    or canonical.canonical_evidence_sha256 != session.canonical_evidence_sha256
                    or canonical.observed_at != session.observed_at
                ):
                    raise KrDailySessionCollectionError(
                        "kr_daily_session_collection_store_receipt_mismatch"
                    )
                return canonical
    except KrDailySessionCollectionError:
        raise
    except Exception:
        pass
    raise KrDailySessionCollectionError("kr_daily_session_collection_store_receipt_invalid")
