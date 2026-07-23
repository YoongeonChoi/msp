from __future__ import annotations

import ast
import hashlib
import json
import traceback
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from app.application.ports.calendar_as_of_reader_port import (
    CalendarAsOfLineageV1,
    CalendarAsOfReadRequest,
    DurableCalendarAsOfSnapshotV1,
    DurableSelectedCalendarSessionV1,
    calendar_as_of_query_sha256,
)
from app.application.ports.daily_candle_as_of_reader_port import (
    DailyCandleAsOfLineageV1,
    DailyCandleAsOfReadRequest,
    DurableDailyCandleAsOfSnapshotV1,
    DurableSelectedDailyCandleV1,
    daily_candle_as_of_query_sha256,
)
from app.application.services.daily_candle_research_certification import (
    PIT_DAILY_CANDLE_RESEARCH_CERTIFICATION_LIMITATIONS,
    PIT_DAILY_CANDLE_RESEARCH_CERTIFICATION_REJECTION_REASONS,
    PIT_DAILY_CANDLE_RESEARCH_CERTIFICATION_STATUS,
    DailyCandleResearchCertificationError,
    DailyCandleResearchCertificationManifestV1,
    build_daily_candle_research_certification_assessment,
    require_full_research_certification,
)
from app.application.services.daily_candle_research_slice import (
    ContiguousDailyCandleResearchSliceV1,
    build_contiguous_daily_candle_research_slice,
)
from app.application.services.retained_kr_calendar_coverage import (
    RetainedKrCalendarDateRangeCoverageV1,
    build_retained_kr_calendar_date_range_coverage,
)
from app.domain.common.time import KST
from app.domain.market_data.calendar_as_of import (
    select_kr_daily_sessions_as_of,
)
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

SESSION_DATE = date(2026, 3, 23)
NEXT_SESSION_DATE = date(2026, 3, 24)
AS_OF = datetime(2026, 3, 25, 3, 0, tzinfo=UTC)
SNAPSHOT_ISSUED_AT = datetime(2026, 3, 26, 0, 0, tzinfo=UTC)
CANDLE_CONTRACT = "a" * 64
CALENDAR_CONTRACT = "b" * 64
RESEARCH_RAW_MANIFEST = "c" * 64
CALENDAR_RAW_MANIFEST = "d" * 64


def test_assessment_emits_exact_blocked_manifest_without_external_evidence() -> None:
    research, calendar = _source_pair()

    result = build_daily_candle_research_certification_assessment(
        research,
        calendar,
    )
    payload = result.to_payload()
    manifest_sha256 = cast(str, payload.pop("certification_manifest_sha256"))
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    assert result.certification_status == PIT_DAILY_CANDLE_RESEARCH_CERTIFICATION_STATUS
    assert result.retained_open_session_chain_validated is True
    assert result.retained_calendar_date_coverage_complete is True
    assert result.cross_source_shared_calendar_lineage_bound is True
    assert result.official_exchange_calendar_completeness_evidence_sha256 is None
    assert result.provider_history_completeness_evidence_sha256 is None
    assert result.provider_authenticity_evidence_sha256 is None
    assert result.provider_finality_evidence_sha256 is None
    assert result.official_exchange_calendar_completeness_certified is False
    assert result.provider_history_completeness_certified is False
    assert result.provider_authenticity_certified is False
    assert result.provider_finality_certified is False
    assert result.full_research_certified is False
    assert result.promotion_allowed is False
    assert result.rejection_reasons == PIT_DAILY_CANDLE_RESEARCH_CERTIFICATION_REJECTION_REASONS
    assert result.limitations == PIT_DAILY_CANDLE_RESEARCH_CERTIFICATION_LIMITATIONS
    assert result.research_slice_spec_sha256 == research.slice_spec_sha256
    assert result.research_data_manifest_sha256 == research.data_manifest_sha256
    assert result.calendar_coverage_spec_sha256 == calendar.coverage_spec_sha256
    assert result.calendar_data_manifest_sha256 == calendar.data_manifest_sha256
    assert (
        result.cross_source_shared_calendar_lineage_sha256
        == "9462c9f92e18115ef61b6df95cafed508f5bd3382bbde2b86c6bcd75020c553f"
    )
    assert manifest_sha256 == result.certification_manifest_sha256
    assert manifest_sha256 == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert manifest_sha256 == "7172cae7282b0d15768ea5b8f247193377dc80ecef9b6f901324f84f79bbbd1b"

    serialized = json.dumps(result.to_payload(), sort_keys=True)
    assert "credential" not in serialized
    assert "secret" not in serialized
    assert "C:\\" not in serialized


def test_assessment_binds_open_sessions_across_mixed_calendar_days() -> None:
    research, calendar = _mixed_open_closed_source_pair()

    result = build_daily_candle_research_certification_assessment(
        research,
        calendar,
    )

    assert result.selected_session_count == 2
    assert result.calendar_day_count == 3
    assert result.retained_open_session_chain_validated is True
    assert result.retained_calendar_date_coverage_complete is True
    assert result.cross_source_shared_calendar_lineage_bound is True
    assert result.full_research_certified is False
    assert result.promotion_allowed is False


def test_negative_only_assessment_is_not_imported_by_runtime_code() -> None:
    app_root = Path(__file__).resolve().parents[2]
    violations: list[str] = []

    for source_path in sorted(app_root.rglob("*.py")):
        relative_path = source_path.relative_to(app_root)
        if (
            "tests" in relative_path.parts
            or "tools" in relative_path.parts
            or source_path.name == "daily_candle_research_certification.py"
        ):
            continue
        if _imports_negative_only_certification(
            source_path.read_text(encoding="utf-8"),
            filename=str(source_path),
        ):
            violations.append(relative_path.as_posix())

    assert violations == []


@pytest.mark.parametrize(
    "source",
    [
        "import app.application.services.daily_candle_research_certification",
        (
            "from app.application.services.daily_candle_research_certification "
            "import require_full_research_certification"
        ),
        "from app.application.services import daily_candle_research_certification",
        "from . import daily_candle_research_certification",
        ("from .daily_candle_research_certification import require_full_research_certification"),
        (
            "import importlib\n"
            "importlib.import_module("
            "'app.application.services.daily_candle_research_certification')"
        ),
        (
            "from importlib import import_module\n"
            "import_module('.daily_candle_research_certification', package=__package__)"
        ),
        "__import__('app.application.services.daily_candle_research_certification')",
    ],
)
def test_no_wiring_guard_recognizes_supported_import_forms(source: str) -> None:
    assert _imports_negative_only_certification(source, filename="synthetic.py") is True


def test_require_full_research_certification_rejects_blocked_assessment() -> None:
    research, calendar = _source_pair()
    assessment = build_daily_candle_research_certification_assessment(
        research,
        calendar,
    )

    with pytest.raises(
        DailyCandleResearchCertificationError,
        match="daily_candle_research_certification_not_certified",
    ) as exc_info:
        require_full_research_certification(assessment)

    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


@pytest.mark.parametrize(
    ("field_name", "payload_path", "forged_value"),
    [
        (
            "schema_version",
            ("schema_version",),
            "pit_daily_candle_research_certification_assessment.v2",
        ),
        ("certification_status", ("certification_status",), "certified"),
        (
            "retained_open_session_chain_validated",
            ("local_evidence", "retained_open_session_chain_validated"),
            False,
        ),
        (
            "retained_calendar_date_coverage_complete",
            ("local_evidence", "retained_calendar_date_coverage_complete"),
            False,
        ),
        (
            "cross_source_shared_calendar_lineage_bound",
            ("local_evidence", "cross_source_shared_calendar_lineage_bound"),
            False,
        ),
        (
            "official_exchange_calendar_completeness_evidence_sha256",
            ("external_evidence", "official_exchange_calendar_completeness_sha256"),
            "e" * 64,
        ),
        (
            "provider_history_completeness_evidence_sha256",
            ("external_evidence", "provider_history_completeness_sha256"),
            "e" * 64,
        ),
        (
            "provider_authenticity_evidence_sha256",
            ("external_evidence", "provider_authenticity_sha256"),
            "e" * 64,
        ),
        (
            "provider_finality_evidence_sha256",
            ("external_evidence", "provider_finality_sha256"),
            "e" * 64,
        ),
        (
            "official_exchange_calendar_completeness_certified",
            ("certification", "official_exchange_calendar_completeness_certified"),
            True,
        ),
        (
            "provider_history_completeness_certified",
            ("certification", "provider_history_completeness_certified"),
            True,
        ),
        (
            "provider_authenticity_certified",
            ("certification", "provider_authenticity_certified"),
            True,
        ),
        (
            "provider_finality_certified",
            ("certification", "provider_finality_certified"),
            True,
        ),
        (
            "full_research_certified",
            ("certification", "full_research_certified"),
            True,
        ),
        ("promotion_allowed", ("certification", "promotion_allowed"), True),
        ("rejection_reasons", ("rejection_reasons",), ()),
        ("limitations", ("limitations",), ()),
    ],
)
def test_manifest_and_require_gate_reject_forged_fields_with_recomputed_digest(
    field_name: str,
    payload_path: tuple[str, ...],
    forged_value: object,
) -> None:
    research, calendar = _source_pair()
    assessment = build_daily_candle_research_certification_assessment(
        research,
        calendar,
    )
    forged_payload = assessment.to_payload()
    forged_payload.pop("certification_manifest_sha256")
    cursor = cast(dict[str, Any], forged_payload)
    for path_part in payload_path[:-1]:
        cursor = cast(dict[str, Any], cursor[path_part])
    cursor[payload_path[-1]] = forged_value
    object.__setattr__(assessment, field_name, forged_value)
    object.__setattr__(
        assessment,
        "certification_manifest_sha256",
        hashlib.sha256(
            json.dumps(
                forged_payload,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    )

    with pytest.raises(
        DailyCandleResearchCertificationError,
        match="daily_candle_research_certification_manifest_invalid",
    ):
        assessment.to_payload()

    with pytest.raises(
        DailyCandleResearchCertificationError,
        match="daily_candle_research_certification_manifest_invalid",
    ):
        require_full_research_certification(assessment)


def test_manifest_and_require_gate_reject_forged_digest() -> None:
    research, calendar = _source_pair()
    assessment = build_daily_candle_research_certification_assessment(
        research,
        calendar,
    )
    object.__setattr__(assessment, "certification_manifest_sha256", "f" * 64)

    with pytest.raises(
        DailyCandleResearchCertificationError,
        match="daily_candle_research_certification_manifest_invalid",
    ):
        assessment.to_payload()

    with pytest.raises(
        DailyCandleResearchCertificationError,
        match="daily_candle_research_certification_manifest_invalid",
    ):
        require_full_research_certification(assessment)


@pytest.mark.parametrize(
    ("field_name", "forged_value"),
    [
        ("full_research_certified", True),
        ("coverage_scope", "full_history"),
        ("limitations", ()),
        ("selected_session_count", 2),
        ("source_page_size", 24),
        ("data_manifest_sha256", "e" * 64),
    ],
)
def test_assessment_rebuilds_research_slice_instead_of_trusting_result_fields(
    field_name: str,
    forged_value: object,
) -> None:
    research, calendar = _source_pair()
    object.__setattr__(research, field_name, forged_value)

    with pytest.raises(
        DailyCandleResearchCertificationError,
        match="daily_candle_research_certification_research_slice_invalid",
    ):
        build_daily_candle_research_certification_assessment(research, calendar)


@pytest.mark.parametrize(
    ("field_name", "forged_value"),
    [
        ("market", "KQ"),
        ("start_session_date", SESSION_DATE - timedelta(days=1)),
        ("selected_as_of", AS_OF + timedelta(minutes=1)),
        ("full_calendar_certified", True),
        ("retained_date_coverage_complete", False),
        ("right_boundary_next_session_verified", True),
        ("open_session_count", 2),
        ("source_page_size", 101),
        ("data_manifest_sha256", "e" * 64),
    ],
)
def test_assessment_rebuilds_calendar_coverage_instead_of_trusting_result_fields(
    field_name: str,
    forged_value: object,
) -> None:
    research, calendar = _source_pair()
    object.__setattr__(calendar, field_name, forged_value)

    with pytest.raises(
        DailyCandleResearchCertificationError,
        match="daily_candle_research_certification_calendar_coverage_invalid",
    ):
        build_daily_candle_research_certification_assessment(research, calendar)


def test_assessment_rejects_forged_calendar_lineage_idempotency_key() -> None:
    research, calendar = _source_pair()
    object.__setattr__(
        calendar.items[0].lineage,
        "calendar_idempotency_key",
        "e" * 64,
    )

    with pytest.raises(
        DailyCandleResearchCertificationError,
        match="daily_candle_research_certification_calendar_coverage_invalid",
    ):
        build_daily_candle_research_certification_assessment(research, calendar)


@pytest.mark.parametrize(
    ("pair_kwargs", "expected_error"),
    [
        (
            {"calendar_provider": "other"},
            "daily_candle_research_certification_source_scope_mismatch",
        ),
        (
            {"coverage_session_date": SESSION_DATE - timedelta(days=1)},
            "daily_candle_research_certification_source_scope_mismatch",
        ),
        (
            {"coverage_as_of": AS_OF + timedelta(minutes=1)},
            "daily_candle_research_certification_source_scope_mismatch",
        ),
        (
            {"coverage_contract": "e" * 64},
            "daily_candle_research_certification_source_contract_mismatch",
        ),
        (
            {"coverage_is_open": False},
            "daily_candle_research_certification_open_session_set_mismatch",
        ),
        (
            {"coverage_observed_offset": timedelta(minutes=1)},
            "daily_candle_research_certification_calendar_evidence_mismatch",
        ),
        (
            {"coverage_revision_id_offset": 100},
            "daily_candle_research_certification_calendar_lineage_mismatch",
        ),
        (
            {"coverage_occurrence_id_offset": 100},
            "daily_candle_research_certification_calendar_lineage_mismatch",
        ),
        (
            {"coverage_revision": 2},
            "daily_candle_research_certification_calendar_lineage_mismatch",
        ),
        (
            {"coverage_revision_received_offset": timedelta(minutes=-1)},
            "daily_candle_research_certification_calendar_lineage_mismatch",
        ),
        (
            {"coverage_occurrence_received_offset": timedelta(minutes=1)},
            "daily_candle_research_certification_calendar_lineage_mismatch",
        ),
        (
            {"coverage_occurrence_origin": "stream_head_recovery"},
            "daily_candle_research_certification_calendar_lineage_mismatch",
        ),
    ],
)
def test_assessment_rejects_cross_source_scope_evidence_and_lineage_mismatch(
    pair_kwargs: dict[str, Any],
    expected_error: str,
) -> None:
    research, calendar = _source_pair(**pair_kwargs)

    with pytest.raises(DailyCandleResearchCertificationError, match=expected_error):
        build_daily_candle_research_certification_assessment(research, calendar)


def test_assessment_discloses_unavailable_calendar_revision_observed_clock() -> None:
    baseline_research, baseline_calendar = _source_pair()
    research, calendar = _source_pair(
        coverage_revision_observed_offset=timedelta(seconds=-1),
    )

    baseline = build_daily_candle_research_certification_assessment(
        baseline_research,
        baseline_calendar,
    )
    result = build_daily_candle_research_certification_assessment(research, calendar)

    assert result.cross_source_shared_calendar_lineage_bound is True
    assert "calendar_revision_observed_clock_not_available_in_research_slice" in result.limitations
    assert (
        result.cross_source_shared_calendar_lineage_sha256
        == baseline.cross_source_shared_calendar_lineage_sha256
    )
    assert result.calendar_data_manifest_sha256 != baseline.calendar_data_manifest_sha256
    assert result.certification_manifest_sha256 != baseline.certification_manifest_sha256


def test_assessment_digest_is_stable_across_transport_metadata_and_timezone() -> None:
    first_research, first_calendar = _source_pair(
        research_page_size=25,
        calendar_page_size=25,
        research_token="1:2:3",
        calendar_token="4:5:6",
    )
    second_research, second_calendar = _source_pair(
        as_of=AS_OF.astimezone(KST),
        research_page_size=100,
        calendar_page_size=100,
        research_token="9:8:7,6",
        calendar_token="5:4:3,2",
        snapshot_issued_at=SNAPSHOT_ISSUED_AT + timedelta(hours=1),
    )

    first = build_daily_candle_research_certification_assessment(
        first_research,
        first_calendar,
    )
    second = build_daily_candle_research_certification_assessment(
        second_research,
        second_calendar,
    )

    assert first.research_slice_spec_sha256 == second.research_slice_spec_sha256
    assert first.research_data_manifest_sha256 == second.research_data_manifest_sha256
    assert first.calendar_coverage_spec_sha256 == second.calendar_coverage_spec_sha256
    assert first.calendar_data_manifest_sha256 == second.calendar_data_manifest_sha256
    assert first.certification_manifest_sha256 == second.certification_manifest_sha256


@pytest.mark.parametrize(
    "source_kwargs",
    [
        {"research_raw_manifest": "e" * 64},
        {"calendar_raw_manifest": "e" * 64},
    ],
)
def test_assessment_digest_changes_with_bound_source_manifest(
    source_kwargs: dict[str, Any],
) -> None:
    first_research, first_calendar = _source_pair()
    second_research, second_calendar = _source_pair(**source_kwargs)

    first = build_daily_candle_research_certification_assessment(
        first_research,
        first_calendar,
    )
    second = build_daily_candle_research_certification_assessment(
        second_research,
        second_calendar,
    )

    assert first.research_slice_spec_sha256 == second.research_slice_spec_sha256
    assert first.calendar_coverage_spec_sha256 == second.calendar_coverage_spec_sha256
    assert (
        first.research_data_manifest_sha256,
        first.calendar_data_manifest_sha256,
    ) != (
        second.research_data_manifest_sha256,
        second.calendar_data_manifest_sha256,
    )
    assert first.certification_manifest_sha256 != second.certification_manifest_sha256


def test_result_is_detached_and_cannot_be_constructed_directly() -> None:
    research, calendar = _source_pair()
    result = build_daily_candle_research_certification_assessment(
        research,
        calendar,
    )
    original_payload = result.to_payload()

    object.__setattr__(research, "data_manifest_sha256", "e" * 64)
    object.__setattr__(calendar, "data_manifest_sha256", "f" * 64)

    assert result.to_payload() == original_payload
    constructor = cast(Any, DailyCandleResearchCertificationManifestV1)
    with pytest.raises(
        DailyCandleResearchCertificationError,
        match="daily_candle_research_certification_requires_gate",
    ):
        constructor()


def test_invalid_source_error_does_not_retain_attacker_text() -> None:
    secret = "credential=must-not-leak"

    with pytest.raises(
        DailyCandleResearchCertificationError,
        match="daily_candle_research_certification_research_slice_invalid",
    ) as exc_info:
        build_daily_candle_research_certification_assessment(
            cast(Any, _ExplodingSlice(secret)),
            cast(Any, object()),
        )

    formatted = "".join(traceback.format_exception(exc_info.value))
    assert secret not in str(exc_info.value)
    assert secret not in formatted
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


class _ExplodingSlice:
    def __init__(self, secret: str) -> None:
        self.secret = secret

    @property
    def provider(self) -> str:
        raise RuntimeError(self.secret)


def _imports_negative_only_certification(source: str, *, filename: str) -> bool:
    target_module = "app.application.services.daily_candle_research_certification"
    target_leaf = "daily_candle_research_certification"

    def matches_target(module_name: str) -> bool:
        normalized = module_name.lstrip(".")
        return (
            normalized in (target_module, target_leaf)
            or normalized.startswith(f"{target_module}.")
            or normalized.endswith(f".{target_leaf}")
        )

    tree = ast.parse(source, filename=filename)
    for node in ast.walk(tree):
        imported_modules: list[str] = []
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None:
                imported_modules.append(node.module)
            for alias in node.names:
                imported_modules.append(alias.name)
                if node.module is not None:
                    imported_modules.append(f"{node.module}.{alias.name}")
        elif isinstance(node, ast.Call) and node.args:
            first_argument = node.args[0]
            is_import_call = (
                isinstance(node.func, ast.Name) and node.func.id in {"__import__", "import_module"}
            ) or (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "importlib"
                and node.func.attr == "import_module"
            )
            if (
                is_import_call
                and isinstance(first_argument, ast.Constant)
                and isinstance(first_argument.value, str)
            ):
                imported_modules.append(first_argument.value)
        if any(matches_target(module_name) for module_name in imported_modules):
            return True
    return False


def _mixed_open_closed_source_pair() -> tuple[
    ContiguousDailyCandleResearchSliceV1,
    RetainedKrCalendarDateRangeCoverageV1,
]:
    first_open_date = SESSION_DATE - timedelta(days=2)
    closed_date = SESSION_DATE - timedelta(days=1)
    first_calendar = _calendar_session(
        session_date=first_open_date,
        next_session_date=SESSION_DATE,
    )
    closed_calendar = _calendar_session(
        session_date=closed_date,
        next_session_date=SESSION_DATE,
        is_open=False,
    )
    last_calendar = _calendar_session(
        session_date=SESSION_DATE,
        next_session_date=NEXT_SESSION_DATE,
    )
    first_research_item = _research_item_for_calendar(first_calendar, serial=10)
    last_research_item = _research_item_for_calendar(last_calendar, serial=20)
    research_items = (first_research_item, last_research_item)
    research_request = DailyCandleAsOfReadRequest(
        provider="toss",
        market="KR",
        symbol="005930",
        interval="1d",
        adjusted=True,
        start_session_date=first_open_date,
        end_session_date=SESSION_DATE,
        as_of=AS_OF,
        page_size=100,
    )
    research = build_contiguous_daily_candle_research_slice(
        research_request,
        DurableDailyCandleAsOfSnapshotV1(
            query_sha256=daily_candle_as_of_query_sha256(research_request),
            snapshot_token="1:2:3",
            snapshot_issued_at=SNAPSHOT_ISSUED_AT,
            snapshot_manifest_sha256=RESEARCH_RAW_MANIFEST,
            candidate_count=len(research_items),
            items=research_items,
        ),
    )

    calendar_items = (
        _coverage_item_for_calendar(
            first_calendar,
            serial=10,
            research_lineage=first_research_item.lineage,
        ),
        _coverage_item_for_calendar(closed_calendar, serial=30),
        _coverage_item_for_calendar(
            last_calendar,
            serial=20,
            research_lineage=last_research_item.lineage,
        ),
    )
    calendar_request = CalendarAsOfReadRequest(
        provider="toss",
        market="KR",
        start_session_date=first_open_date,
        end_session_date=SESSION_DATE,
        as_of=AS_OF,
        page_size=100,
    )
    calendar = build_retained_kr_calendar_date_range_coverage(
        calendar_request,
        DurableCalendarAsOfSnapshotV1(
            query_sha256=calendar_as_of_query_sha256(calendar_request),
            snapshot_token="4:5:6",
            snapshot_issued_at=SNAPSHOT_ISSUED_AT,
            snapshot_manifest_sha256=CALENDAR_RAW_MANIFEST,
            candidate_count=len(calendar_items),
            items=calendar_items,
        ),
    )
    return research, calendar


def _research_item_for_calendar(
    calendar: PointInTimeKrDailySessionV1,
    *,
    serial: int,
) -> DurableSelectedDailyCandleV1:
    regular_end_at = cast(datetime, calendar.regular_end_at)
    candle = PointInTimeCandleV1.create(
        provider=calendar.provider,
        symbol="005930",
        market=calendar.market,
        interval="1d",
        adjusted=True,
        provider_event_at=regular_end_at,
        observed_at=calendar.observed_at - timedelta(minutes=5),
        currency="KRW",
        open_krw=70_000 + serial,
        high_krw=71_000 + serial,
        low_krw=69_000 + serial,
        close_krw=70_500 + serial,
        volume=1_000_000 + serial,
        provider_contract_sha256=CANDLE_CONTRACT,
    )
    timing = build_daily_candle_timing_evidence(candle, calendar)
    selection = select_daily_candles_as_of([(candle, timing)], as_of=AS_OF)[0]
    received_at = timing.evidence_available_at + timedelta(minutes=10)
    lineage = DailyCandleAsOfLineageV1(
        timing_revision_id=_uuid(serial + 1),
        timing_idempotency_key=timing.idempotency_key,
        timing_revision=1,
        timing_canonical_evidence_sha256=timing.canonical_timing_evidence_sha256,
        timing_received_at=received_at,
        candle_revision_id=_uuid(serial + 2),
        candle_revision=1,
        candle_canonical_observation_sha256=candle.canonical_observation_sha256,
        candle_revision_received_at=received_at,
        candle_occurrence_id=_uuid(serial + 3),
        candle_occurrence_received_at=received_at,
        candle_occurrence_origin="rpc",
        calendar_revision_id=_uuid(serial + 4),
        calendar_revision=1,
        calendar_canonical_evidence_sha256=calendar.canonical_evidence_sha256,
        calendar_revision_received_at=received_at,
        calendar_occurrence_id=_uuid(serial + 5),
        calendar_occurrence_received_at=received_at,
        calendar_occurrence_origin="rpc",
    )
    return DurableSelectedDailyCandleV1(
        selection=selection,
        calendar=calendar,
        lineage=lineage,
    )


def _coverage_item_for_calendar(
    calendar: PointInTimeKrDailySessionV1,
    *,
    serial: int,
    research_lineage: DailyCandleAsOfLineageV1 | None = None,
) -> DurableSelectedCalendarSessionV1:
    selection = select_kr_daily_sessions_as_of([calendar], as_of=AS_OF)[0]
    received_at = calendar.observed_at + timedelta(minutes=10)
    lineage = CalendarAsOfLineageV1(
        calendar_revision_id=(
            _uuid(serial + 4) if research_lineage is None else research_lineage.calendar_revision_id
        ),
        calendar_idempotency_key=calendar.idempotency_key,
        calendar_revision=(1 if research_lineage is None else research_lineage.calendar_revision),
        calendar_canonical_evidence_sha256=calendar.canonical_evidence_sha256,
        calendar_revision_observed_at=calendar.observed_at,
        calendar_revision_received_at=(
            received_at
            if research_lineage is None
            else research_lineage.calendar_revision_received_at
        ),
        calendar_occurrence_id=(
            _uuid(serial + 5)
            if research_lineage is None
            else research_lineage.calendar_occurrence_id
        ),
        calendar_occurrence_observed_at=calendar.observed_at,
        calendar_occurrence_received_at=(
            received_at
            if research_lineage is None
            else research_lineage.calendar_occurrence_received_at
        ),
        calendar_occurrence_origin=(
            "rpc" if research_lineage is None else research_lineage.calendar_occurrence_origin
        ),
    )
    return DurableSelectedCalendarSessionV1(selection=selection, lineage=lineage)


def _source_pair(
    *,
    as_of: datetime = AS_OF,
    research_page_size: int = 100,
    calendar_page_size: int = 100,
    research_token: str = "1:2:3",
    calendar_token: str = "4:5:6",
    snapshot_issued_at: datetime = SNAPSHOT_ISSUED_AT,
    research_raw_manifest: str = RESEARCH_RAW_MANIFEST,
    calendar_raw_manifest: str = CALENDAR_RAW_MANIFEST,
    calendar_provider: str = "toss",
    coverage_session_date: date = SESSION_DATE,
    coverage_as_of: datetime | None = None,
    coverage_contract: str = CALENDAR_CONTRACT,
    coverage_is_open: bool = True,
    coverage_observed_offset: timedelta = timedelta(0),
    coverage_revision_id_offset: int = 0,
    coverage_occurrence_id_offset: int = 0,
    coverage_revision: int = 1,
    coverage_revision_observed_offset: timedelta = timedelta(0),
    coverage_revision_received_offset: timedelta = timedelta(0),
    coverage_occurrence_received_offset: timedelta = timedelta(0),
    coverage_occurrence_origin: str = "rpc",
) -> tuple[
    ContiguousDailyCandleResearchSliceV1,
    RetainedKrCalendarDateRangeCoverageV1,
]:
    research_calendar = _calendar_session()
    regular_end_at = cast(datetime, research_calendar.regular_end_at)
    candle = PointInTimeCandleV1.create(
        provider="toss",
        symbol="005930",
        market="KR",
        interval="1d",
        adjusted=True,
        provider_event_at=regular_end_at,
        observed_at=research_calendar.observed_at - timedelta(minutes=5),
        currency="KRW",
        open_krw=70_000,
        high_krw=71_000,
        low_krw=69_000,
        close_krw=70_500,
        volume=1_000_000,
        provider_contract_sha256=CANDLE_CONTRACT,
    )
    timing = build_daily_candle_timing_evidence(candle, research_calendar)
    selection = select_daily_candles_as_of(
        [(candle, timing)],
        as_of=as_of,
    )[0]
    received_at = timing.evidence_available_at + timedelta(minutes=10)
    research_lineage = DailyCandleAsOfLineageV1(
        timing_revision_id=_uuid(1),
        timing_idempotency_key=timing.idempotency_key,
        timing_revision=1,
        timing_canonical_evidence_sha256=(timing.canonical_timing_evidence_sha256),
        timing_received_at=received_at,
        candle_revision_id=_uuid(2),
        candle_revision=1,
        candle_canonical_observation_sha256=(candle.canonical_observation_sha256),
        candle_revision_received_at=received_at,
        candle_occurrence_id=_uuid(3),
        candle_occurrence_received_at=received_at,
        candle_occurrence_origin="rpc",
        calendar_revision_id=_uuid(4),
        calendar_revision=1,
        calendar_canonical_evidence_sha256=(research_calendar.canonical_evidence_sha256),
        calendar_revision_received_at=received_at,
        calendar_occurrence_id=_uuid(5),
        calendar_occurrence_received_at=received_at,
        calendar_occurrence_origin="rpc",
    )
    research_item = DurableSelectedDailyCandleV1(
        selection=selection,
        calendar=research_calendar,
        lineage=research_lineage,
    )
    research_request = DailyCandleAsOfReadRequest(
        provider="toss",
        market="KR",
        symbol="005930",
        interval="1d",
        adjusted=True,
        start_session_date=SESSION_DATE,
        end_session_date=SESSION_DATE,
        as_of=as_of,
        page_size=research_page_size,
    )
    research_snapshot = DurableDailyCandleAsOfSnapshotV1(
        query_sha256=daily_candle_as_of_query_sha256(research_request),
        snapshot_token=research_token,
        snapshot_issued_at=snapshot_issued_at,
        snapshot_manifest_sha256=research_raw_manifest,
        candidate_count=1,
        items=(research_item,),
    )
    research = build_contiguous_daily_candle_research_slice(
        research_request,
        research_snapshot,
    )

    selected_coverage_as_of = as_of if coverage_as_of is None else coverage_as_of
    coverage_session = (
        research_calendar
        if (
            calendar_provider == "toss"
            and coverage_session_date == SESSION_DATE
            and coverage_contract == CALENDAR_CONTRACT
            and coverage_is_open
            and coverage_observed_offset == timedelta(0)
        )
        else _calendar_session(
            provider=calendar_provider,
            session_date=coverage_session_date,
            next_session_date=coverage_session_date + timedelta(days=1),
            provider_contract_sha256=coverage_contract,
            is_open=coverage_is_open,
            observed_offset=coverage_observed_offset,
        )
    )
    coverage_selection = select_kr_daily_sessions_as_of(
        [coverage_session],
        as_of=selected_coverage_as_of,
    )[0]
    coverage_received_at = coverage_session.observed_at + timedelta(minutes=10)
    coverage_lineage = CalendarAsOfLineageV1(
        calendar_revision_id=_uuid(4 + coverage_revision_id_offset),
        calendar_idempotency_key=coverage_session.idempotency_key,
        calendar_revision=coverage_revision,
        calendar_canonical_evidence_sha256=(coverage_session.canonical_evidence_sha256),
        calendar_revision_observed_at=(
            coverage_session.observed_at + coverage_revision_observed_offset
        ),
        calendar_revision_received_at=(coverage_received_at + coverage_revision_received_offset),
        calendar_occurrence_id=_uuid(5 + coverage_occurrence_id_offset),
        calendar_occurrence_observed_at=coverage_session.observed_at,
        calendar_occurrence_received_at=(
            coverage_received_at + coverage_occurrence_received_offset
        ),
        calendar_occurrence_origin=coverage_occurrence_origin,
    )
    coverage_item = DurableSelectedCalendarSessionV1(
        selection=coverage_selection,
        lineage=coverage_lineage,
    )
    calendar_request = CalendarAsOfReadRequest(
        provider=calendar_provider,
        market="KR",
        start_session_date=coverage_session_date,
        end_session_date=coverage_session_date,
        as_of=selected_coverage_as_of,
        page_size=calendar_page_size,
    )
    calendar_snapshot = DurableCalendarAsOfSnapshotV1(
        query_sha256=calendar_as_of_query_sha256(calendar_request),
        snapshot_token=calendar_token,
        snapshot_issued_at=snapshot_issued_at,
        snapshot_manifest_sha256=calendar_raw_manifest,
        candidate_count=1,
        items=(coverage_item,),
    )
    calendar = build_retained_kr_calendar_date_range_coverage(
        calendar_request,
        calendar_snapshot,
    )
    return research, calendar


def _calendar_session(
    *,
    provider: str = "toss",
    session_date: date = SESSION_DATE,
    next_session_date: date = NEXT_SESSION_DATE,
    provider_contract_sha256: str = CALENDAR_CONTRACT,
    is_open: bool = True,
    observed_offset: timedelta = timedelta(0),
) -> PointInTimeKrDailySessionV1:
    regular_start_at = datetime.combine(
        session_date,
        datetime.min.time(),
        tzinfo=KST,
    ) + timedelta(hours=9)
    regular_end_at = regular_start_at + timedelta(hours=6, minutes=30)
    next_regular_start_at = datetime.combine(
        next_session_date,
        datetime.min.time(),
        tzinfo=KST,
    ) + timedelta(hours=9)
    next_regular_end_at = next_regular_start_at + timedelta(hours=6, minutes=30)
    return PointInTimeKrDailySessionV1.create(
        provider=provider,
        market="KR",
        session_date=session_date,
        is_open=is_open,
        regular_start_at=regular_start_at if is_open else None,
        regular_end_at=regular_end_at if is_open else None,
        next_business_date=next_session_date,
        next_regular_start_at=next_regular_start_at,
        next_regular_end_at=next_regular_end_at,
        observed_at=(next_regular_start_at + timedelta(hours=1, minutes=5) + observed_offset),
        provider_contract_sha256=provider_contract_sha256,
    )


def _uuid(value: int) -> str:
    return f"00000000-0000-4000-8000-{value:012x}"
