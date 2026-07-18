from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Protocol

from app.domain.common.errors import KnownFailClosedError
from app.domain.common.json import JsonObject
from app.domain.market_data.daily_candle_as_of import (
    SelectedPointInTimeDailyCandleV1,
)
from app.domain.market_data.daily_candle_timing import (
    DailyCandleTimeWindowError,
    build_daily_candle_timing_evidence,
)
from app.domain.market_data.point_in_time_calendar import (
    PointInTimeKrDailySessionV1,
)

_PROVIDER_RE = re.compile(r"[a-z][a-z0-9._-]{0,63}")
_KR_SYMBOL_RE = re.compile(r"[0-9]{6}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SNAPSHOT_TOKEN_RE = re.compile(r"[0-9]+:[0-9]+:(?:[0-9]+(?:,[0-9]+)*)?")
_ORIGINS = frozenset({"content_revision_backfill", "stream_head_recovery", "rpc"})
PIT_DAILY_CANDLE_AS_OF_READER_SCHEMA_VERSION = "pit_daily_candle_as_of_reader.v1"


class DailyCandleAsOfReaderError(KnownFailClosedError):
    def __init__(self, safe_message: str) -> None:
        super().__init__("daily_candle_as_of_reader", safe_message)


@dataclass(frozen=True, slots=True)
class DailyCandleAsOfReadRequest:
    provider: str
    market: str
    symbol: str
    interval: str
    adjusted: bool
    start_session_date: date
    end_session_date: date
    as_of: datetime
    page_size: int = 100

    def __post_init__(self) -> None:
        if type(self.provider) is not str or _PROVIDER_RE.fullmatch(self.provider) is None:
            raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_provider_invalid")
        if type(self.market) is not str or self.market != "KR":
            raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_market_invalid")
        if type(self.symbol) is not str or _KR_SYMBOL_RE.fullmatch(self.symbol) is None:
            raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_symbol_invalid")
        if type(self.interval) is not str or self.interval != "1d":
            raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_interval_invalid")
        if type(self.adjusted) is not bool:
            raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_adjusted_invalid")
        if type(self.start_session_date) is not date or type(self.end_session_date) is not date:
            raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_date_range_invalid")
        if self.start_session_date > self.end_session_date:
            raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_date_range_invalid")
        if (self.end_session_date - self.start_session_date).days >= 366:
            raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_date_range_too_large")
        if type(self.as_of) is not datetime or self.as_of.tzinfo is None:
            raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_as_of_invalid")
        try:
            if self.as_of.utcoffset() is None:
                raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_as_of_invalid")
            self.as_of.astimezone(UTC)
        except (OverflowError, RuntimeError, TypeError, ValueError) as exc:
            raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_as_of_invalid") from exc
        if type(self.page_size) is not int or not 25 <= self.page_size <= 100:
            raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_page_size_invalid")


@dataclass(frozen=True, slots=True)
class DailyCandleAsOfLineageV1:
    timing_revision_id: str
    timing_idempotency_key: str
    timing_revision: int
    timing_canonical_evidence_sha256: str
    timing_received_at: datetime
    candle_revision_id: str
    candle_revision: int
    candle_canonical_observation_sha256: str
    candle_revision_received_at: datetime
    candle_occurrence_id: str
    candle_occurrence_received_at: datetime
    candle_occurrence_origin: str
    calendar_revision_id: str
    calendar_revision: int
    calendar_canonical_evidence_sha256: str
    calendar_revision_received_at: datetime
    calendar_occurrence_id: str
    calendar_occurrence_received_at: datetime
    calendar_occurrence_origin: str

    def __post_init__(self) -> None:
        for uuid_value in (
            self.timing_revision_id,
            self.candle_revision_id,
            self.candle_occurrence_id,
            self.calendar_revision_id,
            self.calendar_occurrence_id,
        ):
            _require_canonical_uuid(uuid_value)
        for sha256_value in (
            self.timing_idempotency_key,
            self.timing_canonical_evidence_sha256,
            self.candle_canonical_observation_sha256,
            self.calendar_canonical_evidence_sha256,
        ):
            _require_sha256(sha256_value)
        for revision_value in (
            self.timing_revision,
            self.candle_revision,
            self.calendar_revision,
        ):
            if type(revision_value) is not int or revision_value <= 0:
                raise DailyCandleAsOfReaderError(
                    "daily_candle_as_of_reader_lineage_invalid"
                )
        for timestamp_value in (
            self.timing_received_at,
            self.candle_revision_received_at,
            self.candle_occurrence_received_at,
            self.calendar_revision_received_at,
            self.calendar_occurrence_received_at,
        ):
            _require_aware_datetime(timestamp_value)
        if (
            type(self.candle_occurrence_origin) is not str
            or self.candle_occurrence_origin not in _ORIGINS
        ):
            raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_lineage_invalid")
        if (
            type(self.calendar_occurrence_origin) is not str
            or self.calendar_occurrence_origin not in _ORIGINS
        ):
            raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_lineage_invalid")


@dataclass(frozen=True, slots=True)
class DurableSelectedDailyCandleV1:
    selection: SelectedPointInTimeDailyCandleV1
    calendar: PointInTimeKrDailySessionV1
    lineage: DailyCandleAsOfLineageV1

    def __post_init__(self) -> None:
        if type(self.selection) is not SelectedPointInTimeDailyCandleV1:
            raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_snapshot_invalid")
        if type(self.calendar) is not PointInTimeKrDailySessionV1:
            raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_snapshot_invalid")
        if type(self.lineage) is not DailyCandleAsOfLineageV1:
            raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_snapshot_invalid")
        try:
            rebuilt_timing = build_daily_candle_timing_evidence(
                self.selection.candle,
                self.calendar,
            )
        except DailyCandleTimeWindowError as exc:
            raise DailyCandleAsOfReaderError(
                "daily_candle_as_of_reader_snapshot_invalid"
            ) from exc
        if (
            rebuilt_timing != self.selection.timing_evidence
            or self.selection.timing_evidence.idempotency_key
            != self.lineage.timing_idempotency_key
            or self.selection.timing_evidence.canonical_timing_evidence_sha256
            != self.lineage.timing_canonical_evidence_sha256
            or self.selection.candle.canonical_observation_sha256
            != self.lineage.candle_canonical_observation_sha256
            or self.calendar.canonical_evidence_sha256
            != self.lineage.calendar_canonical_evidence_sha256
            or self.lineage.candle_revision_received_at
            > self.lineage.candle_occurrence_received_at
            or self.selection.candle.observed_at
            > self.lineage.candle_occurrence_received_at
            or self.lineage.candle_occurrence_received_at
            > self.lineage.timing_received_at
            or self.lineage.calendar_revision_received_at
            > self.lineage.calendar_occurrence_received_at
            or self.calendar.observed_at
            > self.lineage.calendar_occurrence_received_at
            or self.lineage.calendar_occurrence_received_at
            > self.lineage.timing_received_at
            or self.selection.timing_evidence.evidence_available_at
            > self.lineage.timing_received_at
        ):
            raise DailyCandleAsOfReaderError(
                "daily_candle_as_of_reader_snapshot_invalid"
            )


@dataclass(frozen=True, slots=True)
class DurableDailyCandleAsOfSnapshotV1:
    query_sha256: str
    snapshot_token: str
    snapshot_issued_at: datetime
    snapshot_manifest_sha256: str
    candidate_count: int
    items: tuple[DurableSelectedDailyCandleV1, ...]

    def __post_init__(self) -> None:
        _require_sha256(self.query_sha256)
        _require_sha256(self.snapshot_manifest_sha256)
        if (
            type(self.snapshot_token) is not str
            or len(self.snapshot_token) > 4096
            or _SNAPSHOT_TOKEN_RE.fullmatch(self.snapshot_token) is None
        ):
            raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_snapshot_invalid")
        _require_aware_datetime(self.snapshot_issued_at)
        if type(self.candidate_count) is not int or not 0 <= self.candidate_count <= 1_000:
            raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_candidate_count_invalid")
        if type(self.items) is not tuple:
            raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_snapshot_invalid")
        if any(type(item) is not DurableSelectedDailyCandleV1 for item in self.items):
            raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_snapshot_invalid")


class DailyCandleAsOfReaderPort(Protocol):
    async def read_daily_candles_as_of(
        self,
        request: DailyCandleAsOfReadRequest,
    ) -> DurableDailyCandleAsOfSnapshotV1:
        ...


def daily_candle_as_of_query_sha256(value: object) -> str:
    """Return the canonical Worker/SQL query fingerprint for one read."""

    if type(value) is not DailyCandleAsOfReadRequest:
        raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_request_invalid")
    try:
        request = DailyCandleAsOfReadRequest(
            provider=value.provider,
            market=value.market,
            symbol=value.symbol,
            interval=value.interval,
            adjusted=value.adjusted,
            start_session_date=value.start_session_date,
            end_session_date=value.end_session_date,
            as_of=value.as_of,
            page_size=value.page_size,
        )
        as_of = request.as_of.astimezone(UTC)
    except (
        AttributeError,
        DailyCandleAsOfReaderError,
        OverflowError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        raise DailyCandleAsOfReaderError(
            "daily_candle_as_of_reader_request_invalid"
        ) from exc
    if request != value:
        raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_request_invalid")
    query: JsonObject = {
        "adjusted": request.adjusted,
        "as_of": _canonical_timestamp(as_of),
        "contract_version": PIT_DAILY_CANDLE_AS_OF_READER_SCHEMA_VERSION,
        "end_session_date": request.end_session_date.isoformat(),
        "interval": request.interval,
        "limit": request.page_size,
        "market": request.market,
        "provider": request.provider,
        "start_session_date": request.start_session_date.isoformat(),
        "symbol": request.symbol,
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
        raise DailyCandleAsOfReaderError(
            "daily_candle_as_of_reader_request_invalid"
        ) from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _require_sha256(value: object) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_lineage_invalid")
    return value


def _require_canonical_uuid(value: object) -> str:
    from uuid import UUID

    if type(value) is not str:
        raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_lineage_invalid")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_lineage_invalid") from exc
    if str(parsed) != value or parsed.version not in {1, 2, 3, 4, 5}:
        raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_lineage_invalid")
    return value


def _require_aware_datetime(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None:
        raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_lineage_invalid")
    try:
        if value.utcoffset() is None:
            raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_lineage_invalid")
        return value.astimezone(UTC)
    except (OverflowError, RuntimeError, TypeError, ValueError) as exc:
        raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_lineage_invalid") from exc


def _canonical_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
