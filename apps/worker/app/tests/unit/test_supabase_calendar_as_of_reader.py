from __future__ import annotations

import copy
import gzip
import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from typing import cast

import httpx
import pytest
from pydantic import SecretStr

import app.adapters.persistence.supabase_calendar_as_of_reader as reader_module
from app.adapters.persistence.supabase_calendar_as_of_reader import (
    PIT_CALENDAR_AS_OF_CURSOR_FIELDS,
    PIT_CALENDAR_AS_OF_ITEM_FIELDS,
    PIT_CALENDAR_AS_OF_LINEAGE_FIELDS,
    PIT_CALENDAR_AS_OF_READER_RPC_ALLOWLIST,
    PIT_CALENDAR_AS_OF_READER_SCHEMA_VERSION,
    SupabaseCalendarAsOfReader,
)
from app.application.ports.calendar_as_of_reader_port import (
    CalendarAsOfReaderError,
    CalendarAsOfReadRequest,
    calendar_as_of_query_sha256,
)
from app.config import Settings
from app.domain.market_data.calendar_as_of import (
    select_kr_daily_sessions_as_of,
)
from app.domain.market_data.point_in_time_calendar import (
    PointInTimeKrDailySessionV1,
)

SESSION_DATE = date(2026, 7, 20)
AS_OF = datetime(2026, 7, 19, 5, tzinfo=UTC)
SNAPSHOT_ISSUED_AT = datetime(2026, 7, 19, 8, tzinfo=UTC)
SNAPSHOT_TOKEN = "100:200:150,175"
CONTRACT_SHA256 = "1" * 64


async def test_reader_buffers_all_pages_then_selects_latest_calendar_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_session = _session(observed_hour=1, is_open=True)
    first = _raw_item(first_session, revision=1, serial=1)
    repeated = [
        _raw_item(
            _session(
                observed_hour=1,
                observed_minute=minute,
                is_open=True,
            ),
            content_session=first_session,
            revision=1,
            serial=minute + 1,
        )
        for minute in range(1, 25)
    ]
    corrected_session = _session(observed_hour=2, is_open=False)
    corrected = _raw_item(corrected_session, revision=2, serial=26)
    all_items = [first, *repeated, corrected]
    cursor = _cursor(repeated[-1], all_items, page_size=25)
    requests: list[dict[str, object]] = []
    selector_calls = 0
    real_selector = select_kr_daily_sessions_as_of

    def counting_selector(
        candidates: list[PointInTimeKrDailySessionV1],
        *,
        as_of: object,
    ) -> object:
        nonlocal selector_calls
        selector_calls += 1
        assert len(candidates) == 26
        return real_selector(candidates, as_of=as_of)

    monkeypatch.setattr(
        reader_module,
        "select_kr_daily_sessions_as_of",
        counting_selector,
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = cast(dict[str, object], json.loads(request.content))
        requests.append(payload)
        assert request.url.path.endswith("/list_pit_kr_daily_sessions_as_of_v1")
        assert request.headers["accept-profile"] == "worker_api"
        assert request.headers["accept-encoding"] == "identity"
        assert request.headers["content-profile"] == "worker_api"
        assert payload == {
            "p_provider": "krx-calendar",
            "p_market": "KR",
            "p_start_session_date": SESSION_DATE.isoformat(),
            "p_end_session_date": SESSION_DATE.isoformat(),
            "p_as_of": _timestamp(AS_OF),
            "p_limit": 25,
            "p_cursor": None if len(requests) == 1 else cursor,
        }
        if len(requests) == 1:
            return httpx.Response(
                200,
                json=_envelope(
                    [first, *repeated],
                    all_items,
                    cursor,
                    page_size=25,
                ),
            )
        return httpx.Response(
            200,
            json=_envelope([corrected], all_items, None, page_size=25),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reader = SupabaseCalendarAsOfReader(_settings(), client=client)
    try:
        snapshot = await reader.read_daily_sessions_as_of(
            _request(page_size=25)
        )
    finally:
        await client.aclose()

    assert len(requests) == 2
    assert selector_calls == 1
    assert {
        "list_pit_kr_daily_sessions_as_of_v1"
    } == PIT_CALENDAR_AS_OF_READER_RPC_ALLOWLIST
    assert snapshot.candidate_count == 26
    assert snapshot.snapshot_manifest_sha256 == _manifest(all_items)
    assert len(snapshot.items) == 1
    assert snapshot.items[0].selection.session.is_open is False
    assert snapshot.items[0].lineage.calendar_revision == 2


async def test_reader_treats_received_at_as_lineage_not_semantic_cutoff() -> None:
    session = _session(observed_hour=4, is_open=True)
    received_after_as_of = datetime(2026, 7, 19, 6, tzinfo=UTC)
    item = _raw_item(
        session,
        revision=1,
        serial=10,
        revision_received_at=received_after_as_of,
        occurrence_received_at=received_after_as_of,
    )
    reader, client = _reader_for_envelopes(
        [_envelope([item], [item], None)]
    )
    try:
        snapshot = await reader.read_daily_sessions_as_of(_request())
    finally:
        await client.aclose()

    assert len(snapshot.items) == 1
    assert snapshot.items[0].selection.session == session
    assert (
        snapshot.items[0].lineage.calendar_occurrence_received_at
        > _request().as_of
    )


@pytest.mark.parametrize(
    "field",
    [
        "calendar_idempotency_key",
        "calendar_canonical_evidence_sha256",
        "calendar_revision_id",
        "calendar_occurrence_id",
        "calendar_revision_observed_at",
        "calendar_occurrence_observed_at",
        "calendar_occurrence_origin",
    ],
)
async def test_reader_rejects_invalid_lineage_fields(field: str) -> None:
    item = _raw_item(_session(observed_hour=1, is_open=True), revision=1, serial=1)
    item[field] = "invalid"
    reader, client = _reader_for_envelopes(
        [_envelope([item], [item], None, preserve_manifest=True)]
    )
    try:
        with pytest.raises(CalendarAsOfReaderError):
            await reader.read_daily_sessions_as_of(_request())
    finally:
        await client.aclose()


async def test_reader_recomputes_lineage_and_manifest_hashes() -> None:
    item = _raw_item(_session(observed_hour=1, is_open=True), revision=1, serial=1)
    bad_lineage = copy.deepcopy(item)
    bad_lineage["candidate_lineage_sha256"] = "f" * 64
    reader, client = _reader_for_envelopes(
        [_envelope([bad_lineage], [bad_lineage], None)]
    )
    try:
        with pytest.raises(
            CalendarAsOfReaderError,
            match="candidate_lineage_mismatch",
        ):
            await reader.read_daily_sessions_as_of(_request())
    finally:
        await client.aclose()

    bad_manifest = _envelope([item], [item], None)
    bad_manifest["snapshot_manifest_sha256"] = "e" * 64
    reader, client = _reader_for_envelopes([bad_manifest])
    try:
        with pytest.raises(
            CalendarAsOfReaderError,
            match="snapshot_manifest_mismatch",
        ):
            await reader.read_daily_sessions_as_of(_request())
    finally:
        await client.aclose()


async def test_reader_rejects_payloads_that_differ_beyond_observed_at() -> None:
    open_session = _session(observed_hour=1, is_open=True)
    item = _raw_item(open_session, revision=1, serial=1)
    item["calendar_payload"] = _session(
        observed_hour=1,
        is_open=False,
    ).to_payload()
    reader, client = _reader_for_envelopes(
        [_envelope([item], [item], None, preserve_manifest=True)]
    )
    try:
        with pytest.raises(
            CalendarAsOfReaderError,
            match="content_occurrence_mismatch",
        ):
            await reader.read_daily_sessions_as_of(_request())
    finally:
        await client.aclose()


async def test_reader_rejects_revision_gaps_and_historical_recurrence() -> None:
    first_session = _session(observed_hour=1, is_open=True)
    corrected_session = _session(observed_hour=2, is_open=False)
    first = _raw_item(first_session, revision=1, serial=1)
    gap = _raw_item(corrected_session, revision=3, serial=2)
    reader, client = _reader_for_envelopes(
        [_envelope([first, gap], [first, gap], None)]
    )
    try:
        with pytest.raises(CalendarAsOfReaderError, match="revision_gap"):
            await reader.read_daily_sessions_as_of(_request())
    finally:
        await client.aclose()

    recurrent_session = _session(observed_hour=3, is_open=True)
    second = _raw_item(corrected_session, revision=2, serial=2)
    recurrent = _raw_item(recurrent_session, revision=3, serial=3)
    reader, client = _reader_for_envelopes(
        [_envelope([first, second, recurrent], [first, second, recurrent], None)]
    )
    try:
        with pytest.raises(
            CalendarAsOfReaderError,
            match="historical_hash_recurrence_ambiguous",
        ):
            await reader.read_daily_sessions_as_of(_request())
    finally:
        await client.aclose()


async def test_reader_rejects_cursor_position_and_page_metadata_changes() -> None:
    first = _raw_item(
        _session(observed_hour=1, is_open=True),
        revision=1,
        serial=1,
    )
    repeated = _raw_item(
        _session(observed_hour=2, is_open=True),
        content_session=_session(observed_hour=1, is_open=True),
        revision=1,
        serial=2,
    )
    all_items = [first, repeated]
    bad_cursor = _cursor(first, all_items)
    bad_cursor["last_calendar_occurrence_id"] = _uuid(9999)
    reader, client = _reader_for_envelopes(
        [_envelope([first], all_items, bad_cursor)]
    )
    try:
        with pytest.raises(
            CalendarAsOfReaderError,
            match="cursor_position_invalid",
        ):
            await reader.read_daily_sessions_as_of(_request())
    finally:
        await client.aclose()

    metadata_items = _same_revision_items(26)
    cursor = _cursor(
        metadata_items[24],
        metadata_items,
        page_size=25,
    )
    changed = _envelope(
        metadata_items[25:],
        metadata_items,
        None,
        page_size=25,
    )
    changed["snapshot_token"] = "101:201:151"
    reader, client = _reader_for_envelopes(
        [
            _envelope(
                metadata_items[:25],
                metadata_items,
                cursor,
                page_size=25,
            ),
            changed,
        ]
    )
    try:
        with pytest.raises(
            CalendarAsOfReaderError,
            match="page_metadata_mismatch",
        ):
            await reader.read_daily_sessions_as_of(_request(page_size=25))
    finally:
        await client.aclose()


async def test_reader_rejects_sparse_or_excess_nonterminal_pagination() -> None:
    sparse_items = _same_revision_items(2)
    sparse_cursor = _cursor(
        sparse_items[0],
        sparse_items,
        page_size=25,
    )
    reader, client = _reader_for_envelopes(
        [
            _envelope(
                sparse_items[:1],
                sparse_items,
                sparse_cursor,
                page_size=25,
            )
        ]
    )
    try:
        with pytest.raises(
            CalendarAsOfReaderError,
            match="nonterminal_page_size_invalid",
        ):
            await reader.read_daily_sessions_as_of(_request(page_size=25))
    finally:
        await client.aclose()

    full_page = _same_revision_items(25)
    excess_cursor = _cursor(
        full_page[-1],
        full_page,
        page_size=25,
    )
    reader, client = _reader_for_envelopes(
        [
            _envelope(
                full_page,
                full_page,
                excess_cursor,
                page_size=25,
            )
        ]
    )
    try:
        with pytest.raises(
            CalendarAsOfReaderError,
            match="page_count_invalid",
        ):
            await reader.read_daily_sessions_as_of(_request(page_size=25))
    finally:
        await client.aclose()


async def test_reader_rejects_candidate_count_and_raw_order_mismatch() -> None:
    first = _raw_item(
        _session(observed_hour=1, is_open=True),
        revision=1,
        serial=1,
    )
    repeated = _raw_item(
        _session(observed_hour=2, is_open=True),
        content_session=_session(observed_hour=1, is_open=True),
        revision=1,
        serial=2,
    )
    bad_count = _envelope([first], [first], None)
    bad_count["candidate_count"] = 2
    reader, client = _reader_for_envelopes([bad_count])
    try:
        with pytest.raises(
            CalendarAsOfReaderError,
            match="candidate_count_mismatch",
        ):
            await reader.read_daily_sessions_as_of(_request())
    finally:
        await client.aclose()

    reader, client = _reader_for_envelopes(
        [_envelope([repeated, first], [repeated, first], None)]
    )
    try:
        with pytest.raises(
            CalendarAsOfReaderError,
            match="candidate_order_invalid",
        ):
            await reader.read_daily_sessions_as_of(_request())
    finally:
        await client.aclose()


async def test_reader_suppresses_secret_bearing_http_and_json_errors() -> None:
    secret = "upstream-secret-must-not-leak"

    async def http_failure(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(secret, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(http_failure))
    reader = SupabaseCalendarAsOfReader(_settings(), client=client)
    try:
        with pytest.raises(CalendarAsOfReaderError) as caught:
            await reader.read_daily_sessions_as_of(_request())
    finally:
        await client.aclose()
    assert str(caught.value) == (
        "calendar_as_of_reader_rpc_failed_or_returned_invalid_json"
    )
    assert secret not in repr(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None

    async def invalid_json(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=secret)

    client = httpx.AsyncClient(transport=httpx.MockTransport(invalid_json))
    reader = SupabaseCalendarAsOfReader(_settings(), client=client)
    try:
        with pytest.raises(CalendarAsOfReaderError) as caught:
            await reader.read_daily_sessions_as_of(_request())
    finally:
        await client.aclose()
    assert secret not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


async def test_reader_bounds_streamed_rpc_response_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "oversized-secret-must-not-leak"
    monkeypatch.setattr(reader_module, "_MAX_RPC_RESPONSE_BYTES", 32)

    async def oversized(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=(secret * 4).encode("utf-8"))

    client = httpx.AsyncClient(transport=httpx.MockTransport(oversized))
    reader = SupabaseCalendarAsOfReader(_settings(), client=client)
    try:
        with pytest.raises(
            CalendarAsOfReaderError,
            match="rpc_response_too_large",
        ) as caught:
            await reader.read_daily_sessions_as_of(_request())
    finally:
        await client.aclose()
    assert secret not in repr(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


async def test_reader_rejects_compression_and_deep_json_without_leaking() -> None:
    secret = "compressed-secret-must-not-leak"

    async def compressed(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip"},
            content=gzip.compress((secret * 10_000).encode("utf-8")),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(compressed))
    reader = SupabaseCalendarAsOfReader(_settings(), client=client)
    try:
        with pytest.raises(
            CalendarAsOfReaderError,
            match="rpc_content_encoding_invalid",
        ) as caught:
            await reader.read_daily_sessions_as_of(_request())
    finally:
        await client.aclose()
    assert secret not in repr(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None

    deeply_nested = "[" * 100_000 + "]" * 100_000

    async def deep_json(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=deeply_nested.encode("utf-8"))

    client = httpx.AsyncClient(transport=httpx.MockTransport(deep_json))
    reader = SupabaseCalendarAsOfReader(_settings(), client=client)
    try:
        with pytest.raises(
            CalendarAsOfReaderError,
            match="rpc_failed_or_returned_invalid_json",
        ) as caught:
            await reader.read_daily_sessions_as_of(_request())
    finally:
        await client.aclose()
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    "field",
    [
        "session_date",
        "calendar_revision_id",
        "calendar_revision_observed_at",
        "calendar_payload_observed_at",
    ],
)
async def test_reader_suppresses_secret_bearing_parser_errors(field: str) -> None:
    secret = "malformed-secret-must-not-leak"
    item = _raw_item(
        _session(observed_hour=1, is_open=True),
        revision=1,
        serial=1,
    )
    if field == "calendar_payload_observed_at":
        payload = cast(dict[str, object], item["calendar_payload"])
        payload["observed_at"] = secret
    else:
        item[field] = secret
    reader, client = _reader_for_envelopes(
        [_envelope([item], [item], None, preserve_manifest=True)]
    )
    try:
        with pytest.raises(CalendarAsOfReaderError) as caught:
            await reader.read_daily_sessions_as_of(_request())
    finally:
        await client.aclose()
    assert secret not in repr(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_request_and_query_contract_are_bounded_and_canonical() -> None:
    request = _request(page_size=25)
    expected_json = json.dumps(
        {
            "as_of": _timestamp(AS_OF),
            "contract_version": "pit_calendar_as_of_reader.v1",
            "end_session_date": SESSION_DATE.isoformat(),
            "limit": 25,
            "market": "KR",
            "provider": "krx-calendar",
            "start_session_date": SESSION_DATE.isoformat(),
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert calendar_as_of_query_sha256(request) == hashlib.sha256(
        expected_json.encode("utf-8")
    ).hexdigest()

    CalendarAsOfReadRequest(
        provider="krx-calendar",
        market="KR",
        start_session_date=SESSION_DATE,
        end_session_date=SESSION_DATE + timedelta(days=365),
        as_of=AS_OF,
        page_size=100,
    )
    with pytest.raises(CalendarAsOfReaderError, match="date_range_too_large"):
        CalendarAsOfReadRequest(
            provider="krx-calendar",
            market="KR",
            start_session_date=SESSION_DATE,
            end_session_date=SESSION_DATE + timedelta(days=366),
            as_of=AS_OF,
        )
    with pytest.raises(CalendarAsOfReaderError, match="page_size_invalid"):
        _request(page_size=24)
    with pytest.raises(CalendarAsOfReaderError, match="as_of_invalid"):
        CalendarAsOfReadRequest(
            provider="krx-calendar",
            market="KR",
            start_session_date=SESSION_DATE,
            end_session_date=SESSION_DATE,
            as_of=datetime(2026, 7, 19, 5),
        )


def test_reader_requires_worker_credentials_and_exact_item_contract() -> None:
    with pytest.raises(CalendarAsOfReaderError, match="credentials_missing"):
        SupabaseCalendarAsOfReader(Settings())
    assert PIT_CALENDAR_AS_OF_READER_SCHEMA_VERSION == (
        "pit_calendar_as_of_reader.v1"
    )
    assert PIT_CALENDAR_AS_OF_LINEAGE_FIELDS == (
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
    assert len(PIT_CALENDAR_AS_OF_ITEM_FIELDS) == 14
    assert len(PIT_CALENDAR_AS_OF_CURSOR_FIELDS) == 10


def _request(*, page_size: int = 100) -> CalendarAsOfReadRequest:
    return CalendarAsOfReadRequest(
        provider="krx-calendar",
        market="KR",
        start_session_date=SESSION_DATE,
        end_session_date=SESSION_DATE,
        as_of=AS_OF,
        page_size=page_size,
    )


def _settings() -> Settings:
    return Settings(
        SUPABASE_URL="https://project.supabase.co",
        SUPABASE_SECRET_KEY=SecretStr("worker-secret"),
    )


def _reader_for_envelopes(
    envelopes: list[dict[str, object]],
) -> tuple[SupabaseCalendarAsOfReader, httpx.AsyncClient]:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if calls >= len(envelopes):
            return httpx.Response(500)
        result = envelopes[calls]
        calls += 1
        return httpx.Response(200, json=result)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return SupabaseCalendarAsOfReader(_settings(), client=client), client


def _raw_item(
    session: PointInTimeKrDailySessionV1,
    *,
    revision: int,
    serial: int,
    content_session: PointInTimeKrDailySessionV1 | None = None,
    revision_received_at: datetime | None = None,
    occurrence_received_at: datetime | None = None,
) -> dict[str, object]:
    content = content_session or session
    revision_received = revision_received_at or (
        content.observed_at + timedelta(minutes=10)
    )
    occurrence_received = occurrence_received_at or (
        session.observed_at + timedelta(minutes=10)
    )
    item: dict[str, object] = {
        "session_date": session.session_date.isoformat(),
        "calendar_idempotency_key": session.idempotency_key,
        "calendar_revision_id": _uuid(1000 + revision),
        "calendar_revision": revision,
        "calendar_canonical_evidence_sha256": (
            session.canonical_evidence_sha256
        ),
        "calendar_revision_observed_at": _timestamp(content.observed_at),
        "calendar_revision_received_at": _timestamp(revision_received),
        "calendar_content_payload": content.to_payload(),
        "calendar_occurrence_id": _uuid(2000 + serial),
        "calendar_occurrence_observed_at": _timestamp(session.observed_at),
        "calendar_occurrence_received_at": _timestamp(occurrence_received),
        "calendar_occurrence_origin": "rpc",
        "calendar_payload": session.to_payload(),
        "candidate_lineage_sha256": "0" * 64,
    }
    assert set(item) == PIT_CALENDAR_AS_OF_ITEM_FIELDS
    _rehash_item(item)
    return item


def _rehash_item(item: dict[str, object]) -> None:
    canonical_line = "|".join(
        str(item[field]) for field in PIT_CALENDAR_AS_OF_LINEAGE_FIELDS
    )
    item["candidate_lineage_sha256"] = hashlib.sha256(
        canonical_line.encode("utf-8")
    ).hexdigest()


def _manifest(items: list[dict[str, object]]) -> str:
    return hashlib.sha256(
        "\n".join(
            cast(str, item["candidate_lineage_sha256"]) for item in items
        ).encode("utf-8")
    ).hexdigest()


def _envelope(
    page: list[dict[str, object]],
    all_items: list[dict[str, object]],
    next_cursor: dict[str, object] | None,
    *,
    page_size: int = 100,
    preserve_manifest: bool = False,
) -> dict[str, object]:
    manifest_items = all_items
    if preserve_manifest:
        manifest_items = [copy.deepcopy(item) for item in all_items]
    return {
        "schema_version": PIT_CALENDAR_AS_OF_READER_SCHEMA_VERSION,
        "query_sha256": _query_sha256(page_size),
        "snapshot_token": SNAPSHOT_TOKEN,
        "snapshot_issued_at": _timestamp(SNAPSHOT_ISSUED_AT),
        "snapshot_manifest_sha256": _manifest(manifest_items),
        "candidate_count": len(all_items),
        "items": page,
        "next_cursor": next_cursor,
    }


def _cursor(
    item: dict[str, object],
    all_items: list[dict[str, object]],
    *,
    page_size: int = 100,
) -> dict[str, object]:
    cursor = {
        "schema_version": "pit_calendar_as_of_cursor.v1",
        "query_sha256": _query_sha256(page_size),
        "snapshot_token": SNAPSHOT_TOKEN,
        "snapshot_issued_at": _timestamp(SNAPSHOT_ISSUED_AT),
        "snapshot_manifest_sha256": _manifest(all_items),
        "last_session_date": item["session_date"],
        "last_occurrence_observed_at": item[
            "calendar_occurrence_observed_at"
        ],
        "last_calendar_revision": item["calendar_revision"],
        "last_calendar_revision_id": item["calendar_revision_id"],
        "last_calendar_occurrence_id": item["calendar_occurrence_id"],
    }
    assert set(cursor) == PIT_CALENDAR_AS_OF_CURSOR_FIELDS
    return cursor


def _query_sha256(page_size: int) -> str:
    return calendar_as_of_query_sha256(_request(page_size=page_size))


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _uuid(serial: int) -> str:
    return f"00000000-0000-4000-8000-{serial:012d}"


def _session(
    *,
    observed_hour: int,
    observed_minute: int = 0,
    is_open: bool,
) -> PointInTimeKrDailySessionV1:
    next_business_date = SESSION_DATE + timedelta(days=1)
    return PointInTimeKrDailySessionV1.create(
        provider="krx-calendar",
        market="KR",
        session_date=SESSION_DATE,
        is_open=is_open,
        regular_start_at=(
            datetime.combine(SESSION_DATE, datetime.min.time(), UTC)
            if is_open
            else None
        ),
        regular_end_at=(
            datetime.combine(SESSION_DATE, datetime.min.time(), UTC)
            + timedelta(hours=6, minutes=30)
            if is_open
            else None
        ),
        next_business_date=next_business_date,
        next_regular_start_at=datetime.combine(
            next_business_date,
            datetime.min.time(),
            UTC,
        ),
        next_regular_end_at=datetime.combine(
            next_business_date,
            datetime.min.time(),
            UTC,
        )
        + timedelta(hours=6, minutes=30),
        observed_at=datetime(
            2026,
            7,
            19,
            observed_hour,
            observed_minute,
            tzinfo=UTC,
        ),
        provider_contract_sha256=CONTRACT_SHA256,
    )


def _same_revision_items(count: int) -> list[dict[str, object]]:
    assert 1 <= count <= 59
    content = _session(observed_hour=1, is_open=True)
    items = [_raw_item(content, revision=1, serial=1)]
    items.extend(
        _raw_item(
            _session(
                observed_hour=1,
                observed_minute=minute,
                is_open=True,
            ),
            content_session=content,
            revision=1,
            serial=minute + 1,
        )
        for minute in range(1, count)
    )
    return items
