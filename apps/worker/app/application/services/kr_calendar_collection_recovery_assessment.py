from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from datetime import date
from typing import Literal

from app.application.ports.kr_calendar_collection_job_store_port import (
    KrCalendarCollectionJobInspectorPort,
    KrCalendarCollectionJobSnapshotV1,
    KrCalendarCollectionJobSpecV1,
    KrCalendarCollectionJobState,
    canonical_kr_calendar_collection_job_snapshot,
    canonical_kr_calendar_collection_job_spec,
)
from app.domain.common.errors import KnownFailClosedError

KR_CALENDAR_COLLECTION_RECOVERY_ASSESSMENT_SCHEMA_VERSION = (
    "kr_calendar_collection_recovery_assessment.v1"
)
KR_CALENDAR_COLLECTION_RETRYABLE_STATE_REASON = "collection_failed_before_write"
KR_CALENDAR_COLLECTION_RECOVERY_ASSESSMENT_LIMITATIONS = (
    "read_only_snapshot_assessment_only",
    "recommended_action_requires_separate_operator_review",
    "mutation_not_authorized",
    "retry_not_authorized",
    "manual_recovery_not_authorized",
    "worker_loop_and_scheduler_not_connected",
    "full_calendar_not_certified",
    "calendar_dataset_research_feature_backtest_strategy_order_use_not_authorized",
    "production_live_use_not_authorized",
)

KrCalendarCollectionRecoveryClassification = Literal[
    "missing",
    "ready",
    "paused_retryable",
    "paused_unrecognized",
    "collecting",
    "blocked_unknown",
    "completed",
]
KrCalendarCollectionRecommendedOperatorAction = Literal[
    "create_job_by_explicit_manual_invocation",
    "advance_next_date_by_explicit_manual_invocation",
    "review_pre_write_failure_before_explicit_manual_invocation",
    "investigate_unrecognized_pause_without_retry",
    "investigate_in_flight_attempt_without_retry",
    "reconcile_unknown_write_outcome_without_retry",
    "no_action_completed",
]


class KrCalendarCollectionRecoveryAssessmentError(KnownFailClosedError):
    def __init__(self, safe_message: str) -> None:
        super().__init__("kr_calendar_collection_recovery_assessment", safe_message)


@dataclass(frozen=True, slots=True, init=False)
class KrCalendarCollectionRecoveryAssessmentV1:
    schema_version: str
    job_id: str
    spec_sha256: str
    classification: KrCalendarCollectionRecoveryClassification
    job_state: KrCalendarCollectionJobState | None
    state_reason: str | None
    job_revision: int | None
    confirmed_date_count: int
    remaining_date_count: int
    next_date: date | None
    unresolved_attempt_present: bool
    recommended_operator_action: KrCalendarCollectionRecommendedOperatorAction
    explicit_manual_invocation_candidate: bool
    operator_review_required: bool
    unresolved_write_outcome: bool
    read_only: bool
    automatic_retry_allowed: bool
    mutation_performed: bool
    mutation_authorized: bool
    retry_authorized: bool
    manual_execution_authorized: bool
    manual_recovery_authorized: bool
    production_live_authorized: bool
    full_calendar_certified: bool
    limitations: tuple[str, ...]

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise KrCalendarCollectionRecoveryAssessmentError(
            "kr_calendar_collection_recovery_assessment_requires_service"
        )


class KrCalendarCollectionRecoveryAssessmentService:
    def __init__(self, inspector: KrCalendarCollectionJobInspectorPort) -> None:
        self.inspector = inspector

    async def assess(
        self,
        spec: KrCalendarCollectionJobSpecV1,
        *,
        expected_spec_sha256: str,
    ) -> KrCalendarCollectionRecoveryAssessmentV1:
        canonical_spec = _spec(spec)
        if (
            type(expected_spec_sha256) is not str
            or expected_spec_sha256 != canonical_spec.spec_sha256
        ):
            raise KrCalendarCollectionRecoveryAssessmentError(
                "kr_calendar_collection_recovery_assessment_spec_sha256_mismatch"
            )

        inspected = False
        source_snapshot: object = None
        with suppress(Exception):
            source_snapshot = await self.inspector.inspect_job(canonical_spec.job_id)
            inspected = True
        if not inspected:
            raise KrCalendarCollectionRecoveryAssessmentError(
                "kr_calendar_collection_recovery_assessment_inspection_failed"
            )

        if source_snapshot is None:
            return _assessment(canonical_spec, snapshot=None)

        snapshot = _snapshot(source_snapshot)
        if snapshot.spec != canonical_spec:
            raise KrCalendarCollectionRecoveryAssessmentError(
                "kr_calendar_collection_recovery_assessment_spec_mismatch"
            )
        if snapshot.spec.spec_sha256 != expected_spec_sha256:
            raise KrCalendarCollectionRecoveryAssessmentError(
                "kr_calendar_collection_recovery_assessment_spec_sha256_mismatch"
            )
        return _assessment(canonical_spec, snapshot=snapshot)


def _assessment(
    spec: KrCalendarCollectionJobSpecV1,
    *,
    snapshot: KrCalendarCollectionJobSnapshotV1 | None,
) -> KrCalendarCollectionRecoveryAssessmentV1:
    classification: KrCalendarCollectionRecoveryClassification
    if snapshot is None:
        classification = "missing"
    elif (
        snapshot.state == "paused_retryable"
        and snapshot.state_reason != KR_CALENDAR_COLLECTION_RETRYABLE_STATE_REASON
    ):
        classification = "paused_unrecognized"
    else:
        classification = snapshot.state
    action, explicit_candidate, review_required, unresolved_write_outcome = (
        _classification_policy(classification)
    )
    result = object.__new__(KrCalendarCollectionRecoveryAssessmentV1)
    values: dict[str, object] = {
        "schema_version": KR_CALENDAR_COLLECTION_RECOVERY_ASSESSMENT_SCHEMA_VERSION,
        "job_id": spec.job_id,
        "spec_sha256": spec.spec_sha256,
        "classification": classification,
        "job_state": None if snapshot is None else snapshot.state,
        "state_reason": None if snapshot is None else snapshot.state_reason,
        "job_revision": None if snapshot is None else snapshot.revision,
        "confirmed_date_count": 0 if snapshot is None else snapshot.confirmed_count,
        "remaining_date_count": spec.total_days if snapshot is None else snapshot.remaining_count,
        "next_date": spec.start_date if snapshot is None else snapshot.next_date,
        "unresolved_attempt_present": (
            False if snapshot is None else snapshot.active_attempt is not None
        ),
        "recommended_operator_action": action,
        "explicit_manual_invocation_candidate": explicit_candidate,
        "operator_review_required": review_required,
        "unresolved_write_outcome": unresolved_write_outcome,
        "read_only": True,
        "automatic_retry_allowed": False,
        "mutation_performed": False,
        "mutation_authorized": False,
        "retry_authorized": False,
        "manual_execution_authorized": False,
        "manual_recovery_authorized": False,
        "production_live_authorized": False,
        "full_calendar_certified": False,
        "limitations": KR_CALENDAR_COLLECTION_RECOVERY_ASSESSMENT_LIMITATIONS,
    }
    for field_name, value in values.items():
        object.__setattr__(result, field_name, value)
    return result


def _classification_policy(
    classification: KrCalendarCollectionRecoveryClassification,
) -> tuple[KrCalendarCollectionRecommendedOperatorAction, bool, bool, bool]:
    policies: dict[
        KrCalendarCollectionRecoveryClassification,
        tuple[KrCalendarCollectionRecommendedOperatorAction, bool, bool, bool],
    ] = {
        "missing": (
            "create_job_by_explicit_manual_invocation",
            True,
            True,
            False,
        ),
        "ready": (
            "advance_next_date_by_explicit_manual_invocation",
            True,
            True,
            False,
        ),
        "paused_retryable": (
            "review_pre_write_failure_before_explicit_manual_invocation",
            True,
            True,
            False,
        ),
        "paused_unrecognized": (
            "investigate_unrecognized_pause_without_retry",
            False,
            True,
            True,
        ),
        "collecting": (
            "investigate_in_flight_attempt_without_retry",
            False,
            True,
            True,
        ),
        "blocked_unknown": (
            "reconcile_unknown_write_outcome_without_retry",
            False,
            True,
            True,
        ),
        "completed": ("no_action_completed", False, False, False),
    }
    return policies[classification]


def _spec(value: object) -> KrCalendarCollectionJobSpecV1:
    canonical: KrCalendarCollectionJobSpecV1 | None = None
    with suppress(Exception):
        canonical = canonical_kr_calendar_collection_job_spec(value)
    if canonical is None:
        raise KrCalendarCollectionRecoveryAssessmentError(
            "kr_calendar_collection_recovery_assessment_spec_invalid"
        )
    return canonical


def _snapshot(value: object) -> KrCalendarCollectionJobSnapshotV1:
    canonical: KrCalendarCollectionJobSnapshotV1 | None = None
    with suppress(Exception):
        canonical = canonical_kr_calendar_collection_job_snapshot(value)
    if canonical is None:
        raise KrCalendarCollectionRecoveryAssessmentError(
            "kr_calendar_collection_recovery_assessment_snapshot_invalid"
        )
    return canonical
