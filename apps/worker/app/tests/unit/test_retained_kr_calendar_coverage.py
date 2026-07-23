from __future__ import annotations

import asyncio
import re
import traceback
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any, cast

import pytest

from app.application.ports.calendar_as_of_reader_port import (
    CalendarAsOfLineageV1,
    CalendarAsOfReaderError,
    CalendarAsOfReadRequest,
    DurableCalendarAsOfSnapshotV1,
    DurableSelectedCalendarSessionV1,
    calendar_as_of_query_sha256,
)
from app.application.services.retained_kr_calendar_coverage import (
    PIT_RETAINED_KR_CALENDAR_COVERAGE_LIMITATIONS,
    PIT_RETAINED_KR_CALENDAR_COVERAGE_SCOPE,
    RetainedKrCalendarCoverageError,
    RetainedKrCalendarCoverageService,
    RetainedKrCalendarDateRangeCoverageV1,
    build_retained_kr_calendar_date_range_coverage,
    validate_retained_kr_calendar_date_range_coverage,
)
from app.domain.common.time import KST
from app.domain.market_data.calendar_as_of import (
    select_kr_daily_sessions_as_of,
)
from app.domain.market_data.point_in_time_calendar import (
    PointInTimeKrDailySessionV1,
)

START_DATE = date(2026, 7, 20)
FIRST_OPEN_DATE = date(2026, 7, 20)
SECOND_OPEN_DATE = date(2026, 7, 23)
END_DATE = date(2026, 7, 24)
EXTERNAL_NEXT_DATE = date(2026, 7, 27)
AS_OF = datetime(2026, 7, 19, 12, tzinfo=UTC)
SNAPSHOT_ISSUED_AT = AS_OF + timedelta(hours=3)
CONTRACT_SHA256 = "a" * 64
RAW_MANIFEST_SHA256 = "b" * 64


class FakeReader:
    def __init__(
        self,
        result: DurableCalendarAsOfSnapshotV1 | None,
        *,
        error: BaseException | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls: list[CalendarAsOfReadRequest] = []

    async def read_daily_sessions_as_of(
        self,
        request: CalendarAsOfReadRequest,
    ) -> DurableCalendarAsOfSnapshotV1:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


class ExplodingTzInfo(tzinfo):
    def __init__(self, secret_message: str) -> None:
        self.secret_message = secret_message

    def utcoffset(self, _value: datetime | None) -> timedelta | None:
        raise RuntimeError(self.secret_message)

    def dst(self, _value: datetime | None) -> timedelta | None:
        return timedelta(0)

    def tzname(self, _value: datetime | None) -> str | None:
        return "exploding"


class MutableTzInfo(tzinfo):
    def __init__(self, offset_hours: int) -> None:
        self.offset_hours = offset_hours

    def utcoffset(self, _value: datetime | None) -> timedelta | None:
        return timedelta(hours=self.offset_hours)

    def dst(self, _value: datetime | None) -> timedelta | None:
        return timedelta(0)

    def tzname(self, _value: datetime | None) -> str | None:
        return f"mutable-{self.offset_hours}"


class ExplodingSelection:
    def __init__(self, secret_message: str) -> None:
        self.secret_message = secret_message

    @property
    def session(self) -> object:
        raise RuntimeError(self.secret_message)


class ScopeMutatingReader:
    def __init__(self) -> None:
        self.calls: list[CalendarAsOfReadRequest] = []

    async def read_daily_sessions_as_of(
        self,
        request: CalendarAsOfReadRequest,
    ) -> DurableCalendarAsOfSnapshotV1:
        self.calls.append(request)
        object.__setattr__(request, "end_session_date", START_DATE)
        item = _item(
            START_DATE,
            is_open=True,
            next_business_date=SECOND_OPEN_DATE,
            serial=1,
        )
        return _snapshot(request, (item,))


class TimezoneMutatingReader:
    async def read_daily_sessions_as_of(
        self,
        request: CalendarAsOfReadRequest,
    ) -> DurableCalendarAsOfSnapshotV1:
        reader_timezone = request.as_of.tzinfo
        if isinstance(reader_timezone, MutableTzInfo):
            reader_timezone.offset_hours = 1
        return _snapshot(
            request,
            _items(selected_as_of=request.as_of),
        )


def test_gate_builds_mixed_open_closed_retained_date_coverage() -> None:
    request = _request()
    source_items = _items()

    result = build_retained_kr_calendar_date_range_coverage(
        request,
        _snapshot(request, source_items),
    )

    assert result.provider == "toss"
    assert result.market == "KR"
    assert result.start_session_date == START_DATE
    assert result.end_session_date == END_DATE
    assert result.selected_as_of == AS_OF
    assert result.calendar_day_count == 5
    assert result.open_session_count == 2
    assert result.closed_day_count == 3
    assert result.retained_date_coverage_complete is True
    assert result.coverage_scope == PIT_RETAINED_KR_CALENDAR_COVERAGE_SCOPE
    assert result.full_calendar_certified is False
    assert result.right_boundary_next_session_verified is False
    assert result.limitations == (
        "retained_evidence_at_read_time_only",
        "historical_database_visibility_not_reconstructed",
        "provider_authenticity_and_finality_not_proven",
        "official_exchange_calendar_completeness_not_proven",
        "right_boundary_next_session_unverified",
        "corporate_actions_not_evaluated",
        "full_data_quality_not_certified",
        "calendar_dataset_research_feature_backtest_strategy_order_use_not_authorized",
        "production_live_use_not_authorized",
    )
    assert result.limitations == PIT_RETAINED_KR_CALENDAR_COVERAGE_LIMITATIONS
    assert result.selected_provider_contract_sha256 == CONTRACT_SHA256
    assert result.source_candidate_count == 7
    assert result.source_snapshot_manifest_sha256 == RAW_MANIFEST_SHA256
    assert re.fullmatch(r"[0-9a-f]{64}", result.coverage_spec_sha256)
    assert re.fullmatch(r"[0-9a-f]{64}", result.data_manifest_sha256)
    assert (
        result.coverage_spec_sha256
        == "be2ed16edd41e9070a666c88e470cb64ef1b2055ebc590e6f775c84018ac120f"
    )
    assert (
        result.data_manifest_sha256
        == "f9cf2900fd6630ed89f5383806cec42fca2a8cf5ab549c9a0ac1ee3459123c59"
    )
    assert [item.selection.session.session_date for item in result.items] == [
        START_DATE + timedelta(days=offset) for offset in range(5)
    ]


@pytest.mark.parametrize(
    ("field_name", "forged_value"),
    [
        ("schema_version", "pit_retained_kr_calendar_date_range_coverage.v2"),
        ("source_query_sha256", "c" * 64),
        ("source_snapshot_manifest_sha256", "c" * 64),
        ("coverage_spec_sha256", "c" * 64),
        ("data_manifest_sha256", "c" * 64),
        ("full_calendar_certified", True),
    ],
)
def test_public_validator_rebuilds_and_rejects_forged_coverage_fields(
    field_name: str,
    forged_value: object,
) -> None:
    request = _request()
    result = build_retained_kr_calendar_date_range_coverage(
        request,
        _snapshot(request, _items()),
    )

    validated = validate_retained_kr_calendar_date_range_coverage(result)

    assert validated == result
    assert validated is not result
    assert validated.items[0] is not result.items[0]

    object.__setattr__(result, field_name, forged_value)
    with pytest.raises(
        RetainedKrCalendarCoverageError,
        match="retained_kr_calendar_coverage_result_invalid",
    ):
        validate_retained_kr_calendar_date_range_coverage(result)


def test_public_validator_rejects_nested_coverage_lineage_tamper() -> None:
    request = _request()
    result = build_retained_kr_calendar_date_range_coverage(
        request,
        _snapshot(request, _items()),
    )
    object.__setattr__(result.items[0].lineage, "calendar_revision", 2)

    with pytest.raises(
        RetainedKrCalendarCoverageError,
        match="retained_kr_calendar_coverage_result_invalid",
    ):
        validate_retained_kr_calendar_date_range_coverage(result)


@pytest.mark.parametrize("is_open", [True, False])
def test_gate_allows_one_day_open_or_closed_range(is_open: bool) -> None:
    request = _request(
        start_session_date=START_DATE,
        end_session_date=START_DATE,
    )
    item = _item(
        START_DATE,
        is_open=is_open,
        next_business_date=SECOND_OPEN_DATE,
        serial=1,
    )

    result = build_retained_kr_calendar_date_range_coverage(
        request,
        _snapshot(request, (item,)),
    )

    assert result.calendar_day_count == 1
    assert result.open_session_count == int(is_open)
    assert result.closed_day_count == int(not is_open)
    assert result.right_boundary_next_session_verified is False


def test_gate_allows_an_all_closed_range_with_one_external_target() -> None:
    end_date = START_DATE + timedelta(days=2)
    request = _request(end_session_date=end_date)
    items = tuple(
        _item(
            START_DATE + timedelta(days=offset),
            is_open=False,
            next_business_date=EXTERNAL_NEXT_DATE,
            serial=offset + 1,
        )
        for offset in range(3)
    )

    result = build_retained_kr_calendar_date_range_coverage(
        request,
        _snapshot(request, items),
    )

    assert result.open_session_count == 0
    assert result.closed_day_count == 3
    assert result.retained_date_coverage_complete is True
    assert result.full_calendar_certified is False


def test_gate_allows_the_maximum_366_day_request() -> None:
    end_date = START_DATE + timedelta(days=365)
    external_next_date = end_date + timedelta(days=1)
    request = _request(end_session_date=end_date)
    items = tuple(
        _item(
            START_DATE + timedelta(days=offset),
            is_open=False,
            next_business_date=external_next_date,
            serial=offset + 1,
        )
        for offset in range(366)
    )

    result = build_retained_kr_calendar_date_range_coverage(
        request,
        _snapshot(request, items, candidate_count=400),
    )

    assert result.calendar_day_count == 366
    assert result.closed_day_count == 366
    assert result.source_candidate_count == 400


async def test_service_reads_once_with_a_canonical_request_copy() -> None:
    request = _request()
    reader = FakeReader(_snapshot(request, _items()))

    result = await RetainedKrCalendarCoverageService(reader).build_date_range_coverage(request)

    assert result.calendar_day_count == 5
    assert reader.calls == [request]
    assert reader.calls[0] is not request


async def test_service_rejects_an_invalid_request_before_reading() -> None:
    request = _request()
    reader = FakeReader(_snapshot(request, _items()))
    object.__setattr__(request, "page_size", 10)

    with pytest.raises(
        RetainedKrCalendarCoverageError,
        match="retained_kr_calendar_coverage_request_invalid",
    ):
        await RetainedKrCalendarCoverageService(reader).build_date_range_coverage(request)

    assert reader.calls == []


async def test_service_binds_result_to_scope_retained_before_reader_call() -> None:
    request = _request()
    reader = ScopeMutatingReader()

    with pytest.raises(
        RetainedKrCalendarCoverageError,
        match="retained_kr_calendar_coverage_source_query_mismatch",
    ):
        await RetainedKrCalendarCoverageService(reader).build_date_range_coverage(request)

    assert request.end_session_date == END_DATE
    assert len(reader.calls) == 1
    assert reader.calls[0].end_session_date == START_DATE


async def test_service_detaches_mutable_timezone_from_reader_request() -> None:
    caller_timezone = MutableTzInfo(0)
    caller_as_of = datetime(2026, 7, 19, 12, tzinfo=caller_timezone)
    request = _request(as_of=caller_as_of)

    result = await RetainedKrCalendarCoverageService(
        TimezoneMutatingReader()
    ).build_date_range_coverage(request)

    assert result.selected_as_of == AS_OF
    assert result.selected_as_of.tzinfo is UTC
    assert caller_timezone.offset_hours == 0
    assert request.as_of is caller_as_of
    assert request.as_of.astimezone(UTC) == AS_OF


@pytest.mark.parametrize("error_type", [CalendarAsOfReaderError, RuntimeError])
async def test_service_maps_reader_failure_without_leaking_source_details(
    error_type: type[Exception],
) -> None:
    request = _request()
    secret_message = "upstream payload authorization=must-not-leak"
    reader = FakeReader(
        None,
        error=error_type(secret_message),
    )

    with pytest.raises(
        RetainedKrCalendarCoverageError,
        match="retained_kr_calendar_coverage_source_read_failed",
    ) as exc_info:
        await RetainedKrCalendarCoverageService(reader).build_date_range_coverage(request)

    assert reader.calls == [request]
    assert secret_message not in str(exc_info.value)
    formatted = "".join(traceback.format_exception(exc_info.value))
    assert secret_message not in formatted
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


async def test_service_propagates_cancellation() -> None:
    request = _request()
    reader = FakeReader(None, error=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await RetainedKrCalendarCoverageService(reader).build_date_range_coverage(request)

    assert reader.calls == [request]


@pytest.mark.parametrize("value", [object(), None, "invalid"])
def test_gate_rejects_invalid_request_type(value: object) -> None:
    with pytest.raises(
        RetainedKrCalendarCoverageError,
        match="retained_kr_calendar_coverage_request_invalid",
    ):
        build_retained_kr_calendar_date_range_coverage(
            cast(Any, value),
            cast(Any, object()),
        )


def test_gate_rejects_a_mutated_request_before_source_use() -> None:
    request = _request()
    object.__setattr__(request, "page_size", 10)

    with pytest.raises(
        RetainedKrCalendarCoverageError,
        match="retained_kr_calendar_coverage_request_invalid",
    ):
        build_retained_kr_calendar_date_range_coverage(
            request,
            cast(Any, object()),
        )


def test_gate_rejects_invalid_snapshot_and_query_binding() -> None:
    request = _request()
    with pytest.raises(
        RetainedKrCalendarCoverageError,
        match="retained_kr_calendar_coverage_snapshot_invalid",
    ):
        build_retained_kr_calendar_date_range_coverage(
            request,
            cast(Any, object()),
        )

    mismatched = replace(
        _snapshot(request, _items()),
        query_sha256="c" * 64,
    )
    with pytest.raises(
        RetainedKrCalendarCoverageError,
        match="retained_kr_calendar_coverage_source_query_mismatch",
    ):
        build_retained_kr_calendar_date_range_coverage(request, mismatched)


def test_snapshot_validation_removes_untrusted_timezone_exception_chain() -> None:
    request = _request()
    snapshot = _snapshot(request, _items())
    secret_message = "snapshot timezone credential=must-not-leak"
    malicious_time = datetime(
        2026,
        7,
        19,
        tzinfo=ExplodingTzInfo(secret_message),
    )
    object.__setattr__(snapshot, "snapshot_issued_at", malicious_time)

    with pytest.raises(
        RetainedKrCalendarCoverageError,
        match="retained_kr_calendar_coverage_snapshot_invalid",
    ) as exc_info:
        build_retained_kr_calendar_date_range_coverage(request, snapshot)

    assert secret_message not in "".join(traceback.format_exception(exc_info.value))
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_gate_rejects_empty_and_impossible_candidate_count() -> None:
    request = _request()
    with pytest.raises(
        RetainedKrCalendarCoverageError,
        match="retained_kr_calendar_coverage_empty",
    ):
        build_retained_kr_calendar_date_range_coverage(
            request,
            _snapshot(request, (), candidate_count=0),
        )

    with pytest.raises(
        RetainedKrCalendarCoverageError,
        match="retained_kr_calendar_coverage_candidate_count_invalid",
    ):
        build_retained_kr_calendar_date_range_coverage(
            request,
            _snapshot(request, _items(), candidate_count=4),
        )


@pytest.mark.parametrize("missing_index", [0, 2, 4])
def test_gate_rejects_a_missing_first_middle_or_last_date(
    missing_index: int,
) -> None:
    request = _request()
    items = list(_items())
    items.pop(missing_index)

    with pytest.raises(
        RetainedKrCalendarCoverageError,
        match="retained_kr_calendar_coverage_date_sequence_incomplete",
    ):
        build_retained_kr_calendar_date_range_coverage(
            request,
            _snapshot(request, tuple(items)),
        )


@pytest.mark.parametrize("sequence_case", ["duplicate", "reversed", "outside"])
def test_gate_rejects_duplicate_reversed_or_out_of_range_dates(
    sequence_case: str,
) -> None:
    request = _request()
    items = _items()
    changed: tuple[DurableSelectedCalendarSessionV1, ...]
    if sequence_case == "duplicate":
        changed = (items[0], items[1], items[1], items[3], items[4])
    elif sequence_case == "reversed":
        changed = tuple(reversed(items))
    else:
        outside = _item(
            START_DATE - timedelta(days=1),
            is_open=False,
            next_business_date=FIRST_OPEN_DATE,
            serial=99,
        )
        changed = (outside, *items[1:])

    with pytest.raises(
        RetainedKrCalendarCoverageError,
        match="retained_kr_calendar_coverage_date_sequence_incomplete",
    ):
        build_retained_kr_calendar_date_range_coverage(
            request,
            _snapshot(request, changed),
        )


def test_gate_rechecks_scope_and_selected_as_of() -> None:
    request = _request()
    wrong_provider_items = _items(provider="other")
    with pytest.raises(
        RetainedKrCalendarCoverageError,
        match="retained_kr_calendar_coverage_scope_mismatch",
    ):
        build_retained_kr_calendar_date_range_coverage(
            request,
            _snapshot(request, wrong_provider_items),
        )

    wrong_as_of_items = _items(selected_as_of=AS_OF - timedelta(seconds=1))
    with pytest.raises(
        RetainedKrCalendarCoverageError,
        match="retained_kr_calendar_coverage_selected_as_of_mismatch",
    ):
        build_retained_kr_calendar_date_range_coverage(
            request,
            _snapshot(request, wrong_as_of_items),
        )


def test_gate_rejects_mixed_selected_provider_contracts() -> None:
    request = _request()
    items = list(_items())
    items[2] = _item(
        START_DATE + timedelta(days=2),
        is_open=False,
        next_business_date=SECOND_OPEN_DATE,
        serial=3,
        contract_sha256="d" * 64,
    )

    with pytest.raises(
        RetainedKrCalendarCoverageError,
        match="retained_kr_calendar_coverage_source_contract_mixed",
    ):
        build_retained_kr_calendar_date_range_coverage(
            request,
            _snapshot(request, tuple(items)),
        )


@pytest.mark.parametrize("lineage_field", ["calendar_revision_id", "calendar_occurrence_id"])
def test_gate_rejects_reused_selected_lineage_ids(lineage_field: str) -> None:
    request = _request()
    items = list(_items())
    changed_lineage = replace(
        items[2].lineage,
        **{lineage_field: getattr(items[0].lineage, lineage_field)},
    )
    items[2] = replace(items[2], lineage=changed_lineage)

    with pytest.raises(
        RetainedKrCalendarCoverageError,
        match="retained_kr_calendar_coverage_lineage_reused",
    ):
        build_retained_kr_calendar_date_range_coverage(
            request,
            _snapshot(request, tuple(items)),
        )


def test_received_after_as_of_is_lineage_not_a_semantic_cutoff() -> None:
    request = _request()
    result = build_retained_kr_calendar_date_range_coverage(
        request,
        _snapshot(request, _items()),
    )

    assert all(
        item.lineage.calendar_occurrence_received_at > result.selected_as_of
        for item in result.items
    )


def test_gate_rejects_lineage_received_after_snapshot_issue() -> None:
    request = _request()
    items = list(_items())
    invalid_received_at = SNAPSHOT_ISSUED_AT + timedelta(seconds=1)
    changed_lineage = replace(
        items[2].lineage,
        calendar_revision_received_at=invalid_received_at,
        calendar_occurrence_received_at=invalid_received_at,
    )
    items[2] = replace(items[2], lineage=changed_lineage)

    with pytest.raises(
        RetainedKrCalendarCoverageError,
        match="retained_kr_calendar_coverage_snapshot_clock_invalid",
    ):
        build_retained_kr_calendar_date_range_coverage(
            request,
            _snapshot(request, tuple(items)),
        )


def test_gate_accepts_lineage_received_exactly_at_snapshot_issue() -> None:
    request = _request()
    items = list(_items())
    changed_lineage = replace(
        items[2].lineage,
        calendar_revision_received_at=SNAPSHOT_ISSUED_AT,
        calendar_occurrence_received_at=SNAPSHOT_ISSUED_AT,
    )
    items[2] = replace(items[2], lineage=changed_lineage)

    result = build_retained_kr_calendar_date_range_coverage(
        request,
        _snapshot(request, tuple(items)),
    )

    assert (
        result.items[2].lineage.calendar_occurrence_received_at == result.source_snapshot_issued_at
    )


def test_gate_rejects_an_in_range_next_business_target_that_is_closed() -> None:
    request = _request()
    items = list(_items())
    items[3] = _item(
        SECOND_OPEN_DATE,
        is_open=False,
        next_business_date=EXTERNAL_NEXT_DATE,
        serial=4,
    )

    with pytest.raises(
        RetainedKrCalendarCoverageError,
        match="retained_kr_calendar_coverage_next_business_chain_broken",
    ):
        build_retained_kr_calendar_date_range_coverage(
            request,
            _snapshot(request, tuple(items)),
        )


def test_gate_rejects_in_range_next_session_hours_mismatch() -> None:
    request = _request()
    items = list(_items())
    items[0] = _item(
        FIRST_OPEN_DATE,
        is_open=True,
        next_business_date=SECOND_OPEN_DATE,
        serial=1,
        next_start_offset=timedelta(minutes=1),
    )

    with pytest.raises(
        RetainedKrCalendarCoverageError,
        match="retained_kr_calendar_coverage_next_session_hours_mismatch",
    ):
        build_retained_kr_calendar_date_range_coverage(
            request,
            _snapshot(request, tuple(items)),
        )


def test_gate_rejects_skipping_an_open_date_inside_the_range() -> None:
    request = _request()
    items = list(_items())
    items[0] = _item(
        FIRST_OPEN_DATE,
        is_open=True,
        next_business_date=EXTERNAL_NEXT_DATE,
        serial=1,
    )

    with pytest.raises(
        RetainedKrCalendarCoverageError,
        match="retained_kr_calendar_coverage_next_business_chain_broken",
    ):
        build_retained_kr_calendar_date_range_coverage(
            request,
            _snapshot(request, tuple(items)),
        )


def test_gate_rejects_mixed_right_boundary_next_session_claims() -> None:
    request = _request()
    items = list(_items())
    items[4] = _item(
        END_DATE,
        is_open=False,
        next_business_date=EXTERNAL_NEXT_DATE + timedelta(days=1),
        serial=5,
    )

    with pytest.raises(
        RetainedKrCalendarCoverageError,
        match="retained_kr_calendar_coverage_right_boundary_target_mixed",
    ):
        build_retained_kr_calendar_date_range_coverage(
            request,
            _snapshot(request, tuple(items)),
        )


def test_gate_rejects_mixed_right_boundary_next_session_hours() -> None:
    request = _request()
    items = list(_items())
    items[4] = _item(
        END_DATE,
        is_open=False,
        next_business_date=EXTERNAL_NEXT_DATE,
        serial=5,
        next_start_offset=timedelta(minutes=1),
    )

    with pytest.raises(
        RetainedKrCalendarCoverageError,
        match="retained_kr_calendar_coverage_right_boundary_target_mixed",
    ):
        build_retained_kr_calendar_date_range_coverage(
            request,
            _snapshot(request, tuple(items)),
        )


def test_stable_manifests_ignore_transport_and_snapshot_issue_metadata() -> None:
    utc_request = _request(page_size=25)
    kst_request = _request(as_of=AS_OF.astimezone(KST), page_size=100)
    items = _items()
    first = build_retained_kr_calendar_date_range_coverage(
        utc_request,
        _snapshot(
            utc_request,
            items,
            token="1:2:3",
            snapshot_issued_at=SNAPSHOT_ISSUED_AT,
        ),
    )
    second = build_retained_kr_calendar_date_range_coverage(
        kst_request,
        _snapshot(
            kst_request,
            items,
            token="9:8:7,6",
            snapshot_issued_at=SNAPSHOT_ISSUED_AT + timedelta(hours=1),
        ),
    )

    assert first.coverage_spec_sha256 == second.coverage_spec_sha256
    assert first.data_manifest_sha256 == second.data_manifest_sha256
    assert first.source_page_size != second.source_page_size
    assert first.source_query_sha256 != second.source_query_sha256
    assert first.source_snapshot_issued_at != second.source_snapshot_issued_at


def test_data_manifest_binds_raw_manifest_selected_lineage_and_contract() -> None:
    request = _request()
    items = _items()
    original = build_retained_kr_calendar_date_range_coverage(
        request,
        _snapshot(request, items),
    )
    raw_changed = build_retained_kr_calendar_date_range_coverage(
        request,
        replace(
            _snapshot(request, items),
            snapshot_manifest_sha256="e" * 64,
        ),
    )
    candidate_count_changed = build_retained_kr_calendar_date_range_coverage(
        request,
        _snapshot(request, items, candidate_count=8),
    )
    changed_lineage = replace(
        items[2].lineage,
        calendar_revision_id=_uuid(999),
    )
    lineage_changed_items = list(items)
    lineage_changed_items[2] = replace(
        items[2],
        lineage=changed_lineage,
    )
    lineage_changed = build_retained_kr_calendar_date_range_coverage(
        request,
        _snapshot(request, tuple(lineage_changed_items)),
    )
    contract_changed = build_retained_kr_calendar_date_range_coverage(
        request,
        _snapshot(request, _items(contract_sha256="f" * 64)),
    )

    assert original.coverage_spec_sha256 == raw_changed.coverage_spec_sha256
    assert original.coverage_spec_sha256 == candidate_count_changed.coverage_spec_sha256
    assert original.coverage_spec_sha256 == lineage_changed.coverage_spec_sha256
    assert original.coverage_spec_sha256 == contract_changed.coverage_spec_sha256
    assert original.data_manifest_sha256 != raw_changed.data_manifest_sha256
    assert original.data_manifest_sha256 != candidate_count_changed.data_manifest_sha256
    assert original.data_manifest_sha256 != lineage_changed.data_manifest_sha256
    assert original.data_manifest_sha256 != contract_changed.data_manifest_sha256


def test_result_owns_canonical_copies_of_nested_source_objects() -> None:
    request = _request()
    source_items = _items()
    result = build_retained_kr_calendar_date_range_coverage(
        request,
        _snapshot(request, source_items),
    )
    original_is_open = result.items[0].selection.session.is_open
    original_manifest = result.data_manifest_sha256

    object.__setattr__(
        source_items[0].selection.session,
        "is_open",
        not original_is_open,
    )

    assert result.items[0] is not source_items[0]
    assert result.items[0].selection is not source_items[0].selection
    assert result.items[0].selection.session is not source_items[0].selection.session
    assert result.items[0].lineage is not source_items[0].lineage
    assert result.items[0].selection.session.is_open is original_is_open
    assert result.data_manifest_sha256 == original_manifest


def test_result_detaches_mutable_timezone_from_lineage_timestamps() -> None:
    request = _request()
    source_items = list(_items())
    source_lineage = source_items[0].lineage
    lineage_timezone = MutableTzInfo(0)
    changed_lineage = replace(
        source_lineage,
        calendar_revision_observed_at=(
            source_lineage.calendar_revision_observed_at.replace(tzinfo=lineage_timezone)
        ),
        calendar_revision_received_at=(
            source_lineage.calendar_revision_received_at.replace(tzinfo=lineage_timezone)
        ),
        calendar_occurrence_observed_at=(
            source_lineage.calendar_occurrence_observed_at.replace(tzinfo=lineage_timezone)
        ),
        calendar_occurrence_received_at=(
            source_lineage.calendar_occurrence_received_at.replace(tzinfo=lineage_timezone)
        ),
    )
    source_items[0] = replace(source_items[0], lineage=changed_lineage)

    result = build_retained_kr_calendar_date_range_coverage(
        request,
        _snapshot(request, tuple(source_items)),
    )
    original_occurrence = result.items[0].lineage.calendar_occurrence_observed_at
    original_manifest = result.data_manifest_sha256

    lineage_timezone.offset_hours = 1

    assert result.items[0].lineage.calendar_occurrence_observed_at == original_occurrence
    assert result.items[0].lineage.calendar_occurrence_observed_at.tzinfo is UTC
    assert (
        result.items[0].lineage.calendar_occurrence_observed_at
        == result.items[0].selection.session.observed_at
    )
    assert result.data_manifest_sha256 == original_manifest


def test_gate_rejects_a_mutated_selected_session() -> None:
    request = _request()
    items = list(_items())
    object.__setattr__(
        items[2].selection.session,
        "observed_at",
        AS_OF + timedelta(seconds=1),
    )

    with pytest.raises(
        RetainedKrCalendarCoverageError,
        match="retained_kr_calendar_coverage_item_invalid",
    ):
        build_retained_kr_calendar_date_range_coverage(
            request,
            _snapshot(request, tuple(items)),
        )


def test_item_validation_removes_untrusted_nested_exception_chain() -> None:
    request = _request()
    items = list(_items())
    secret_message = "nested payload bearer=must-not-leak"
    object.__setattr__(
        items[0],
        "selection",
        cast(Any, ExplodingSelection(secret_message)),
    )

    with pytest.raises(
        RetainedKrCalendarCoverageError,
        match="retained_kr_calendar_coverage_item_invalid",
    ) as exc_info:
        build_retained_kr_calendar_date_range_coverage(
            request,
            _snapshot(request, tuple(items)),
        )

    assert secret_message not in "".join(traceback.format_exception(exc_info.value))
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_result_type_cannot_be_constructed_outside_the_gate() -> None:
    assert not hasattr(RetainedKrCalendarDateRangeCoverageV1, "_from_gate")
    constructor = cast(Any, RetainedKrCalendarDateRangeCoverageV1)
    with pytest.raises(
        RetainedKrCalendarCoverageError,
        match="retained_kr_calendar_coverage_requires_gate",
    ):
        constructor()


def _request(
    *,
    start_session_date: date = START_DATE,
    end_session_date: date = END_DATE,
    as_of: datetime = AS_OF,
    page_size: int = 100,
) -> CalendarAsOfReadRequest:
    return CalendarAsOfReadRequest(
        provider="toss",
        market="KR",
        start_session_date=start_session_date,
        end_session_date=end_session_date,
        as_of=as_of,
        page_size=page_size,
    )


def _items(
    *,
    provider: str = "toss",
    contract_sha256: str = CONTRACT_SHA256,
    selected_as_of: datetime = AS_OF,
) -> tuple[DurableSelectedCalendarSessionV1, ...]:
    return (
        _item(
            START_DATE,
            is_open=True,
            next_business_date=SECOND_OPEN_DATE,
            serial=1,
            provider=provider,
            contract_sha256=contract_sha256,
            selected_as_of=selected_as_of,
        ),
        _item(
            START_DATE + timedelta(days=1),
            is_open=False,
            next_business_date=SECOND_OPEN_DATE,
            serial=2,
            provider=provider,
            contract_sha256=contract_sha256,
            selected_as_of=selected_as_of,
        ),
        _item(
            START_DATE + timedelta(days=2),
            is_open=False,
            next_business_date=SECOND_OPEN_DATE,
            serial=3,
            provider=provider,
            contract_sha256=contract_sha256,
            selected_as_of=selected_as_of,
        ),
        _item(
            SECOND_OPEN_DATE,
            is_open=True,
            next_business_date=EXTERNAL_NEXT_DATE,
            serial=4,
            provider=provider,
            contract_sha256=contract_sha256,
            selected_as_of=selected_as_of,
        ),
        _item(
            END_DATE,
            is_open=False,
            next_business_date=EXTERNAL_NEXT_DATE,
            serial=5,
            provider=provider,
            contract_sha256=contract_sha256,
            selected_as_of=selected_as_of,
        ),
    )


def _item(
    session_date: date,
    *,
    is_open: bool,
    next_business_date: date,
    serial: int,
    provider: str = "toss",
    contract_sha256: str = CONTRACT_SHA256,
    selected_as_of: datetime = AS_OF,
    next_start_offset: timedelta = timedelta(0),
) -> DurableSelectedCalendarSessionV1:
    regular_start_at = _regular_start(session_date) if is_open else None
    regular_end_at = (
        regular_start_at + timedelta(hours=6, minutes=30) if regular_start_at is not None else None
    )
    next_regular_start_at = _regular_start(next_business_date) + next_start_offset
    next_regular_end_at = next_regular_start_at + timedelta(
        hours=6,
        minutes=30,
    )
    observed_at = datetime(2026, 7, 19, 1, tzinfo=UTC) + timedelta(minutes=serial)
    session = PointInTimeKrDailySessionV1.create(
        provider=provider,
        market="KR",
        session_date=session_date,
        is_open=is_open,
        regular_start_at=regular_start_at,
        regular_end_at=regular_end_at,
        next_business_date=next_business_date,
        next_regular_start_at=next_regular_start_at,
        next_regular_end_at=next_regular_end_at,
        observed_at=observed_at,
        provider_contract_sha256=contract_sha256,
    )
    selection = select_kr_daily_sessions_as_of(
        [session],
        as_of=selected_as_of,
    )[0]
    received_at = AS_OF + timedelta(hours=1)
    lineage = CalendarAsOfLineageV1(
        calendar_revision_id=_uuid(serial * 10 + 1),
        calendar_idempotency_key=session.idempotency_key,
        calendar_revision=1,
        calendar_canonical_evidence_sha256=(session.canonical_evidence_sha256),
        calendar_revision_observed_at=observed_at,
        calendar_revision_received_at=received_at,
        calendar_occurrence_id=_uuid(serial * 10 + 2),
        calendar_occurrence_observed_at=observed_at,
        calendar_occurrence_received_at=received_at,
        calendar_occurrence_origin="rpc",
    )
    return DurableSelectedCalendarSessionV1(
        selection=selection,
        lineage=lineage,
    )


def _snapshot(
    request: CalendarAsOfReadRequest,
    items: tuple[DurableSelectedCalendarSessionV1, ...],
    *,
    candidate_count: int | None = None,
    token: str = "1:2:3",
    snapshot_issued_at: datetime = SNAPSHOT_ISSUED_AT,
) -> DurableCalendarAsOfSnapshotV1:
    return DurableCalendarAsOfSnapshotV1(
        query_sha256=calendar_as_of_query_sha256(request),
        snapshot_token=token,
        snapshot_issued_at=snapshot_issued_at,
        snapshot_manifest_sha256=RAW_MANIFEST_SHA256,
        candidate_count=(len(items) + 2 if candidate_count is None else candidate_count),
        items=items,
    )


def _regular_start(session_date: date) -> datetime:
    return datetime.combine(
        session_date,
        datetime.min.time(),
        tzinfo=KST,
    ) + timedelta(hours=9)


def _uuid(value: int) -> str:
    return f"00000000-0000-4000-8000-{value:012x}"
