from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal, cast
from uuid import UUID

import httpx

from app.application.ports.calendar_as_of_reader_port import (
    PIT_CALENDAR_AS_OF_READER_SCHEMA_VERSION as _READER_SCHEMA_VERSION,
)
from app.application.ports.calendar_as_of_reader_port import (
    CalendarAsOfLineageV1,
    CalendarAsOfReaderError,
    CalendarAsOfReadRequest,
    DurableCalendarAsOfSnapshotV1,
    DurableSelectedCalendarSessionV1,
    calendar_as_of_query_sha256,
)
from app.config import Settings
from app.domain.common.json import JsonObject
from app.domain.market_data.calendar_as_of import (
    CalendarAsOfError,
    select_kr_daily_sessions_as_of,
)
from app.domain.market_data.point_in_time_calendar import (
    PointInTimeCalendarError,
    PointInTimeKrDailySessionV1,
)
from app.infrastructure.supabase_headers import supabase_api_headers

PIT_CALENDAR_AS_OF_READER_SCHEMA_VERSION = _READER_SCHEMA_VERSION
PIT_CALENDAR_AS_OF_CURSOR_SCHEMA_VERSION = "pit_calendar_as_of_cursor.v1"
PITCalendarAsOfReaderRpc = Literal["list_pit_kr_daily_sessions_as_of_v1"]
PIT_CALENDAR_AS_OF_READER_RPC_ALLOWLIST: frozenset[str] = frozenset(
    {"list_pit_kr_daily_sessions_as_of_v1"}
)
PIT_CALENDAR_AS_OF_ENVELOPE_FIELDS: frozenset[str] = frozenset(
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
PIT_CALENDAR_AS_OF_CURSOR_FIELDS: frozenset[str] = frozenset(
    {
        "schema_version",
        "query_sha256",
        "snapshot_token",
        "snapshot_issued_at",
        "snapshot_manifest_sha256",
        "last_session_date",
        "last_occurrence_observed_at",
        "last_calendar_revision",
        "last_calendar_revision_id",
        "last_calendar_occurrence_id",
    }
)
PIT_CALENDAR_AS_OF_ITEM_FIELDS: frozenset[str] = frozenset(
    {
        "session_date",
        "calendar_idempotency_key",
        "calendar_revision_id",
        "calendar_revision",
        "calendar_canonical_evidence_sha256",
        "calendar_revision_observed_at",
        "calendar_revision_received_at",
        "calendar_content_payload",
        "calendar_occurrence_id",
        "calendar_occurrence_observed_at",
        "calendar_occurrence_received_at",
        "calendar_occurrence_origin",
        "calendar_payload",
        "candidate_lineage_sha256",
    }
)
PIT_CALENDAR_AS_OF_LINEAGE_FIELDS: tuple[str, ...] = (
    "calendar_revision_id",
    "calendar_idempotency_key",
    "calendar_revision",
    "calendar_canonical_evidence_sha256",
    "calendar_revision_observed_at",
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
_MAX_RPC_RESPONSE_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class _RawCandidate:
    content_session: PointInTimeKrDailySessionV1
    session: PointInTimeKrDailySessionV1
    lineage: CalendarAsOfLineageV1
    candidate_lineage_sha256: str

    @property
    def order_key(self) -> tuple[date, datetime, int, str, str]:
        return (
            self.session.session_date,
            self.lineage.calendar_occurrence_observed_at,
            self.lineage.calendar_revision,
            self.lineage.calendar_revision_id,
            self.lineage.calendar_occurrence_id,
        )


class SupabaseCalendarAsOfReader:
    """Read a complete occurrence-backed KR calendar snapshot.

    Construction is explicit. This adapter is intentionally not wired into
    runtime, scheduling, research, strategy, broker, or order paths.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not settings.supabase_url or settings.supabase_secret_key is None:
            raise CalendarAsOfReaderError(
                "calendar_as_of_reader_credentials_missing"
            )
        secret = settings.supabase_secret_key.get_secret_value()
        self.base_url = settings.supabase_url.rstrip("/") + "/rest/v1/rpc"
        self.headers = supabase_api_headers(secret) | {
            "accept-profile": "worker_api",
            "accept-encoding": "identity",
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

    async def read_daily_sessions_as_of(
        self,
        request: CalendarAsOfReadRequest,
    ) -> DurableCalendarAsOfSnapshotV1:
        valid_request = _canonical_request(request)
        request_as_of = valid_request.as_of.astimezone(UTC)
        expected_query_sha256 = calendar_as_of_query_sha256(valid_request)
        cursor: JsonObject | None = None
        seen_cursors: set[str] = set()
        raw_candidates: list[_RawCandidate] = []
        expected_metadata: tuple[str, str, datetime, str, int] | None = None
        previous_order_key: tuple[date, datetime, int, str, str] | None = None
        pages_read = 0

        while True:
            pages_read += 1
            envelope = _envelope(
                await self._rpc(
                    "list_pit_kr_daily_sessions_as_of_v1",
                    {
                        "p_provider": valid_request.provider,
                        "p_market": valid_request.market,
                        "p_start_session_date": (
                            valid_request.start_session_date.isoformat()
                        ),
                        "p_end_session_date": (
                            valid_request.end_session_date.isoformat()
                        ),
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
                raise CalendarAsOfReaderError(
                    "calendar_as_of_reader_page_metadata_mismatch"
                )

            query_sha256, snapshot_token, snapshot_issued_at, manifest, count = (
                metadata
            )
            if query_sha256 != expected_query_sha256:
                raise CalendarAsOfReaderError(
                    "calendar_as_of_reader_query_mismatch"
                )
            page = _items(envelope)
            if len(page) > valid_request.page_size:
                raise CalendarAsOfReaderError(
                    "calendar_as_of_reader_page_size_exceeded"
                )
            if cursor is not None and not page:
                raise CalendarAsOfReaderError(
                    "calendar_as_of_reader_empty_continuation_page"
                )

            for raw_item in page:
                candidate = _candidate(
                    raw_item,
                    valid_request,
                    request_as_of,
                    snapshot_issued_at,
                )
                if (
                    previous_order_key is not None
                    and candidate.order_key <= previous_order_key
                ):
                    raise CalendarAsOfReaderError(
                        "calendar_as_of_reader_candidate_order_invalid"
                    )
                previous_order_key = candidate.order_key
                raw_candidates.append(candidate)
                if (
                    len(raw_candidates) > _MAX_CANDIDATES
                    or len(raw_candidates) > count
                ):
                    raise CalendarAsOfReaderError(
                        "calendar_as_of_reader_candidate_count_invalid"
                    )

            raw_next_cursor = envelope.get("next_cursor")
            cursor_fingerprint: str | None = None
            if (
                type(raw_next_cursor) is dict
                and set(raw_next_cursor) == PIT_CALENDAR_AS_OF_CURSOR_FIELDS
            ):
                cursor_fingerprint = _canonical_json_text(
                    cast(JsonObject, raw_next_cursor)
                )
                if cursor_fingerprint in seen_cursors:
                    raise CalendarAsOfReaderError(
                        "calendar_as_of_reader_cursor_cycle"
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
                raise CalendarAsOfReaderError(
                    "calendar_as_of_reader_empty_page_with_cursor"
                )
            if len(page) != valid_request.page_size:
                raise CalendarAsOfReaderError(
                    "calendar_as_of_reader_nonterminal_page_size_invalid"
                )
            maximum_pages = max(
                1,
                (count + valid_request.page_size - 1)
                // valid_request.page_size,
            )
            if pages_read >= maximum_pages:
                raise CalendarAsOfReaderError(
                    "calendar_as_of_reader_page_count_invalid"
                )
            if cursor_fingerprint is None:
                raise CalendarAsOfReaderError(
                    "calendar_as_of_reader_cursor_invalid"
                )
            seen_cursors.add(cursor_fingerprint)
            cursor = next_cursor

        if expected_metadata is None:
            raise CalendarAsOfReaderError(
                "calendar_as_of_reader_rpc_result_invalid"
            )
        query_sha256, snapshot_token, snapshot_issued_at, manifest, count = (
            expected_metadata
        )
        if len(raw_candidates) != count:
            raise CalendarAsOfReaderError(
                "calendar_as_of_reader_candidate_count_mismatch"
            )
        calculated_manifest = hashlib.sha256(
            "\n".join(
                candidate.candidate_lineage_sha256
                for candidate in raw_candidates
            ).encode("utf-8")
        ).hexdigest()
        if calculated_manifest != manifest:
            raise CalendarAsOfReaderError(
                "calendar_as_of_reader_snapshot_manifest_mismatch"
            )

        _validate_revision_timelines(raw_candidates)
        candidate_bindings = {
            candidate.session: candidate for candidate in raw_candidates
        }
        if len(candidate_bindings) != len(raw_candidates):
            raise CalendarAsOfReaderError(
                "calendar_as_of_reader_candidate_binding_ambiguous"
            )
        try:
            selected = select_kr_daily_sessions_as_of(
                [candidate.session for candidate in raw_candidates],
                as_of=request_as_of,
            )
        except CalendarAsOfError as exc:
            raise CalendarAsOfReaderError(
                "calendar_as_of_reader_selection_failed"
            ) from exc

        durable_items: list[DurableSelectedCalendarSessionV1] = []
        for selection in selected:
            source = candidate_bindings.get(selection.session)
            if source is None:
                raise CalendarAsOfReaderError(
                    "calendar_as_of_reader_selection_lineage_missing"
                )
            durable_items.append(
                DurableSelectedCalendarSessionV1(
                    selection=selection,
                    lineage=source.lineage,
                )
            )

        return DurableCalendarAsOfSnapshotV1(
            query_sha256=query_sha256,
            snapshot_token=snapshot_token,
            snapshot_issued_at=snapshot_issued_at,
            snapshot_manifest_sha256=manifest,
            candidate_count=count,
            items=tuple(durable_items),
        )

    async def _rpc(
        self,
        rpc: PITCalendarAsOfReaderRpc,
        payload: JsonObject,
    ) -> object:
        if rpc not in PIT_CALENDAR_AS_OF_READER_RPC_ALLOWLIST:
            raise CalendarAsOfReaderError(
                "calendar_as_of_reader_rpc_not_allowed"
            )
        result: object = None
        failure: str | None = None
        try:
            async with self.client.stream(
                "POST",
                f"{self.base_url}/{rpc}",
                headers=self.headers,
                json=payload,
            ) as response:
                response.raise_for_status()
                body = bytearray()
                content_encoding = response.headers.get(
                    "content-encoding", "identity"
                ).strip().lower()
                if content_encoding not in {"", "identity"}:
                    failure = (
                        "calendar_as_of_reader_rpc_content_encoding_invalid"
                    )
                else:
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > _MAX_RPC_RESPONSE_BYTES:
                            failure = (
                                "calendar_as_of_reader_rpc_response_too_large"
                            )
                            break
                        body.extend(chunk)
                if failure is None:
                    result = json.loads(body)
        except (httpx.HTTPError, RecursionError, ValueError):
            failure = (
                "calendar_as_of_reader_rpc_failed_or_returned_invalid_json"
            )
        if failure is not None:
            raise CalendarAsOfReaderError(
                failure
            )
        return result


def _canonical_request(value: object) -> CalendarAsOfReadRequest:
    if type(value) is not CalendarAsOfReadRequest:
        raise CalendarAsOfReaderError("calendar_as_of_reader_request_invalid")
    canonical: CalendarAsOfReadRequest | None = None
    with suppress(
        AttributeError,
        CalendarAsOfReaderError,
        OverflowError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        canonical = CalendarAsOfReadRequest(
            provider=value.provider,
            market=value.market,
            start_session_date=value.start_session_date,
            end_session_date=value.end_session_date,
            as_of=value.as_of,
            page_size=value.page_size,
        )
    if canonical is None:
        raise CalendarAsOfReaderError(
            "calendar_as_of_reader_request_invalid"
        )
    if canonical != value:
        raise CalendarAsOfReaderError("calendar_as_of_reader_request_invalid")
    return canonical


def _envelope(value: object) -> Mapping[str, object]:
    if type(value) is not dict or set(value) != PIT_CALENDAR_AS_OF_ENVELOPE_FIELDS:
        raise CalendarAsOfReaderError(
            "calendar_as_of_reader_rpc_result_invalid"
        )
    if value.get("schema_version") != PIT_CALENDAR_AS_OF_READER_SCHEMA_VERSION:
        raise CalendarAsOfReaderError(
            "calendar_as_of_reader_schema_version_invalid"
        )
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
        raise CalendarAsOfReaderError(
            "calendar_as_of_reader_candidate_limit_exceeded"
        )
    return query_sha256, snapshot_token, snapshot_issued_at, manifest, count


def _items(envelope: Mapping[str, object]) -> Sequence[object]:
    items = envelope.get("items")
    if type(items) is not list:
        raise CalendarAsOfReaderError("calendar_as_of_reader_items_invalid")
    return items


def _candidate(
    value: object,
    request: CalendarAsOfReadRequest,
    as_of: datetime,
    snapshot_issued_at: datetime,
) -> _RawCandidate:
    if type(value) is not dict or set(value) != PIT_CALENDAR_AS_OF_ITEM_FIELDS:
        raise CalendarAsOfReaderError(
            "calendar_as_of_reader_item_shape_invalid"
        )
    item = cast(Mapping[str, object], value)
    session_date = _canonical_date(item.get("session_date"), "session_date")
    candidate_lineage_sha256 = _sha256(
        item.get("candidate_lineage_sha256"),
        "candidate_lineage_sha256",
    )
    revision_id = _uuid(
        item.get("calendar_revision_id"), "calendar_revision_id"
    )
    idempotency_key = _sha256(
        item.get("calendar_idempotency_key"),
        "calendar_idempotency_key",
    )
    revision = _positive_int(
        item.get("calendar_revision"), "calendar_revision"
    )
    evidence_sha256 = _sha256(
        item.get("calendar_canonical_evidence_sha256"),
        "calendar_canonical_evidence_sha256",
    )
    revision_observed_at = _canonical_datetime(
        item.get("calendar_revision_observed_at"),
        "calendar_revision_observed_at",
    )
    revision_received_at = _canonical_datetime(
        item.get("calendar_revision_received_at"),
        "calendar_revision_received_at",
    )
    occurrence_id = _uuid(
        item.get("calendar_occurrence_id"), "calendar_occurrence_id"
    )
    occurrence_observed_at = _canonical_datetime(
        item.get("calendar_occurrence_observed_at"),
        "calendar_occurrence_observed_at",
    )
    occurrence_received_at = _canonical_datetime(
        item.get("calendar_occurrence_received_at"),
        "calendar_occurrence_received_at",
    )
    occurrence_origin = _origin(
        item.get("calendar_occurrence_origin"),
        "calendar_occurrence_origin",
    )

    calculated_lineage_sha256 = hashlib.sha256(
        "|".join(
            (
                revision_id,
                idempotency_key,
                str(revision),
                evidence_sha256,
                _canonical_timestamp(revision_observed_at),
                _canonical_timestamp(revision_received_at),
                occurrence_id,
                _canonical_timestamp(occurrence_observed_at),
                _canonical_timestamp(occurrence_received_at),
                occurrence_origin,
            )
        ).encode("utf-8")
    ).hexdigest()
    if calculated_lineage_sha256 != candidate_lineage_sha256:
        raise CalendarAsOfReaderError(
            "calendar_as_of_reader_candidate_lineage_mismatch"
        )

    content_payload = _payload(
        item.get("calendar_content_payload"),
        "calendar_content_payload",
    )
    occurrence_payload = _payload(
        item.get("calendar_payload"),
        "calendar_payload",
    )
    content_session = _calendar(content_payload, "calendar_content_payload")
    session = _calendar(occurrence_payload, "calendar_payload")
    if not _payloads_differ_only_by_observed_at(
        content_payload,
        occurrence_payload,
    ):
        raise CalendarAsOfReaderError(
            "calendar_as_of_reader_content_occurrence_mismatch"
        )

    if (
        session.session_date != session_date
        or content_session.session_date != session_date
        or session.idempotency_key != idempotency_key
        or content_session.idempotency_key != idempotency_key
        or session.canonical_evidence_sha256 != evidence_sha256
        or content_session.canonical_evidence_sha256 != evidence_sha256
        or content_session.observed_at.astimezone(UTC) != revision_observed_at
        or session.observed_at.astimezone(UTC) != occurrence_observed_at
    ):
        raise CalendarAsOfReaderError(
            "calendar_as_of_reader_source_lineage_mismatch"
        )
    if (
        revision_observed_at > occurrence_observed_at
        or revision_observed_at > revision_received_at
        or revision_received_at > occurrence_received_at
        or occurrence_observed_at > occurrence_received_at
        or occurrence_received_at > snapshot_issued_at
    ):
        raise CalendarAsOfReaderError(
            "calendar_as_of_reader_lineage_clock_invalid"
        )
    if (
        session.provider != request.provider
        or session.market != request.market
        or not request.start_session_date
        <= session.session_date
        <= request.end_session_date
        or occurrence_observed_at > as_of
    ):
        raise CalendarAsOfReaderError(
            "calendar_as_of_reader_candidate_outside_query"
        )

    lineage = CalendarAsOfLineageV1(
        calendar_revision_id=revision_id,
        calendar_idempotency_key=idempotency_key,
        calendar_revision=revision,
        calendar_canonical_evidence_sha256=evidence_sha256,
        calendar_revision_observed_at=revision_observed_at,
        calendar_revision_received_at=revision_received_at,
        calendar_occurrence_id=occurrence_id,
        calendar_occurrence_observed_at=occurrence_observed_at,
        calendar_occurrence_received_at=occurrence_received_at,
        calendar_occurrence_origin=occurrence_origin,
    )
    return _RawCandidate(
        content_session=content_session,
        session=session,
        lineage=lineage,
        candidate_lineage_sha256=candidate_lineage_sha256,
    )


def _validate_revision_timelines(candidates: Sequence[_RawCandidate]) -> None:
    by_stream: dict[str, list[_RawCandidate]] = {}
    for candidate in candidates:
        by_stream.setdefault(
            candidate.lineage.calendar_idempotency_key, []
        ).append(candidate)

    for stream in by_stream.values():
        revisions: dict[int, _RawCandidate] = {}
        occurrence_ids: set[str] = set()
        occurrence_clocks: set[datetime] = set()
        previous_revision = 0
        previous_candidate: _RawCandidate | None = None
        for candidate in stream:
            lineage = candidate.lineage
            if (
                lineage.calendar_occurrence_id in occurrence_ids
                or lineage.calendar_occurrence_observed_at in occurrence_clocks
            ):
                raise CalendarAsOfReaderError(
                    "calendar_as_of_reader_occurrence_ambiguous"
                )
            occurrence_ids.add(lineage.calendar_occurrence_id)
            occurrence_clocks.add(lineage.calendar_occurrence_observed_at)
            if lineage.calendar_revision < previous_revision:
                raise CalendarAsOfReaderError(
                    "calendar_as_of_reader_revision_regressed"
                )
            previous_revision = lineage.calendar_revision

            existing = revisions.get(lineage.calendar_revision)
            if existing is None:
                revisions[lineage.calendar_revision] = candidate
            elif _revision_binding(existing) != _revision_binding(candidate):
                raise CalendarAsOfReaderError(
                    "calendar_as_of_reader_revision_ambiguous"
                )

            if previous_candidate is not None and (
                lineage.calendar_occurrence_observed_at
                <= previous_candidate.lineage.calendar_occurrence_observed_at
            ):
                raise CalendarAsOfReaderError(
                    "calendar_as_of_reader_timeline_ambiguous"
                )
            previous_candidate = candidate

        revision_numbers = sorted(revisions)
        if revision_numbers != list(range(1, len(revision_numbers) + 1)):
            raise CalendarAsOfReaderError(
                "calendar_as_of_reader_revision_gap"
            )
        seen_evidence_hashes: set[str] = set()
        previous_revision_candidate: _RawCandidate | None = None
        for revision in revision_numbers:
            candidate = revisions[revision]
            evidence_hash = (
                candidate.lineage.calendar_canonical_evidence_sha256
            )
            if evidence_hash in seen_evidence_hashes:
                raise CalendarAsOfReaderError(
                    "calendar_as_of_reader_historical_hash_recurrence_ambiguous"
                )
            seen_evidence_hashes.add(evidence_hash)
            if previous_revision_candidate is not None:
                previous_revision_occurrences = [
                    item.lineage.calendar_occurrence_observed_at
                    for item in stream
                    if item.lineage.calendar_revision == revision - 1
                ]
                if (
                    candidate.lineage.calendar_revision_observed_at
                    <= max(previous_revision_occurrences)
                ):
                    raise CalendarAsOfReaderError(
                        "calendar_as_of_reader_revision_clock_invalid"
                    )
            previous_revision_candidate = candidate


def _revision_binding(candidate: _RawCandidate) -> tuple[object, ...]:
    lineage = candidate.lineage
    return (
        lineage.calendar_revision_id,
        lineage.calendar_idempotency_key,
        lineage.calendar_revision,
        lineage.calendar_canonical_evidence_sha256,
        lineage.calendar_revision_observed_at,
        lineage.calendar_revision_received_at,
        candidate.content_session,
    )


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
    if type(raw) is not dict or set(raw) != PIT_CALENDAR_AS_OF_CURSOR_FIELDS:
        raise CalendarAsOfReaderError("calendar_as_of_reader_cursor_invalid")
    cursor = cast(Mapping[str, object], raw)
    if (
        cursor.get("schema_version") != PIT_CALENDAR_AS_OF_CURSOR_SCHEMA_VERSION
        or _sha256(cursor.get("query_sha256"), "cursor_query_sha256")
        != query_sha256
        or _snapshot_token(cursor.get("snapshot_token")) != snapshot_token
        or _canonical_datetime(
            cursor.get("snapshot_issued_at"),
            "cursor_snapshot_issued_at",
        )
        != snapshot_issued_at
        or _sha256(
            cursor.get("snapshot_manifest_sha256"),
            "cursor_snapshot_manifest_sha256",
        )
        != manifest
    ):
        raise CalendarAsOfReaderError(
            "calendar_as_of_reader_cursor_mismatch"
        )
    if last_candidate is None:
        raise CalendarAsOfReaderError("calendar_as_of_reader_cursor_invalid")
    lineage = last_candidate.lineage
    if (
        _canonical_date(cursor.get("last_session_date"), "last_session_date")
        != last_candidate.session.session_date
        or _canonical_datetime(
            cursor.get("last_occurrence_observed_at"),
            "last_occurrence_observed_at",
        )
        != lineage.calendar_occurrence_observed_at
        or _positive_int(
            cursor.get("last_calendar_revision"),
            "last_calendar_revision",
        )
        != lineage.calendar_revision
        or _uuid(
            cursor.get("last_calendar_revision_id"),
            "last_calendar_revision_id",
        )
        != lineage.calendar_revision_id
        or _uuid(
            cursor.get("last_calendar_occurrence_id"),
            "last_calendar_occurrence_id",
        )
        != lineage.calendar_occurrence_id
    ):
        raise CalendarAsOfReaderError(
            "calendar_as_of_reader_cursor_position_invalid"
        )
    return cast(JsonObject, raw)


def _payload(value: object, field_name: str) -> JsonObject:
    if type(value) is not dict:
        raise CalendarAsOfReaderError(
            f"calendar_as_of_reader_{field_name}_invalid"
        )
    return cast(JsonObject, value)


def _calendar(
    value: JsonObject,
    field_name: str,
) -> PointInTimeKrDailySessionV1:
    calendar: PointInTimeKrDailySessionV1 | None = None
    with suppress(
        AttributeError,
        OverflowError,
        PointInTimeCalendarError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        calendar = PointInTimeKrDailySessionV1.from_payload(value)
    if calendar is None:
        raise CalendarAsOfReaderError(
            f"calendar_as_of_reader_{field_name}_invalid"
        )
    if calendar.to_payload() != value:
        raise CalendarAsOfReaderError(
            f"calendar_as_of_reader_{field_name}_invalid"
        )
    return calendar


def _payloads_differ_only_by_observed_at(
    content_payload: JsonObject,
    occurrence_payload: JsonObject,
) -> bool:
    content_semantics = dict(content_payload)
    occurrence_semantics = dict(occurrence_payload)
    content_semantics.pop("observed_at", None)
    occurrence_semantics.pop("observed_at", None)
    return content_semantics == occurrence_semantics


def _sha256(value: object, field_name: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise CalendarAsOfReaderError(
            f"calendar_as_of_reader_{field_name}_invalid"
        )
    return value


def _snapshot_token(value: object) -> str:
    if (
        type(value) is not str
        or len(value) > 4096
        or _SNAPSHOT_TOKEN_RE.fullmatch(value) is None
    ):
        raise CalendarAsOfReaderError(
            "calendar_as_of_reader_snapshot_token_invalid"
        )
    return value


def _uuid(value: object, field_name: str) -> str:
    if type(value) is not str:
        raise CalendarAsOfReaderError(
            f"calendar_as_of_reader_{field_name}_invalid"
        )
    parsed: UUID | None = None
    with suppress(ValueError):
        parsed = UUID(value)
    if parsed is None:
        raise CalendarAsOfReaderError(
            f"calendar_as_of_reader_{field_name}_invalid"
        )
    if str(parsed) != value or parsed.version not in {1, 2, 3, 4, 5}:
        raise CalendarAsOfReaderError(
            f"calendar_as_of_reader_{field_name}_invalid"
        )
    return value


def _positive_int(value: object, field_name: str) -> int:
    if type(value) is not int or value <= 0:
        raise CalendarAsOfReaderError(
            f"calendar_as_of_reader_{field_name}_invalid"
        )
    return value


def _nonnegative_int(value: object, field_name: str) -> int:
    if type(value) is not int or value < 0:
        raise CalendarAsOfReaderError(
            f"calendar_as_of_reader_{field_name}_invalid"
        )
    return value


def _origin(value: object, field_name: str) -> str:
    if type(value) is not str or value not in _OCCURRENCE_ORIGINS:
        raise CalendarAsOfReaderError(
            f"calendar_as_of_reader_{field_name}_invalid"
        )
    return value


def _canonical_datetime(value: object, field_name: str) -> datetime:
    if type(value) is not str:
        raise CalendarAsOfReaderError(
            f"calendar_as_of_reader_{field_name}_invalid"
        )
    canonical: datetime | None = None
    with suppress(OverflowError, RuntimeError, TypeError, ValueError):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is not None and parsed.utcoffset() is not None:
            canonical = parsed.astimezone(UTC)
    if canonical is None:
        raise CalendarAsOfReaderError(
            f"calendar_as_of_reader_{field_name}_invalid"
        )
    if _canonical_timestamp(canonical) != value:
        raise CalendarAsOfReaderError(
            f"calendar_as_of_reader_{field_name}_invalid"
        )
    return canonical


def _canonical_date(value: object, field_name: str) -> date:
    if type(value) is not str:
        raise CalendarAsOfReaderError(
            f"calendar_as_of_reader_{field_name}_invalid"
        )
    parsed: date | None = None
    with suppress(ValueError):
        parsed = date.fromisoformat(value)
    if parsed is None:
        raise CalendarAsOfReaderError(
            f"calendar_as_of_reader_{field_name}_invalid"
        )
    if parsed.isoformat() != value:
        raise CalendarAsOfReaderError(
            f"calendar_as_of_reader_{field_name}_invalid"
        )
    return parsed


def _canonical_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _canonical_json_text(value: JsonObject) -> str:
    canonical: str | None = None
    with suppress(TypeError, ValueError):
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    if canonical is None:
        raise CalendarAsOfReaderError(
            "calendar_as_of_reader_cursor_invalid"
        )
    return canonical
