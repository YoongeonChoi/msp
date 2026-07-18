from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from app.application.ports.calendar_as_of_reader_port import (
    CalendarAsOfLineageV1,
    CalendarAsOfReaderError,
    CalendarAsOfReaderPort,
    CalendarAsOfReadRequest,
    DurableCalendarAsOfSnapshotV1,
    DurableSelectedCalendarSessionV1,
    calendar_as_of_query_sha256,
)
from app.domain.common.errors import KnownFailClosedError
from app.domain.common.json import JsonObject, JsonValue
from app.domain.market_data.calendar_as_of import (
    CalendarAsOfError,
    select_kr_daily_sessions_as_of,
)

PIT_RETAINED_KR_CALENDAR_COVERAGE_SCHEMA_VERSION = "pit_retained_kr_calendar_coverage.v1"
PIT_RETAINED_KR_CALENDAR_COVERAGE_SCOPE_SCHEMA_VERSION = (
    "pit_retained_kr_calendar_coverage_scope.v1"
)
PIT_RETAINED_KR_CALENDAR_COVERAGE_SCOPE = "retained_calendar_date_range_only"
PIT_RETAINED_KR_CALENDAR_COVERAGE_LIMITATIONS = (
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


class RetainedKrCalendarCoverageError(KnownFailClosedError):
    def __init__(self, safe_message: str) -> None:
        super().__init__("retained_kr_calendar_coverage", safe_message)


@dataclass(frozen=True, slots=True, init=False)
class RetainedKrCalendarDateRangeCoverageV1:
    schema_version: str
    provider: str
    market: str
    start_session_date: date
    end_session_date: date
    selected_as_of: datetime
    calendar_day_count: int
    open_session_count: int
    closed_day_count: int
    retained_date_coverage_complete: bool
    coverage_scope: str
    full_calendar_certified: bool
    right_boundary_next_session_verified: bool
    limitations: tuple[str, ...]
    selected_provider_contract_sha256: str
    source_page_size: int
    source_query_sha256: str
    source_snapshot_manifest_sha256: str
    source_candidate_count: int
    source_snapshot_issued_at: datetime
    coverage_spec_sha256: str
    data_manifest_sha256: str
    items: tuple[DurableSelectedCalendarSessionV1, ...]

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise RetainedKrCalendarCoverageError("retained_kr_calendar_coverage_requires_gate")


class RetainedKrCalendarCoverageService:
    def __init__(self, reader: CalendarAsOfReaderPort) -> None:
        self.reader = reader

    async def build_date_range_coverage(
        self,
        request: CalendarAsOfReadRequest,
    ) -> RetainedKrCalendarDateRangeCoverageV1:
        valid_request = _request(request)
        reader_request = _request(valid_request)
        try:
            snapshot = await self.reader.read_daily_sessions_as_of(reader_request)
        except Exception:
            pass
        else:
            return build_retained_kr_calendar_date_range_coverage(
                valid_request,
                snapshot,
            )
        raise RetainedKrCalendarCoverageError("retained_kr_calendar_coverage_source_read_failed")


def build_retained_kr_calendar_date_range_coverage(
    request: CalendarAsOfReadRequest,
    snapshot: DurableCalendarAsOfSnapshotV1,
) -> RetainedKrCalendarDateRangeCoverageV1:
    valid_request = _request(request)
    expected_query_sha256 = _query_sha256(valid_request)
    valid_snapshot = _snapshot(snapshot)
    if valid_snapshot.query_sha256 != expected_query_sha256:
        raise RetainedKrCalendarCoverageError("retained_kr_calendar_coverage_source_query_mismatch")

    items = valid_snapshot.items
    if not items:
        raise RetainedKrCalendarCoverageError("retained_kr_calendar_coverage_empty")
    if valid_snapshot.candidate_count < len(items):
        raise RetainedKrCalendarCoverageError(
            "retained_kr_calendar_coverage_candidate_count_invalid"
        )

    expected_day_count = (
        valid_request.end_session_date - valid_request.start_session_date
    ).days + 1
    if len(items) != expected_day_count:
        raise RetainedKrCalendarCoverageError(
            "retained_kr_calendar_coverage_date_sequence_incomplete"
        )

    selected_as_of = _utc(valid_request.as_of)
    snapshot_issued_at = _utc(valid_snapshot.snapshot_issued_at)
    canonical_items: list[DurableSelectedCalendarSessionV1] = []
    revision_ids: set[str] = set()
    occurrence_ids: set[str] = set()
    idempotency_keys: set[str] = set()
    selected_contracts: set[str] = set()

    for index, item in enumerate(items):
        canonical_item = _item(item)
        selection = canonical_item.selection
        session = selection.session
        lineage = canonical_item.lineage
        expected_date = valid_request.start_session_date + timedelta(days=index)
        if session.session_date != expected_date:
            raise RetainedKrCalendarCoverageError(
                "retained_kr_calendar_coverage_date_sequence_incomplete"
            )
        if session.provider != valid_request.provider or session.market != valid_request.market:
            raise RetainedKrCalendarCoverageError("retained_kr_calendar_coverage_scope_mismatch")
        if _utc(selection.selected_as_of) != selected_as_of:
            raise RetainedKrCalendarCoverageError(
                "retained_kr_calendar_coverage_selected_as_of_mismatch"
            )
        if (
            _utc(lineage.calendar_revision_received_at) > snapshot_issued_at
            or _utc(lineage.calendar_occurrence_received_at) > snapshot_issued_at
        ):
            raise RetainedKrCalendarCoverageError(
                "retained_kr_calendar_coverage_snapshot_clock_invalid"
            )
        if (
            lineage.calendar_revision_id in revision_ids
            or lineage.calendar_occurrence_id in occurrence_ids
            or session.idempotency_key in idempotency_keys
        ):
            raise RetainedKrCalendarCoverageError("retained_kr_calendar_coverage_lineage_reused")
        revision_ids.add(lineage.calendar_revision_id)
        occurrence_ids.add(lineage.calendar_occurrence_id)
        idempotency_keys.add(session.idempotency_key)
        selected_contracts.add(session.provider_contract_sha256)
        canonical_items.append(canonical_item)

    if len(selected_contracts) != 1:
        raise RetainedKrCalendarCoverageError("retained_kr_calendar_coverage_source_contract_mixed")

    canonical_tuple = tuple(canonical_items)
    try:
        reselected = select_kr_daily_sessions_as_of(
            [item.selection.session for item in canonical_tuple],
            as_of=selected_as_of,
        )
    except CalendarAsOfError:
        raise RetainedKrCalendarCoverageError(
            "retained_kr_calendar_coverage_item_invalid"
        ) from None
    if reselected != tuple(item.selection for item in canonical_tuple):
        raise RetainedKrCalendarCoverageError("retained_kr_calendar_coverage_item_invalid")

    _validate_next_business_chain(
        canonical_tuple,
        end_session_date=valid_request.end_session_date,
    )

    open_count = sum(item.selection.session.is_open for item in canonical_tuple)
    closed_count = len(canonical_tuple) - open_count
    selected_contract = next(iter(selected_contracts))
    scope_payload = _coverage_scope_payload(valid_request)
    coverage_spec_sha256 = _payload_sha256(scope_payload)
    data_manifest_sha256 = _payload_sha256(
        _data_manifest_payload(
            request=valid_request,
            snapshot=valid_snapshot,
            items=canonical_tuple,
            open_session_count=open_count,
            closed_day_count=closed_count,
            selected_provider_contract_sha256=selected_contract,
        )
    )

    result = object.__new__(RetainedKrCalendarDateRangeCoverageV1)
    values: dict[str, object] = {
        "schema_version": PIT_RETAINED_KR_CALENDAR_COVERAGE_SCHEMA_VERSION,
        "provider": valid_request.provider,
        "market": valid_request.market,
        "start_session_date": valid_request.start_session_date,
        "end_session_date": valid_request.end_session_date,
        "selected_as_of": selected_as_of,
        "calendar_day_count": len(canonical_tuple),
        "open_session_count": open_count,
        "closed_day_count": closed_count,
        "retained_date_coverage_complete": True,
        "coverage_scope": PIT_RETAINED_KR_CALENDAR_COVERAGE_SCOPE,
        "full_calendar_certified": False,
        "right_boundary_next_session_verified": False,
        "limitations": PIT_RETAINED_KR_CALENDAR_COVERAGE_LIMITATIONS,
        "selected_provider_contract_sha256": selected_contract,
        "source_page_size": valid_request.page_size,
        "source_query_sha256": valid_snapshot.query_sha256,
        "source_snapshot_manifest_sha256": (valid_snapshot.snapshot_manifest_sha256),
        "source_candidate_count": valid_snapshot.candidate_count,
        "source_snapshot_issued_at": snapshot_issued_at,
        "coverage_spec_sha256": coverage_spec_sha256,
        "data_manifest_sha256": data_manifest_sha256,
        "items": canonical_tuple,
    }
    for field_name, value in values.items():
        object.__setattr__(result, field_name, value)
    return result


def _validate_next_business_chain(
    items: tuple[DurableSelectedCalendarSessionV1, ...],
    *,
    end_session_date: date,
) -> None:
    sessions_by_date = {
        item.selection.session.session_date: item.selection.session for item in items
    }
    right_boundary_target: tuple[date, datetime, datetime] | None = None

    for item in items:
        session = item.selection.session
        next_date = session.next_business_date
        last_intermediate_date = min(
            next_date - timedelta(days=1),
            end_session_date,
        )
        current_date = session.session_date + timedelta(days=1)
        while current_date <= last_intermediate_date:
            intermediate = sessions_by_date[current_date]
            if intermediate.is_open:
                raise RetainedKrCalendarCoverageError(
                    "retained_kr_calendar_coverage_next_business_chain_broken"
                )
            current_date += timedelta(days=1)

        if next_date <= end_session_date:
            target = sessions_by_date[next_date]
            if not target.is_open:
                raise RetainedKrCalendarCoverageError(
                    "retained_kr_calendar_coverage_next_business_chain_broken"
                )
            if (
                session.next_regular_start_at != target.regular_start_at
                or session.next_regular_end_at != target.regular_end_at
            ):
                raise RetainedKrCalendarCoverageError(
                    "retained_kr_calendar_coverage_next_session_hours_mismatch"
                )
            continue

        candidate = (
            next_date,
            session.next_regular_start_at,
            session.next_regular_end_at,
        )
        if right_boundary_target is None:
            right_boundary_target = candidate
        elif right_boundary_target != candidate:
            raise RetainedKrCalendarCoverageError(
                "retained_kr_calendar_coverage_right_boundary_target_mixed"
            )


def _request(value: object) -> CalendarAsOfReadRequest:
    if type(value) is not CalendarAsOfReadRequest:
        raise RetainedKrCalendarCoverageError("retained_kr_calendar_coverage_request_invalid")
    canonical: CalendarAsOfReadRequest | None = None
    try:
        canonical_as_of = value.as_of.astimezone(UTC)
        canonical = CalendarAsOfReadRequest(
            provider=value.provider,
            market=value.market,
            start_session_date=value.start_session_date,
            end_session_date=value.end_session_date,
            as_of=canonical_as_of,
            page_size=value.page_size,
        )
        _query_sha256(canonical)
        matches = canonical == value
    except (
        AttributeError,
        CalendarAsOfReaderError,
        OverflowError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        pass
    else:
        if matches:
            return canonical
    raise RetainedKrCalendarCoverageError("retained_kr_calendar_coverage_request_invalid")


def _query_sha256(request: CalendarAsOfReadRequest) -> str:
    try:
        return calendar_as_of_query_sha256(request)
    except CalendarAsOfReaderError:
        raise RetainedKrCalendarCoverageError(
            "retained_kr_calendar_coverage_request_invalid"
        ) from None


def _snapshot(value: object) -> DurableCalendarAsOfSnapshotV1:
    if type(value) is not DurableCalendarAsOfSnapshotV1:
        raise RetainedKrCalendarCoverageError("retained_kr_calendar_coverage_snapshot_invalid")
    canonical: DurableCalendarAsOfSnapshotV1 | None = None
    try:
        canonical = DurableCalendarAsOfSnapshotV1(
            query_sha256=value.query_sha256,
            snapshot_token=value.snapshot_token,
            snapshot_issued_at=value.snapshot_issued_at,
            snapshot_manifest_sha256=value.snapshot_manifest_sha256,
            candidate_count=value.candidate_count,
            items=value.items,
        )
        matches = canonical == value
    except (
        AttributeError,
        CalendarAsOfReaderError,
        OverflowError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        pass
    else:
        if matches:
            return canonical
    raise RetainedKrCalendarCoverageError("retained_kr_calendar_coverage_snapshot_invalid")


def _item(value: object) -> DurableSelectedCalendarSessionV1:
    if type(value) is not DurableSelectedCalendarSessionV1:
        raise RetainedKrCalendarCoverageError("retained_kr_calendar_coverage_item_invalid")
    canonical: DurableSelectedCalendarSessionV1 | None = None
    try:
        selected = select_kr_daily_sessions_as_of(
            [value.selection.session],
            as_of=value.selection.selected_as_of,
        )
        if len(selected) != 1:
            raise RetainedKrCalendarCoverageError("retained_kr_calendar_coverage_item_invalid")
        source_lineage = value.lineage
        lineage = CalendarAsOfLineageV1(
            calendar_revision_id=source_lineage.calendar_revision_id,
            calendar_idempotency_key=(source_lineage.calendar_idempotency_key),
            calendar_revision=source_lineage.calendar_revision,
            calendar_canonical_evidence_sha256=(source_lineage.calendar_canonical_evidence_sha256),
            calendar_revision_observed_at=_utc(source_lineage.calendar_revision_observed_at),
            calendar_revision_received_at=_utc(source_lineage.calendar_revision_received_at),
            calendar_occurrence_id=source_lineage.calendar_occurrence_id,
            calendar_occurrence_observed_at=_utc(source_lineage.calendar_occurrence_observed_at),
            calendar_occurrence_received_at=_utc(source_lineage.calendar_occurrence_received_at),
            calendar_occurrence_origin=(source_lineage.calendar_occurrence_origin),
        )
        canonical = DurableSelectedCalendarSessionV1(
            selection=selected[0],
            lineage=lineage,
        )
        matches = canonical == value
    except Exception:
        pass
    else:
        if matches:
            return canonical
    raise RetainedKrCalendarCoverageError("retained_kr_calendar_coverage_item_invalid")


def _coverage_scope_payload(request: CalendarAsOfReadRequest) -> JsonObject:
    return {
        "schema_version": (PIT_RETAINED_KR_CALENDAR_COVERAGE_SCOPE_SCHEMA_VERSION),
        "provider": request.provider,
        "market": request.market,
        "first_session_date": request.start_session_date.isoformat(),
        "last_session_date": request.end_session_date.isoformat(),
        "as_of": _canonical_timestamp(request.as_of),
        "coverage_scope": PIT_RETAINED_KR_CALENDAR_COVERAGE_SCOPE,
        "full_calendar_certified": False,
        "right_boundary_next_session_verified": False,
        "limitations": list(PIT_RETAINED_KR_CALENDAR_COVERAGE_LIMITATIONS),
    }


def _data_manifest_payload(
    *,
    request: CalendarAsOfReadRequest,
    snapshot: DurableCalendarAsOfSnapshotV1,
    items: tuple[DurableSelectedCalendarSessionV1, ...],
    open_session_count: int,
    closed_day_count: int,
    selected_provider_contract_sha256: str,
) -> JsonObject:
    item_payloads: list[JsonValue] = [_item_manifest_payload(item) for item in items]
    return {
        "schema_version": PIT_RETAINED_KR_CALENDAR_COVERAGE_SCHEMA_VERSION,
        "scope": _coverage_scope_payload(request),
        "retained_date_coverage_complete": True,
        "coverage_scope": PIT_RETAINED_KR_CALENDAR_COVERAGE_SCOPE,
        "full_calendar_certified": False,
        "right_boundary_next_session_verified": False,
        "limitations": list(PIT_RETAINED_KR_CALENDAR_COVERAGE_LIMITATIONS),
        "source": {
            "candidate_count": snapshot.candidate_count,
            "snapshot_manifest_sha256": snapshot.snapshot_manifest_sha256,
        },
        "selected_provider_contract_sha256": (selected_provider_contract_sha256),
        "calendar_day_count": len(items),
        "open_session_count": open_session_count,
        "closed_day_count": closed_day_count,
        "items": item_payloads,
    }


def _item_manifest_payload(
    item: DurableSelectedCalendarSessionV1,
) -> JsonObject:
    lineage = item.lineage
    body: JsonObject = {
        "calendar": item.selection.session.to_payload(),
        "selected_as_of": _canonical_timestamp(item.selection.selected_as_of),
        "lineage": {
            "calendar_revision_id": lineage.calendar_revision_id,
            "calendar_idempotency_key": lineage.calendar_idempotency_key,
            "calendar_revision": lineage.calendar_revision,
            "calendar_canonical_evidence_sha256": (lineage.calendar_canonical_evidence_sha256),
            "calendar_revision_observed_at": _canonical_timestamp(
                lineage.calendar_revision_observed_at
            ),
            "calendar_revision_received_at": _canonical_timestamp(
                lineage.calendar_revision_received_at
            ),
            "calendar_occurrence_id": lineage.calendar_occurrence_id,
            "calendar_occurrence_observed_at": _canonical_timestamp(
                lineage.calendar_occurrence_observed_at
            ),
            "calendar_occurrence_received_at": _canonical_timestamp(
                lineage.calendar_occurrence_received_at
            ),
            "calendar_occurrence_origin": lineage.calendar_occurrence_origin,
        },
    }
    return {
        "item_sha256": _payload_sha256(body),
        "calendar": body["calendar"],
        "selected_as_of": body["selected_as_of"],
        "lineage": body["lineage"],
    }


def _payload_sha256(value: JsonObject) -> str:
    try:
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        raise RetainedKrCalendarCoverageError(
            "retained_kr_calendar_coverage_manifest_invalid"
        ) from None
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_timestamp(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _utc(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None:
        raise RetainedKrCalendarCoverageError("retained_kr_calendar_coverage_item_invalid")
    converted: datetime | None = None
    try:
        if value.utcoffset() is not None:
            converted = value.astimezone(UTC)
    except (OverflowError, RuntimeError, TypeError, ValueError):
        pass
    if converted is not None:
        return converted
    raise RetainedKrCalendarCoverageError("retained_kr_calendar_coverage_item_invalid")
