from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime

from app.application.ports.daily_candle_as_of_reader_port import (
    DailyCandleAsOfLineageV1,
    DailyCandleAsOfReaderError,
    DailyCandleAsOfReaderPort,
    DailyCandleAsOfReadRequest,
    DurableDailyCandleAsOfSnapshotV1,
    DurableSelectedDailyCandleV1,
    daily_candle_as_of_query_sha256,
)
from app.domain.common.errors import KnownFailClosedError
from app.domain.common.json import JsonObject, JsonValue
from app.domain.common.time import KST
from app.domain.market_data.daily_candle_as_of import (
    DailyCandleAsOfError,
    select_daily_candles_as_of,
)
from app.domain.market_data.point_in_time_calendar import (
    PointInTimeKrDailySessionV1,
)

PIT_DAILY_CANDLE_RESEARCH_SLICE_SCHEMA_VERSION = (
    "pit_daily_candle_research_slice.v1"
)
PIT_DAILY_CANDLE_RESEARCH_SLICE_SCOPE_SCHEMA_VERSION = (
    "pit_daily_candle_research_slice_scope.v1"
)
PIT_DAILY_CANDLE_RESEARCH_SLICE_COVERAGE_SCOPE = (
    "retained_open_session_chain_only"
)
PIT_DAILY_CANDLE_RESEARCH_SLICE_LIMITATIONS = (
    "retained_evidence_at_read_time_only",
    "historical_database_visibility_not_reconstructed",
    "provider_authenticity_and_finality_not_proven",
    "corporate_actions_not_evaluated",
    "full_data_quality_not_certified",
    "feature_backtest_strategy_order_use_not_authorized",
)


class DailyCandleResearchSliceError(KnownFailClosedError):
    def __init__(self, safe_message: str) -> None:
        super().__init__("daily_candle_research_slice", safe_message)


@dataclass(frozen=True, slots=True, init=False)
class ContiguousDailyCandleResearchSliceV1:
    schema_version: str
    provider: str
    market: str
    symbol: str
    interval: str
    adjusted: bool
    start_session_date: date
    end_session_date: date
    selected_as_of: datetime
    selected_session_count: int
    coverage_scope: str
    full_research_certified: bool
    limitations: tuple[str, ...]
    candle_provider_contract_sha256: str
    calendar_provider_contract_sha256: str
    source_page_size: int
    source_query_sha256: str
    source_snapshot_manifest_sha256: str
    source_candidate_count: int
    source_snapshot_issued_at: datetime
    slice_spec_sha256: str
    data_manifest_sha256: str
    items: tuple[DurableSelectedDailyCandleV1, ...]

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise DailyCandleResearchSliceError(
            "daily_candle_research_slice_requires_gate"
        )


class DailyCandleResearchSliceService:
    def __init__(self, reader: DailyCandleAsOfReaderPort) -> None:
        self.reader = reader

    async def build_contiguous_slice(
        self,
        request: DailyCandleAsOfReadRequest,
    ) -> ContiguousDailyCandleResearchSliceV1:
        valid_request = _request(request)
        try:
            snapshot = await self.reader.read_daily_candles_as_of(valid_request)
        except Exception:
            pass
        else:
            return build_contiguous_daily_candle_research_slice(
                valid_request,
                snapshot,
            )
        raise DailyCandleResearchSliceError(
            "daily_candle_research_slice_source_read_failed"
        )


def build_contiguous_daily_candle_research_slice(
    request: DailyCandleAsOfReadRequest,
    snapshot: DurableDailyCandleAsOfSnapshotV1,
) -> ContiguousDailyCandleResearchSliceV1:
    valid_request = _request(request)
    expected_query_sha256 = _query_sha256(valid_request)
    valid_snapshot = _snapshot(snapshot)
    if valid_snapshot.query_sha256 != expected_query_sha256:
        raise DailyCandleResearchSliceError(
            "daily_candle_research_slice_source_query_mismatch"
        )

    items = valid_snapshot.items
    if not items:
        raise DailyCandleResearchSliceError("daily_candle_research_slice_empty")
    if (
        len(items) > 366
        or valid_snapshot.candidate_count < len(items)
        or valid_snapshot.candidate_count > 1_000
    ):
        raise DailyCandleResearchSliceError(
            "daily_candle_research_slice_candidate_count_invalid"
        )

    selected_as_of = _utc(valid_request.as_of)
    previous: DurableSelectedDailyCandleV1 | None = None
    canonical_items: list[DurableSelectedDailyCandleV1] = []
    seen_lineage_keys: set[tuple[str, str]] = set()
    candle_contracts: set[str] = set()
    calendar_contracts: set[str] = set()

    for item in items:
        canonical_item = _item(item)
        selection = canonical_item.selection
        candle = selection.candle
        timing = selection.timing_evidence
        calendar = canonical_item.calendar
        lineage = canonical_item.lineage
        if (
            candle.provider != valid_request.provider
            or candle.market != valid_request.market
            or candle.symbol != valid_request.symbol
            or candle.interval != valid_request.interval
            or candle.adjusted is not valid_request.adjusted
            or timing.provider != valid_request.provider
            or timing.market != valid_request.market
            or timing.symbol != valid_request.symbol
            or timing.interval != valid_request.interval
            or timing.adjusted is not valid_request.adjusted
            or calendar.provider != valid_request.provider
            or calendar.market != valid_request.market
        ):
            raise DailyCandleResearchSliceError(
                "daily_candle_research_slice_scope_mismatch"
            )
        if _utc(selection.selected_as_of) != selected_as_of:
            raise DailyCandleResearchSliceError(
                "daily_candle_research_slice_selected_as_of_mismatch"
            )
        try:
            candle_session_date = candle.provider_event_at.astimezone(KST).date()
        except (OverflowError, RuntimeError, TypeError, ValueError) as exc:
            raise DailyCandleResearchSliceError(
                "daily_candle_research_slice_item_invalid"
            ) from exc
        if (
            not calendar.is_open
            or timing.session_date != calendar.session_date
            or candle_session_date != timing.session_date
            or not valid_request.start_session_date
            <= timing.session_date
            <= valid_request.end_session_date
        ):
            raise DailyCandleResearchSliceError(
                "daily_candle_research_slice_scope_mismatch"
            )
        if lineage.timing_received_at > valid_snapshot.snapshot_issued_at:
            raise DailyCandleResearchSliceError(
                "daily_candle_research_slice_snapshot_clock_invalid"
            )
        if previous is not None:
            previous_timing = previous.selection.timing_evidence
            previous_calendar = previous.calendar
            if previous_timing.session_date >= timing.session_date:
                raise DailyCandleResearchSliceError(
                    "daily_candle_research_slice_order_invalid"
                )
            if previous_calendar.next_business_date != timing.session_date:
                raise DailyCandleResearchSliceError(
                    "daily_candle_research_slice_session_chain_broken"
                )
            if (
                previous_calendar.next_regular_start_at
                != calendar.regular_start_at
                or previous_calendar.next_regular_end_at
                != calendar.regular_end_at
            ):
                raise DailyCandleResearchSliceError(
                    "daily_candle_research_slice_next_session_hours_mismatch"
                )
        lineage_keys = (
            ("timing_revision_id", lineage.timing_revision_id),
            ("timing_idempotency_key", lineage.timing_idempotency_key),
            ("candle_revision_id", lineage.candle_revision_id),
            ("candle_occurrence_id", lineage.candle_occurrence_id),
            ("candle_idempotency_key", candle.idempotency_key),
            ("calendar_revision_id", lineage.calendar_revision_id),
            ("calendar_occurrence_id", lineage.calendar_occurrence_id),
            ("calendar_idempotency_key", calendar.idempotency_key),
        )
        if any(key in seen_lineage_keys for key in lineage_keys):
            raise DailyCandleResearchSliceError(
                "daily_candle_research_slice_item_invalid"
            )
        seen_lineage_keys.update(lineage_keys)
        candle_contracts.add(candle.provider_contract_sha256)
        calendar_contracts.add(calendar.provider_contract_sha256)
        canonical_items.append(canonical_item)
        previous = canonical_item

    first_date = canonical_items[0].selection.timing_evidence.session_date
    last_date = canonical_items[-1].selection.timing_evidence.session_date
    if (
        first_date != valid_request.start_session_date
        or last_date != valid_request.end_session_date
    ):
        raise DailyCandleResearchSliceError(
            "daily_candle_research_slice_range_boundary_mismatch"
        )
    if len(candle_contracts) != 1 or len(calendar_contracts) != 1:
        raise DailyCandleResearchSliceError(
            "daily_candle_research_slice_source_contract_mixed"
        )

    try:
        reselected = select_daily_candles_as_of(
            [
                (item.selection.candle, item.selection.timing_evidence)
                for item in canonical_items
            ],
            as_of=selected_as_of,
        )
    except DailyCandleAsOfError as exc:
        raise DailyCandleResearchSliceError(
            "daily_candle_research_slice_item_invalid"
        ) from exc
    if reselected != tuple(item.selection for item in canonical_items):
        raise DailyCandleResearchSliceError(
            "daily_candle_research_slice_item_invalid"
        )

    candle_contract = next(iter(candle_contracts))
    calendar_contract = next(iter(calendar_contracts))
    scope_payload = _slice_scope_payload(valid_request)
    slice_spec_sha256 = _payload_sha256(scope_payload)
    data_manifest_sha256 = _payload_sha256(
        _data_manifest_payload(
            request=valid_request,
            snapshot=valid_snapshot,
            items=tuple(canonical_items),
            candle_provider_contract_sha256=candle_contract,
            calendar_provider_contract_sha256=calendar_contract,
        )
    )
    result = object.__new__(ContiguousDailyCandleResearchSliceV1)
    values: dict[str, object] = {
        "schema_version": PIT_DAILY_CANDLE_RESEARCH_SLICE_SCHEMA_VERSION,
        "provider": valid_request.provider,
        "market": valid_request.market,
        "symbol": valid_request.symbol,
        "interval": valid_request.interval,
        "adjusted": valid_request.adjusted,
        "start_session_date": valid_request.start_session_date,
        "end_session_date": valid_request.end_session_date,
        "selected_as_of": selected_as_of,
        "selected_session_count": len(canonical_items),
        "coverage_scope": PIT_DAILY_CANDLE_RESEARCH_SLICE_COVERAGE_SCOPE,
        "full_research_certified": False,
        "limitations": PIT_DAILY_CANDLE_RESEARCH_SLICE_LIMITATIONS,
        "candle_provider_contract_sha256": candle_contract,
        "calendar_provider_contract_sha256": calendar_contract,
        "source_page_size": valid_request.page_size,
        "source_query_sha256": valid_snapshot.query_sha256,
        "source_snapshot_manifest_sha256": (
            valid_snapshot.snapshot_manifest_sha256
        ),
        "source_candidate_count": valid_snapshot.candidate_count,
        "source_snapshot_issued_at": _utc(valid_snapshot.snapshot_issued_at),
        "slice_spec_sha256": slice_spec_sha256,
        "data_manifest_sha256": data_manifest_sha256,
        "items": tuple(canonical_items),
    }
    for field_name, value in values.items():
        object.__setattr__(result, field_name, value)
    return result


def _request(value: object) -> DailyCandleAsOfReadRequest:
    if type(value) is not DailyCandleAsOfReadRequest:
        raise DailyCandleResearchSliceError(
            "daily_candle_research_slice_request_invalid"
        )
    _query_sha256(value)
    return value


def _query_sha256(request: DailyCandleAsOfReadRequest) -> str:
    try:
        return daily_candle_as_of_query_sha256(request)
    except DailyCandleAsOfReaderError as exc:
        raise DailyCandleResearchSliceError(
            "daily_candle_research_slice_request_invalid"
        ) from exc


def _snapshot(value: object) -> DurableDailyCandleAsOfSnapshotV1:
    if type(value) is not DurableDailyCandleAsOfSnapshotV1:
        raise DailyCandleResearchSliceError(
            "daily_candle_research_slice_snapshot_invalid"
        )
    try:
        canonical = DurableDailyCandleAsOfSnapshotV1(
            query_sha256=value.query_sha256,
            snapshot_token=value.snapshot_token,
            snapshot_issued_at=value.snapshot_issued_at,
            snapshot_manifest_sha256=value.snapshot_manifest_sha256,
            candidate_count=value.candidate_count,
            items=value.items,
        )
    except (
        AttributeError,
        DailyCandleAsOfReaderError,
        OverflowError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        raise DailyCandleResearchSliceError(
            "daily_candle_research_slice_snapshot_invalid"
        ) from exc
    if canonical != value:
        raise DailyCandleResearchSliceError(
            "daily_candle_research_slice_snapshot_invalid"
        )
    return canonical


def _item(value: object) -> DurableSelectedDailyCandleV1:
    if type(value) is not DurableSelectedDailyCandleV1:
        raise DailyCandleResearchSliceError(
            "daily_candle_research_slice_item_invalid"
    )
    try:
        selected = select_daily_candles_as_of(
            [
                (
                    value.selection.candle,
                    value.selection.timing_evidence,
                )
            ],
            as_of=value.selection.selected_as_of,
        )
        if len(selected) != 1:
            raise DailyCandleResearchSliceError(
                "daily_candle_research_slice_item_invalid"
            )
        calendar = PointInTimeKrDailySessionV1.from_payload(
            value.calendar.to_payload()
        )
        source_lineage = value.lineage
        lineage = DailyCandleAsOfLineageV1(
            timing_revision_id=source_lineage.timing_revision_id,
            timing_idempotency_key=source_lineage.timing_idempotency_key,
            timing_revision=source_lineage.timing_revision,
            timing_canonical_evidence_sha256=(
                source_lineage.timing_canonical_evidence_sha256
            ),
            timing_received_at=source_lineage.timing_received_at,
            candle_revision_id=source_lineage.candle_revision_id,
            candle_revision=source_lineage.candle_revision,
            candle_canonical_observation_sha256=(
                source_lineage.candle_canonical_observation_sha256
            ),
            candle_revision_received_at=(
                source_lineage.candle_revision_received_at
            ),
            candle_occurrence_id=source_lineage.candle_occurrence_id,
            candle_occurrence_received_at=(
                source_lineage.candle_occurrence_received_at
            ),
            candle_occurrence_origin=source_lineage.candle_occurrence_origin,
            calendar_revision_id=source_lineage.calendar_revision_id,
            calendar_revision=source_lineage.calendar_revision,
            calendar_canonical_evidence_sha256=(
                source_lineage.calendar_canonical_evidence_sha256
            ),
            calendar_revision_received_at=(
                source_lineage.calendar_revision_received_at
            ),
            calendar_occurrence_id=source_lineage.calendar_occurrence_id,
            calendar_occurrence_received_at=(
                source_lineage.calendar_occurrence_received_at
            ),
            calendar_occurrence_origin=(
                source_lineage.calendar_occurrence_origin
            ),
        )
        canonical = DurableSelectedDailyCandleV1(
            selection=selected[0],
            calendar=calendar,
            lineage=lineage,
        )
    except Exception:
        raise DailyCandleResearchSliceError(
            "daily_candle_research_slice_item_invalid"
        ) from None
    if canonical != value:
        raise DailyCandleResearchSliceError(
            "daily_candle_research_slice_item_invalid"
        )
    return canonical


def _slice_scope_payload(request: DailyCandleAsOfReadRequest) -> JsonObject:
    return {
        "schema_version": PIT_DAILY_CANDLE_RESEARCH_SLICE_SCOPE_SCHEMA_VERSION,
        "provider": request.provider,
        "market": request.market,
        "symbol": request.symbol,
        "interval": request.interval,
        "adjusted": request.adjusted,
        "first_session_date": request.start_session_date.isoformat(),
        "last_session_date": request.end_session_date.isoformat(),
        "as_of": _canonical_timestamp(request.as_of),
    }


def _data_manifest_payload(
    *,
    request: DailyCandleAsOfReadRequest,
    snapshot: DurableDailyCandleAsOfSnapshotV1,
    items: tuple[DurableSelectedDailyCandleV1, ...],
    candle_provider_contract_sha256: str,
    calendar_provider_contract_sha256: str,
) -> JsonObject:
    item_payloads: list[JsonValue] = [_item_manifest_payload(item) for item in items]
    return {
        "schema_version": PIT_DAILY_CANDLE_RESEARCH_SLICE_SCHEMA_VERSION,
        "scope": _slice_scope_payload(request),
        "coverage_scope": PIT_DAILY_CANDLE_RESEARCH_SLICE_COVERAGE_SCOPE,
        "full_research_certified": False,
        "limitations": list(PIT_DAILY_CANDLE_RESEARCH_SLICE_LIMITATIONS),
        "source": {
            "candidate_count": snapshot.candidate_count,
            "snapshot_manifest_sha256": snapshot.snapshot_manifest_sha256,
        },
        "provider_contracts": {
            "candle_sha256": candle_provider_contract_sha256,
            "calendar_sha256": calendar_provider_contract_sha256,
        },
        "selected_session_count": len(items),
        "items": item_payloads,
    }


def _item_manifest_payload(item: DurableSelectedDailyCandleV1) -> JsonObject:
    lineage = item.lineage
    body: JsonObject = {
        "candle": item.selection.candle.to_payload(),
        "timing": item.selection.timing_evidence.to_payload(),
        "calendar": item.calendar.to_payload(),
        "lineage": {
            "timing_revision_id": lineage.timing_revision_id,
            "timing_idempotency_key": lineage.timing_idempotency_key,
            "timing_revision": lineage.timing_revision,
            "timing_canonical_evidence_sha256": (
                lineage.timing_canonical_evidence_sha256
            ),
            "timing_received_at": _canonical_timestamp(
                lineage.timing_received_at
            ),
            "candle_revision_id": lineage.candle_revision_id,
            "candle_revision": lineage.candle_revision,
            "candle_canonical_observation_sha256": (
                lineage.candle_canonical_observation_sha256
            ),
            "candle_revision_received_at": _canonical_timestamp(
                lineage.candle_revision_received_at
            ),
            "candle_occurrence_id": lineage.candle_occurrence_id,
            "candle_occurrence_received_at": _canonical_timestamp(
                lineage.candle_occurrence_received_at
            ),
            "candle_occurrence_origin": lineage.candle_occurrence_origin,
            "calendar_revision_id": lineage.calendar_revision_id,
            "calendar_revision": lineage.calendar_revision,
            "calendar_canonical_evidence_sha256": (
                lineage.calendar_canonical_evidence_sha256
            ),
            "calendar_revision_received_at": _canonical_timestamp(
                lineage.calendar_revision_received_at
            ),
            "calendar_occurrence_id": lineage.calendar_occurrence_id,
            "calendar_occurrence_received_at": _canonical_timestamp(
                lineage.calendar_occurrence_received_at
            ),
            "calendar_occurrence_origin": lineage.calendar_occurrence_origin,
        },
    }
    return {
        "item_sha256": _payload_sha256(body),
        "candle": body["candle"],
        "timing": body["timing"],
        "calendar": body["calendar"],
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
    except (TypeError, ValueError) as exc:
        raise DailyCandleResearchSliceError(
            "daily_candle_research_slice_manifest_invalid"
        ) from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_timestamp(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _utc(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None:
        raise DailyCandleResearchSliceError(
            "daily_candle_research_slice_item_invalid"
        )
    try:
        if value.utcoffset() is None:
            raise DailyCandleResearchSliceError(
                "daily_candle_research_slice_item_invalid"
            )
        return value.astimezone(UTC)
    except (OverflowError, RuntimeError, TypeError, ValueError) as exc:
        raise DailyCandleResearchSliceError(
            "daily_candle_research_slice_item_invalid"
        ) from exc
