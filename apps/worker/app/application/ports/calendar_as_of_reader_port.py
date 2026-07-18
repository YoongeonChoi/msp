from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Protocol
from uuid import UUID

from app.domain.common.errors import KnownFailClosedError
from app.domain.common.json import JsonObject
from app.domain.market_data.calendar_as_of import (
    SelectedPointInTimeKrDailySessionV1,
)

_PROVIDER_RE = re.compile(r"[a-z][a-z0-9._-]{0,63}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SNAPSHOT_TOKEN_RE = re.compile(r"[0-9]+:[0-9]+:(?:[0-9]+(?:,[0-9]+)*)?")
_ORIGINS = frozenset({"content_revision_backfill", "stream_head_recovery", "rpc"})
PIT_CALENDAR_AS_OF_READER_SCHEMA_VERSION = "pit_calendar_as_of_reader.v1"


class CalendarAsOfReaderError(KnownFailClosedError):
    def __init__(self, safe_message: str) -> None:
        super().__init__("calendar_as_of_reader", safe_message)


@dataclass(frozen=True, slots=True)
class CalendarAsOfReadRequest:
    provider: str
    market: str
    start_session_date: date
    end_session_date: date
    as_of: datetime
    page_size: int = 100

    def __post_init__(self) -> None:
        if (
            type(self.provider) is not str
            or _PROVIDER_RE.fullmatch(self.provider) is None
        ):
            raise CalendarAsOfReaderError(
                "calendar_as_of_reader_provider_invalid"
            )
        if type(self.market) is not str or self.market != "KR":
            raise CalendarAsOfReaderError("calendar_as_of_reader_market_invalid")
        if (
            type(self.start_session_date) is not date
            or type(self.end_session_date) is not date
            or self.start_session_date > self.end_session_date
        ):
            raise CalendarAsOfReaderError(
                "calendar_as_of_reader_date_range_invalid"
            )
        if (self.end_session_date - self.start_session_date).days >= 366:
            raise CalendarAsOfReaderError(
                "calendar_as_of_reader_date_range_too_large"
            )
        _request_as_of(self.as_of)
        if type(self.page_size) is not int or not 25 <= self.page_size <= 100:
            raise CalendarAsOfReaderError(
                "calendar_as_of_reader_page_size_invalid"
            )


@dataclass(frozen=True, slots=True)
class CalendarAsOfLineageV1:
    calendar_revision_id: str
    calendar_idempotency_key: str
    calendar_revision: int
    calendar_canonical_evidence_sha256: str
    calendar_revision_observed_at: datetime
    calendar_revision_received_at: datetime
    calendar_occurrence_id: str
    calendar_occurrence_observed_at: datetime
    calendar_occurrence_received_at: datetime
    calendar_occurrence_origin: str

    def __post_init__(self) -> None:
        _require_canonical_uuid(self.calendar_revision_id)
        _require_canonical_uuid(self.calendar_occurrence_id)
        _require_sha256(self.calendar_idempotency_key)
        _require_sha256(self.calendar_canonical_evidence_sha256)
        if type(self.calendar_revision) is not int or self.calendar_revision <= 0:
            raise CalendarAsOfReaderError(
                "calendar_as_of_reader_lineage_invalid"
            )
        for timestamp in (
            self.calendar_revision_observed_at,
            self.calendar_revision_received_at,
            self.calendar_occurrence_observed_at,
            self.calendar_occurrence_received_at,
        ):
            _require_aware_datetime(timestamp)
        if (
            type(self.calendar_occurrence_origin) is not str
            or self.calendar_occurrence_origin not in _ORIGINS
            or self.calendar_revision_observed_at
            > self.calendar_occurrence_observed_at
            or self.calendar_revision_observed_at
            > self.calendar_revision_received_at
            or self.calendar_revision_received_at
            > self.calendar_occurrence_received_at
            or self.calendar_occurrence_observed_at
            > self.calendar_occurrence_received_at
        ):
            raise CalendarAsOfReaderError(
                "calendar_as_of_reader_lineage_invalid"
            )


@dataclass(frozen=True, slots=True)
class DurableSelectedCalendarSessionV1:
    selection: SelectedPointInTimeKrDailySessionV1
    lineage: CalendarAsOfLineageV1

    def __post_init__(self) -> None:
        if (
            type(self.selection) is not SelectedPointInTimeKrDailySessionV1
            or type(self.lineage) is not CalendarAsOfLineageV1
        ):
            raise CalendarAsOfReaderError(
                "calendar_as_of_reader_snapshot_invalid"
            )
        session = self.selection.session
        if (
            session.idempotency_key != self.lineage.calendar_idempotency_key
            or session.canonical_evidence_sha256
            != self.lineage.calendar_canonical_evidence_sha256
            or session.observed_at.astimezone(UTC)
            != self.lineage.calendar_occurrence_observed_at.astimezone(UTC)
        ):
            raise CalendarAsOfReaderError(
                "calendar_as_of_reader_snapshot_invalid"
            )


@dataclass(frozen=True, slots=True)
class DurableCalendarAsOfSnapshotV1:
    query_sha256: str
    snapshot_token: str
    snapshot_issued_at: datetime
    snapshot_manifest_sha256: str
    candidate_count: int
    items: tuple[DurableSelectedCalendarSessionV1, ...]

    def __post_init__(self) -> None:
        _require_sha256(self.query_sha256)
        _require_sha256(self.snapshot_manifest_sha256)
        if (
            type(self.snapshot_token) is not str
            or len(self.snapshot_token) > 4096
            or _SNAPSHOT_TOKEN_RE.fullmatch(self.snapshot_token) is None
        ):
            raise CalendarAsOfReaderError(
                "calendar_as_of_reader_snapshot_invalid"
            )
        _require_aware_datetime(self.snapshot_issued_at)
        if (
            type(self.candidate_count) is not int
            or not 0 <= self.candidate_count <= 1_000
        ):
            raise CalendarAsOfReaderError(
                "calendar_as_of_reader_candidate_count_invalid"
            )
        if type(self.items) is not tuple or any(
            type(item) is not DurableSelectedCalendarSessionV1
            for item in self.items
        ):
            raise CalendarAsOfReaderError(
                "calendar_as_of_reader_snapshot_invalid"
            )


class CalendarAsOfReaderPort(Protocol):
    async def read_daily_sessions_as_of(
        self,
        request: CalendarAsOfReadRequest,
    ) -> DurableCalendarAsOfSnapshotV1:
        ...


def calendar_as_of_query_sha256(value: object) -> str:
    """Return the canonical Worker/SQL query fingerprint for one read."""

    if type(value) is not CalendarAsOfReadRequest:
        raise CalendarAsOfReaderError("calendar_as_of_reader_request_invalid")
    try:
        request = CalendarAsOfReadRequest(
            provider=value.provider,
            market=value.market,
            start_session_date=value.start_session_date,
            end_session_date=value.end_session_date,
            as_of=value.as_of,
            page_size=value.page_size,
        )
        as_of = _request_as_of(request.as_of)
    except (
        AttributeError,
        CalendarAsOfReaderError,
        OverflowError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        raise CalendarAsOfReaderError(
            "calendar_as_of_reader_request_invalid"
        ) from exc
    if request != value:
        raise CalendarAsOfReaderError("calendar_as_of_reader_request_invalid")

    query: JsonObject = {
        "as_of": _canonical_timestamp(as_of),
        "contract_version": PIT_CALENDAR_AS_OF_READER_SCHEMA_VERSION,
        "end_session_date": request.end_session_date.isoformat(),
        "limit": request.page_size,
        "market": request.market,
        "provider": request.provider,
        "start_session_date": request.start_session_date.isoformat(),
    }
    try:
        canonical = json.dumps(
            query,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise CalendarAsOfReaderError(
            "calendar_as_of_reader_request_invalid"
        ) from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _request_as_of(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None:
        raise CalendarAsOfReaderError("calendar_as_of_reader_as_of_invalid")
    try:
        if value.utcoffset() is None:
            raise CalendarAsOfReaderError(
                "calendar_as_of_reader_as_of_invalid"
            )
        return value.astimezone(UTC)
    except (OverflowError, RuntimeError, TypeError, ValueError) as exc:
        raise CalendarAsOfReaderError(
            "calendar_as_of_reader_as_of_invalid"
        ) from exc


def _require_sha256(value: object) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise CalendarAsOfReaderError("calendar_as_of_reader_lineage_invalid")
    return value


def _require_canonical_uuid(value: object) -> str:
    if type(value) is not str:
        raise CalendarAsOfReaderError("calendar_as_of_reader_lineage_invalid")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise CalendarAsOfReaderError(
            "calendar_as_of_reader_lineage_invalid"
        ) from exc
    if str(parsed) != value or parsed.version not in {1, 2, 3, 4, 5}:
        raise CalendarAsOfReaderError("calendar_as_of_reader_lineage_invalid")
    return value


def _require_aware_datetime(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None:
        raise CalendarAsOfReaderError("calendar_as_of_reader_lineage_invalid")
    try:
        if value.utcoffset() is None:
            raise CalendarAsOfReaderError(
                "calendar_as_of_reader_lineage_invalid"
            )
        return value.astimezone(UTC)
    except (OverflowError, RuntimeError, TypeError, ValueError) as exc:
        raise CalendarAsOfReaderError(
            "calendar_as_of_reader_lineage_invalid"
        ) from exc


def _canonical_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
