from __future__ import annotations

import asyncio
import traceback
from datetime import UTC, date, datetime, timedelta
from typing import TypedDict, cast
from uuid import UUID

import pytest

from app.adapters.persistence.in_memory_kr_calendar_collection_job_store import (
    InMemoryKrCalendarCollectionJobStore,
)
from app.application.ports.calendar_observation_store_port import (
    CalendarObservationWriteReceipt,
)
from app.application.ports.kr_calendar_collection_job_store_port import (
    KrCalendarCollectionJobSnapshotV1,
    KrCalendarCollectionJobSpecV1,
)
from app.application.ports.kr_daily_session_collection_port import (
    CollectedKrDailySessionObservationV1,
)
from app.application.services.kr_calendar_collection_recovery_assessment import (
    KR_CALENDAR_COLLECTION_RECOVERY_ASSESSMENT_LIMITATIONS,
    KR_CALENDAR_COLLECTION_RECOVERY_ASSESSMENT_SCHEMA_VERSION,
    KR_CALENDAR_COLLECTION_RETRYABLE_STATE_REASON,
    KrCalendarCollectionRecommendedOperatorAction,
    KrCalendarCollectionRecoveryAssessmentError,
    KrCalendarCollectionRecoveryAssessmentService,
    KrCalendarCollectionRecoveryAssessmentV1,
    KrCalendarCollectionRecoveryClassification,
)
from app.domain.common.time import KST
from app.domain.market_data.point_in_time_calendar import (
    PointInTimeKrDailySessionV1,
)

JOB_ID = "00000000-0000-4000-8000-000000000301"
HOLDER_ID = "00000000-0000-4000-8000-000000000302"
ATTEMPT_ID = "00000000-0000-4000-8000-000000000303"
OCCURRENCE_ID = UUID("00000000-0000-4000-8000-000000000304")
START_DATE = date(2026, 7, 20)
CREATED_AT = datetime(2026, 7, 19, 22, 0, tzinfo=UTC)


class FakeInspector:
    def __init__(
        self,
        result: object,
        *,
        error: BaseException | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls: list[str] = []

    async def inspect_job(self, job_id: str) -> KrCalendarCollectionJobSnapshotV1 | None:
        self.calls.append(job_id)
        if self.error is not None:
            raise self.error
        return cast(KrCalendarCollectionJobSnapshotV1 | None, self.result)


class ActiveAttemptArgs(TypedDict):
    job_id: str
    spec_sha256: str
    expected_revision: int
    attempt_id: str
    holder_id: str
    target_date: date


@pytest.mark.parametrize(
    "classification",
    [
        "missing",
        "ready",
        "paused_retryable",
        "collecting",
        "blocked_unknown",
        "completed",
    ],
)
async def test_assessment_classifies_one_snapshot_without_authorizing_action(
    classification: KrCalendarCollectionRecoveryClassification,
) -> None:
    spec = _spec()
    snapshot = await _snapshot_for(classification, spec=spec)
    inspector = FakeInspector(snapshot)

    result = await KrCalendarCollectionRecoveryAssessmentService(inspector).assess(
        spec,
        expected_spec_sha256=spec.spec_sha256,
    )

    assert inspector.calls == [JOB_ID]
    assert result.schema_version == KR_CALENDAR_COLLECTION_RECOVERY_ASSESSMENT_SCHEMA_VERSION
    assert result.job_id == JOB_ID
    assert result.spec_sha256 == spec.spec_sha256
    assert result.classification == classification
    assert result.job_state == (None if snapshot is None else snapshot.state)
    assert result.state_reason == (None if snapshot is None else snapshot.state_reason)
    assert result.job_revision == (None if snapshot is None else snapshot.revision)
    assert result.confirmed_date_count == (0 if snapshot is None else snapshot.confirmed_count)
    assert result.remaining_date_count == (
        spec.total_days if snapshot is None else snapshot.remaining_count
    )
    assert result.next_date == (spec.start_date if snapshot is None else snapshot.next_date)
    assert result.unresolved_attempt_present is (
        snapshot is not None and snapshot.active_attempt is not None
    )
    expected_policy: dict[
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
    expected_action, expected_candidate, expected_review, expected_unresolved = (
        expected_policy[classification]
    )
    assert result.recommended_operator_action == expected_action
    assert result.explicit_manual_invocation_candidate is expected_candidate
    assert result.operator_review_required is expected_review
    assert result.unresolved_write_outcome is expected_unresolved
    assert result.read_only is True
    assert result.automatic_retry_allowed is False
    assert result.mutation_performed is False
    assert result.mutation_authorized is False
    assert result.retry_authorized is False
    assert result.manual_execution_authorized is False
    assert result.manual_recovery_authorized is False
    assert result.production_live_authorized is False
    assert result.full_calendar_certified is False
    assert result.limitations == KR_CALENDAR_COLLECTION_RECOVERY_ASSESSMENT_LIMITATIONS


async def test_unrecognized_paused_reason_is_visible_but_never_an_execution_candidate() -> None:
    spec = _spec()
    store = InMemoryKrCalendarCollectionJobStore()
    ready = await store.load_or_create_job(spec, now=CREATED_AT)
    collecting = await store.begin_date_attempt(
        job_id=spec.job_id,
        spec_sha256=spec.spec_sha256,
        expected_revision=ready.revision,
        attempt_id=ATTEMPT_ID,
        holder_id=HOLDER_ID,
        target_date=START_DATE,
        now=CREATED_AT + timedelta(minutes=1),
    )
    paused = await store.pause_retryable(
        **_active_args(collecting),
        reason_code="operator_requested_pause",
        now=CREATED_AT + timedelta(minutes=2),
    )

    result = await KrCalendarCollectionRecoveryAssessmentService(store).assess(
        spec,
        expected_spec_sha256=spec.spec_sha256,
    )

    assert paused.state == "paused_retryable"
    assert result.classification == "paused_unrecognized"
    assert result.job_state == "paused_retryable"
    assert result.state_reason == "operator_requested_pause"
    assert result.recommended_operator_action == (
        "investigate_unrecognized_pause_without_retry"
    )
    assert result.explicit_manual_invocation_candidate is False
    assert result.unresolved_write_outcome is True


async def test_request_spec_hash_mismatch_fails_before_inspection() -> None:
    spec = _spec()
    inspector = FakeInspector(None)

    with pytest.raises(
        KrCalendarCollectionRecoveryAssessmentError,
        match="kr_calendar_collection_recovery_assessment_spec_sha256_mismatch",
    ):
        await KrCalendarCollectionRecoveryAssessmentService(inspector).assess(
            spec,
            expected_spec_sha256="a" * 64,
        )

    assert inspector.calls == []


async def test_inspected_snapshot_must_match_the_exact_requested_spec() -> None:
    spec = _spec()
    different_spec = _spec(end_date=START_DATE + timedelta(days=1))
    inspector = FakeInspector(await _snapshot_for("ready", spec=different_spec))

    with pytest.raises(
        KrCalendarCollectionRecoveryAssessmentError,
        match="kr_calendar_collection_recovery_assessment_spec_mismatch",
    ):
        await KrCalendarCollectionRecoveryAssessmentService(inspector).assess(
            spec,
            expected_spec_sha256=spec.spec_sha256,
        )

    assert inspector.calls == [JOB_ID]


async def test_invalid_snapshot_fails_closed_after_one_inspection() -> None:
    spec = _spec()
    snapshot = await _snapshot_for("ready", spec=spec)
    assert snapshot is not None
    object.__setattr__(snapshot, "state", "unexpected")
    inspector = FakeInspector(snapshot)

    with pytest.raises(
        KrCalendarCollectionRecoveryAssessmentError,
        match="kr_calendar_collection_recovery_assessment_snapshot_invalid",
    ):
        await KrCalendarCollectionRecoveryAssessmentService(inspector).assess(
            spec,
            expected_spec_sha256=spec.spec_sha256,
        )

    assert inspector.calls == [JOB_ID]


async def test_inspector_failure_is_sanitized_without_secret_exception_context() -> None:
    secret = "service_role=must-not-leak upstream-body=must-not-leak"
    inspector = FakeInspector(None, error=RuntimeError(secret))
    spec = _spec()

    with pytest.raises(
        KrCalendarCollectionRecoveryAssessmentError,
        match="kr_calendar_collection_recovery_assessment_inspection_failed",
    ) as captured:
        await KrCalendarCollectionRecoveryAssessmentService(inspector).assess(
            spec,
            expected_spec_sha256=spec.spec_sha256,
        )

    assert inspector.calls == [JOB_ID]
    assert captured.value.__context__ is None
    assert secret not in traceback.format_exc()


async def test_inspector_cancellation_propagates_after_one_read_attempt() -> None:
    inspector = FakeInspector(None, error=asyncio.CancelledError())
    spec = _spec()

    with pytest.raises(asyncio.CancelledError):
        await KrCalendarCollectionRecoveryAssessmentService(inspector).assess(
            spec,
            expected_spec_sha256=spec.spec_sha256,
        )

    assert inspector.calls == [JOB_ID]


async def test_invalid_spec_is_sanitized_before_inspection() -> None:
    secret = "secret-spec-value-must-not-leak"
    spec = _spec()
    object.__setattr__(spec, "market", secret)
    inspector = FakeInspector(None)

    with pytest.raises(
        KrCalendarCollectionRecoveryAssessmentError,
        match="kr_calendar_collection_recovery_assessment_spec_invalid",
    ) as captured:
        await KrCalendarCollectionRecoveryAssessmentService(inspector).assess(
            spec,
            expected_spec_sha256="a" * 64,
        )

    assert inspector.calls == []
    assert captured.value.__context__ is None
    assert secret not in traceback.format_exc()


def test_assessment_result_cannot_be_constructed_outside_service() -> None:
    with pytest.raises(
        KrCalendarCollectionRecoveryAssessmentError,
        match="kr_calendar_collection_recovery_assessment_requires_service",
    ):
        KrCalendarCollectionRecoveryAssessmentV1()


async def _snapshot_for(
    classification: KrCalendarCollectionRecoveryClassification,
    *,
    spec: KrCalendarCollectionJobSpecV1,
) -> KrCalendarCollectionJobSnapshotV1 | None:
    if classification == "missing":
        return None
    store = InMemoryKrCalendarCollectionJobStore()
    ready = await store.load_or_create_job(spec, now=CREATED_AT)
    if classification == "ready":
        return ready
    collecting = await store.begin_date_attempt(
        job_id=spec.job_id,
        spec_sha256=spec.spec_sha256,
        expected_revision=ready.revision,
        attempt_id=ATTEMPT_ID,
        holder_id=HOLDER_ID,
        target_date=START_DATE,
        now=CREATED_AT + timedelta(minutes=1),
    )
    if classification == "collecting":
        return collecting
    if classification == "paused_retryable":
        return await store.pause_retryable(
            **_active_args(collecting),
            reason_code=KR_CALENDAR_COLLECTION_RETRYABLE_STATE_REASON,
            now=CREATED_AT + timedelta(minutes=2),
        )
    if classification == "blocked_unknown":
        return await store.block_unknown(
            **_active_args(collecting),
            reason_code="collection_write_outcome_unknown",
            now=CREATED_AT + timedelta(minutes=2),
        )
    if classification == "completed":
        return await store.confirm_date(
            **_active_args(collecting),
            collection=_collection(),
            now=CREATED_AT + timedelta(minutes=3),
        )
    raise AssertionError(f"unsupported classification: {classification}")


def _active_args(snapshot: KrCalendarCollectionJobSnapshotV1) -> ActiveAttemptArgs:
    attempt = snapshot.active_attempt
    assert attempt is not None
    return {
        "job_id": snapshot.spec.job_id,
        "spec_sha256": snapshot.spec.spec_sha256,
        "expected_revision": snapshot.revision,
        "attempt_id": attempt.attempt_id,
        "holder_id": attempt.holder_id,
        "target_date": attempt.target_date,
    }


def _spec(*, end_date: date = START_DATE) -> KrCalendarCollectionJobSpecV1:
    return KrCalendarCollectionJobSpecV1(
        job_id=JOB_ID,
        provider="toss",
        market="KR",
        start_date=START_DATE,
        end_date=end_date,
        trigger="manual",
    )


def _collection() -> CollectedKrDailySessionObservationV1:
    observed_at = CREATED_AT + timedelta(minutes=2)
    next_business_date = START_DATE + timedelta(days=1)
    session = PointInTimeKrDailySessionV1.create(
        provider="toss",
        market="KR",
        session_date=START_DATE,
        is_open=True,
        regular_start_at=datetime.combine(
            START_DATE,
            datetime.min.time(),
            tzinfo=KST,
        )
        + timedelta(hours=9),
        regular_end_at=datetime.combine(
            START_DATE,
            datetime.min.time(),
            tzinfo=KST,
        )
        + timedelta(hours=15, minutes=30),
        next_business_date=next_business_date,
        next_regular_start_at=datetime.combine(
            next_business_date,
            datetime.min.time(),
            tzinfo=KST,
        )
        + timedelta(hours=9),
        next_regular_end_at=datetime.combine(
            next_business_date,
            datetime.min.time(),
            tzinfo=KST,
        )
        + timedelta(hours=15, minutes=30),
        observed_at=observed_at,
        provider_contract_sha256="c" * 64,
    )
    receipt = CalendarObservationWriteReceipt(
        status="stored",
        calendar_idempotency_key=session.idempotency_key,
        canonical_evidence_sha256=session.canonical_evidence_sha256,
        revision=1,
        revision_inserted=True,
        occurrence_id=OCCURRENCE_ID,
        occurrence_inserted=True,
        observed_at=observed_at,
    )
    return CollectedKrDailySessionObservationV1(session=session, receipt=receipt)
