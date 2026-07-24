from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal, cast
from uuid import UUID

import httpx

from app.application.ports.daily_candle_as_of_reader_port import (
    PIT_DAILY_CANDLE_AS_OF_READER_SCHEMA_VERSION as _READER_SCHEMA_VERSION,
)
from app.application.ports.daily_candle_as_of_reader_port import (
    DailyCandleAsOfLineageV1,
    DailyCandleAsOfReaderError,
    DailyCandleAsOfReadRequest,
    DurableDailyCandleAsOfSnapshotV1,
    DurableSelectedDailyCandleV1,
    daily_candle_as_of_query_sha256,
)
from app.config import Settings
from app.domain.common.json import JsonObject
from app.domain.market_data.daily_candle_as_of import (
    DailyCandleAsOfError,
    select_daily_candles_as_of,
)
from app.domain.market_data.daily_candle_timing import (
    DailyCandleTimeWindowError,
    PointInTimeDailyCandleTimingEvidenceV1,
    build_daily_candle_timing_evidence,
)
from app.domain.market_data.point_in_time import (
    PointInTimeCandleV1,
    PointInTimeDataError,
)
from app.domain.market_data.point_in_time_calendar import (
    PointInTimeCalendarError,
    PointInTimeKrDailySessionV1,
)
from app.infrastructure.supabase_headers import supabase_api_headers

PIT_DAILY_CANDLE_AS_OF_READER_SCHEMA_VERSION = _READER_SCHEMA_VERSION
PIT_DAILY_CANDLE_AS_OF_CURSOR_SCHEMA_VERSION = "pit_daily_candle_as_of_cursor.v1"
PITDailyCandleAsOfReaderRpc = Literal["list_pit_daily_candles_as_of_v1"]
PIT_DAILY_CANDLE_AS_OF_READER_RPC_ALLOWLIST: frozenset[str] = frozenset(
    {"list_pit_daily_candles_as_of_v1"}
)
PIT_DAILY_CANDLE_AS_OF_ENVELOPE_FIELDS: frozenset[str] = frozenset(
    {
        "schema_version",
        "query_sha256",
        "snapshot_token",
        "snapshot_issued_at",
        "snapshot_manifest_sha256",
        "candidate_count",
        "items",
        "next_cursor",
    }
)
PIT_DAILY_CANDLE_AS_OF_CURSOR_FIELDS: frozenset[str] = frozenset(
    {
        "schema_version",
        "query_sha256",
        "snapshot_token",
        "snapshot_issued_at",
        "snapshot_manifest_sha256",
        "last_session_date",
        "last_candle_observed_at",
        "last_evidence_available_at",
        "last_timing_revision",
        "last_timing_revision_id",
    }
)
PIT_DAILY_CANDLE_AS_OF_ITEM_FIELDS: frozenset[str] = frozenset(
    {
        "candidate_lineage_sha256",
        "timing_revision_id",
        "timing_idempotency_key",
        "timing_revision",
        "timing_canonical_evidence_sha256",
        "timing_evidence_available_at",
        "timing_received_at",
        "timing_payload",
        "candle_revision_id",
        "candle_revision",
        "candle_canonical_observation_sha256",
        "candle_revision_received_at",
        "candle_occurrence_id",
        "candle_occurrence_observed_at",
        "candle_occurrence_received_at",
        "candle_occurrence_origin",
        "candle_payload",
        "calendar_revision_id",
        "calendar_revision",
        "calendar_canonical_evidence_sha256",
        "calendar_revision_received_at",
        "calendar_occurrence_id",
        "calendar_occurrence_observed_at",
        "calendar_occurrence_received_at",
        "calendar_occurrence_origin",
        "calendar_payload",
    }
)
PIT_DAILY_CANDLE_AS_OF_LINEAGE_FIELDS: tuple[str, ...] = (
    "timing_revision_id",
    "timing_idempotency_key",
    "timing_revision",
    "timing_canonical_evidence_sha256",
    "timing_evidence_available_at",
    "timing_received_at",
    "candle_revision_id",
    "candle_revision",
    "candle_canonical_observation_sha256",
    "candle_revision_received_at",
    "candle_occurrence_id",
    "candle_occurrence_observed_at",
    "candle_occurrence_received_at",
    "candle_occurrence_origin",
    "calendar_revision_id",
    "calendar_revision",
    "calendar_canonical_evidence_sha256",
    "calendar_revision_received_at",
    "calendar_occurrence_id",
    "calendar_occurrence_observed_at",
    "calendar_occurrence_received_at",
    "calendar_occurrence_origin",
)

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SNAPSHOT_TOKEN_RE = re.compile(r"[0-9]+:[0-9]+:(?:[0-9]+(?:,[0-9]+)*)?")
_OCCURRENCE_ORIGINS = frozenset(
    {"content_revision_backfill", "stream_head_recovery", "rpc"}
)
_MAX_CANDIDATES = 1_000


@dataclass(frozen=True, slots=True)
class _RawCandidate:
    candle: PointInTimeCandleV1
    timing: PointInTimeDailyCandleTimingEvidenceV1
    calendar: PointInTimeKrDailySessionV1
    lineage: DailyCandleAsOfLineageV1
    candidate_lineage_sha256: str

    @property
    def order_key(self) -> tuple[date, datetime, datetime, int, str]:
        return (
            self.timing.session_date,
            self.candle.observed_at,
            self.timing.evidence_available_at,
            self.lineage.timing_revision,
            self.lineage.timing_revision_id,
        )


class SupabaseDailyCandleAsOfReader:
    """Read one complete occurrence-backed as-of snapshot through Worker RPC."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not settings.supabase_url or settings.supabase_secret_key is None:
            raise DailyCandleAsOfReaderError(
                "daily_candle_as_of_reader_credentials_missing"
            )
        secret = settings.supabase_secret_key.get_secret_value()
        self.base_url = settings.supabase_url.rstrip("/") + "/rest/v1/rpc"
        self.headers = supabase_api_headers(secret) | {
            "accept-profile": "worker_api",
            "content-profile": "worker_api",
            "content-type": "application/json",
        }
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=10.0,
            headers=self.headers,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def read_daily_candles_as_of(
        self,
        request: DailyCandleAsOfReadRequest,
    ) -> DurableDailyCandleAsOfSnapshotV1:
        valid_request = _canonical_request(request)
        request_as_of = valid_request.as_of.astimezone(UTC)
        expected_query_sha256 = daily_candle_as_of_query_sha256(valid_request)
        cursor: JsonObject | None = None
        seen_cursors: set[str] = set()
        raw_candidates: list[_RawCandidate] = []
        expected_metadata: tuple[str, str, datetime, str, int] | None = None
        previous_order_key: tuple[date, datetime, datetime, int, str] | None = None

        while True:
            envelope = _envelope(
                await self._rpc(
                    "list_pit_daily_candles_as_of_v1",
                    {
                        "p_provider": valid_request.provider,
                        "p_market": valid_request.market,
                        "p_symbol": valid_request.symbol,
                        "p_interval": valid_request.interval,
                        "p_adjusted": valid_request.adjusted,
                        "p_start_session_date": valid_request.start_session_date.isoformat(),
                        "p_end_session_date": valid_request.end_session_date.isoformat(),
                        "p_as_of": _canonical_timestamp(request_as_of),
                        "p_limit": valid_request.page_size,
                        "p_cursor": cursor,
                    },
                )
            )
            metadata = _metadata(envelope)
            if expected_metadata is None:
                expected_metadata = metadata
            elif metadata != expected_metadata:
                raise DailyCandleAsOfReaderError(
                    "daily_candle_as_of_reader_page_metadata_mismatch"
                )

            query_sha256, snapshot_token, snapshot_issued_at, manifest, count = metadata
            if query_sha256 != expected_query_sha256:
                raise DailyCandleAsOfReaderError(
                    "daily_candle_as_of_reader_query_mismatch"
                )
            page = _items(envelope)
            if len(page) > valid_request.page_size:
                raise DailyCandleAsOfReaderError(
                    "daily_candle_as_of_reader_page_size_exceeded"
                )
            if cursor is not None and not page:
                raise DailyCandleAsOfReaderError(
                    "daily_candle_as_of_reader_empty_continuation_page"
                )
            for raw_item in page:
                candidate = _candidate(
                    raw_item,
                    valid_request,
                    request_as_of,
                    snapshot_issued_at,
                )
                if previous_order_key is not None and candidate.order_key <= previous_order_key:
                    raise DailyCandleAsOfReaderError(
                        "daily_candle_as_of_reader_candidate_order_invalid"
                    )
                previous_order_key = candidate.order_key
                raw_candidates.append(candidate)
                if len(raw_candidates) > _MAX_CANDIDATES or len(raw_candidates) > count:
                    raise DailyCandleAsOfReaderError(
                        "daily_candle_as_of_reader_candidate_count_invalid"
                    )

            raw_next_cursor = envelope.get("next_cursor")
            cursor_fingerprint: str | None = None
            if (
                type(raw_next_cursor) is dict
                and set(raw_next_cursor) == PIT_DAILY_CANDLE_AS_OF_CURSOR_FIELDS
            ):
                cursor_fingerprint = _canonical_json_text(
                    cast(JsonObject, raw_next_cursor)
                )
                if cursor_fingerprint in seen_cursors:
                    raise DailyCandleAsOfReaderError(
                        "daily_candle_as_of_reader_cursor_cycle"
                    )
            next_cursor = _next_cursor(
                envelope,
                query_sha256=query_sha256,
                snapshot_token=snapshot_token,
                snapshot_issued_at=snapshot_issued_at,
                manifest=manifest,
                last_candidate=raw_candidates[-1] if page else None,
            )
            if next_cursor is None:
                break
            if not page:
                raise DailyCandleAsOfReaderError(
                    "daily_candle_as_of_reader_empty_page_with_cursor"
                )
            if cursor_fingerprint is None:
                raise DailyCandleAsOfReaderError(
                    "daily_candle_as_of_reader_cursor_invalid"
                )
            seen_cursors.add(cursor_fingerprint)
            cursor = next_cursor

        if expected_metadata is None:
            raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_rpc_result_invalid")
        query_sha256, snapshot_token, snapshot_issued_at, manifest, count = expected_metadata
        if len(raw_candidates) != count:
            raise DailyCandleAsOfReaderError(
                "daily_candle_as_of_reader_candidate_count_mismatch"
            )
        calculated_manifest = hashlib.sha256(
            "\n".join(
                candidate.candidate_lineage_sha256 for candidate in raw_candidates
            ).encode("utf-8")
        ).hexdigest()
        if calculated_manifest != manifest:
            raise DailyCandleAsOfReaderError(
                "daily_candle_as_of_reader_snapshot_manifest_mismatch"
            )

        collapsed = _precollapse_timing_revisions(raw_candidates)
        candidate_bindings = {(item.candle, item.timing): item for item in collapsed}
        if len(candidate_bindings) != len(collapsed):
            raise DailyCandleAsOfReaderError(
                "daily_candle_as_of_reader_candidate_binding_ambiguous"
            )
        try:
            selected = select_daily_candles_as_of(
                [(item.candle, item.timing) for item in collapsed],
                as_of=request_as_of,
            )
        except DailyCandleAsOfError as exc:
            raise DailyCandleAsOfReaderError(
                "daily_candle_as_of_reader_selection_failed"
            ) from exc

        durable_items: list[DurableSelectedDailyCandleV1] = []
        for selection in selected:
            source = candidate_bindings.get(
                (selection.candle, selection.timing_evidence)
            )
            if source is None:
                raise DailyCandleAsOfReaderError(
                    "daily_candle_as_of_reader_selection_lineage_missing"
                )
            durable_items.append(
                DurableSelectedDailyCandleV1(
                    selection=selection,
                    calendar=source.calendar,
                    lineage=source.lineage,
                )
            )

        return DurableDailyCandleAsOfSnapshotV1(
            query_sha256=query_sha256,
            snapshot_token=snapshot_token,
            snapshot_issued_at=snapshot_issued_at,
            snapshot_manifest_sha256=manifest,
            candidate_count=count,
            items=tuple(durable_items),
        )

    async def _rpc(
        self,
        rpc: PITDailyCandleAsOfReaderRpc,
        payload: JsonObject,
    ) -> object:
        if rpc not in PIT_DAILY_CANDLE_AS_OF_READER_RPC_ALLOWLIST:
            raise DailyCandleAsOfReaderError(
                "daily_candle_as_of_reader_rpc_not_allowed"
            )
        try:
            response = await self.client.post(
                f"{self.base_url}/{rpc}",
                headers=self.headers,
                json=payload,
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise DailyCandleAsOfReaderError(
                "daily_candle_as_of_reader_rpc_failed_or_returned_invalid_json"
            ) from exc


def _canonical_request(value: object) -> DailyCandleAsOfReadRequest:
    if type(value) is not DailyCandleAsOfReadRequest:
        raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_request_invalid")
    try:
        canonical = DailyCandleAsOfReadRequest(
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
    except (AttributeError, OverflowError, RuntimeError, TypeError, ValueError) as exc:
        raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_request_invalid") from exc
    if canonical != value:
        raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_request_invalid")
    return canonical


def _envelope(value: object) -> Mapping[str, object]:
    if type(value) is not dict or set(value) != PIT_DAILY_CANDLE_AS_OF_ENVELOPE_FIELDS:
        raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_rpc_result_invalid")
    if value.get("schema_version") != PIT_DAILY_CANDLE_AS_OF_READER_SCHEMA_VERSION:
        raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_schema_version_invalid")
    return value


def _metadata(
    envelope: Mapping[str, object],
) -> tuple[str, str, datetime, str, int]:
    query_sha256 = _sha256(envelope.get("query_sha256"), "query_sha256")
    snapshot_token = _snapshot_token(envelope.get("snapshot_token"))
    snapshot_issued_at = _canonical_datetime(
        envelope.get("snapshot_issued_at"), "snapshot_issued_at"
    )
    manifest = _sha256(
        envelope.get("snapshot_manifest_sha256"),
        "snapshot_manifest_sha256",
    )
    count = _nonnegative_int(envelope.get("candidate_count"), "candidate_count")
    if count > _MAX_CANDIDATES:
        raise DailyCandleAsOfReaderError(
            "daily_candle_as_of_reader_candidate_limit_exceeded"
        )
    return query_sha256, snapshot_token, snapshot_issued_at, manifest, count


def _items(envelope: Mapping[str, object]) -> Sequence[object]:
    items = envelope.get("items")
    if type(items) is not list:
        raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_items_invalid")
    return items


def _candidate(
    value: object,
    request: DailyCandleAsOfReadRequest,
    as_of: datetime,
    snapshot_issued_at: datetime,
) -> _RawCandidate:
    if type(value) is not dict or set(value) != PIT_DAILY_CANDLE_AS_OF_ITEM_FIELDS:
        raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_item_shape_invalid")
    item = cast(Mapping[str, object], value)
    candidate_lineage_sha256 = _sha256(
        item.get("candidate_lineage_sha256"), "candidate_lineage_sha256"
    )
    canonical_line_values: dict[str, str] = {}

    timing_revision_id = _uuid(item.get("timing_revision_id"), "timing_revision_id")
    timing_idempotency_key = _sha256(
        item.get("timing_idempotency_key"), "timing_idempotency_key"
    )
    timing_revision = _positive_int(item.get("timing_revision"), "timing_revision")
    timing_sha = _sha256(
        item.get("timing_canonical_evidence_sha256"),
        "timing_canonical_evidence_sha256",
    )
    timing_available_at = _canonical_datetime(
        item.get("timing_evidence_available_at"),
        "timing_evidence_available_at",
    )
    timing_received_at = _canonical_datetime(
        item.get("timing_received_at"), "timing_received_at"
    )

    candle_revision_id = _uuid(item.get("candle_revision_id"), "candle_revision_id")
    candle_revision = _positive_int(item.get("candle_revision"), "candle_revision")
    candle_sha = _sha256(
        item.get("candle_canonical_observation_sha256"),
        "candle_canonical_observation_sha256",
    )
    candle_revision_received_at = _canonical_datetime(
        item.get("candle_revision_received_at"),
        "candle_revision_received_at",
    )
    candle_occurrence_id = _uuid(
        item.get("candle_occurrence_id"), "candle_occurrence_id"
    )
    candle_occurrence_observed_at = _canonical_datetime(
        item.get("candle_occurrence_observed_at"),
        "candle_occurrence_observed_at",
    )
    candle_occurrence_received_at = _canonical_datetime(
        item.get("candle_occurrence_received_at"),
        "candle_occurrence_received_at",
    )
    candle_origin = _origin(
        item.get("candle_occurrence_origin"), "candle_occurrence_origin"
    )

    calendar_revision_id = _uuid(
        item.get("calendar_revision_id"), "calendar_revision_id"
    )
    calendar_revision = _positive_int(
        item.get("calendar_revision"), "calendar_revision"
    )
    calendar_sha = _sha256(
        item.get("calendar_canonical_evidence_sha256"),
        "calendar_canonical_evidence_sha256",
    )
    calendar_revision_received_at = _canonical_datetime(
        item.get("calendar_revision_received_at"),
        "calendar_revision_received_at",
    )
    calendar_occurrence_id = _uuid(
        item.get("calendar_occurrence_id"), "calendar_occurrence_id"
    )
    calendar_occurrence_observed_at = _canonical_datetime(
        item.get("calendar_occurrence_observed_at"),
        "calendar_occurrence_observed_at",
    )
    calendar_occurrence_received_at = _canonical_datetime(
        item.get("calendar_occurrence_received_at"),
        "calendar_occurrence_received_at",
    )
    calendar_origin = _origin(
        item.get("calendar_occurrence_origin"), "calendar_occurrence_origin"
    )

    canonical_line_values.update(
        {
            "timing_revision_id": timing_revision_id,
            "timing_idempotency_key": timing_idempotency_key,
            "timing_revision": str(timing_revision),
            "timing_canonical_evidence_sha256": timing_sha,
            "timing_evidence_available_at": _canonical_timestamp(timing_available_at),
            "timing_received_at": _canonical_timestamp(timing_received_at),
            "candle_revision_id": candle_revision_id,
            "candle_revision": str(candle_revision),
            "candle_canonical_observation_sha256": candle_sha,
            "candle_revision_received_at": _canonical_timestamp(
                candle_revision_received_at
            ),
            "candle_occurrence_id": candle_occurrence_id,
            "candle_occurrence_observed_at": _canonical_timestamp(
                candle_occurrence_observed_at
            ),
            "candle_occurrence_received_at": _canonical_timestamp(
                candle_occurrence_received_at
            ),
            "candle_occurrence_origin": candle_origin,
            "calendar_revision_id": calendar_revision_id,
            "calendar_revision": str(calendar_revision),
            "calendar_canonical_evidence_sha256": calendar_sha,
            "calendar_revision_received_at": _canonical_timestamp(
                calendar_revision_received_at
            ),
            "calendar_occurrence_id": calendar_occurrence_id,
            "calendar_occurrence_observed_at": _canonical_timestamp(
                calendar_occurrence_observed_at
            ),
            "calendar_occurrence_received_at": _canonical_timestamp(
                calendar_occurrence_received_at
            ),
            "calendar_occurrence_origin": calendar_origin,
        }
    )
    calculated_lineage_sha256 = hashlib.sha256(
        "|".join(
            canonical_line_values[field]
            for field in PIT_DAILY_CANDLE_AS_OF_LINEAGE_FIELDS
        ).encode("utf-8")
    ).hexdigest()
    if calculated_lineage_sha256 != candidate_lineage_sha256:
        raise DailyCandleAsOfReaderError(
            "daily_candle_as_of_reader_candidate_lineage_mismatch"
        )

    candle = _candle(item.get("candle_payload"))
    calendar = _calendar(item.get("calendar_payload"))
    timing = _timing(item.get("timing_payload"))
    try:
        rebuilt_timing = build_daily_candle_timing_evidence(candle, calendar)
    except DailyCandleTimeWindowError as exc:
        raise DailyCandleAsOfReaderError(
            "daily_candle_as_of_reader_source_binding_invalid"
        ) from exc
    if rebuilt_timing != timing:
        raise DailyCandleAsOfReaderError(
            "daily_candle_as_of_reader_source_binding_invalid"
        )
    if (
        timing.idempotency_key != timing_idempotency_key
        or timing.canonical_timing_evidence_sha256 != timing_sha
        or timing.evidence_available_at != timing_available_at
        or candle.canonical_observation_sha256 != candle_sha
        or candle.observed_at != candle_occurrence_observed_at
        or calendar.canonical_evidence_sha256 != calendar_sha
        or calendar.observed_at != calendar_occurrence_observed_at
    ):
        raise DailyCandleAsOfReaderError(
            "daily_candle_as_of_reader_source_lineage_mismatch"
        )
    if (
        candle_revision_received_at > candle_occurrence_received_at
        or candle_occurrence_observed_at > candle_occurrence_received_at
        or candle_occurrence_received_at > timing_received_at
        or calendar_revision_received_at > calendar_occurrence_received_at
        or calendar_occurrence_observed_at > calendar_occurrence_received_at
        or calendar_occurrence_received_at > timing_received_at
        or timing_received_at > snapshot_issued_at
    ):
        raise DailyCandleAsOfReaderError(
            "daily_candle_as_of_reader_lineage_clock_invalid"
        )
    if (
        timing.provider != request.provider
        or timing.market != request.market
        or timing.symbol != request.symbol
        or timing.interval != request.interval
        or timing.adjusted is not request.adjusted
        or not request.start_session_date <= timing.session_date <= request.end_session_date
        or timing.evidence_available_at > as_of
    ):
        raise DailyCandleAsOfReaderError(
            "daily_candle_as_of_reader_candidate_outside_query"
        )

    lineage = DailyCandleAsOfLineageV1(
        timing_revision_id=timing_revision_id,
        timing_idempotency_key=timing_idempotency_key,
        timing_revision=timing_revision,
        timing_canonical_evidence_sha256=timing_sha,
        timing_received_at=timing_received_at,
        candle_revision_id=candle_revision_id,
        candle_revision=candle_revision,
        candle_canonical_observation_sha256=candle_sha,
        candle_revision_received_at=candle_revision_received_at,
        candle_occurrence_id=candle_occurrence_id,
        candle_occurrence_received_at=candle_occurrence_received_at,
        candle_occurrence_origin=candle_origin,
        calendar_revision_id=calendar_revision_id,
        calendar_revision=calendar_revision,
        calendar_canonical_evidence_sha256=calendar_sha,
        calendar_revision_received_at=calendar_revision_received_at,
        calendar_occurrence_id=calendar_occurrence_id,
        calendar_occurrence_received_at=calendar_occurrence_received_at,
        calendar_occurrence_origin=calendar_origin,
    )
    return _RawCandidate(
        candle=candle,
        timing=timing,
        calendar=calendar,
        lineage=lineage,
        candidate_lineage_sha256=candidate_lineage_sha256,
    )


def _precollapse_timing_revisions(
    candidates: Sequence[_RawCandidate],
) -> tuple[_RawCandidate, ...]:
    by_stream: dict[str, list[_RawCandidate]] = {}
    for candidate in candidates:
        by_stream.setdefault(candidate.lineage.timing_idempotency_key, []).append(candidate)

    kept: list[_RawCandidate] = []
    for stream in by_stream.values():
        by_revision = sorted(stream, key=lambda item: item.lineage.timing_revision)
        seen_revisions: set[int] = set()
        previous: _RawCandidate | None = None
        for candidate in by_revision:
            revision = candidate.lineage.timing_revision
            if revision in seen_revisions:
                raise DailyCandleAsOfReaderError(
                    "daily_candle_as_of_reader_timing_revision_ambiguous"
                )
            seen_revisions.add(revision)
            if previous is not None:
                if (
                    candidate.candle.observed_at < previous.candle.observed_at
                    or candidate.calendar.observed_at < previous.calendar.observed_at
                    or candidate.timing.evidence_available_at
                    < previous.timing.evidence_available_at
                    or candidate.lineage.timing_received_at
                    < previous.lineage.timing_received_at
                ):
                    raise DailyCandleAsOfReaderError(
                        "daily_candle_as_of_reader_timing_revision_regressed"
                    )
                if (
                    candidate.candle.observed_at == previous.candle.observed_at
                    and candidate.calendar.observed_at == previous.calendar.observed_at
                ):
                    raise DailyCandleAsOfReaderError(
                        "daily_candle_as_of_reader_timing_revision_ambiguous"
                    )
            previous = candidate

        if sorted(seen_revisions) != list(range(1, len(seen_revisions) + 1)):
            raise DailyCandleAsOfReaderError(
                "daily_candle_as_of_reader_timing_revision_gap"
            )

        same_max_component_revisions: dict[tuple[str, datetime], _RawCandidate] = {}
        for candidate in by_revision:
            key = (
                candidate.lineage.candle_occurrence_id,
                candidate.timing.evidence_available_at,
            )
            existing = same_max_component_revisions.get(key)
            if (
                existing is None
                or candidate.lineage.timing_revision > existing.lineage.timing_revision
            ):
                same_max_component_revisions[key] = candidate
        kept.extend(same_max_component_revisions.values())
    return tuple(sorted(kept, key=lambda item: item.order_key))


def _next_cursor(
    envelope: Mapping[str, object],
    *,
    query_sha256: str,
    snapshot_token: str,
    snapshot_issued_at: datetime,
    manifest: str,
    last_candidate: _RawCandidate | None,
) -> JsonObject | None:
    raw = envelope.get("next_cursor")
    if raw is None:
        return None
    if type(raw) is not dict or set(raw) != PIT_DAILY_CANDLE_AS_OF_CURSOR_FIELDS:
        raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_cursor_invalid")
    cursor = cast(Mapping[str, object], raw)
    if (
        cursor.get("schema_version") != PIT_DAILY_CANDLE_AS_OF_CURSOR_SCHEMA_VERSION
        or _sha256(cursor.get("query_sha256"), "cursor_query_sha256") != query_sha256
        or _snapshot_token(cursor.get("snapshot_token")) != snapshot_token
        or _canonical_datetime(
            cursor.get("snapshot_issued_at"), "cursor_snapshot_issued_at"
        )
        != snapshot_issued_at
        or _sha256(
            cursor.get("snapshot_manifest_sha256"),
            "cursor_snapshot_manifest_sha256",
        )
        != manifest
    ):
        raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_cursor_mismatch")
    if last_candidate is None:
        raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_cursor_invalid")
    if (
        _canonical_date(cursor.get("last_session_date"), "last_session_date")
        != last_candidate.timing.session_date
        or _canonical_datetime(
            cursor.get("last_candle_observed_at"), "last_candle_observed_at"
        )
        != last_candidate.candle.observed_at
        or _canonical_datetime(
            cursor.get("last_evidence_available_at"),
            "last_evidence_available_at",
        )
        != last_candidate.timing.evidence_available_at
        or _positive_int(
            cursor.get("last_timing_revision"), "last_timing_revision"
        )
        != last_candidate.lineage.timing_revision
        or _uuid(
            cursor.get("last_timing_revision_id"), "last_timing_revision_id"
        )
        != last_candidate.lineage.timing_revision_id
    ):
        raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_cursor_position_invalid")
    return cast(JsonObject, raw)


def _candle(value: object) -> PointInTimeCandleV1:
    if type(value) is not dict:
        raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_candle_invalid")
    try:
        return PointInTimeCandleV1.from_payload(value)
    except (
        AttributeError,
        OverflowError,
        PointInTimeDataError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_candle_invalid") from exc


def _calendar(value: object) -> PointInTimeKrDailySessionV1:
    if type(value) is not dict:
        raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_calendar_invalid")
    try:
        return PointInTimeKrDailySessionV1.from_payload(value)
    except (
        AttributeError,
        OverflowError,
        PointInTimeCalendarError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_calendar_invalid") from exc


def _timing(value: object) -> PointInTimeDailyCandleTimingEvidenceV1:
    if type(value) is not dict:
        raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_timing_invalid")
    try:
        return PointInTimeDailyCandleTimingEvidenceV1.from_payload(value)
    except (
        AttributeError,
        DailyCandleTimeWindowError,
        OverflowError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_timing_invalid") from exc


def _sha256(value: object, field_name: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise DailyCandleAsOfReaderError(f"daily_candle_as_of_reader_{field_name}_invalid")
    return value


def _snapshot_token(value: object) -> str:
    if (
        type(value) is not str
        or len(value) > 4096
        or _SNAPSHOT_TOKEN_RE.fullmatch(value) is None
    ):
        raise DailyCandleAsOfReaderError("daily_candle_as_of_reader_snapshot_token_invalid")
    return value


def _uuid(value: object, field_name: str) -> str:
    if type(value) is not str:
        raise DailyCandleAsOfReaderError(f"daily_candle_as_of_reader_{field_name}_invalid")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise DailyCandleAsOfReaderError(
            f"daily_candle_as_of_reader_{field_name}_invalid"
        ) from exc
    if str(parsed) != value or parsed.version not in {1, 2, 3, 4, 5}:
        raise DailyCandleAsOfReaderError(f"daily_candle_as_of_reader_{field_name}_invalid")
    return value


def _positive_int(value: object, field_name: str) -> int:
    if type(value) is not int or value <= 0:
        raise DailyCandleAsOfReaderError(f"daily_candle_as_of_reader_{field_name}_invalid")
    return value


def _nonnegative_int(value: object, field_name: str) -> int:
    if type(value) is not int or value < 0:
        raise DailyCandleAsOfReaderError(f"daily_candle_as_of_reader_{field_name}_invalid")
    return value


def _origin(value: object, field_name: str) -> str:
    if type(value) is not str or value not in _OCCURRENCE_ORIGINS:
        raise DailyCandleAsOfReaderError(f"daily_candle_as_of_reader_{field_name}_invalid")
    return value


def _canonical_datetime(value: object, field_name: str) -> datetime:
    if type(value) is not str:
        raise DailyCandleAsOfReaderError(f"daily_candle_as_of_reader_{field_name}_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise DailyCandleAsOfReaderError(
                f"daily_candle_as_of_reader_{field_name}_invalid"
            )
        canonical = parsed.astimezone(UTC)
    except (OverflowError, RuntimeError, TypeError, ValueError) as exc:
        raise DailyCandleAsOfReaderError(
            f"daily_candle_as_of_reader_{field_name}_invalid"
        ) from exc
    if _canonical_timestamp(canonical) != value:
        raise DailyCandleAsOfReaderError(f"daily_candle_as_of_reader_{field_name}_invalid")
    return canonical


def _canonical_date(value: object, field_name: str) -> date:
    if type(value) is not str:
        raise DailyCandleAsOfReaderError(f"daily_candle_as_of_reader_{field_name}_invalid")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise DailyCandleAsOfReaderError(
            f"daily_candle_as_of_reader_{field_name}_invalid"
        ) from exc
    if parsed.isoformat() != value:
        raise DailyCandleAsOfReaderError(f"daily_candle_as_of_reader_{field_name}_invalid")
    return parsed


def _canonical_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _canonical_json_text(value: JsonObject) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise DailyCandleAsOfReaderError(
            "daily_candle_as_of_reader_cursor_invalid"
        ) from exc
