from __future__ import annotations

from contextlib import suppress
from datetime import date
from typing import Literal

from app.application.ports.kr_calendar_collection_job_store_port import (
    KrCalendarCollectionJobSpecV1,
    canonical_kr_calendar_collection_job_spec,
)
from app.application.services.kr_calendar_collection_recovery_assessment import (
    KR_CALENDAR_COLLECTION_RETRYABLE_STATE_REASON,
    KrCalendarCollectionRecoveryAssessmentService,
    KrCalendarCollectionRecoveryAssessmentV1,
)
from app.application.use_cases.run_kr_calendar_date_range_collection_job import (
    KrCalendarDateRangeCollectionJobError,
    KrCalendarDateRangeCollectionRunResultV1,
    RunKrCalendarDateRangeCollectionJob,
)
from app.domain.common.errors import KnownFailClosedError

GuardedKrCalendarExpectedClassification = Literal[
    "missing",
    "ready",
    "paused_retryable",
]


class GuardedKrCalendarCollectionJobOnceError(KnownFailClosedError):
    def __init__(self, safe_message: str) -> None:
        super().__init__("guarded_kr_calendar_collection_job_once", safe_message)


class RunGuardedKrCalendarCollectionJobOnce:
    """Run one durable calendar date after explicit operator-reviewed recovery."""

    def __init__(
        self,
        recovery_assessment_service: KrCalendarCollectionRecoveryAssessmentService,
        runner: RunKrCalendarDateRangeCollectionJob,
        *,
        manual_execution_enabled: bool = False,
    ) -> None:
        self.recovery_assessment_service = recovery_assessment_service
        self.runner = runner
        self.manual_execution_enabled = manual_execution_enabled

    async def execute(
        self,
        spec: KrCalendarCollectionJobSpecV1,
        *,
        expected_spec_sha256: str,
        expected_classification: GuardedKrCalendarExpectedClassification,
        expected_revision: int | None,
        expected_confirmed_count: int,
        expected_next_date: date,
        expected_state_reason: str | None,
        manual_confirmation: bool = False,
        paused_retry_confirmation: bool = False,
    ) -> KrCalendarDateRangeCollectionRunResultV1:
        if self.manual_execution_enabled is not True:
            raise GuardedKrCalendarCollectionJobOnceError(
                "guarded_kr_calendar_collection_job_manual_execution_disabled"
            )
        if manual_confirmation is not True:
            raise GuardedKrCalendarCollectionJobOnceError(
                "guarded_kr_calendar_collection_job_manual_confirmation_required"
            )
        if (
            self.runner.persistence_kind != "durable"
            or self.runner.manual_execution_enabled is not True
        ):
            raise GuardedKrCalendarCollectionJobOnceError(
                "guarded_kr_calendar_collection_job_durable_runner_required"
            )
        if id(self.recovery_assessment_service.inspector) != id(self.runner.job_store):
            raise GuardedKrCalendarCollectionJobOnceError(
                "guarded_kr_calendar_collection_job_dependency_binding_invalid"
            )

        canonical_spec = _spec(spec)
        _expected_precondition(
            canonical_spec,
            expected_spec_sha256=expected_spec_sha256,
            expected_classification=expected_classification,
            expected_revision=expected_revision,
            expected_confirmed_count=expected_confirmed_count,
            expected_next_date=expected_next_date,
            expected_state_reason=expected_state_reason,
            paused_retry_confirmation=paused_retry_confirmation,
        )
        assessment = await self._assess(
            canonical_spec,
            expected_spec_sha256=expected_spec_sha256,
        )
        if assessment.explicit_manual_invocation_candidate is not True:
            raise GuardedKrCalendarCollectionJobOnceError(
                "guarded_kr_calendar_collection_job_state_not_executable"
            )
        if not _matches_expected_precondition(
            assessment,
            expected_spec_sha256=expected_spec_sha256,
            expected_classification=expected_classification,
            expected_revision=expected_revision,
            expected_confirmed_count=expected_confirmed_count,
            expected_next_date=expected_next_date,
            expected_state_reason=expected_state_reason,
        ):
            raise GuardedKrCalendarCollectionJobOnceError(
                "guarded_kr_calendar_collection_job_assessment_mismatch"
            )
        try:
            return await self.runner.execute_assessed(
                canonical_spec,
                assessment,
                paused_retry_confirmation=paused_retry_confirmation,
            )
        except KrCalendarDateRangeCollectionJobError:
            raise
        except Exception:
            raise GuardedKrCalendarCollectionJobOnceError(
                "guarded_kr_calendar_collection_job_execution_failed"
            ) from None

    async def _assess(
        self,
        spec: KrCalendarCollectionJobSpecV1,
        *,
        expected_spec_sha256: str,
    ) -> KrCalendarCollectionRecoveryAssessmentV1:
        assessment: object = None
        assessed = False
        try:
            assessment = await self.recovery_assessment_service.assess(
                spec,
                expected_spec_sha256=expected_spec_sha256,
            )
            assessed = True
        except Exception:
            pass
        if not assessed or type(assessment) is not KrCalendarCollectionRecoveryAssessmentV1:
            raise GuardedKrCalendarCollectionJobOnceError(
                "guarded_kr_calendar_collection_job_assessment_failed"
            )
        return assessment


def _expected_precondition(
    spec: KrCalendarCollectionJobSpecV1,
    *,
    expected_spec_sha256: object,
    expected_classification: object,
    expected_revision: object,
    expected_confirmed_count: object,
    expected_next_date: object,
    expected_state_reason: object,
    paused_retry_confirmation: object,
) -> None:
    confirmed_count = (
        expected_confirmed_count
        if type(expected_confirmed_count) is int
        else -1
    )
    valid = (
        type(expected_spec_sha256) is str
        and expected_spec_sha256 == spec.spec_sha256
        and type(expected_classification) is str
        and expected_classification in {"missing", "ready", "paused_retryable"}
        and 0 <= confirmed_count < spec.total_days
        and type(expected_next_date) is date
    )
    # Calculate independently after exact type checks so subclasses cannot
    # supply overloaded date arithmetic.
    if valid:
        expected_date = date.fromordinal(
            spec.start_date.toordinal() + confirmed_count
        )
        valid = expected_next_date == expected_date
    if expected_classification == "missing":
        valid = (
            valid
            and expected_revision is None
            and confirmed_count == 0
            and expected_next_date == spec.start_date
        )
    else:
        valid = valid and type(expected_revision) is int and expected_revision > 0
    if expected_classification == "paused_retryable":
        valid = (
            valid
            and expected_state_reason == KR_CALENDAR_COLLECTION_RETRYABLE_STATE_REASON
            and paused_retry_confirmation is True
        )
    else:
        valid = (
            valid
            and expected_state_reason is None
            and paused_retry_confirmation is False
        )
    if not valid:
        raise GuardedKrCalendarCollectionJobOnceError(
            "guarded_kr_calendar_collection_job_expected_precondition_invalid"
        )


def _matches_expected_precondition(
    assessment: KrCalendarCollectionRecoveryAssessmentV1,
    *,
    expected_spec_sha256: str,
    expected_classification: GuardedKrCalendarExpectedClassification,
    expected_revision: int | None,
    expected_confirmed_count: int,
    expected_next_date: date,
    expected_state_reason: str | None,
) -> bool:
    matches = False
    with suppress(Exception):
        matches = (
            assessment.spec_sha256 == expected_spec_sha256
            and assessment.classification == expected_classification
            and assessment.job_revision == expected_revision
            and assessment.confirmed_date_count == expected_confirmed_count
            and assessment.next_date == expected_next_date
            and assessment.state_reason == expected_state_reason
        )
    return matches


def _spec(value: object) -> KrCalendarCollectionJobSpecV1:
    canonical: KrCalendarCollectionJobSpecV1 | None = None
    with suppress(Exception):
        canonical = canonical_kr_calendar_collection_job_spec(value)
    if canonical is None:
        raise GuardedKrCalendarCollectionJobOnceError(
            "guarded_kr_calendar_collection_job_spec_invalid"
        )
    return canonical
