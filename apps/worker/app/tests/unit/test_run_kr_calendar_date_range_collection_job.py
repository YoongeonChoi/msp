from __future__ import annotations

import asyncio
import traceback
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID, uuid4

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
from app.application.use_cases.collect_kr_daily_session_observation import (
    KrDailySessionCollectionError,
)
from app.application.use_cases.run_kr_calendar_date_range_collection_job import (
    KR_CALENDAR_DATE_RANGE_COLLECTION_DURABLE_LIMITATIONS,
    KR_CALENDAR_DATE_RANGE_COLLECTION_LIMITATIONS,
    KrCalendarDateRangeCollectionJobError,
    KrCalendarDateRangeCollectionRunResultV1,
    RunKrCalendarDateRangeCollectionJob,
)
from app.domain.common.time import KST
from app.domain.market_data.point_in_time_calendar import (
    PointInTimeKrDailySessionV1,
)

JOB_ID = "00000000-0000-4000-8000-000000000201"
HOLDER_ID = "00000000-0000-4000-8000-000000000202"
START_DATE = date(2026, 3, 25)
STARTED_AT = datetime(2026, 3, 24, 22, 0, tzinfo=UTC)
CONTRACT_SHA256 = "c" * 64


class FakeCollector:
    def __init__(self, *outcomes: object) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[date] = []

    async def execute_with_evidence(
        self,
        target_date: date,
    ) -> CollectedKrDailySessionObservationV1:
        self.calls.append(target_date)
        call_index = len(self.calls) - 1
        await asyncio.sleep(0)
        if not self.outcomes:
            return _collection(
                target_date,
                observed_at=STARTED_AT + timedelta(seconds=2 * call_index),
            )
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return cast(
                CollectedKrDailySessionObservationV1,
                cast(Callable[[date], object], outcome)(target_date),
            )
        return cast(CollectedKrDailySessionObservationV1, outcome)


class CountingClock:
    def __init__(self, start: datetime = STARTED_AT) -> None:
        self.current = start
        self.calls = 0

    def __call__(self) -> datetime:
        result = self.current
        self.current += timedelta(seconds=1)
        self.calls += 1
        return result


class AttemptIdFactory:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> UUID:
        self.calls += 1
        return UUID(f"00000000-0000-4000-8000-{self.calls:012d}")


FaultOperation = Literal["begin", "pause", "block", "confirm"]
FaultTiming = Literal["before", "after"]


class FaultingJobStore(InMemoryKrCalendarCollectionJobStore):
    def __init__(
        self,
        operation: FaultOperation,
        timing: FaultTiming,
        *,
        secret: str = "store response service_key=must-not-leak",
    ) -> None:
        super().__init__()
        self.operation = operation
        self.timing = timing
        self.secret = secret

    def _before(self, operation: FaultOperation) -> None:
        if self.operation == operation and self.timing == "before":
            raise RuntimeError(self.secret)

    def _after(self, operation: FaultOperation) -> None:
        if self.operation == operation and self.timing == "after":
            raise RuntimeError(self.secret)

    async def begin_date_attempt(
        self,
        *,
        job_id: str,
        spec_sha256: str,
        expected_revision: int,
        attempt_id: str,
        holder_id: str,
        target_date: date,
        now: datetime,
    ) -> KrCalendarCollectionJobSnapshotV1:
        self._before("begin")
        result = await super().begin_date_attempt(
            job_id=job_id,
            spec_sha256=spec_sha256,
            expected_revision=expected_revision,
            attempt_id=attempt_id,
            holder_id=holder_id,
            target_date=target_date,
            now=now,
        )
        self._after("begin")
        return result

    async def pause_retryable(self, **kwargs: Any) -> KrCalendarCollectionJobSnapshotV1:
        self._before("pause")
        result = await super().pause_retryable(**kwargs)
        self._after("pause")
        return result

    async def block_unknown(self, **kwargs: Any) -> KrCalendarCollectionJobSnapshotV1:
        self._before("block")
        result = await super().block_unknown(**kwargs)
        self._after("block")
        return result

    async def confirm_date(self, **kwargs: Any) -> KrCalendarCollectionJobSnapshotV1:
        self._before("confirm")
        result = await super().confirm_date(**kwargs)
        self._after("confirm")
        return result


class CancellingJobStore(FaultingJobStore):
    def _before(self, operation: FaultOperation) -> None:
        if self.operation == operation and self.timing == "before":
            raise asyncio.CancelledError

    def _after(self, operation: FaultOperation) -> None:
        if self.operation == operation and self.timing == "after":
            raise asyncio.CancelledError


class MutatingLoadSpecStore(InMemoryKrCalendarCollectionJobStore):
    async def load_or_create_job(
        self,
        spec: KrCalendarCollectionJobSpecV1,
        *,
        now: datetime,
    ) -> KrCalendarCollectionJobSnapshotV1:
        object.__setattr__(spec, "end_date", spec.end_date + timedelta(days=1))
        return await super().load_or_create_job(spec, now=now)


class DurableCountingJobStore(InMemoryKrCalendarCollectionJobStore):
    persistence_kind = "durable"

    def __init__(self) -> None:
        super().__init__()
        self.load_calls = 0

    async def load_or_create_job(
        self,
        spec: KrCalendarCollectionJobSpecV1,
        *,
        now: datetime,
    ) -> KrCalendarCollectionJobSnapshotV1:
        self.load_calls += 1
        return await super().load_or_create_job(spec, now=now)


class MutatingConfirmCollectionStore(InMemoryKrCalendarCollectionJobStore):
    async def confirm_date(self, **kwargs: Any) -> KrCalendarCollectionJobSnapshotV1:
        source = cast(CollectedKrDailySessionObservationV1, kwargs["collection"])
        replacement = _collection(
            source.target_date,
            observed_at=source.session.observed_at,
            is_open=False,
        )
        object.__setattr__(source, "session", replacement.session)
        object.__setattr__(source, "receipt", replacement.receipt)
        return await super().confirm_date(**kwargs)


class DriftingBeginClockStore(InMemoryKrCalendarCollectionJobStore):
    async def begin_date_attempt(
        self,
        *,
        job_id: str,
        spec_sha256: str,
        expected_revision: int,
        attempt_id: str,
        holder_id: str,
        target_date: date,
        now: datetime,
    ) -> KrCalendarCollectionJobSnapshotV1:
        return await super().begin_date_attempt(
            job_id=job_id,
            spec_sha256=spec_sha256,
            expected_revision=expected_revision,
            attempt_id=attempt_id,
            holder_id=holder_id,
            target_date=target_date,
            now=now + timedelta(seconds=1),
        )


@pytest.mark.parametrize("enabled", [False, 1, "true"])
async def test_manual_execution_is_exact_bool_and_blocks_before_all_dependencies(
    enabled: object,
) -> None:
    collector = FakeCollector()
    store = InMemoryKrCalendarCollectionJobStore()
    clock = CountingClock()
    attempts = AttemptIdFactory()
    runner = RunKrCalendarDateRangeCollectionJob(
        collector,
        store,
        holder_id=HOLDER_ID,
        clock=clock,
        attempt_id_factory=attempts,
        manual_execution_enabled=cast(bool, enabled),
    )

    with pytest.raises(
        KrCalendarDateRangeCollectionJobError,
        match="kr_calendar_date_range_collection_manual_execution_disabled",
    ):
        await runner.execute(cast(KrCalendarCollectionJobSpecV1, object()))

    assert collector.calls == []
    assert clock.calls == 0
    assert attempts.calls == 0
    assert await store.inspect_job(JOB_ID) is None


async def test_each_manual_invocation_advances_exactly_one_date_then_replays_completion() -> None:
    collector = FakeCollector()
    store = InMemoryKrCalendarCollectionJobStore()
    attempts = AttemptIdFactory()
    runner = _runner(collector, store, attempt_id_factory=attempts)
    spec = _spec(end_date=START_DATE + timedelta(days=2))

    first = await runner.execute(spec)
    second = await runner.execute(spec)
    third = await runner.execute(spec)
    replay = await runner.execute(spec)

    assert collector.calls == [
        START_DATE,
        START_DATE + timedelta(days=1),
        START_DATE + timedelta(days=2),
    ]
    assert [first.action, second.action, third.action, replay.action] == [
        "advanced",
        "advanced",
        "completed",
        "completed_replay",
    ]
    assert first.confirmed_date_count == 1
    assert second.confirmed_date_count == 2
    assert third.confirmed_date_count == 3
    assert replay.processed_date is None
    assert attempts.calls == 3
    assert third.terminal_manifest_sha256 is not None
    assert third.limitations == KR_CALENDAR_DATE_RANGE_COLLECTION_LIMITATIONS
    assert third.manual_execution_only is True
    assert third.automatic_retry_allowed is False
    assert third.durable_runtime_configured is False
    assert third.full_calendar_certified is False
    snapshot = await store.inspect_job(JOB_ID)
    assert snapshot is not None
    checkpoint = snapshot.checkpoints[-1]
    receipt = checkpoint.collection.receipt
    assert third.processed_checkpoint_attempt_id == checkpoint.attempt_id
    assert third.processed_checkpoint_holder_id == checkpoint.holder_id
    assert third.processed_checkpoint_fencing_revision == checkpoint.fencing_revision
    assert third.processed_checkpoint_begun_at == checkpoint.begun_at
    assert third.processed_checkpoint_confirmed_at == checkpoint.confirmed_at
    assert third.processed_receipt_status == receipt.status
    assert (
        third.processed_receipt_calendar_idempotency_key
        == receipt.calendar_idempotency_key
    )
    assert (
        third.processed_receipt_canonical_evidence_sha256
        == receipt.canonical_evidence_sha256
    )
    assert third.processed_receipt_revision == receipt.revision
    assert third.processed_receipt_revision_inserted is receipt.revision_inserted
    assert third.processed_receipt_occurrence_id == str(receipt.occurrence_id)
    assert third.processed_receipt_occurrence_inserted is receipt.occurrence_inserted
    assert third.processed_receipt_observed_at == receipt.observed_at
    assert replay.processed_checkpoint_attempt_id is None
    assert replay.processed_receipt_canonical_evidence_sha256 is None


async def test_durable_store_requires_assessment_before_clock_store_or_collector() -> None:
    collector = FakeCollector()
    store = DurableCountingJobStore()
    clock = CountingClock()
    attempts = AttemptIdFactory()
    runner = RunKrCalendarDateRangeCollectionJob(
        collector,
        store,
        holder_id=HOLDER_ID,
        clock=clock,
        attempt_id_factory=attempts,
        manual_execution_enabled=True,
    )

    with pytest.raises(
        KrCalendarDateRangeCollectionJobError,
        match="kr_calendar_date_range_collection_recovery_assessment_required",
    ):
        await runner.execute(_spec())

    assert runner.persistence_kind == "durable"
    assert collector.calls == []
    assert clock.calls == 0
    assert attempts.calls == 0
    assert store.load_calls == 0
    assert KR_CALENDAR_DATE_RANGE_COLLECTION_DURABLE_LIMITATIONS != (
        KR_CALENDAR_DATE_RANGE_COLLECTION_LIMITATIONS
    )


async def test_366_day_job_still_advances_only_first_date() -> None:
    collector = FakeCollector()
    store = InMemoryKrCalendarCollectionJobStore()
    result = await _runner(collector, store).execute(
        _spec(end_date=START_DATE + timedelta(days=365))
    )

    assert collector.calls == [START_DATE]
    assert result.action == "advanced"
    assert result.total_date_count == 366
    assert result.confirmed_date_count == 1
    assert result.remaining_date_count == 365


async def test_known_pre_write_failure_pauses_until_next_explicit_manual_call() -> None:
    collector = FakeCollector(
        KrDailySessionCollectionError(
            "kr_daily_session_collection_source_failed",
            write_outcome="not_attempted",
        )
    )
    store = InMemoryKrCalendarCollectionJobStore()
    runner = _runner(collector, store)

    with pytest.raises(
        KrCalendarDateRangeCollectionJobError,
        match="kr_calendar_date_range_collection_paused_retryable",
    ):
        await runner.execute(_spec())
    paused = await store.inspect_job(JOB_ID)

    assert paused is not None
    assert paused.state == "paused_retryable"
    assert paused.next_date == START_DATE
    completed = await runner.execute(_spec())
    assert collector.calls == [START_DATE, START_DATE]
    assert completed.action == "completed"


@pytest.mark.parametrize(
    "failure",
    [
        KrDailySessionCollectionError(
            "kr_daily_session_collection_store_outcome_unknown",
            write_outcome="unknown",
        ),
        KrDailySessionCollectionError(
            "untrusted_retry_claim",
            write_outcome="not_attempted",
        ),
        RuntimeError("collector secret=must-not-leak"),
    ],
)
async def test_unknown_or_untrusted_failure_blocks_all_future_collection(
    failure: BaseException,
) -> None:
    collector = FakeCollector(failure)
    store = InMemoryKrCalendarCollectionJobStore()
    attempts = AttemptIdFactory()
    runner = _runner(collector, store, attempt_id_factory=attempts)

    with pytest.raises(
        KrCalendarDateRangeCollectionJobError,
        match="kr_calendar_date_range_collection_blocked_unknown",
    ) as captured:
        await runner.execute(_spec())
    first_attempt_calls = attempts.calls
    with pytest.raises(
        KrCalendarDateRangeCollectionJobError,
        match="kr_calendar_date_range_collection_unresolved_attempt",
    ):
        await runner.execute(_spec())

    assert collector.calls == [START_DATE]
    assert attempts.calls == first_attempt_calls
    snapshot = await store.inspect_job(JOB_ID)
    assert snapshot is not None
    assert snapshot.state == "blocked_unknown"
    _assert_secret_absent(captured.value, "collector secret=must-not-leak")


async def test_invalid_collection_scope_is_blocked_without_checkpoint() -> None:
    collector = FakeCollector(lambda target: _collection(target, provider="naver"))
    store = InMemoryKrCalendarCollectionJobStore()
    runner = _runner(collector, store)

    with pytest.raises(
        KrCalendarDateRangeCollectionJobError,
        match="kr_calendar_date_range_collection_collection_evidence_invalid",
    ):
        await runner.execute(_spec())

    snapshot = await store.inspect_job(JOB_ID)
    assert snapshot is not None
    assert snapshot.state == "blocked_unknown"
    assert snapshot.checkpoints == ()


async def test_cancellation_after_begin_leaves_unresolved_attempt_and_never_retries() -> None:
    collector = FakeCollector(asyncio.CancelledError())
    store = InMemoryKrCalendarCollectionJobStore()
    runner = _runner(collector, store)

    with pytest.raises(asyncio.CancelledError):
        await runner.execute(_spec())
    with pytest.raises(
        KrCalendarDateRangeCollectionJobError,
        match="kr_calendar_date_range_collection_unresolved_attempt",
    ):
        await runner.execute(_spec())

    assert collector.calls == [START_DATE]
    snapshot = await store.inspect_job(JOB_ID)
    assert snapshot is not None
    assert snapshot.state == "collecting"


@pytest.mark.parametrize("timing", ["before", "after"])
async def test_begin_response_loss_never_calls_collector(
    timing: FaultTiming,
) -> None:
    collector = FakeCollector()
    store = FaultingJobStore("begin", timing)
    runner = _runner(collector, store)

    with pytest.raises(
        KrCalendarDateRangeCollectionJobError,
        match="kr_calendar_date_range_collection_begin_outcome_unknown",
    ) as captured:
        await runner.execute(_spec())

    assert collector.calls == []
    _assert_secret_absent(captured.value, store.secret)
    snapshot = await store.inspect_job(JOB_ID)
    assert snapshot is not None
    assert snapshot.state == ("ready" if timing == "before" else "collecting")


@pytest.mark.parametrize("timing", ["before", "after"])
async def test_pause_response_loss_never_claims_safe_retry(
    timing: FaultTiming,
) -> None:
    collector = FakeCollector(
        KrDailySessionCollectionError(
            "kr_daily_session_collection_source_failed",
            write_outcome="not_attempted",
        )
    )
    store = FaultingJobStore("pause", timing)

    with pytest.raises(
        KrCalendarDateRangeCollectionJobError,
        match="kr_calendar_date_range_collection_pause_outcome_unknown",
    ):
        await _runner(collector, store).execute(_spec())

    snapshot = await store.inspect_job(JOB_ID)
    assert snapshot is not None
    assert snapshot.state == ("collecting" if timing == "before" else "paused_retryable")


@pytest.mark.parametrize("timing", ["before", "after"])
async def test_block_response_loss_never_allows_automatic_retry(
    timing: FaultTiming,
) -> None:
    collector = FakeCollector(RuntimeError("unknown"))
    store = FaultingJobStore("block", timing)
    runner = _runner(collector, store)

    with pytest.raises(
        KrCalendarDateRangeCollectionJobError,
        match="kr_calendar_date_range_collection_block_outcome_unknown",
    ):
        await runner.execute(_spec())
    with pytest.raises(
        KrCalendarDateRangeCollectionJobError,
        match="kr_calendar_date_range_collection_unresolved_attempt",
    ):
        await runner.execute(_spec())

    assert collector.calls == [START_DATE]
    snapshot = await store.inspect_job(JOB_ID)
    assert snapshot is not None
    assert snapshot.state == ("collecting" if timing == "before" else "blocked_unknown")


@pytest.mark.parametrize("timing", ["before", "after"])
async def test_confirm_response_loss_never_recollects_same_date(
    timing: FaultTiming,
) -> None:
    collector = FakeCollector()
    store = FaultingJobStore("confirm", timing)
    runner = _runner(collector, store)

    with pytest.raises(
        KrCalendarDateRangeCollectionJobError,
        match="kr_calendar_date_range_collection_checkpoint_outcome_unknown",
    ):
        await runner.execute(_spec())

    if timing == "before":
        with pytest.raises(
            KrCalendarDateRangeCollectionJobError,
            match="kr_calendar_date_range_collection_unresolved_attempt",
        ):
            await runner.execute(_spec())
    else:
        replay = await runner.execute(_spec())
        assert replay.action == "completed_replay"
    assert collector.calls == [START_DATE]


async def test_confirm_response_loss_on_multiday_job_advances_to_next_date() -> None:
    collector = FakeCollector()
    store = FaultingJobStore("confirm", "after")
    runner = _runner(collector, store)
    spec = _spec(end_date=START_DATE + timedelta(days=1))

    with pytest.raises(
        KrCalendarDateRangeCollectionJobError,
        match="kr_calendar_date_range_collection_checkpoint_outcome_unknown",
    ):
        await runner.execute(spec)
    with pytest.raises(
        KrCalendarDateRangeCollectionJobError,
        match="kr_calendar_date_range_collection_checkpoint_outcome_unknown",
    ):
        await runner.execute(spec)
    replay = await runner.execute(spec)

    assert collector.calls == [START_DATE, START_DATE + timedelta(days=1)]
    assert replay.action == "completed_replay"


@pytest.mark.parametrize(
    ("operation", "collector_failure", "before_state", "after_state", "collector_calls"),
    [
        ("begin", None, "ready", "collecting", 0),
        (
            "pause",
            KrDailySessionCollectionError(
                "kr_daily_session_collection_source_failed",
                write_outcome="not_attempted",
            ),
            "collecting",
            "paused_retryable",
            1,
        ),
        ("block", RuntimeError("unknown"), "collecting", "blocked_unknown", 1),
        ("confirm", None, "collecting", "completed", 1),
    ],
)
@pytest.mark.parametrize("timing", ["before", "after"])
async def test_cancellation_at_each_transition_preserves_committed_state_truth(
    operation: FaultOperation,
    collector_failure: BaseException | None,
    before_state: str,
    after_state: str,
    collector_calls: int,
    timing: FaultTiming,
) -> None:
    collector = FakeCollector(*(() if collector_failure is None else (collector_failure,)))
    store = CancellingJobStore(operation, timing)

    with pytest.raises(asyncio.CancelledError):
        await _runner(collector, store).execute(_spec())

    snapshot = await store.inspect_job(JOB_ID)
    assert snapshot is not None
    assert snapshot.state == (before_state if timing == "before" else after_state)
    assert len(collector.calls) == collector_calls


async def test_concurrent_calls_allow_only_one_collector_invocation() -> None:
    collector = FakeCollector()
    store = InMemoryKrCalendarCollectionJobStore()
    runner = _runner(collector, store)

    outcomes = await asyncio.gather(
        runner.execute(_spec()),
        runner.execute(_spec()),
        return_exceptions=True,
    )

    assert len(collector.calls) == 1
    assert sum(isinstance(item, KrCalendarDateRangeCollectionRunResultV1) for item in outcomes) == 1
    assert sum(isinstance(item, KrCalendarDateRangeCollectionJobError) for item in outcomes) == 1


async def test_store_cannot_mutate_trusted_job_scope_during_load() -> None:
    collector = FakeCollector()
    store = MutatingLoadSpecStore()

    with pytest.raises(
        KrCalendarDateRangeCollectionJobError,
        match="kr_calendar_date_range_collection_job_load_outcome_unknown",
    ):
        await _runner(collector, store).execute(_spec())

    assert collector.calls == []


async def test_store_cannot_mutate_trusted_collection_during_confirm() -> None:
    collector = FakeCollector()
    store = MutatingConfirmCollectionStore()
    runner = _runner(collector, store)

    with pytest.raises(
        KrCalendarDateRangeCollectionJobError,
        match="kr_calendar_date_range_collection_checkpoint_outcome_unknown",
    ):
        await runner.execute(_spec())

    replay = await runner.execute(_spec())
    assert replay.action == "completed_replay"
    assert collector.calls == [START_DATE]


async def test_store_transition_clock_must_match_begin_request_exactly() -> None:
    collector = FakeCollector()
    store = DriftingBeginClockStore()

    with pytest.raises(
        KrCalendarDateRangeCollectionJobError,
        match="kr_calendar_date_range_collection_begin_outcome_unknown",
    ):
        await _runner(collector, store).execute(_spec())

    assert collector.calls == []


def test_run_result_cannot_be_constructed_outside_gate() -> None:
    with pytest.raises(
        KrCalendarDateRangeCollectionJobError,
        match="kr_calendar_date_range_collection_result_requires_gate",
    ):
        KrCalendarDateRangeCollectionRunResultV1()


def _runner(
    collector: FakeCollector,
    store: InMemoryKrCalendarCollectionJobStore,
    *,
    attempt_id_factory: AttemptIdFactory | None = None,
) -> RunKrCalendarDateRangeCollectionJob:
    return RunKrCalendarDateRangeCollectionJob(
        collector,
        store,
        holder_id=HOLDER_ID,
        clock=CountingClock(),
        attempt_id_factory=attempt_id_factory or AttemptIdFactory(),
        manual_execution_enabled=True,
    )


def _spec(
    *,
    end_date: date = START_DATE,
) -> KrCalendarCollectionJobSpecV1:
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
    provider: str = "toss",
    observed_at: datetime = STARTED_AT,
    is_open: bool = True,
) -> CollectedKrDailySessionObservationV1:
    next_date = target_date + timedelta(days=1)
    session = PointInTimeKrDailySessionV1.create(
        provider=provider,
        market="KR",
        session_date=target_date,
        is_open=is_open,
        regular_start_at=(
            datetime.combine(target_date, datetime.min.time(), tzinfo=KST) + timedelta(hours=9)
            if is_open
            else None
        ),
        regular_end_at=(
            datetime.combine(target_date, datetime.min.time(), tzinfo=KST)
            + timedelta(hours=15, minutes=30)
            if is_open
            else None
        ),
        next_business_date=next_date,
        next_regular_start_at=datetime.combine(next_date, datetime.min.time(), tzinfo=KST)
        + timedelta(hours=9),
        next_regular_end_at=datetime.combine(next_date, datetime.min.time(), tzinfo=KST)
        + timedelta(hours=15, minutes=30),
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


def _assert_secret_absent(error: BaseException, secret: str) -> None:
    assert secret not in str(error)
    assert secret not in "".join(traceback.format_exception(error))
    assert error.__cause__ is None
    assert error.__context__ is None
