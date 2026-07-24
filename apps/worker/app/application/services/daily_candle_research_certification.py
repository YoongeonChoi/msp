from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Never

from app.application.ports.daily_candle_as_of_reader_port import (
    DailyCandleAsOfReadRequest,
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
from app.domain.common.json import JsonObject, JsonValue

PIT_DAILY_CANDLE_RESEARCH_CERTIFICATION_SCHEMA_VERSION = (
    "pit_daily_candle_research_certification_assessment.v1"
)
PIT_DAILY_CANDLE_RESEARCH_CERTIFICATION_STATUS = "blocked_missing_external_evidence"
PIT_DAILY_CANDLE_RESEARCH_CERTIFICATION_REJECTION_REASONS = (
    "official_exchange_calendar_completeness_evidence_missing",
    "provider_history_completeness_evidence_missing",
    "provider_authenticity_evidence_missing",
    "provider_finality_evidence_missing",
)
PIT_DAILY_CANDLE_RESEARCH_CERTIFICATION_LIMITATIONS = (
    "local_retained_evidence_only",
    "historical_database_visibility_not_reconstructed",
    "corporate_actions_not_evaluated",
    "full_data_quality_not_certified",
    "dataset_feature_backtest_strategy_order_use_not_authorized",
    "production_live_use_not_authorized",
    "calendar_revision_observed_clock_not_available_in_research_slice",
)

_CROSS_SOURCE_SHARED_CALENDAR_LINEAGE_SCHEMA_VERSION = (
    "pit_daily_candle_research_cross_source_shared_calendar_lineage.v1"
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class DailyCandleResearchCertificationError(KnownFailClosedError):
    def __init__(self, safe_message: str) -> None:
        super().__init__("daily_candle_research_certification", safe_message)


@dataclass(frozen=True, slots=True, init=False)
class DailyCandleResearchCertificationManifestV1:
    schema_version: str
    certification_status: str
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
    candle_provider_contract_sha256: str
    calendar_provider_contract_sha256: str
    research_slice_spec_sha256: str
    research_data_manifest_sha256: str
    calendar_coverage_spec_sha256: str
    calendar_data_manifest_sha256: str
    cross_source_shared_calendar_lineage_sha256: str
    retained_open_session_chain_validated: bool
    retained_calendar_date_coverage_complete: bool
    cross_source_shared_calendar_lineage_bound: bool
    official_exchange_calendar_completeness_evidence_sha256: str | None
    provider_history_completeness_evidence_sha256: str | None
    provider_authenticity_evidence_sha256: str | None
    provider_finality_evidence_sha256: str | None
    official_exchange_calendar_completeness_certified: bool
    provider_history_completeness_certified: bool
    provider_authenticity_certified: bool
    provider_finality_certified: bool
    full_research_certified: bool
    promotion_allowed: bool
    rejection_reasons: tuple[str, ...]
    limitations: tuple[str, ...]
    certification_manifest_sha256: str

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise DailyCandleResearchCertificationError(
            "daily_candle_research_certification_requires_gate"
        )

    def to_payload(self) -> JsonObject:
        assessment = _validate_certification_manifest(self)
        payload = _certification_manifest_body(assessment)
        payload["certification_manifest_sha256"] = assessment.certification_manifest_sha256
        return payload


def build_daily_candle_research_certification_assessment(
    research_slice: object,
    calendar_coverage: object,
) -> DailyCandleResearchCertificationManifestV1:
    """Bind local retained evidence without claiming external certification."""

    try:
        valid_research = validate_contiguous_daily_candle_research_slice(research_slice)
    except Exception:
        pass
    else:
        try:
            valid_calendar = validate_retained_kr_calendar_date_range_coverage(calendar_coverage)
        except Exception:
            pass
        else:
            return _build_certification_assessment(
                valid_research,
                valid_calendar,
            )
        raise DailyCandleResearchCertificationError(
            "daily_candle_research_certification_calendar_coverage_invalid"
        )
    raise DailyCandleResearchCertificationError(
        "daily_candle_research_certification_research_slice_invalid"
    )


def _build_certification_assessment(
    valid_research: ContiguousDailyCandleResearchSliceV1,
    valid_calendar: RetainedKrCalendarDateRangeCoverageV1,
) -> DailyCandleResearchCertificationManifestV1:
    cross_source_rows = _cross_bind_sources(valid_research, valid_calendar)
    cross_source_sha256 = _payload_sha256(
        {
            "schema_version": _CROSS_SOURCE_SHARED_CALENDAR_LINEAGE_SCHEMA_VERSION,
            "items": cross_source_rows,
        }
    )

    result = object.__new__(DailyCandleResearchCertificationManifestV1)
    values: dict[str, object] = {
        "schema_version": PIT_DAILY_CANDLE_RESEARCH_CERTIFICATION_SCHEMA_VERSION,
        "certification_status": PIT_DAILY_CANDLE_RESEARCH_CERTIFICATION_STATUS,
        "provider": valid_research.provider,
        "market": valid_research.market,
        "symbol": valid_research.symbol,
        "interval": valid_research.interval,
        "adjusted": valid_research.adjusted,
        "start_session_date": valid_research.start_session_date,
        "end_session_date": valid_research.end_session_date,
        "selected_as_of": _utc(valid_research.selected_as_of),
        "selected_session_count": valid_research.selected_session_count,
        "calendar_day_count": valid_calendar.calendar_day_count,
        "candle_provider_contract_sha256": (valid_research.candle_provider_contract_sha256),
        "calendar_provider_contract_sha256": (valid_research.calendar_provider_contract_sha256),
        "research_slice_spec_sha256": valid_research.slice_spec_sha256,
        "research_data_manifest_sha256": valid_research.data_manifest_sha256,
        "calendar_coverage_spec_sha256": valid_calendar.coverage_spec_sha256,
        "calendar_data_manifest_sha256": valid_calendar.data_manifest_sha256,
        "cross_source_shared_calendar_lineage_sha256": cross_source_sha256,
        "retained_open_session_chain_validated": True,
        "retained_calendar_date_coverage_complete": True,
        "cross_source_shared_calendar_lineage_bound": True,
        "official_exchange_calendar_completeness_evidence_sha256": None,
        "provider_history_completeness_evidence_sha256": None,
        "provider_authenticity_evidence_sha256": None,
        "provider_finality_evidence_sha256": None,
        "official_exchange_calendar_completeness_certified": False,
        "provider_history_completeness_certified": False,
        "provider_authenticity_certified": False,
        "provider_finality_certified": False,
        "full_research_certified": False,
        "promotion_allowed": False,
        "rejection_reasons": (PIT_DAILY_CANDLE_RESEARCH_CERTIFICATION_REJECTION_REASONS),
        "limitations": PIT_DAILY_CANDLE_RESEARCH_CERTIFICATION_LIMITATIONS,
    }
    for field_name, value in values.items():
        object.__setattr__(result, field_name, value)
    object.__setattr__(
        result,
        "certification_manifest_sha256",
        _payload_sha256(_certification_manifest_body(result)),
    )
    return _validate_certification_manifest(result)


def require_full_research_certification(assessment: object) -> Never:
    """Reject V1 local assessments before any feature or research promotion."""

    _validate_certification_manifest(assessment)
    raise DailyCandleResearchCertificationError("daily_candle_research_certification_not_certified")


def _cross_bind_sources(
    research: ContiguousDailyCandleResearchSliceV1,
    calendar: RetainedKrCalendarDateRangeCoverageV1,
) -> list[JsonValue]:
    if (
        research.provider != calendar.provider
        or research.market != calendar.market
        or research.start_session_date != calendar.start_session_date
        or research.end_session_date != calendar.end_session_date
        or research.selected_as_of != calendar.selected_as_of
    ):
        raise DailyCandleResearchCertificationError(
            "daily_candle_research_certification_source_scope_mismatch"
        )
    if research.calendar_provider_contract_sha256 != calendar.selected_provider_contract_sha256:
        raise DailyCandleResearchCertificationError(
            "daily_candle_research_certification_source_contract_mismatch"
        )

    open_calendar_items = tuple(item for item in calendar.items if item.selection.session.is_open)
    if (
        len(open_calendar_items) != research.selected_session_count
        or len(open_calendar_items) != calendar.open_session_count
        or len(open_calendar_items) != len(research.items)
    ):
        raise DailyCandleResearchCertificationError(
            "daily_candle_research_certification_open_session_set_mismatch"
        )

    rows: list[JsonValue] = []
    for research_item, calendar_item in zip(
        research.items,
        open_calendar_items,
        strict=True,
    ):
        research_calendar = research_item.calendar
        selected_calendar = calendar_item.selection.session
        if research_calendar != selected_calendar:
            raise DailyCandleResearchCertificationError(
                "daily_candle_research_certification_calendar_evidence_mismatch"
            )

        research_lineage = research_item.lineage
        calendar_lineage = calendar_item.lineage
        if (
            research_lineage.calendar_revision_id != calendar_lineage.calendar_revision_id
            or research_lineage.calendar_revision != calendar_lineage.calendar_revision
            or research_lineage.calendar_canonical_evidence_sha256
            != calendar_lineage.calendar_canonical_evidence_sha256
            or research_lineage.calendar_revision_received_at
            != calendar_lineage.calendar_revision_received_at
            or research_lineage.calendar_occurrence_id != calendar_lineage.calendar_occurrence_id
            or research_lineage.calendar_occurrence_received_at
            != calendar_lineage.calendar_occurrence_received_at
            or research_lineage.calendar_occurrence_origin
            != calendar_lineage.calendar_occurrence_origin
            or research_calendar.idempotency_key != calendar_lineage.calendar_idempotency_key
        ):
            raise DailyCandleResearchCertificationError(
                "daily_candle_research_certification_calendar_lineage_mismatch"
            )

        rows.append(
            {
                "session_date": research_calendar.session_date.isoformat(),
                "calendar_idempotency_key": research_calendar.idempotency_key,
                "calendar_canonical_evidence_sha256": (
                    research_lineage.calendar_canonical_evidence_sha256
                ),
                "calendar_revision_id": research_lineage.calendar_revision_id,
                "calendar_revision": research_lineage.calendar_revision,
                "calendar_revision_received_at": _canonical_timestamp(
                    research_lineage.calendar_revision_received_at
                ),
                "calendar_occurrence_id": research_lineage.calendar_occurrence_id,
                "calendar_occurrence_received_at": _canonical_timestamp(
                    research_lineage.calendar_occurrence_received_at
                ),
                "calendar_occurrence_origin": (research_lineage.calendar_occurrence_origin),
            }
        )
    return rows


def _validate_certification_manifest(
    value: object,
) -> DailyCandleResearchCertificationManifestV1:
    if type(value) is not DailyCandleResearchCertificationManifestV1:
        raise DailyCandleResearchCertificationError(
            "daily_candle_research_certification_manifest_invalid"
        )
    assessment = value
    try:
        request = DailyCandleAsOfReadRequest(
            provider=assessment.provider,
            market=assessment.market,
            symbol=assessment.symbol,
            interval=assessment.interval,
            adjusted=assessment.adjusted,
            start_session_date=assessment.start_session_date,
            end_session_date=assessment.end_session_date,
            as_of=assessment.selected_as_of,
            page_size=25,
        )
        calendar_day_count = (request.end_session_date - request.start_session_date).days + 1
        for sha256_value in (
            assessment.candle_provider_contract_sha256,
            assessment.calendar_provider_contract_sha256,
            assessment.research_slice_spec_sha256,
            assessment.research_data_manifest_sha256,
            assessment.calendar_coverage_spec_sha256,
            assessment.calendar_data_manifest_sha256,
            assessment.cross_source_shared_calendar_lineage_sha256,
            assessment.certification_manifest_sha256,
        ):
            _require_sha256(sha256_value)
        expected_manifest_sha256 = _payload_sha256(_certification_manifest_body(assessment))
        valid = (
            type(assessment.schema_version) is str
            and type(assessment.certification_status) is str
            and type(assessment.selected_session_count) is int
            and type(assessment.calendar_day_count) is int
            and type(assessment.rejection_reasons) is tuple
            and type(assessment.limitations) is tuple
            and assessment.schema_version == PIT_DAILY_CANDLE_RESEARCH_CERTIFICATION_SCHEMA_VERSION
            and assessment.certification_status == PIT_DAILY_CANDLE_RESEARCH_CERTIFICATION_STATUS
            and assessment.selected_as_of.tzinfo is UTC
            and assessment.selected_session_count > 0
            and assessment.selected_session_count <= calendar_day_count
            and assessment.calendar_day_count == calendar_day_count
            and assessment.retained_open_session_chain_validated is True
            and assessment.retained_calendar_date_coverage_complete is True
            and assessment.cross_source_shared_calendar_lineage_bound is True
            and assessment.official_exchange_calendar_completeness_evidence_sha256 is None
            and assessment.provider_history_completeness_evidence_sha256 is None
            and assessment.provider_authenticity_evidence_sha256 is None
            and assessment.provider_finality_evidence_sha256 is None
            and assessment.official_exchange_calendar_completeness_certified is False
            and assessment.provider_history_completeness_certified is False
            and assessment.provider_authenticity_certified is False
            and assessment.provider_finality_certified is False
            and assessment.full_research_certified is False
            and assessment.promotion_allowed is False
            and assessment.rejection_reasons
            == PIT_DAILY_CANDLE_RESEARCH_CERTIFICATION_REJECTION_REASONS
            and assessment.limitations == PIT_DAILY_CANDLE_RESEARCH_CERTIFICATION_LIMITATIONS
            and assessment.certification_manifest_sha256 == expected_manifest_sha256
        )
    except Exception:
        pass
    else:
        if valid:
            return assessment
    raise DailyCandleResearchCertificationError(
        "daily_candle_research_certification_manifest_invalid"
    ) from None


def _certification_manifest_body(
    assessment: DailyCandleResearchCertificationManifestV1,
) -> JsonObject:
    return {
        "schema_version": assessment.schema_version,
        "certification_status": assessment.certification_status,
        "scope": {
            "provider": assessment.provider,
            "market": assessment.market,
            "symbol": assessment.symbol,
            "interval": assessment.interval,
            "adjusted": assessment.adjusted,
            "first_session_date": assessment.start_session_date.isoformat(),
            "last_session_date": assessment.end_session_date.isoformat(),
            "as_of": _canonical_timestamp(assessment.selected_as_of),
        },
        "local_evidence": {
            "research_slice_spec_sha256": assessment.research_slice_spec_sha256,
            "research_data_manifest_sha256": (assessment.research_data_manifest_sha256),
            "calendar_coverage_spec_sha256": (assessment.calendar_coverage_spec_sha256),
            "calendar_data_manifest_sha256": (assessment.calendar_data_manifest_sha256),
            "cross_source_shared_calendar_lineage_sha256": (
                assessment.cross_source_shared_calendar_lineage_sha256
            ),
            "candle_provider_contract_sha256": (assessment.candle_provider_contract_sha256),
            "calendar_provider_contract_sha256": (assessment.calendar_provider_contract_sha256),
            "selected_session_count": assessment.selected_session_count,
            "calendar_day_count": assessment.calendar_day_count,
            "retained_open_session_chain_validated": (
                assessment.retained_open_session_chain_validated
            ),
            "retained_calendar_date_coverage_complete": (
                assessment.retained_calendar_date_coverage_complete
            ),
            "cross_source_shared_calendar_lineage_bound": (
                assessment.cross_source_shared_calendar_lineage_bound
            ),
        },
        "external_evidence": {
            "official_exchange_calendar_completeness_sha256": (
                assessment.official_exchange_calendar_completeness_evidence_sha256
            ),
            "provider_history_completeness_sha256": (
                assessment.provider_history_completeness_evidence_sha256
            ),
            "provider_authenticity_sha256": (assessment.provider_authenticity_evidence_sha256),
            "provider_finality_sha256": (assessment.provider_finality_evidence_sha256),
        },
        "certification": {
            "official_exchange_calendar_completeness_certified": (
                assessment.official_exchange_calendar_completeness_certified
            ),
            "provider_history_completeness_certified": (
                assessment.provider_history_completeness_certified
            ),
            "provider_authenticity_certified": (assessment.provider_authenticity_certified),
            "provider_finality_certified": assessment.provider_finality_certified,
            "full_research_certified": assessment.full_research_certified,
            "promotion_allowed": assessment.promotion_allowed,
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
        raise DailyCandleResearchCertificationError(
            "daily_candle_research_certification_manifest_invalid"
        ) from None
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _require_sha256(value: object) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise DailyCandleResearchCertificationError(
            "daily_candle_research_certification_manifest_invalid"
        )
    return value


def _canonical_timestamp(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _utc(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None:
        raise DailyCandleResearchCertificationError(
            "daily_candle_research_certification_manifest_invalid"
        )
    try:
        if value.utcoffset() is None:
            raise DailyCandleResearchCertificationError(
                "daily_candle_research_certification_manifest_invalid"
            )
        return value.astimezone(UTC)
    except (OverflowError, RuntimeError, TypeError, ValueError):
        raise DailyCandleResearchCertificationError(
            "daily_candle_research_certification_manifest_invalid"
        ) from None
