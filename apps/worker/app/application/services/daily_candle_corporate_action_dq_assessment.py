from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Never

from app.application.services.daily_candle_research_certification import (
    build_daily_candle_research_certification_assessment,
)
from app.application.services.daily_candle_research_slice import (
    ContiguousDailyCandleResearchSliceV1,
    validate_contiguous_daily_candle_research_slice,
)
from app.application.services.retained_kr_calendar_coverage import (
    RetainedKrCalendarDateRangeCoverageV1,
    validate_retained_kr_calendar_date_range_coverage,
)
from app.domain.common.errors import KnownFailClosedError
from app.domain.common.json import JsonObject

PIT_DAILY_CANDLE_CORPORATE_ACTION_DQ_SCHEMA_VERSION = (
    "pit_daily_candle_corporate_action_dq_assessment.v1"
)
PIT_DAILY_CANDLE_LOCAL_DQ_POLICY_VERSION = "pit_daily_candle_local_dq_policy.v1"
PIT_DAILY_CANDLE_CORPORATE_ACTION_DQ_STATUS = (
    "local_checks_passed_blocked_missing_corporate_action_evidence"
)
PIT_DAILY_CANDLE_CORPORATE_ACTION_STATUS = "blocked_missing_verified_point_in_time_evidence"
PIT_DAILY_CANDLE_LOCAL_DQ_CHECK_IDS = (
    "canonical_candle_payload_and_hash",
    "canonical_calendar_payload_and_hash",
    "canonical_timing_payload_and_hash",
    "ohlcv_structural_invariants",
    "timing_available_after_next_session_open",
    "retained_open_session_chain",
    "retained_calendar_date_range_coverage",
    "revision_occurrence_lineage_uniqueness",
    "uniform_provider_contract_pins",
    "cross_source_shared_calendar_lineage_binding",
)
PIT_DAILY_CANDLE_CORPORATE_ACTION_DQ_REJECTION_REASONS = (
    "verified_point_in_time_corporate_action_evidence_missing",
    "corporate_action_range_coverage_not_verified",
    "corporate_action_adjustment_semantics_not_verified",
    "full_research_certification_not_available",
)
PIT_DAILY_CANDLE_CORPORATE_ACTION_DQ_LIMITATIONS = (
    "local_structural_timing_and_lineage_checks_only",
    "corporate_action_source_contract_not_selected",
    "corporate_action_empty_range_coverage_not_verified",
    "corporate_action_adjustment_mapping_not_verified",
    "price_outlier_trading_halt_listing_and_delisting_not_evaluated",
    "full_data_quality_not_certified",
    "dataset_feature_backtest_strategy_order_use_not_authorized",
    "production_live_use_not_authorized",
)


class DailyCandleCorporateActionDqAssessmentError(KnownFailClosedError):
    def __init__(self, safe_message: str) -> None:
        super().__init__("daily_candle_corporate_action_dq_assessment", safe_message)


@dataclass(frozen=True, slots=True, init=False)
class DailyCandleCorporateActionDqManifestV1:
    schema_version: str
    assessment_status: str
    provider: str
    market: str
    symbol: str
    interval: str
    adjusted: bool
    start_session_date: date
    end_session_date: date
    selected_as_of: datetime
    selected_session_count: int
    calendar_day_count: int
    research_certification_status: str
    research_certification_manifest_sha256: str
    research_slice_spec_sha256: str
    research_data_manifest_sha256: str
    calendar_coverage_spec_sha256: str
    calendar_data_manifest_sha256: str
    cross_source_shared_calendar_lineage_sha256: str
    dq_policy_version: str
    dq_policy_sha256: str
    dq_check_ids: tuple[str, ...]
    dq_check_count: int
    dq_results_sha256: str
    local_retained_dq_checks_passed: bool
    corporate_action_status: str
    corporate_action_evidence_sha256: str | None
    corporate_action_coverage_verified: bool
    corporate_action_adjustment_semantics_verified: bool
    full_data_quality_certified: bool
    dataset_registration_allowed: bool
    feature_use_allowed: bool
    backtest_use_allowed: bool
    strategy_promotion_allowed: bool
    order_use_allowed: bool
    rejection_reasons: tuple[str, ...]
    limitations: tuple[str, ...]
    assessment_manifest_sha256: str
    _research_slice: ContiguousDailyCandleResearchSliceV1 = field(repr=False)
    _calendar_coverage: RetainedKrCalendarDateRangeCoverageV1 = field(repr=False)

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise DailyCandleCorporateActionDqAssessmentError(
            "daily_candle_corporate_action_dq_assessment_requires_gate"
        )

    def to_payload(self) -> JsonObject:
        assessment = validate_daily_candle_corporate_action_dq_assessment(self)
        payload = _assessment_manifest_body(assessment)
        payload["assessment_manifest_sha256"] = assessment.assessment_manifest_sha256
        return payload


def build_daily_candle_corporate_action_dq_assessment(
    research_slice: object,
    calendar_coverage: object,
) -> DailyCandleCorporateActionDqManifestV1:
    """Approve bounded local checks while keeping corporate-action use blocked."""

    valid_research, valid_calendar = _canonical_sources(
        research_slice,
        calendar_coverage,
    )
    result = _make_assessment(valid_research, valid_calendar)
    return validate_daily_candle_corporate_action_dq_assessment(result)


def validate_daily_candle_corporate_action_dq_assessment(
    value: object,
) -> DailyCandleCorporateActionDqManifestV1:
    """Rebuild the assessment from its retained canonical sources."""

    if type(value) is not DailyCandleCorporateActionDqManifestV1:
        raise DailyCandleCorporateActionDqAssessmentError(
            "daily_candle_corporate_action_dq_assessment_manifest_invalid"
        )
    try:
        valid_research, valid_calendar = _canonical_sources(
            value._research_slice,
            value._calendar_coverage,
        )
        expected = _make_assessment(valid_research, valid_calendar)
        matches = _assessment_has_exact_types(value) and expected == value
    except Exception:
        pass
    else:
        if matches:
            return expected
    raise DailyCandleCorporateActionDqAssessmentError(
        "daily_candle_corporate_action_dq_assessment_manifest_invalid"
    ) from None


def require_corporate_action_dq_certification(assessment: object) -> Never:
    """Reject V1 before dataset registration or any downstream use."""

    validate_daily_candle_corporate_action_dq_assessment(assessment)
    raise DailyCandleCorporateActionDqAssessmentError(
        "daily_candle_corporate_action_dq_assessment_not_certified"
    )


def _canonical_sources(
    research_slice: object,
    calendar_coverage: object,
) -> tuple[
    ContiguousDailyCandleResearchSliceV1,
    RetainedKrCalendarDateRangeCoverageV1,
]:
    try:
        valid_research = validate_contiguous_daily_candle_research_slice(research_slice)
        valid_calendar = validate_retained_kr_calendar_date_range_coverage(calendar_coverage)
        build_daily_candle_research_certification_assessment(
            valid_research,
            valid_calendar,
        )
    except Exception:
        pass
    else:
        return valid_research, valid_calendar
    raise DailyCandleCorporateActionDqAssessmentError(
        "daily_candle_corporate_action_dq_assessment_source_invalid"
    ) from None


def _make_assessment(
    research: ContiguousDailyCandleResearchSliceV1,
    calendar: RetainedKrCalendarDateRangeCoverageV1,
) -> DailyCandleCorporateActionDqManifestV1:
    upstream = build_daily_candle_research_certification_assessment(
        research,
        calendar,
    )
    dq_policy_sha256 = _payload_sha256(_dq_policy_payload())
    dq_results_sha256 = _payload_sha256(
        _dq_results_payload(
            dq_policy_sha256=dq_policy_sha256,
            research_certification_manifest_sha256=(upstream.certification_manifest_sha256),
            research_slice_spec_sha256=upstream.research_slice_spec_sha256,
            research_data_manifest_sha256=upstream.research_data_manifest_sha256,
            calendar_coverage_spec_sha256=upstream.calendar_coverage_spec_sha256,
            calendar_data_manifest_sha256=upstream.calendar_data_manifest_sha256,
            cross_source_shared_calendar_lineage_sha256=(
                upstream.cross_source_shared_calendar_lineage_sha256
            ),
        )
    )

    result = object.__new__(DailyCandleCorporateActionDqManifestV1)
    values: dict[str, object] = {
        "schema_version": PIT_DAILY_CANDLE_CORPORATE_ACTION_DQ_SCHEMA_VERSION,
        "assessment_status": PIT_DAILY_CANDLE_CORPORATE_ACTION_DQ_STATUS,
        "provider": upstream.provider,
        "market": upstream.market,
        "symbol": upstream.symbol,
        "interval": upstream.interval,
        "adjusted": upstream.adjusted,
        "start_session_date": upstream.start_session_date,
        "end_session_date": upstream.end_session_date,
        "selected_as_of": upstream.selected_as_of,
        "selected_session_count": upstream.selected_session_count,
        "calendar_day_count": upstream.calendar_day_count,
        "research_certification_status": upstream.certification_status,
        "research_certification_manifest_sha256": (upstream.certification_manifest_sha256),
        "research_slice_spec_sha256": upstream.research_slice_spec_sha256,
        "research_data_manifest_sha256": upstream.research_data_manifest_sha256,
        "calendar_coverage_spec_sha256": upstream.calendar_coverage_spec_sha256,
        "calendar_data_manifest_sha256": upstream.calendar_data_manifest_sha256,
        "cross_source_shared_calendar_lineage_sha256": (
            upstream.cross_source_shared_calendar_lineage_sha256
        ),
        "dq_policy_version": PIT_DAILY_CANDLE_LOCAL_DQ_POLICY_VERSION,
        "dq_policy_sha256": dq_policy_sha256,
        "dq_check_ids": PIT_DAILY_CANDLE_LOCAL_DQ_CHECK_IDS,
        "dq_check_count": len(PIT_DAILY_CANDLE_LOCAL_DQ_CHECK_IDS),
        "dq_results_sha256": dq_results_sha256,
        "local_retained_dq_checks_passed": True,
        "corporate_action_status": PIT_DAILY_CANDLE_CORPORATE_ACTION_STATUS,
        "corporate_action_evidence_sha256": None,
        "corporate_action_coverage_verified": False,
        "corporate_action_adjustment_semantics_verified": False,
        "full_data_quality_certified": False,
        "dataset_registration_allowed": False,
        "feature_use_allowed": False,
        "backtest_use_allowed": False,
        "strategy_promotion_allowed": False,
        "order_use_allowed": False,
        "rejection_reasons": (PIT_DAILY_CANDLE_CORPORATE_ACTION_DQ_REJECTION_REASONS),
        "limitations": PIT_DAILY_CANDLE_CORPORATE_ACTION_DQ_LIMITATIONS,
        "_research_slice": research,
        "_calendar_coverage": calendar,
    }
    for field_name, field_value in values.items():
        object.__setattr__(result, field_name, field_value)
    object.__setattr__(
        result,
        "assessment_manifest_sha256",
        _payload_sha256(_assessment_manifest_body(result)),
    )
    return result


def _dq_policy_payload() -> JsonObject:
    return {
        "schema_version": PIT_DAILY_CANDLE_LOCAL_DQ_POLICY_VERSION,
        "local_checks": [
            {"check_id": check_id, "required_status": "pass"}
            for check_id in PIT_DAILY_CANDLE_LOCAL_DQ_CHECK_IDS
        ],
        "corporate_action": {
            "verified_point_in_time_evidence_required": True,
            "adjusted_request_flag_is_evidence": False,
            "empty_event_list_proves_coverage": False,
            "missing_evidence_behavior": "block_full_data_quality",
        },
        "authorization": {
            "local_checks_may_authorize_dataset_registration": False,
            "local_checks_may_authorize_feature_or_backtest_use": False,
            "local_checks_may_authorize_strategy_or_order_use": False,
        },
    }


def _dq_results_payload(
    *,
    dq_policy_sha256: str,
    research_certification_manifest_sha256: str,
    research_slice_spec_sha256: str,
    research_data_manifest_sha256: str,
    calendar_coverage_spec_sha256: str,
    calendar_data_manifest_sha256: str,
    cross_source_shared_calendar_lineage_sha256: str,
) -> JsonObject:
    return {
        "schema_version": PIT_DAILY_CANDLE_LOCAL_DQ_POLICY_VERSION,
        "policy_sha256": dq_policy_sha256,
        "source_binding": {
            "research_certification_manifest_sha256": (research_certification_manifest_sha256),
            "research_slice_spec_sha256": research_slice_spec_sha256,
            "research_data_manifest_sha256": research_data_manifest_sha256,
            "calendar_coverage_spec_sha256": calendar_coverage_spec_sha256,
            "calendar_data_manifest_sha256": calendar_data_manifest_sha256,
            "cross_source_shared_calendar_lineage_sha256": (
                cross_source_shared_calendar_lineage_sha256
            ),
        },
        "results": [
            {"check_id": check_id, "status": "pass"}
            for check_id in PIT_DAILY_CANDLE_LOCAL_DQ_CHECK_IDS
        ],
    }


def _assessment_has_exact_types(
    assessment: DailyCandleCorporateActionDqManifestV1,
) -> bool:
    string_values = (
        assessment.schema_version,
        assessment.assessment_status,
        assessment.provider,
        assessment.market,
        assessment.symbol,
        assessment.interval,
        assessment.research_certification_status,
        assessment.research_certification_manifest_sha256,
        assessment.research_slice_spec_sha256,
        assessment.research_data_manifest_sha256,
        assessment.calendar_coverage_spec_sha256,
        assessment.calendar_data_manifest_sha256,
        assessment.cross_source_shared_calendar_lineage_sha256,
        assessment.dq_policy_version,
        assessment.dq_policy_sha256,
        assessment.dq_results_sha256,
        assessment.corporate_action_status,
        assessment.assessment_manifest_sha256,
    )
    boolean_values = (
        assessment.adjusted,
        assessment.local_retained_dq_checks_passed,
        assessment.corporate_action_coverage_verified,
        assessment.corporate_action_adjustment_semantics_verified,
        assessment.full_data_quality_certified,
        assessment.dataset_registration_allowed,
        assessment.feature_use_allowed,
        assessment.backtest_use_allowed,
        assessment.strategy_promotion_allowed,
        assessment.order_use_allowed,
    )
    return (
        all(type(item) is str for item in string_values)
        and all(type(item) is bool for item in boolean_values)
        and type(assessment.start_session_date) is date
        and type(assessment.end_session_date) is date
        and type(assessment.selected_as_of) is datetime
        and assessment.selected_as_of.tzinfo is UTC
        and type(assessment.selected_session_count) is int
        and type(assessment.calendar_day_count) is int
        and type(assessment.dq_check_count) is int
        and type(assessment.dq_check_ids) is tuple
        and all(type(item) is str for item in assessment.dq_check_ids)
        and assessment.corporate_action_evidence_sha256 is None
        and type(assessment.rejection_reasons) is tuple
        and all(type(item) is str for item in assessment.rejection_reasons)
        and type(assessment.limitations) is tuple
        and all(type(item) is str for item in assessment.limitations)
        and _research_source_has_exact_types(assessment._research_slice)
        and _calendar_source_has_exact_types(assessment._calendar_coverage)
    )


def _research_source_has_exact_types(
    source: ContiguousDailyCandleResearchSliceV1,
) -> bool:
    return (
        type(source) is ContiguousDailyCandleResearchSliceV1
        and type(source.schema_version) is str
        and type(source.provider) is str
        and type(source.market) is str
        and type(source.symbol) is str
        and type(source.interval) is str
        and type(source.adjusted) is bool
        and type(source.start_session_date) is date
        and type(source.end_session_date) is date
        and type(source.selected_as_of) is datetime
        and source.selected_as_of.tzinfo is UTC
        and type(source.selected_session_count) is int
        and type(source.coverage_scope) is str
        and type(source.full_research_certified) is bool
        and type(source.limitations) is tuple
        and all(type(item) is str for item in source.limitations)
        and type(source.candle_provider_contract_sha256) is str
        and type(source.calendar_provider_contract_sha256) is str
        and type(source.source_page_size) is int
        and type(source.source_query_sha256) is str
        and type(source.source_snapshot_manifest_sha256) is str
        and type(source.source_candidate_count) is int
        and type(source.source_snapshot_issued_at) is datetime
        and source.source_snapshot_issued_at.tzinfo is UTC
        and type(source.slice_spec_sha256) is str
        and type(source.data_manifest_sha256) is str
        and type(source.items) is tuple
    )


def _calendar_source_has_exact_types(
    source: RetainedKrCalendarDateRangeCoverageV1,
) -> bool:
    return (
        type(source) is RetainedKrCalendarDateRangeCoverageV1
        and type(source.schema_version) is str
        and type(source.provider) is str
        and type(source.market) is str
        and type(source.start_session_date) is date
        and type(source.end_session_date) is date
        and type(source.selected_as_of) is datetime
        and source.selected_as_of.tzinfo is UTC
        and type(source.calendar_day_count) is int
        and type(source.open_session_count) is int
        and type(source.closed_day_count) is int
        and type(source.retained_date_coverage_complete) is bool
        and type(source.coverage_scope) is str
        and type(source.full_calendar_certified) is bool
        and type(source.right_boundary_next_session_verified) is bool
        and type(source.limitations) is tuple
        and all(type(item) is str for item in source.limitations)
        and type(source.selected_provider_contract_sha256) is str
        and type(source.source_page_size) is int
        and type(source.source_query_sha256) is str
        and type(source.source_snapshot_manifest_sha256) is str
        and type(source.source_candidate_count) is int
        and type(source.source_snapshot_issued_at) is datetime
        and source.source_snapshot_issued_at.tzinfo is UTC
        and type(source.coverage_spec_sha256) is str
        and type(source.data_manifest_sha256) is str
        and type(source.items) is tuple
    )


def _assessment_manifest_body(
    assessment: DailyCandleCorporateActionDqManifestV1,
) -> JsonObject:
    return {
        "schema_version": assessment.schema_version,
        "assessment_status": assessment.assessment_status,
        "scope": {
            "provider": assessment.provider,
            "market": assessment.market,
            "symbol": assessment.symbol,
            "interval": assessment.interval,
            "adjusted": assessment.adjusted,
            "first_session_date": assessment.start_session_date.isoformat(),
            "last_session_date": assessment.end_session_date.isoformat(),
            "as_of": _canonical_timestamp(assessment.selected_as_of),
            "selected_session_count": assessment.selected_session_count,
            "calendar_day_count": assessment.calendar_day_count,
        },
        "source_binding": {
            "research_certification_status": (assessment.research_certification_status),
            "research_certification_manifest_sha256": (
                assessment.research_certification_manifest_sha256
            ),
            "research_slice_spec_sha256": assessment.research_slice_spec_sha256,
            "research_data_manifest_sha256": (assessment.research_data_manifest_sha256),
            "calendar_coverage_spec_sha256": (assessment.calendar_coverage_spec_sha256),
            "calendar_data_manifest_sha256": (assessment.calendar_data_manifest_sha256),
            "cross_source_shared_calendar_lineage_sha256": (
                assessment.cross_source_shared_calendar_lineage_sha256
            ),
        },
        "local_data_quality": {
            "policy_version": assessment.dq_policy_version,
            "policy_sha256": assessment.dq_policy_sha256,
            "check_ids": list(assessment.dq_check_ids),
            "check_count": assessment.dq_check_count,
            "results_sha256": assessment.dq_results_sha256,
            "checks_passed": assessment.local_retained_dq_checks_passed,
        },
        "corporate_action": {
            "status": assessment.corporate_action_status,
            "evidence_sha256": assessment.corporate_action_evidence_sha256,
            "coverage_verified": assessment.corporate_action_coverage_verified,
            "adjustment_semantics_verified": (
                assessment.corporate_action_adjustment_semantics_verified
            ),
        },
        "certification": {
            "full_data_quality_certified": assessment.full_data_quality_certified,
            "dataset_registration_allowed": assessment.dataset_registration_allowed,
            "feature_use_allowed": assessment.feature_use_allowed,
            "backtest_use_allowed": assessment.backtest_use_allowed,
            "strategy_promotion_allowed": assessment.strategy_promotion_allowed,
            "order_use_allowed": assessment.order_use_allowed,
        },
        "rejection_reasons": list(assessment.rejection_reasons),
        "limitations": list(assessment.limitations),
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
        raise DailyCandleCorporateActionDqAssessmentError(
            "daily_candle_corporate_action_dq_assessment_manifest_invalid"
        ) from None
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_timestamp(value: datetime) -> str:
    try:
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    except (OverflowError, RuntimeError, TypeError, ValueError):
        raise DailyCandleCorporateActionDqAssessmentError(
            "daily_candle_corporate_action_dq_assessment_manifest_invalid"
        ) from None
