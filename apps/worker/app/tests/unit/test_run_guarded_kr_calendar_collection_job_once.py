from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from typing import Any, TypedDict, cast
from uuid import UUID, uuid4

import pytest

from app.adapters.persistence.in_memory_kr_calendar_collection_job_store import (
    InMemoryKrCalendarCollectionJobStore,
)
from app.application.ports.calendar_observation_store_port import (
    CalendarObservationWriteReceipt,
)
from app.application.ports.kr_calendar_collection_job_store_port import (
    KrCalendarCollectionDateCheckpointV1,
    KrCalendarCollectionJobSnapshotV1,
    KrCalendarCollectionJobSpecV1,
)
from app.application.ports.kr_daily_session_collection_port import (
    CollectedKrDailySessionObservationV1,
)
from app.application.services.kr_calendar_collection_recovery_assessment import (
    KR_CALENDAR_COLLECTION_RETRYABLE_STATE_REASON,
    KrCalendarCollectionRecoveryAssessmentService,
)
from app.application.use_cases.run_guarded_kr_calendar_collection_job_once import (
    GuardedKrCalendarCollectionJobOnceError,
    GuardedKrCalendarExpectedClassification,
    RunGuardedKrCalendarCollectionJobOnce,
)
from app.application.use_cases.run_kr_calendar_date_range_collection_job import (
    KR_CALENDAR_DATE_RANGE_COLLECTION_DURABLE_LIMITATIONS,
    KrCalendarDateRangeCollectionJobError,
    RunKrCalendarDateRangeCollectionJob,
)
from app.domain.common.time import KST
from app.domain.market_data.point_in_time_calendar import PointInTimeKrDailySessionV1

JOB_ID = "00000000-0000-4000-8000-000000000301"
HOLDER_ID = "00000000-0000-4000-8000-000000000302"
START_DATE = date(2026, 3, 25)
STARTED_AT = datetime(2026, 3, 24, 22, 0, tzinfo=UTC)
CONTRACT_SHA256 = "d" * 64


class FakeCollector:
    def __init__(self, *, observed_at: datetime = STARTED_AT) -> None:
        self.observed_at = observed_at
        self.calls: list[date] = []

    async def execute_with_evidence(
        self,
        target_date: date,
    ) -> CollectedKrDailySessionObservationV1:
        self.calls.append(target_date)
        await asyncio.sleep(0)
        return _collection(target_date, observed_at=self.observed_at)


class CountingClock:
    def __init__(self, start: datetime = STARTED_AT) -> None:
        self.current = start
        self.calls = 0

    def __call__(self) -> datetime:
        value = self.current
        self.current += timedelta(seconds=1)
        self.calls += 1
        return value


class AttemptIdFactory:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> UUID:
        self.calls += 1
        return UUID(f"00000000-0000-4000-8000-{400 + self.calls:012d}")


class ExpectedArguments(TypedDict):
    expected_spec_sha256: str
    expected_classification: GuardedKrCalendarExpectedClassification
    expected_revision: int | None
    expected_confirmed_count: int
    expected_next_date: date
    expected_state_reason: str | None


class DurableInMemoryStore(InMemoryKrCalendarCollectionJobStore):
    persistence_kind = "durable"

    def __init__(self) -> None:
        super().__init__()
        self.inspect_calls = 0
        self.load_calls = 0
        self.begin_calls = 0

    async def inspect_job(self, job_id: str) -> KrCalendarCollectionJobSnapshotV1 | None:
        self.inspect_calls += 1
        return await super().inspect_job(job_id)

    async def load_or_create_job(
        self,
        spec: KrCalendarCollectionJobSpecV1,
        *,
        now: datetime,
    ) -> KrCalendarCollectionJobSnapshotV1:
        self.load_calls += 1
        return await super().load_or_create_job(spec, now=now)

    async def begin_date_attempt(self, **kwargs: Any) -> KrCalendarCollectionJobSnapshotV1:
        self.begin_calls += 1
        return await super().begin_date_attempt(**kwargs)


class DriftAfterInspectionStore(DurableInMemoryStore):
    def __init__(self, *, drift_at: datetime) -> None:
        super().__init__()
        self.drift_at = drift_at
        self.drift_enabled = False

    async def inspect_job(self, job_id: str) -> KrCalendarCollectionJobSnapshotV1 | None:
        inspected = await super().inspect_job(job_id)
        if self.drift_enabled and inspected is not None and inspected.revision == 1:
            checkpoint = KrCalendarCollectionDateCheckpointV1(
                attempt_id="00000000-0000-4000-8000-000000000451",
                holder_id="00000000-0000-4000-8000-000000000452",
                target_date=inspected.spec.start_date,
                fencing_revision=2,
                begun_at=self.drift_at,
                collection=_collection(inspected.spec.start_date, observed_at=self.drift_at),
                confirmed_at=self.drift_at + timedelta(seconds=1),
            )
            advanced = KrCalendarCollectionJobSnapshotV1(
                spec=inspected.spec,
                revision=3,
                state="ready",
                checkpoints=(checkpoint,),
                active_attempt=None,
                state_reason=None,
                terminal_manifest_sha256=None,
                created_at=inspected.created_at,
                updated_at=self.drift_at + timedelta(seconds=1),
            )
            async with self._lock:
                self._jobs[job_id] = advanced
            self.drift_enabled = False
        return inspected


@pytest.mark.parametrize(
    ("wrapper_enabled", "manual_confirmation", "message"),
    [
        (
            False,
            True,
            "guarded_kr_calendar_collection_job_manual_execution_disabled",
        ),
        (
            cast(bool, 1),
            True,
            "guarded_kr_calendar_collection_job_manual_execution_disabled",
        ),
        (
            True,
            False,
            "guarded_kr_calendar_collection_job_manual_confirmation_required",
        ),
        (
            True,
            cast(bool, 1),
            "guarded_kr_calendar_collection_job_manual_confirmation_required",
        ),
    ],
)
async def test_config_and_manual_confirmation_block_before_assessment_and_io(
    wrapper_enabled: bool,
    manual_confirmation: bool,
    message: str,
) -> None:
    guarded, collector, store, clock, attempts = _guarded(
        wrapper_enabled=wrapper_enabled
    )
    spec = _spec()

    with pytest.raises(GuardedKrCalendarCollectionJobOnceError, match=message):
        await guarded.execute(
            spec,
            **_expected(spec),
            manual_confirmation=manual_confirmation,
        )

    assert store.inspect_calls == 0
    assert store.load_calls == 0
    assert store.begin_calls == 0
    assert collector.calls == []
    assert clock.calls == 0
    assert attempts.calls == 0


async def test_missing_assessment_runs_exactly_one_durable_date_with_safe_evidence() -> None:
    guarded, collector, store, _clock, attempts = _guarded()
    spec = _spec()

    result = await guarded.execute(
        spec,
        **_expected(spec),
        manual_confirmation=True,
    )

    assert result.action == "completed"
    assert result.processed_date == START_DATE
    assert result.durable_runtime_configured is True
    assert result.limitations == KR_CALENDAR_DATE_RANGE_COLLECTION_DURABLE_LIMITATIONS
    assert result.processed_checkpoint_attempt_id is not None
    assert result.processed_checkpoint_holder_id == HOLDER_ID
    assert result.processed_checkpoint_fencing_revision == 2
    assert result.processed_receipt_status == "stored"
    assert result.processed_receipt_canonical_evidence_sha256 is not None
    assert result.processed_receipt_occurrence_id is not None
    assert collector.calls == [START_DATE]
    assert attempts.calls == 1
    assert store.inspect_calls == 1
    assert store.load_calls == 1
    assert store.begin_calls == 1


async def test_user_reviewed_precondition_mismatch_stops_before_mutating_load() -> None:
    guarded, collector, store, clock, attempts = _guarded()
    spec = _spec()

    with pytest.raises(
        GuardedKrCalendarCollectionJobOnceError,
        match="guarded_kr_calendar_collection_job_assessment_mismatch",
    ):
        await guarded.execute(
            spec,
            **_expected(spec, classification="ready", revision=1),
            manual_confirmation=True,
        )

    assert store.inspect_calls == 1
    assert store.load_calls == 0
    assert store.begin_calls == 0
    assert collector.calls == []
    assert clock.calls == 0
    assert attempts.calls == 0


async def test_tampered_assessment_is_fixed_error_and_never_leaks_or_calls_io() -> None:
    guarded, collector, store, clock, attempts = _guarded()
    spec = _spec()
    assessment = await guarded.recovery_assessment_service.assess(
        spec,
        expected_spec_sha256=spec.spec_sha256,
    )
    secret = "service_key=assessment-payload-must-not-leak"
    object.__setattr__(assessment, "limitations", (secret,))

    with pytest.raises(
        KrCalendarDateRangeCollectionJobError,
        match="kr_calendar_date_range_collection_recovery_assessment_invalid",
    ) as captured:
        await guarded.runner.execute_assessed(spec, assessment)

    assert secret not in str(captured.value)
    assert secret not in repr(captured.value)
    assert store.load_calls == 0
    assert store.begin_calls == 0
    assert collector.calls == []
    assert clock.calls == 0
    assert attempts.calls == 0


async def test_paused_retry_requires_separate_confirmation_then_runs_one_date() -> None:
    spec = _spec()
    store = DurableInMemoryStore()
    created = await store.load_or_create_job(spec, now=STARTED_AT)
    active = await store.begin_date_attempt(
        job_id=JOB_ID,
        spec_sha256=spec.spec_sha256,
        expected_revision=created.revision,
        attempt_id="00000000-0000-4000-8000-000000000461",
        holder_id="00000000-0000-4000-8000-000000000462",
        target_date=START_DATE,
        now=STARTED_AT + timedelta(seconds=1),
    )
    active_attempt = active.active_attempt
    assert active_attempt is not None
    paused = await store.pause_retryable(
        job_id=JOB_ID,
        spec_sha256=spec.spec_sha256,
        expected_revision=active.revision,
        attempt_id=active_attempt.attempt_id,
        holder_id=active_attempt.holder_id,
        target_date=START_DATE,
        reason_code=KR_CALENDAR_COLLECTION_RETRYABLE_STATE_REASON,
        now=STARTED_AT + timedelta(seconds=2),
    )
    collector = FakeCollector(observed_at=STARTED_AT + timedelta(seconds=3))
    clock = CountingClock(STARTED_AT + timedelta(seconds=3))
    attempts = AttemptIdFactory()
    guarded = _guarded_for(store, collector, clock, attempts)
    expected = _expected(
        spec,
        classification="paused_retryable",
        revision=paused.revision,
    )
    baseline = (store.inspect_calls, store.load_calls, store.begin_calls)

    with pytest.raises(
        GuardedKrCalendarCollectionJobOnceError,
        match="guarded_kr_calendar_collection_job_expected_precondition_invalid",
    ):
        await guarded.execute(spec, **expected, manual_confirmation=True)

    assert (store.inspect_calls, store.load_calls, store.begin_calls) == baseline
    result = await guarded.execute(
        spec,
        **expected,
        manual_confirmation=True,
        paused_retry_confirmation=True,
    )
    assert result.action == "completed"
    assert collector.calls == [START_DATE]
    assert attempts.calls == 1


async def test_unrecognized_paused_reason_cannot_be_confirmed_or_executed() -> None:
    spec = _spec()
    store = DurableInMemoryStore()
    created = await store.load_or_create_job(spec, now=STARTED_AT)
    active = await store.begin_date_attempt(
        job_id=JOB_ID,
        spec_sha256=spec.spec_sha256,
        expected_revision=created.revision,
        attempt_id="00000000-0000-4000-8000-000000000463",
        holder_id="00000000-0000-4000-8000-000000000464",
        target_date=START_DATE,
        now=STARTED_AT + timedelta(seconds=1),
    )
    active_attempt = active.active_attempt
    assert active_attempt is not None
    paused = await store.pause_retryable(
        job_id=JOB_ID,
        spec_sha256=spec.spec_sha256,
        expected_revision=active.revision,
        attempt_id=active_attempt.attempt_id,
        holder_id=active_attempt.holder_id,
        target_date=START_DATE,
        reason_code="operator_requested_pause",
        now=STARTED_AT + timedelta(seconds=2),
    )
    collector = FakeCollector(observed_at=STARTED_AT + timedelta(seconds=3))
    clock = CountingClock(STARTED_AT + timedelta(seconds=3))
    attempts = AttemptIdFactory()
    guarded = _guarded_for(store, collector, clock, attempts)
    baseline = (store.load_calls, store.begin_calls)

    with pytest.raises(
        GuardedKrCalendarCollectionJobOnceError,
        match="guarded_kr_calendar_collection_job_state_not_executable",
    ):
        await guarded.execute(
            spec,
            **_expected(
                spec,
                classification="paused_retryable",
                revision=paused.revision,
                state_reason=KR_CALENDAR_COLLECTION_RETRYABLE_STATE_REASON,
            ),
            manual_confirmation=True,
            paused_retry_confirmation=True,
        )

    assert (store.load_calls, store.begin_calls) == baseline
    assert collector.calls == []
    assert clock.calls == 0
    assert attempts.calls == 0


@pytest.mark.parametrize("terminal_state", ["collecting", "blocked_unknown", "completed"])
async def test_unresolved_and_completed_states_are_not_execution_candidates(
    terminal_state: str,
) -> None:
    spec = _spec()
    store = DurableInMemoryStore()
    created = await store.load_or_create_job(spec, now=STARTED_AT)
    active = await store.begin_date_attempt(
        job_id=JOB_ID,
        spec_sha256=spec.spec_sha256,
        expected_revision=created.revision,
        attempt_id="00000000-0000-4000-8000-000000000471",
        holder_id="00000000-0000-4000-8000-000000000472",
        target_date=START_DATE,
        now=STARTED_AT + timedelta(seconds=1),
    )
    attempt = active.active_attempt
    assert attempt is not None
    if terminal_state == "blocked_unknown":
        await store.block_unknown(
            job_id=JOB_ID,
            spec_sha256=spec.spec_sha256,
            expected_revision=active.revision,
            attempt_id=attempt.attempt_id,
            holder_id=attempt.holder_id,
            target_date=START_DATE,
            reason_code="collection_write_outcome_unknown",
            now=STARTED_AT + timedelta(seconds=2),
        )
    elif terminal_state == "completed":
        await store.confirm_date(
            job_id=JOB_ID,
            spec_sha256=spec.spec_sha256,
            expected_revision=active.revision,
            attempt_id=attempt.attempt_id,
            holder_id=attempt.holder_id,
            target_date=START_DATE,
            collection=_collection(START_DATE, observed_at=STARTED_AT + timedelta(seconds=1)),
            now=STARTED_AT + timedelta(seconds=2),
        )
    collector = FakeCollector(observed_at=STARTED_AT + timedelta(seconds=3))
    clock = CountingClock(STARTED_AT + timedelta(seconds=3))
    attempts = AttemptIdFactory()
    guarded = _guarded_for(store, collector, clock, attempts)
    baseline_loads = store.load_calls
    baseline_begins = store.begin_calls

    with pytest.raises(
        GuardedKrCalendarCollectionJobOnceError,
        match="guarded_kr_calendar_collection_job_state_not_executable",
    ):
        await guarded.execute(
            spec,
            **_expected(spec, classification="ready", revision=1),
            manual_confirmation=True,
        )

    assert store.load_calls == baseline_loads
    assert store.begin_calls == baseline_begins
    assert collector.calls == []
    assert clock.calls == 0
    assert attempts.calls == 0


async def test_state_drift_after_assessment_stops_before_uuid_begin_and_collector() -> None:
    spec = _spec(end_date=START_DATE + timedelta(days=1))
    store = DriftAfterInspectionStore(drift_at=STARTED_AT + timedelta(seconds=1))
    await store.load_or_create_job(spec, now=STARTED_AT)
    store.drift_enabled = True
    store.inspect_calls = store.load_calls = store.begin_calls = 0
    collector = FakeCollector(observed_at=STARTED_AT + timedelta(seconds=4))
    clock = CountingClock(STARTED_AT + timedelta(seconds=3))
    attempts = AttemptIdFactory()
    guarded = _guarded_for(store, collector, clock, attempts)

    with pytest.raises(
        KrCalendarDateRangeCollectionJobError,
        match="kr_calendar_date_range_collection_recovery_assessment_stale",
    ):
        await guarded.execute(
            spec,
            **_expected(spec, classification="ready", revision=1),
            manual_confirmation=True,
        )

    assert store.inspect_calls == 1
    assert store.load_calls == 1
    assert store.begin_calls == 0
    assert collector.calls == []
    assert attempts.calls == 0
    snapshot = await InMemoryKrCalendarCollectionJobStore.inspect_job(store, JOB_ID)
    assert snapshot is not None
    assert snapshot.revision == 3
    assert snapshot.confirmed_count == 1
    assert snapshot.next_date == START_DATE + timedelta(days=1)


def _guarded(
    *,
    wrapper_enabled: bool = True,
) -> tuple[
    RunGuardedKrCalendarCollectionJobOnce,
    FakeCollector,
    DurableInMemoryStore,
    CountingClock,
    AttemptIdFactory,
]:
    store = DurableInMemoryStore()
    collector = FakeCollector()
    clock = CountingClock()
    attempts = AttemptIdFactory()
    return (
        _guarded_for(
            store,
            collector,
            clock,
            attempts,
            wrapper_enabled=wrapper_enabled,
        ),
        collector,
        store,
        clock,
        attempts,
    )


def _guarded_for(
    store: DurableInMemoryStore,
    collector: FakeCollector,
    clock: CountingClock,
    attempts: AttemptIdFactory,
    *,
    wrapper_enabled: bool = True,
) -> RunGuardedKrCalendarCollectionJobOnce:
    runner = RunKrCalendarDateRangeCollectionJob(
        collector,
        store,
        holder_id=HOLDER_ID,
        clock=clock,
        attempt_id_factory=attempts,
        manual_execution_enabled=True,
    )
    return RunGuardedKrCalendarCollectionJobOnce(
        KrCalendarCollectionRecoveryAssessmentService(store),
        runner,
        manual_execution_enabled=wrapper_enabled,
    )


def _expected(
    spec: KrCalendarCollectionJobSpecV1,
    *,
    classification: GuardedKrCalendarExpectedClassification = "missing",
    revision: int | None = None,
    confirmed_count: int = 0,
    state_reason: str | None = None,
) -> ExpectedArguments:
    if classification == "paused_retryable" and state_reason is None:
        state_reason = KR_CALENDAR_COLLECTION_RETRYABLE_STATE_REASON
    return {
        "expected_spec_sha256": spec.spec_sha256,
        "expected_classification": classification,
        "expected_revision": revision,
        "expected_confirmed_count": confirmed_count,
        "expected_next_date": spec.start_date + timedelta(days=confirmed_count),
        "expected_state_reason": state_reason,
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


def _collection(
    target_date: date,
    *,
    observed_at: datetime,
) -> CollectedKrDailySessionObservationV1:
    next_date = target_date + timedelta(days=1)
    session = PointInTimeKrDailySessionV1.create(
        provider="toss",
        market="KR",
        session_date=target_date,
        is_open=True,
        regular_start_at=(
            datetime.combine(target_date, datetime.min.time(), tzinfo=KST)
            + timedelta(hours=9)
        ),
        regular_end_at=(
            datetime.combine(target_date, datetime.min.time(), tzinfo=KST)
            + timedelta(hours=15, minutes=30)
        ),
        next_business_date=next_date,
        next_regular_start_at=(
            datetime.combine(next_date, datetime.min.time(), tzinfo=KST)
            + timedelta(hours=9)
        ),
        next_regular_end_at=(
            datetime.combine(next_date, datetime.min.time(), tzinfo=KST)
            + timedelta(hours=15, minutes=30)
        ),
        observed_at=observed_at,
        provider_contract_sha256=CONTRACT_SHA256,
    )
    return CollectedKrDailySessionObservationV1(
        session=session,
        receipt=CalendarObservationWriteReceipt(
            status="stored",
            calendar_idempotency_key=session.idempotency_key,
            canonical_evidence_sha256=session.canonical_evidence_sha256,
            revision=1,
            revision_inserted=True,
            occurrence_id=uuid4(),
            occurrence_inserted=True,
            observed_at=session.observed_at,
        ),
    )
