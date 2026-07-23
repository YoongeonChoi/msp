from __future__ import annotations

import hashlib
import json
import traceback
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

import pytest

from app.application.ports.daily_candle_as_of_reader_port import (
    PIT_DAILY_CANDLE_AS_OF_READER_SCHEMA_VERSION,
    DailyCandleAsOfLineageV1,
    DailyCandleAsOfReaderError,
    DailyCandleAsOfReadRequest,
    DurableDailyCandleAsOfSnapshotV1,
    DurableSelectedDailyCandleV1,
    daily_candle_as_of_query_sha256,
)
from app.application.services.daily_candle_research_slice import (
    PIT_DAILY_CANDLE_RESEARCH_SLICE_COVERAGE_SCOPE,
    PIT_DAILY_CANDLE_RESEARCH_SLICE_LIMITATIONS,
    ContiguousDailyCandleResearchSliceV1,
    DailyCandleResearchSliceError,
    DailyCandleResearchSliceService,
    build_contiguous_daily_candle_research_slice,
    validate_contiguous_daily_candle_research_slice,
)
from app.domain.common.time import KST
from app.domain.market_data.daily_candle_as_of import (
    select_daily_candles_as_of,
)
from app.domain.market_data.daily_candle_timing import (
    build_daily_candle_timing_evidence,
)
from app.domain.market_data.point_in_time import PointInTimeCandleV1
from app.domain.market_data.point_in_time_calendar import (
    PointInTimeKrDailySessionV1,
)

FIRST_SESSION = date(2026, 3, 23)
SECOND_SESSION = date(2026, 3, 24)
LAST_SESSION = date(2026, 3, 25)
AFTER_LAST_SESSION = date(2026, 3, 26)
AS_OF = datetime(2026, 3, 27, 3, 0, tzinfo=UTC)
SNAPSHOT_ISSUED_AT = datetime(2026, 3, 30, 0, 0, tzinfo=UTC)
CANDLE_CONTRACT = "a" * 64
CALENDAR_CONTRACT = "b" * 64
RAW_MANIFEST = "c" * 64


class FakeReader:
    def __init__(
        self,
        snapshot: object,
        *,
        error: Exception | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.error = error
        self.calls: list[DailyCandleAsOfReadRequest] = []

    async def read_daily_candles_as_of(
        self,
        request: DailyCandleAsOfReadRequest,
    ) -> DurableDailyCandleAsOfSnapshotV1:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return cast(DurableDailyCandleAsOfSnapshotV1, self.snapshot)


async def test_service_builds_three_session_slice_after_one_complete_read() -> None:
    request = _request()
    items = _items()
    snapshot = _snapshot(request, items)
    reader = FakeReader(snapshot)

    result = await DailyCandleResearchSliceService(reader).build_contiguous_slice(request)

    assert reader.calls == [request]
    assert result.provider == "toss"
    assert result.market == "KR"
    assert result.symbol == "005930"
    assert result.start_session_date == FIRST_SESSION
    assert result.end_session_date == LAST_SESSION
    assert result.selected_as_of == AS_OF
    assert result.selected_session_count == 3
    assert result.coverage_scope == PIT_DAILY_CANDLE_RESEARCH_SLICE_COVERAGE_SCOPE
    assert result.full_research_certified is False
    assert result.limitations == PIT_DAILY_CANDLE_RESEARCH_SLICE_LIMITATIONS
    assert result.source_candidate_count == 3
    assert result.source_snapshot_manifest_sha256 == RAW_MANIFEST
    assert result.items == items
    assert (
        result.slice_spec_sha256
        == "c7f26bf8b33f4c27405574855004fc209fbc8238728a7c86d37a056b3bc3a1e0"
    )
    assert (
        result.data_manifest_sha256
        == "c99f0913e526a6990eeea5e09804f4b215908a9dbc744fedb2f0e09f7f97cc6c"
    )


async def test_service_keeps_caller_scope_when_reader_mutates_its_request() -> None:
    class MutatingReader:
        received_request: DailyCandleAsOfReadRequest | None = None

        async def read_daily_candles_as_of(
            self,
            request: DailyCandleAsOfReadRequest,
        ) -> DurableDailyCandleAsOfSnapshotV1:
            self.received_request = request
            object.__setattr__(request, "symbol", "000660")
            return _snapshot(request, _items())

    request = _request()
    reader = MutatingReader()

    with pytest.raises(
        DailyCandleResearchSliceError,
        match="daily_candle_research_slice_source_query_mismatch",
    ):
        await DailyCandleResearchSliceService(reader).build_contiguous_slice(request)

    assert request.symbol == "005930"
    assert reader.received_request is not request


@pytest.mark.parametrize(
    ("field_name", "forged_value"),
    [
        ("schema_version", "pit_daily_candle_research_slice.v2"),
        ("source_query_sha256", "d" * 64),
        ("source_snapshot_manifest_sha256", "d" * 64),
        ("slice_spec_sha256", "d" * 64),
        ("data_manifest_sha256", "d" * 64),
        ("full_research_certified", True),
    ],
)
def test_public_validator_rebuilds_and_rejects_forged_slice_fields(
    field_name: str,
    forged_value: object,
) -> None:
    request = _request()
    result = build_contiguous_daily_candle_research_slice(
        request,
        _snapshot(request, _items()),
    )

    validated = validate_contiguous_daily_candle_research_slice(result)

    assert validated == result
    assert validated is not result
    assert validated.items[0] is not result.items[0]

    object.__setattr__(result, field_name, forged_value)
    with pytest.raises(
        DailyCandleResearchSliceError,
        match="daily_candle_research_slice_result_invalid",
    ):
        validate_contiguous_daily_candle_research_slice(result)


def test_public_validator_rejects_nested_slice_lineage_tamper() -> None:
    request = _request()
    result = build_contiguous_daily_candle_research_slice(
        request,
        _snapshot(request, _items()),
    )
    object.__setattr__(result.items[0].lineage, "calendar_revision", 2)

    with pytest.raises(
        DailyCandleResearchSliceError,
        match="daily_candle_research_slice_result_invalid",
    ):
        validate_contiguous_daily_candle_research_slice(result)


def test_one_session_slice_is_allowed_when_both_boundaries_match() -> None:
    request = _request(
        start_session_date=FIRST_SESSION,
        end_session_date=FIRST_SESSION,
    )
    item = _items()[0]

    result = build_contiguous_daily_candle_research_slice(
        request,
        _snapshot(request, (item,)),
    )

    assert result.selected_session_count == 1
    assert result.start_session_date == result.end_session_date == FIRST_SESSION


def test_stable_manifests_ignore_page_transport_and_snapshot_issue_time() -> None:
    utc_request = _request(page_size=25)
    kst_request = _request(
        as_of=AS_OF.astimezone(KST),
        page_size=100,
    )
    items = _items()
    first = build_contiguous_daily_candle_research_slice(
        utc_request,
        _snapshot(
            utc_request,
            items,
            token="1:2:3",
            snapshot_issued_at=SNAPSHOT_ISSUED_AT,
        ),
    )
    second = build_contiguous_daily_candle_research_slice(
        kst_request,
        _snapshot(
            kst_request,
            items,
            token="9:8:7,6",
            snapshot_issued_at=SNAPSHOT_ISSUED_AT + timedelta(hours=1),
        ),
    )

    assert first.slice_spec_sha256 == second.slice_spec_sha256
    assert first.data_manifest_sha256 == second.data_manifest_sha256
    assert first.source_query_sha256 != second.source_query_sha256
    assert first.source_page_size != second.source_page_size
    assert first.source_snapshot_issued_at != second.source_snapshot_issued_at


def test_public_query_fingerprint_matches_worker_sql_contract() -> None:
    request = _request(page_size=25)
    payload = {
        "adjusted": True,
        "as_of": "2026-03-27T03:00:00Z",
        "contract_version": PIT_DAILY_CANDLE_AS_OF_READER_SCHEMA_VERSION,
        "end_session_date": "2026-03-25",
        "interval": "1d",
        "limit": 25,
        "market": "KR",
        "provider": "toss",
        "start_session_date": "2026-03-23",
        "symbol": "005930",
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    assert (
        daily_candle_as_of_query_sha256(request)
        == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    )


async def test_service_maps_reader_failure_without_leaking_source_message() -> None:
    request = _request()
    secret_message = "upstream secret=must-not-leak"
    reader = FakeReader(
        object(),
        error=DailyCandleAsOfReaderError(secret_message),
    )

    with pytest.raises(
        DailyCandleResearchSliceError,
        match="daily_candle_research_slice_source_read_failed",
    ) as exc_info:
        await DailyCandleResearchSliceService(reader).build_contiguous_slice(request)

    assert reader.calls == [request]
    assert secret_message not in str(exc_info.value)
    formatted = "".join(traceback.format_exception(exc_info.value))
    assert secret_message not in formatted
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


@pytest.mark.parametrize("value", [object(), None, "invalid"])
def test_gate_rejects_invalid_request_type(value: object) -> None:
    with pytest.raises(
        DailyCandleResearchSliceError,
        match="daily_candle_research_slice_request_invalid",
    ):
        build_contiguous_daily_candle_research_slice(
            cast(Any, value),
            cast(Any, object()),
        )


def test_gate_rejects_invalid_snapshot_and_query_binding() -> None:
    request = _request()
    with pytest.raises(
        DailyCandleResearchSliceError,
        match="daily_candle_research_slice_snapshot_invalid",
    ):
        build_contiguous_daily_candle_research_slice(
            request,
            cast(Any, object()),
        )

    snapshot = replace(
        _snapshot(request, _items()),
        query_sha256="d" * 64,
    )
    with pytest.raises(
        DailyCandleResearchSliceError,
        match="daily_candle_research_slice_source_query_mismatch",
    ):
        build_contiguous_daily_candle_research_slice(request, snapshot)


def test_gate_rejects_empty_and_impossible_candidate_count() -> None:
    request = _request()
    with pytest.raises(
        DailyCandleResearchSliceError,
        match="daily_candle_research_slice_empty",
    ):
        build_contiguous_daily_candle_research_slice(
            request,
            _snapshot(request, (), candidate_count=0),
        )

    items = _items()
    with pytest.raises(
        DailyCandleResearchSliceError,
        match="daily_candle_research_slice_candidate_count_invalid",
    ):
        build_contiguous_daily_candle_research_slice(
            request,
            _snapshot(request, items, candidate_count=2),
        )


@pytest.mark.parametrize(
    "boundary_case",
    [
        "last-boundary-missing",
        "first-boundary-missing",
    ],
)
def test_gate_requires_exact_first_and_last_session_boundaries(
    boundary_case: str,
) -> None:
    request = _request()
    all_items = _items()
    items = all_items[:-1] if boundary_case == "last-boundary-missing" else all_items[1:]
    with pytest.raises(
        DailyCandleResearchSliceError,
        match="daily_candle_research_slice_range_boundary_mismatch",
    ):
        build_contiguous_daily_candle_research_slice(
            request,
            _snapshot(request, items),
        )


@pytest.mark.parametrize(
    "order_case",
    [
        "reversed",
        "duplicate",
    ],
)
def test_gate_rejects_non_increasing_or_duplicate_sessions(
    order_case: str,
) -> None:
    request = _request()
    all_items = _items()
    items = (
        tuple(reversed(all_items))
        if order_case == "reversed"
        else (all_items[0], all_items[0], all_items[2])
    )
    with pytest.raises(
        DailyCandleResearchSliceError,
        match="daily_candle_research_slice_order_invalid",
    ):
        build_contiguous_daily_candle_research_slice(
            request,
            _snapshot(request, items),
        )


def test_gate_rejects_a_missing_session_in_the_calendar_chain() -> None:
    request = _request()
    first, _, last = _items()

    with pytest.raises(
        DailyCandleResearchSliceError,
        match="daily_candle_research_slice_session_chain_broken",
    ):
        build_contiguous_daily_candle_research_slice(
            request,
            _snapshot(request, (first, last)),
        )


def test_gate_rejects_next_session_hours_that_do_not_match_next_evidence() -> None:
    request = _request()
    mismatched_first = _item_for_session(
        FIRST_SESSION,
        SECOND_SESSION,
        serial=1,
        next_start_offset=timedelta(minutes=1),
    )
    _, second, last = _items()

    with pytest.raises(
        DailyCandleResearchSliceError,
        match="daily_candle_research_slice_next_session_hours_mismatch",
    ):
        build_contiguous_daily_candle_research_slice(
            request,
            _snapshot(request, (mismatched_first, second, last)),
        )


@pytest.mark.parametrize("mixed_source", ["candle", "calendar"])
def test_gate_rejects_mixed_provider_contract_pins(mixed_source: str) -> None:
    request = _request()
    first, _, last = _items()
    if mixed_source == "candle":
        mixed_second = _item_for_session(
            SECOND_SESSION,
            LAST_SESSION,
            serial=2,
            candle_contract="e" * 64,
        )
    else:
        mixed_second = _item_for_session(
            SECOND_SESSION,
            LAST_SESSION,
            serial=2,
            calendar_contract="e" * 64,
        )

    with pytest.raises(
        DailyCandleResearchSliceError,
        match="daily_candle_research_slice_source_contract_mixed",
    ):
        build_contiguous_daily_candle_research_slice(
            request,
            _snapshot(request, (first, mixed_second, last)),
        )


def test_gate_rechecks_scope_and_selected_as_of() -> None:
    request = _request()
    first, second, last = _items()
    wrong_symbol = _item_for_session(
        SECOND_SESSION,
        LAST_SESSION,
        serial=2,
        symbol="000660",
    )
    with pytest.raises(
        DailyCandleResearchSliceError,
        match="daily_candle_research_slice_scope_mismatch",
    ):
        build_contiguous_daily_candle_research_slice(
            request,
            _snapshot(request, (first, wrong_symbol, last)),
        )

    wrong_as_of = _item_for_session(
        SECOND_SESSION,
        LAST_SESSION,
        serial=2,
        selected_as_of=AS_OF + timedelta(seconds=1),
    )
    with pytest.raises(
        DailyCandleResearchSliceError,
        match="daily_candle_research_slice_selected_as_of_mismatch",
    ):
        build_contiguous_daily_candle_research_slice(
            request,
            _snapshot(request, (first, wrong_as_of, last)),
        )
    assert second.selection.selected_as_of == AS_OF


def test_data_manifest_binds_selected_lineage_fields() -> None:
    request = _request()
    items = _items()
    original = build_contiguous_daily_candle_research_slice(
        request,
        _snapshot(request, items),
    )
    changed_lineage = replace(
        items[1].lineage,
        timing_revision_id=_uuid(999),
    )
    changed_item = replace(items[1], lineage=changed_lineage)
    changed = build_contiguous_daily_candle_research_slice(
        request,
        _snapshot(request, (items[0], changed_item, items[2])),
    )

    assert original.slice_spec_sha256 == changed.slice_spec_sha256
    assert original.data_manifest_sha256 != changed.data_manifest_sha256


def test_result_owns_canonical_copies_of_nested_source_objects() -> None:
    request = _request()
    source_items = _items()
    result = build_contiguous_daily_candle_research_slice(
        request,
        _snapshot(request, source_items),
    )
    original_close = result.items[0].selection.candle.close_krw
    original_manifest = result.data_manifest_sha256

    object.__setattr__(
        source_items[0].selection.candle,
        "close_krw",
        original_close + 999,
    )

    assert result.items[0] is not source_items[0]
    assert result.items[0].selection is not source_items[0].selection
    assert result.items[0].selection.candle is not source_items[0].selection.candle
    assert result.items[0].calendar is not source_items[0].calendar
    assert result.items[0].lineage is not source_items[0].lineage
    assert result.items[0].selection.candle.close_krw == original_close
    assert result.data_manifest_sha256 == original_manifest


def test_gate_rejects_reused_content_revision_ids_across_sessions() -> None:
    request = _request()
    items = _items()
    reused_lineages = (
        replace(
            items[1].lineage,
            candle_revision_id=items[0].lineage.candle_revision_id,
        ),
        replace(
            items[1].lineage,
            calendar_revision_id=items[0].lineage.calendar_revision_id,
        ),
    )

    for reused_lineage in reused_lineages:
        changed_item = replace(items[1], lineage=reused_lineage)
        with pytest.raises(
            DailyCandleResearchSliceError,
            match="daily_candle_research_slice_item_invalid",
        ):
            build_contiguous_daily_candle_research_slice(
                request,
                _snapshot(request, (items[0], changed_item, items[2])),
            )


def test_received_clock_after_as_of_is_lineage_not_an_alternate_cutoff() -> None:
    request = _request()
    late_received_at = AS_OF + timedelta(hours=1)
    late_items: list[DurableSelectedDailyCandleV1] = []
    for item in _items():
        late_lineage = replace(
            item.lineage,
            timing_received_at=late_received_at,
            candle_revision_received_at=late_received_at,
            candle_occurrence_received_at=late_received_at,
            calendar_revision_received_at=late_received_at,
            calendar_occurrence_received_at=late_received_at,
        )
        late_items.append(replace(item, lineage=late_lineage))

    result = build_contiguous_daily_candle_research_slice(
        request,
        _snapshot(request, tuple(late_items)),
    )

    assert result.selected_session_count == 3
    assert all(item.lineage.timing_received_at > result.selected_as_of for item in result.items)


def test_gate_rejects_lineage_received_after_snapshot_issue() -> None:
    request = _request()
    items = _items()
    impossible_received_at = SNAPSHOT_ISSUED_AT + timedelta(seconds=1)
    changed_lineage = replace(
        items[1].lineage,
        timing_received_at=impossible_received_at,
        candle_revision_received_at=impossible_received_at,
        candle_occurrence_received_at=impossible_received_at,
        calendar_revision_received_at=impossible_received_at,
        calendar_occurrence_received_at=impossible_received_at,
    )
    changed_item = replace(items[1], lineage=changed_lineage)

    with pytest.raises(
        DailyCandleResearchSliceError,
        match="daily_candle_research_slice_snapshot_clock_invalid",
    ):
        build_contiguous_daily_candle_research_slice(
            request,
            _snapshot(request, (items[0], changed_item, items[2])),
        )


def test_result_type_cannot_be_constructed_outside_the_gate() -> None:
    assert not hasattr(ContiguousDailyCandleResearchSliceV1, "_from_gate")
    constructor = cast(Any, ContiguousDailyCandleResearchSliceV1)
    with pytest.raises(
        DailyCandleResearchSliceError,
        match="daily_candle_research_slice_requires_gate",
    ):
        constructor()


def _request(
    *,
    start_session_date: date = FIRST_SESSION,
    end_session_date: date = LAST_SESSION,
    as_of: datetime = AS_OF,
    page_size: int = 100,
) -> DailyCandleAsOfReadRequest:
    return DailyCandleAsOfReadRequest(
        provider="toss",
        market="KR",
        symbol="005930",
        interval="1d",
        adjusted=True,
        start_session_date=start_session_date,
        end_session_date=end_session_date,
        as_of=as_of,
        page_size=page_size,
    )


def _items() -> tuple[DurableSelectedDailyCandleV1, ...]:
    return (
        _item_for_session(FIRST_SESSION, SECOND_SESSION, serial=1),
        _item_for_session(SECOND_SESSION, LAST_SESSION, serial=2),
        _item_for_session(LAST_SESSION, AFTER_LAST_SESSION, serial=3),
    )


def _item_for_session(
    session_date: date,
    next_business_date: date,
    *,
    serial: int,
    symbol: str = "005930",
    candle_contract: str = CANDLE_CONTRACT,
    calendar_contract: str = CALENDAR_CONTRACT,
    next_start_offset: timedelta = timedelta(0),
    selected_as_of: datetime = AS_OF,
) -> DurableSelectedDailyCandleV1:
    regular_start_at = datetime.combine(
        session_date,
        datetime.min.time(),
        tzinfo=KST,
    ) + timedelta(hours=9)
    regular_end_at = regular_start_at + timedelta(hours=6, minutes=30)
    next_regular_start_at = (
        datetime.combine(
            next_business_date,
            datetime.min.time(),
            tzinfo=KST,
        )
        + timedelta(hours=9)
        + next_start_offset
    )
    next_regular_end_at = next_regular_start_at + timedelta(
        hours=6,
        minutes=30,
    )
    candle = PointInTimeCandleV1.create(
        provider="toss",
        symbol=symbol,
        market="KR",
        interval="1d",
        adjusted=True,
        provider_event_at=regular_end_at,
        observed_at=next_regular_start_at + timedelta(hours=1),
        currency="KRW",
        open_krw=70_000 + serial,
        high_krw=71_000 + serial,
        low_krw=69_000 + serial,
        close_krw=70_500 + serial,
        volume=1_000_000 + serial,
        provider_contract_sha256=candle_contract,
    )
    calendar = PointInTimeKrDailySessionV1.create(
        provider="toss",
        market="KR",
        session_date=session_date,
        is_open=True,
        regular_start_at=regular_start_at,
        regular_end_at=regular_end_at,
        next_business_date=next_business_date,
        next_regular_start_at=next_regular_start_at,
        next_regular_end_at=next_regular_end_at,
        observed_at=next_regular_start_at + timedelta(hours=1, minutes=5),
        provider_contract_sha256=calendar_contract,
    )
    timing = build_daily_candle_timing_evidence(candle, calendar)
    selection = select_daily_candles_as_of(
        [(candle, timing)],
        as_of=selected_as_of,
    )[0]
    received_at = timing.evidence_available_at + timedelta(minutes=10)
    lineage = DailyCandleAsOfLineageV1(
        timing_revision_id=_uuid(serial * 10 + 1),
        timing_idempotency_key=timing.idempotency_key,
        timing_revision=1,
        timing_canonical_evidence_sha256=(timing.canonical_timing_evidence_sha256),
        timing_received_at=received_at,
        candle_revision_id=_uuid(serial * 10 + 2),
        candle_revision=1,
        candle_canonical_observation_sha256=(candle.canonical_observation_sha256),
        candle_revision_received_at=received_at,
        candle_occurrence_id=_uuid(serial * 10 + 3),
        candle_occurrence_received_at=received_at,
        candle_occurrence_origin="rpc",
        calendar_revision_id=_uuid(serial * 10 + 4),
        calendar_revision=1,
        calendar_canonical_evidence_sha256=(calendar.canonical_evidence_sha256),
        calendar_revision_received_at=received_at,
        calendar_occurrence_id=_uuid(serial * 10 + 5),
        calendar_occurrence_received_at=received_at,
        calendar_occurrence_origin="rpc",
    )
    return DurableSelectedDailyCandleV1(
        selection=selection,
        calendar=calendar,
        lineage=lineage,
    )


def _snapshot(
    request: DailyCandleAsOfReadRequest,
    items: tuple[DurableSelectedDailyCandleV1, ...],
    *,
    candidate_count: int | None = None,
    token: str = "1:2:3",
    snapshot_issued_at: datetime = SNAPSHOT_ISSUED_AT,
) -> DurableDailyCandleAsOfSnapshotV1:
    return DurableDailyCandleAsOfSnapshotV1(
        query_sha256=daily_candle_as_of_query_sha256(request),
        snapshot_token=token,
        snapshot_issued_at=snapshot_issued_at,
        snapshot_manifest_sha256=RAW_MANIFEST,
        candidate_count=(len(items) if candidate_count is None else candidate_count),
        items=items,
    )


def _uuid(value: int) -> str:
    return f"00000000-0000-4000-8000-{value:012x}"
