from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import pytest
from pydantic import SecretStr

import app.adapters.persistence.supabase_daily_candle_as_of_reader as reader_module
from app.adapters.persistence.supabase_daily_candle_as_of_reader import (
    PIT_DAILY_CANDLE_AS_OF_ITEM_FIELDS,
    PIT_DAILY_CANDLE_AS_OF_LINEAGE_FIELDS,
    PIT_DAILY_CANDLE_AS_OF_READER_RPC_ALLOWLIST,
    PIT_DAILY_CANDLE_AS_OF_READER_SCHEMA_VERSION,
    SupabaseDailyCandleAsOfReader,
)
from app.application.ports.daily_candle_as_of_reader_port import (
    DailyCandleAsOfReaderError,
    DailyCandleAsOfReadRequest,
    DurableSelectedDailyCandleV1,
)
from app.config import Settings
from app.domain.market_data.daily_candle_as_of import select_daily_candles_as_of
from app.domain.market_data.daily_candle_timing import (
    build_daily_candle_timing_evidence,
)
from app.domain.market_data.point_in_time import PointInTimeCandleV1
from app.domain.market_data.point_in_time_calendar import (
    PointInTimeKrDailySessionV1,
)
from app.tests.unit.test_daily_candle_timing import (
    CUTOFF,
    SESSION_DATE,
    _candle,
    _session,
)

SNAPSHOT_TOKEN = "100:200:150,175"
SNAPSHOT_ISSUED_AT = datetime(2026, 3, 27, 1, 0, tzinfo=UTC)
AS_OF = CUTOFF + timedelta(hours=12)


async def test_reader_buffers_pages_and_returns_only_selected_durable_result() -> None:
    first_candle = _candle(observed_at=CUTOFF, close_krw=72_000)
    second_candle = _candle(
        observed_at=CUTOFF + timedelta(hours=1),
        close_krw=72_100,
    )
    first = _raw_item(first_candle, _session(), timing_revision=1, serial=1)
    second = _raw_item(
        second_candle,
        _session(observed_at=CUTOFF + timedelta(minutes=30)),
        timing_revision=2,
        candle_revision=2,
        calendar_revision=2,
        serial=2,
    )
    all_items = [first, second]
    first_cursor = _cursor(first, all_items, page_size=25)
    requests: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = cast(dict[str, object], json.loads(request.content))
        requests.append(payload)
        assert request.url.path.endswith("/list_pit_daily_candles_as_of_v1")
        assert request.headers["accept-profile"] == "worker_api"
        assert request.headers["content-profile"] == "worker_api"
        assert payload == {
            "p_provider": "toss",
            "p_market": "KR",
            "p_symbol": "005930",
            "p_interval": "1d",
            "p_adjusted": True,
            "p_start_session_date": SESSION_DATE.isoformat(),
            "p_end_session_date": SESSION_DATE.isoformat(),
            "p_as_of": _timestamp(AS_OF),
            "p_limit": 25,
            "p_cursor": None if len(requests) == 1 else first_cursor,
        }
        page = [first] if len(requests) == 1 else [second]
        cursor = first_cursor if len(requests) == 1 else None
        return httpx.Response(
            200,
            json=_envelope(page, all_items, cursor, page_size=25),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reader = SupabaseDailyCandleAsOfReader(_settings(), client=client)
    try:
        snapshot = await reader.read_daily_candles_as_of(_request(page_size=25))
    finally:
        await client.aclose()

    assert len(requests) == 2
    assert {"list_pit_daily_candles_as_of_v1"} == (
        PIT_DAILY_CANDLE_AS_OF_READER_RPC_ALLOWLIST
    )
    assert snapshot.candidate_count == 2
    assert snapshot.snapshot_manifest_sha256 == _manifest(all_items)
    assert len(snapshot.items) == 1
    selected = snapshot.items[0]
    assert selected.selection.candle.close_krw == 72_100
    assert selected.lineage.timing_revision == 2
    assert selected.calendar.observed_at == CUTOFF + timedelta(minutes=30)


async def test_reader_precollapses_legal_same_max_component_advance_and_calls_selector_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candle = _candle(observed_at=CUTOFF + timedelta(hours=2))
    first_calendar = _session(observed_at=CUTOFF)
    second_calendar = _session(observed_at=CUTOFF + timedelta(hours=1))
    first = _raw_item(
        candle,
        first_calendar,
        timing_revision=1,
        candle_occurrence_serial=501,
        serial=10,
    )
    second = _raw_item(
        candle,
        second_calendar,
        timing_revision=2,
        calendar_revision=1,
        candle_occurrence_serial=501,
        serial=11,
    )
    items = [first, second]
    calls: list[list[object]] = []
    original = select_daily_candles_as_of

    def tracking_selector(candidates: list[object], *, as_of: object) -> object:
        calls.append(candidates)
        return original(candidates, as_of=as_of)

    monkeypatch.setattr(reader_module, "select_daily_candles_as_of", tracking_selector)
    reader, client = _reader_for_envelopes([_envelope(items, items, None)])
    try:
        snapshot = await reader.read_daily_candles_as_of(_request())
    finally:
        await client.aclose()

    assert len(calls) == 1
    assert len(calls[0]) == 1
    assert snapshot.items[0].lineage.timing_revision == 2
    assert snapshot.items[0].calendar.observed_at == second_calendar.observed_at


async def test_reader_calls_selector_once_for_valid_empty_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    original = select_daily_candles_as_of

    def tracking_selector(candidates: list[object], *, as_of: object) -> object:
        nonlocal calls
        calls += 1
        return original(candidates, as_of=as_of)

    monkeypatch.setattr(reader_module, "select_daily_candles_as_of", tracking_selector)
    reader, client = _reader_for_envelopes([_envelope([], [], None)])
    try:
        snapshot = await reader.read_daily_candles_as_of(_request())
    finally:
        await client.aclose()

    assert calls == 1
    assert snapshot.candidate_count == 0
    assert snapshot.items == ()


@pytest.mark.parametrize(
    "mutation",
    [
        {"unexpected": True},
        {"schema_version": "pit_daily_candle_as_of_reader.v2"},
        {"query_sha256": "bad"},
        {"snapshot_token": "not-a-snapshot"},
        {"snapshot_issued_at": "2026-03-26T01:00:00+00:00"},
        {"candidate_count": True},
        {"items": {}},
    ],
)
async def test_reader_rejects_malformed_exact_envelope(mutation: dict[str, object]) -> None:
    envelope = _envelope([], [], None)
    envelope.update(mutation)
    reader, client = _reader_for_envelopes([envelope])
    try:
        with pytest.raises(DailyCandleAsOfReaderError):
            await reader.read_daily_candles_as_of(_request())
    finally:
        await client.aclose()


async def test_reader_rejects_cross_page_metadata_mismatch_without_result() -> None:
    first = _raw_item(_candle(), _session(), timing_revision=1, serial=20)
    second = _raw_item(
        _candle(observed_at=CUTOFF + timedelta(hours=1), close_krw=72_100),
        _session(),
        timing_revision=2,
        candle_revision=2,
        serial=21,
    )
    items = [first, second]
    cursor = _cursor(first, items, page_size=25)
    page_one = _envelope([first], items, cursor, page_size=25)
    page_two = _envelope([second], items, None, page_size=25)
    page_two["query_sha256"] = "2" * 64
    reader, client = _reader_for_envelopes([page_one, page_two])
    try:
        with pytest.raises(
            DailyCandleAsOfReaderError,
            match="page_metadata_mismatch",
        ):
            await reader.read_daily_candles_as_of(_request(page_size=25))
    finally:
        await client.aclose()


async def test_reader_binds_even_empty_snapshot_to_exact_request_query() -> None:
    envelope = _envelope([], [], None)
    envelope["query_sha256"] = "2" * 64
    reader, client = _reader_for_envelopes([envelope])
    try:
        with pytest.raises(DailyCandleAsOfReaderError, match="query_mismatch"):
            await reader.read_daily_candles_as_of(_request())
    finally:
        await client.aclose()


async def test_reader_maps_extreme_timestamp_overflow_to_safe_error() -> None:
    envelope = _envelope([], [], None)
    envelope["snapshot_issued_at"] = "9999-12-31T23:59:59-23:59"
    reader, client = _reader_for_envelopes([envelope])
    try:
        with pytest.raises(
            DailyCandleAsOfReaderError,
            match="snapshot_issued_at_invalid",
        ):
            await reader.read_daily_candles_as_of(_request())
    finally:
        await client.aclose()


async def test_reader_recomputes_candidate_and_snapshot_manifest() -> None:
    item = _raw_item(_candle(), _session(), timing_revision=1, serial=30)
    bad_candidate = dict(item)
    bad_candidate["candidate_lineage_sha256"] = "f" * 64
    reader, client = _reader_for_envelopes(
        [_envelope([bad_candidate], [bad_candidate], None)]
    )
    try:
        with pytest.raises(
            DailyCandleAsOfReaderError,
            match="candidate_lineage_mismatch",
        ):
            await reader.read_daily_candles_as_of(_request())
    finally:
        await client.aclose()

    good_envelope = _envelope([item], [item], None)
    good_envelope["snapshot_manifest_sha256"] = "e" * 64
    reader, client = _reader_for_envelopes([good_envelope])
    try:
        with pytest.raises(
            DailyCandleAsOfReaderError,
            match="snapshot_manifest_mismatch",
        ):
            await reader.read_daily_candles_as_of(_request())
    finally:
        await client.aclose()


async def test_reader_enforces_candidate_cap_before_buffering() -> None:
    envelope = _envelope([], [], None)
    envelope["candidate_count"] = 1_001
    reader, client = _reader_for_envelopes([envelope])
    try:
        with pytest.raises(
            DailyCandleAsOfReaderError,
            match="candidate_limit_exceeded",
        ):
            await reader.read_daily_candles_as_of(_request())
    finally:
        await client.aclose()


async def test_reader_rejects_cursor_cycle() -> None:
    first = _raw_item(_candle(), _session(), timing_revision=1, serial=40)
    second = _raw_item(
        _candle(observed_at=CUTOFF + timedelta(hours=1), close_krw=72_100),
        _session(),
        timing_revision=2,
        candle_revision=2,
        serial=41,
    )
    third = _raw_item(
        _candle(observed_at=CUTOFF + timedelta(hours=2), close_krw=72_200),
        _session(),
        timing_revision=3,
        candle_revision=3,
        serial=42,
    )
    items = [first, second, third]
    repeated_cursor = _cursor(first, items, page_size=25)
    pages = [
        _envelope([first], items, repeated_cursor, page_size=25),
        _envelope([second], items, repeated_cursor, page_size=25),
    ]
    reader, client = _reader_for_envelopes(pages)
    try:
        with pytest.raises(DailyCandleAsOfReaderError, match="cursor_cycle"):
            await reader.read_daily_candles_as_of(_request(page_size=25))
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "mutation",
    [
        {"unknown": "value"},
        {"timing_revision": True},
        {"candle_occurrence_id": "not-a-uuid"},
        {"candle_occurrence_origin": "forged"},
        {"timing_evidence_available_at": "2026-03-26T00:00:00+00:00"},
    ],
)
async def test_reader_rejects_malformed_item_shape_or_lineage(
    mutation: dict[str, object],
) -> None:
    item = _raw_item(_candle(), _session(), timing_revision=1, serial=50)
    item.update(mutation)
    reader, client = _reader_for_envelopes([_envelope([item], [item], None)])
    try:
        with pytest.raises(DailyCandleAsOfReaderError):
            await reader.read_daily_candles_as_of(_request())
    finally:
        await client.aclose()


async def test_reader_rejects_duplicate_or_out_of_order_candidate() -> None:
    item = _raw_item(_candle(), _session(), timing_revision=1, serial=60)
    items = [item, dict(item)]
    reader, client = _reader_for_envelopes([_envelope(items, items, None)])
    try:
        with pytest.raises(
            DailyCandleAsOfReaderError,
            match="candidate_order_invalid",
        ):
            await reader.read_daily_candles_as_of(_request())
    finally:
        await client.aclose()


async def test_reader_rejects_missing_timing_revision() -> None:
    first = _raw_item(_candle(), _session(), timing_revision=1, serial=65)
    third = _raw_item(
        _candle(observed_at=CUTOFF + timedelta(hours=1), close_krw=72_100),
        _session(),
        timing_revision=3,
        candle_revision=2,
        serial=66,
    )
    items = [first, third]
    reader, client = _reader_for_envelopes([_envelope(items, items, None)])
    try:
        with pytest.raises(DailyCandleAsOfReaderError, match="timing_revision_gap"):
            await reader.read_daily_candles_as_of(_request())
    finally:
        await client.aclose()


async def test_reader_rejects_timing_received_clock_regression() -> None:
    first = _raw_item(_candle(), _session(), timing_revision=1, serial=75)
    second = _raw_item(
        _candle(observed_at=CUTOFF + timedelta(hours=1), close_krw=72_100),
        _session(),
        timing_revision=2,
        candle_revision=2,
        serial=76,
    )
    regressed_received_at = _timestamp(CUTOFF + timedelta(hours=1, minutes=1))
    for field in (
        "timing_received_at",
        "candle_revision_received_at",
        "candle_occurrence_received_at",
        "calendar_revision_received_at",
        "calendar_occurrence_received_at",
    ):
        second[field] = regressed_received_at
    _rehash_item(second)
    items = [first, second]
    reader, client = _reader_for_envelopes([_envelope(items, items, None)])
    try:
        with pytest.raises(
            DailyCandleAsOfReaderError,
            match="timing_revision_regressed",
        ):
            await reader.read_daily_candles_as_of(_request())
    finally:
        await client.aclose()


async def test_reader_rejects_received_clock_after_snapshot_issue() -> None:
    item = _raw_item(_candle(), _session(), timing_revision=1, serial=77)
    future_received_at = _timestamp(SNAPSHOT_ISSUED_AT + timedelta(minutes=1))
    for field in (
        "timing_received_at",
        "candle_revision_received_at",
        "candle_occurrence_received_at",
        "calendar_revision_received_at",
        "calendar_occurrence_received_at",
    ):
        item[field] = future_received_at
    _rehash_item(item)
    reader, client = _reader_for_envelopes([_envelope([item], [item], None)])
    try:
        with pytest.raises(
            DailyCandleAsOfReaderError,
            match="lineage_clock_invalid",
        ):
            await reader.read_daily_candles_as_of(_request())
    finally:
        await client.aclose()


async def test_reader_wraps_selector_ambiguity_without_payload_disclosure() -> None:
    first = _raw_item(
        _candle(close_krw=72_000),
        _session(),
        timing_revision=1,
        serial=70,
    )
    second = _raw_item(
        _candle(observed_at=CUTOFF + timedelta(hours=1), close_krw=72_100),
        _session(),
        timing_revision=2,
        candle_revision=2,
        serial=71,
    )
    recurrence = _raw_item(
        _candle(observed_at=CUTOFF + timedelta(hours=2), close_krw=72_000),
        _session(),
        timing_revision=3,
        candle_revision=1,
        serial=72,
    )
    items = [first, second, recurrence]
    reader, client = _reader_for_envelopes([_envelope(items, items, None)])
    try:
        with pytest.raises(
            DailyCandleAsOfReaderError,
            match="^daily_candle_as_of_reader_selection_failed$",
        ) as exc_info:
            await reader.read_daily_candles_as_of(_request())
    finally:
        await client.aclose()

    assert "72000" not in str(exc_info.value)
    assert "005930" not in str(exc_info.value)


async def test_reader_rejects_payload_binding_mismatch() -> None:
    item = _raw_item(_candle(), _session(), timing_revision=1, serial=80)
    other_calendar = _session(observed_at=CUTOFF + timedelta(hours=1))
    item["calendar_payload"] = other_calendar.to_payload()
    item["calendar_occurrence_observed_at"] = _timestamp(other_calendar.observed_at)
    _rehash_item(item)
    reader, client = _reader_for_envelopes([_envelope([item], [item], None)])
    try:
        with pytest.raises(
            DailyCandleAsOfReaderError,
            match="source_binding_invalid",
        ):
            await reader.read_daily_candles_as_of(_request())
    finally:
        await client.aclose()


def test_durable_result_rejects_cross_bound_calendar() -> None:
    item = _raw_item(_candle(), _session(), timing_revision=1, serial=74)
    candidate = reader_module._candidate(
        item,
        _request(),
        AS_OF,
        SNAPSHOT_ISSUED_AT,
    )
    selected = select_daily_candles_as_of(
        [(candidate.candle, candidate.timing)],
        as_of=AS_OF,
    )[0]

    with pytest.raises(DailyCandleAsOfReaderError, match="snapshot_invalid"):
        DurableSelectedDailyCandleV1(
            selection=selected,
            calendar=_session(observed_at=CUTOFF + timedelta(minutes=1)),
            lineage=candidate.lineage,
        )


@pytest.mark.parametrize(
    "page_size",
    [True, False, 0, 24, 101, 25.0, "25"],
)
def test_request_rejects_boolean_and_out_of_range_page_sizes(page_size: object) -> None:
    with pytest.raises(DailyCandleAsOfReaderError, match="page_size_invalid"):
        _request(page_size=cast(Any, page_size))


async def test_reader_maps_transport_failure_to_safe_error() -> None:
    secret_body = "upstream payload contained secret=do-not-return"

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text=secret_body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reader = SupabaseDailyCandleAsOfReader(_settings(), client=client)
    try:
        with pytest.raises(
            DailyCandleAsOfReaderError,
            match="rpc_failed_or_returned_invalid_json",
        ) as exc_info:
            await reader.read_daily_candles_as_of(_request())
    finally:
        await client.aclose()
    assert secret_body not in str(exc_info.value)


def test_reader_requires_worker_credentials() -> None:
    with pytest.raises(
        DailyCandleAsOfReaderError,
        match="credentials_missing",
    ):
        SupabaseDailyCandleAsOfReader(Settings())


def _request(*, page_size: int = 100) -> DailyCandleAsOfReadRequest:
    return DailyCandleAsOfReadRequest(
        provider="toss",
        market="KR",
        symbol="005930",
        interval="1d",
        adjusted=True,
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
) -> tuple[SupabaseDailyCandleAsOfReader, httpx.AsyncClient]:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if calls >= len(envelopes):
            return httpx.Response(500)
        result = envelopes[calls]
        calls += 1
        return httpx.Response(200, json=result)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return SupabaseDailyCandleAsOfReader(_settings(), client=client), client


def _raw_item(
    candle: PointInTimeCandleV1,
    calendar: PointInTimeKrDailySessionV1,
    *,
    timing_revision: int,
    serial: int,
    candle_revision: int = 1,
    calendar_revision: int = 1,
    candle_occurrence_serial: int | None = None,
) -> dict[str, object]:
    timing = build_daily_candle_timing_evidence(candle, calendar)
    base_received_at = timing.evidence_available_at + timedelta(minutes=serial)
    item: dict[str, object] = {
        "candidate_lineage_sha256": "0" * 64,
        "timing_revision_id": _uuid(1000 + serial),
        "timing_idempotency_key": timing.idempotency_key,
        "timing_revision": timing_revision,
        "timing_canonical_evidence_sha256": timing.canonical_timing_evidence_sha256,
        "timing_evidence_available_at": _timestamp(timing.evidence_available_at),
        "timing_received_at": _timestamp(base_received_at),
        "timing_payload": timing.to_payload(),
        "candle_revision_id": _uuid(2000 + candle_revision),
        "candle_revision": candle_revision,
        "candle_canonical_observation_sha256": candle.canonical_observation_sha256,
        "candle_revision_received_at": _timestamp(base_received_at),
        "candle_occurrence_id": _uuid(candle_occurrence_serial or 3000 + serial),
        "candle_occurrence_observed_at": _timestamp(candle.observed_at),
        "candle_occurrence_received_at": _timestamp(base_received_at),
        "candle_occurrence_origin": "rpc",
        "candle_payload": candle.to_payload(),
        "calendar_revision_id": _uuid(4000 + calendar_revision),
        "calendar_revision": calendar_revision,
        "calendar_canonical_evidence_sha256": calendar.canonical_evidence_sha256,
        "calendar_revision_received_at": _timestamp(base_received_at),
        "calendar_occurrence_id": _uuid(5000 + serial),
        "calendar_occurrence_observed_at": _timestamp(calendar.observed_at),
        "calendar_occurrence_received_at": _timestamp(base_received_at),
        "calendar_occurrence_origin": "rpc",
        "calendar_payload": calendar.to_payload(),
    }
    assert set(item) == PIT_DAILY_CANDLE_AS_OF_ITEM_FIELDS
    _rehash_item(item)
    return item


def _rehash_item(item: dict[str, object]) -> None:
    canonical_line = "|".join(str(item[field]) for field in PIT_DAILY_CANDLE_AS_OF_LINEAGE_FIELDS)
    item["candidate_lineage_sha256"] = hashlib.sha256(
        canonical_line.encode("utf-8")
    ).hexdigest()


def _manifest(items: list[dict[str, object]]) -> str:
    return hashlib.sha256(
        "\n".join(cast(str, item["candidate_lineage_sha256"]) for item in items).encode(
            "utf-8"
        )
    ).hexdigest()


def _envelope(
    page: list[dict[str, object]],
    all_items: list[dict[str, object]],
    next_cursor: dict[str, object] | None,
    *,
    page_size: int = 100,
) -> dict[str, object]:
    return {
        "schema_version": PIT_DAILY_CANDLE_AS_OF_READER_SCHEMA_VERSION,
        "query_sha256": _query_sha256(page_size),
        "snapshot_token": SNAPSHOT_TOKEN,
        "snapshot_issued_at": _timestamp(SNAPSHOT_ISSUED_AT),
        "snapshot_manifest_sha256": _manifest(all_items),
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
    timing_payload = cast(dict[str, object], item["timing_payload"])
    return {
        "schema_version": "pit_daily_candle_as_of_cursor.v1",
        "query_sha256": _query_sha256(page_size),
        "snapshot_token": SNAPSHOT_TOKEN,
        "snapshot_issued_at": _timestamp(SNAPSHOT_ISSUED_AT),
        "snapshot_manifest_sha256": _manifest(all_items),
        "last_session_date": timing_payload["session_date"],
        "last_candle_observed_at": item["candle_occurrence_observed_at"],
        "last_evidence_available_at": item["timing_evidence_available_at"],
        "last_timing_revision": item["timing_revision"],
        "last_timing_revision_id": item["timing_revision_id"],
    }


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _query_sha256(page_size: int) -> str:
    canonical = json.dumps(
        {
            "adjusted": True,
            "as_of": _timestamp(AS_OF),
            "contract_version": PIT_DAILY_CANDLE_AS_OF_READER_SCHEMA_VERSION,
            "end_session_date": SESSION_DATE.isoformat(),
            "interval": "1d",
            "limit": page_size,
            "market": "KR",
            "provider": "toss",
            "start_session_date": SESSION_DATE.isoformat(),
            "symbol": "005930",
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _uuid(value: int) -> str:
    return f"00000000-0000-4000-8000-{value:012x}"
