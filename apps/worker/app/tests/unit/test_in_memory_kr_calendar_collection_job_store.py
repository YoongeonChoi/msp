from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any, cast
from uuid import uuid4

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
    KrCalendarCollectionJobStoreError,
    canonical_kr_calendar_collection_job_snapshot,
    kr_calendar_collection_job_manifest_sha256,
)
from app.application.ports.kr_daily_session_collection_port import (
    CollectedKrDailySessionObservationV1,
)
from app.domain.common.time import KST
from app.domain.market_data.point_in_time_calendar import (
    PointInTimeKrDailySessionV1,
)

JOB_ID = "00000000-0000-4000-8000-000000000101"
HOLDER_ID = "00000000-0000-4000-8000-000000000102"
ATTEMPT_1 = "00000000-0000-4000-8000-000000000103"
ATTEMPT_2 = "00000000-0000-4000-8000-000000000104"
START_DATE = date(2026, 3, 25)
CREATED_AT = datetime(2026, 3, 24, 22, 0, tzinfo=UTC)
CONTRACT_SHA256 = "c" * 64


class MutableOffsetTz(tzinfo):
    def __init__(self) -> None:
        self.offset = timedelta(0)

    def utcoffset(self, _value: datetime | None) -> timedelta:
        return self.offset

    def dst(self, _value: datetime | None) -> timedelta:
        return timedelta(0)

    def tzname(self, _value: datetime | None) -> str:
        return "mutable"


async def test_store_advances_contiguous_prefix_and_emits_terminal_manifest() -> None:
    store = InMemoryKrCalendarCollectionJobStore()
    spec = _spec(end_date=START_DATE + timedelta(days=1))
    created = await store.load_or_create_job(spec, now=CREATED_AT)

    first_active = await _begin(
        store,
        created,
        attempt_id=ATTEMPT_1,
        now=CREATED_AT,
    )
    first_done = await _confirm(
        store,
        first_active,
        _collection(START_DATE),
        now=CREATED_AT + timedelta(seconds=1),
    )
    second_active = await _begin(
        store,
        first_done,
        attempt_id=ATTEMPT_2,
        now=CREATED_AT + timedelta(seconds=2),
    )
    completed = await _confirm(
        store,
        second_active,
        _collection(
            START_DATE + timedelta(days=1),
            observed_at=CREATED_AT + timedelta(seconds=2),
        ),
        now=CREATED_AT + timedelta(seconds=3),
    )

    assert created.state == "ready"
    assert first_active.revision == 2
    assert first_active.active_attempt is not None
    assert first_active.active_attempt.fencing_revision == 2
    assert first_done.state == "ready"
    assert first_done.next_date == START_DATE + timedelta(days=1)
    with pytest.raises(
        KrCalendarCollectionJobStoreError,
        match="terminal_manifest_requires_complete_range",
    ):
        kr_calendar_collection_job_manifest_sha256(spec, first_done.checkpoints)
    assert completed.state == "completed"
    assert completed.revision == 5
    assert completed.confirmed_count == 2
    assert completed.remaining_count == 0
    assert completed.next_date is None
    assert completed.terminal_manifest_sha256 == (
        kr_calendar_collection_job_manifest_sha256(spec, completed.checkpoints)
    )
    assert completed.checkpoints[0].holder_id == HOLDER_ID
    assert completed.checkpoints[0].fencing_revision == 2
    assert completed.checkpoints[1].fencing_revision == 4


async def test_pause_requires_new_manual_attempt_and_rejects_old_fence() -> None:
    store = InMemoryKrCalendarCollectionJobStore()
    created = await store.load_or_create_job(_spec(), now=CREATED_AT)
    active = await _begin(store, created, attempt_id=ATTEMPT_1, now=CREATED_AT)
    paused = await store.pause_retryable(
        **_active_args(active),
        reason_code="collection_failed_before_write",
        now=CREATED_AT + timedelta(seconds=1),
    )
    retried = await _begin(
        store,
        paused,
        attempt_id=ATTEMPT_2,
        now=CREATED_AT + timedelta(seconds=2),
    )

    assert paused.state == "paused_retryable"
    assert paused.active_attempt is None
    assert retried.active_attempt is not None
    assert retried.active_attempt.target_date == START_DATE
    with pytest.raises(KrCalendarCollectionJobStoreError):
        await store.confirm_date(
            job_id=created.spec.job_id,
            spec_sha256=created.spec.spec_sha256,
            expected_revision=retried.revision,
            attempt_id=ATTEMPT_1,
            holder_id=HOLDER_ID,
            target_date=START_DATE,
            collection=_collection(START_DATE),
            now=CREATED_AT + timedelta(seconds=3),
        )


async def test_blocked_attempt_has_no_ttl_or_takeover_path() -> None:
    store = InMemoryKrCalendarCollectionJobStore()
    created = await store.load_or_create_job(_spec(), now=CREATED_AT)
    active = await _begin(store, created, attempt_id=ATTEMPT_1, now=CREATED_AT)
    blocked = await store.block_unknown(
        **_active_args(active),
        reason_code="collection_write_outcome_unknown",
        now=CREATED_AT + timedelta(seconds=1),
    )

    assert blocked.state == "blocked_unknown"
    assert blocked.active_attempt == active.active_attempt
    assert blocked.automatic_retry_allowed is False
    with pytest.raises(KrCalendarCollectionJobStoreError):
        await _begin(
            store,
            blocked,
            attempt_id=ATTEMPT_2,
            now=CREATED_AT + timedelta(days=3650),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("expected_revision", 999),
        ("expected_revision", True),
        ("attempt_id", ATTEMPT_2),
        ("holder_id", "00000000-0000-4000-8000-000000000999"),
        ("target_date", START_DATE + timedelta(days=1)),
        ("spec_sha256", "f" * 64),
    ],
)
@pytest.mark.parametrize("operation", ["pause", "block", "confirm"])
async def test_active_mutations_reject_each_stale_fence_without_state_change(
    field: str,
    value: object,
    operation: str,
) -> None:
    store = InMemoryKrCalendarCollectionJobStore()
    created = await store.load_or_create_job(_spec(), now=CREATED_AT)
    active = await _begin(store, created, attempt_id=ATTEMPT_1, now=CREATED_AT)
    args: dict[str, Any] = _active_args(active)
    args[field] = value

    with pytest.raises(KrCalendarCollectionJobStoreError):
        if operation == "pause":
            await store.pause_retryable(
                **args,
                reason_code="collection_failed_before_write",
                now=CREATED_AT + timedelta(seconds=1),
            )
        elif operation == "block":
            await store.block_unknown(
                **args,
                reason_code="collection_write_outcome_unknown",
                now=CREATED_AT + timedelta(seconds=1),
            )
        else:
            await store.confirm_date(
                **args,
                collection=_collection(START_DATE),
                now=CREATED_AT + timedelta(seconds=1),
            )

    assert await store.inspect_job(JOB_ID) == active


async def test_concurrent_begin_has_exactly_one_winner() -> None:
    store = InMemoryKrCalendarCollectionJobStore()
    created = await store.load_or_create_job(_spec(), now=CREATED_AT)

    outcomes = await asyncio.gather(
        _begin(store, created, attempt_id=ATTEMPT_1, now=CREATED_AT),
        _begin(store, created, attempt_id=ATTEMPT_2, now=CREATED_AT),
        return_exceptions=True,
    )

    assert sum(isinstance(item, KrCalendarCollectionJobSnapshotV1) for item in outcomes) == 1
    assert sum(isinstance(item, KrCalendarCollectionJobStoreError) for item in outcomes) == 1
    stored = await store.inspect_job(JOB_ID)
    assert stored is not None
    assert stored.state == "collecting"


async def test_attempt_identifier_cannot_be_reused_after_pause() -> None:
    store = InMemoryKrCalendarCollectionJobStore()
    created = await store.load_or_create_job(_spec(), now=CREATED_AT)
    active = await _begin(store, created, attempt_id=ATTEMPT_1, now=CREATED_AT)
    paused = await store.pause_retryable(
        **_active_args(active),
        reason_code="collection_failed_before_write",
        now=CREATED_AT + timedelta(seconds=1),
    )

    with pytest.raises(
        KrCalendarCollectionJobStoreError,
        match="kr_calendar_collection_job_attempt_reused",
    ):
        await _begin(
            store,
            paused,
            attempt_id=ATTEMPT_1,
            now=CREATED_AT + timedelta(seconds=2),
        )


async def test_same_job_identifier_rejects_conflicting_spec_without_mutation() -> None:
    store = InMemoryKrCalendarCollectionJobStore()
    original = await store.load_or_create_job(_spec(), now=CREATED_AT)
    conflicting = _spec(provider="naver")

    with pytest.raises(KrCalendarCollectionJobStoreError):
        await store.load_or_create_job(
            conflicting,
            now=CREATED_AT + timedelta(days=1),
        )

    assert await store.inspect_job(JOB_ID) == original


async def test_returned_snapshots_are_detached_from_internal_state() -> None:
    store = InMemoryKrCalendarCollectionJobStore()
    created = await store.load_or_create_job(_spec(), now=CREATED_AT)
    object.__setattr__(created.spec, "end_date", START_DATE + timedelta(days=10))
    object.__setattr__(created, "revision", 999)

    stored = await store.inspect_job(JOB_ID)

    assert stored is not None
    assert stored.revision == 1
    assert stored.spec.end_date == START_DATE


async def test_checkpoint_rejects_observation_outside_attempt_window() -> None:
    store = InMemoryKrCalendarCollectionJobStore()
    created = await store.load_or_create_job(_spec(), now=CREATED_AT)
    active = await _begin(store, created, attempt_id=ATTEMPT_1, now=CREATED_AT)

    with pytest.raises(KrCalendarCollectionJobStoreError):
        await _confirm(
            store,
            active,
            _collection(
                START_DATE,
                observed_at=CREATED_AT - timedelta(microseconds=1),
            ),
            now=CREATED_AT + timedelta(seconds=1),
        )

    assert await store.inspect_job(JOB_ID) == active


async def test_mutating_submitted_collection_cannot_change_stored_manifest() -> None:
    store = InMemoryKrCalendarCollectionJobStore()
    created = await store.load_or_create_job(_spec(), now=CREATED_AT)
    active = await _begin(store, created, attempt_id=ATTEMPT_1, now=CREATED_AT)
    submitted = _collection(START_DATE)
    completed = await _confirm(
        store,
        active,
        submitted,
        now=CREATED_AT + timedelta(seconds=1),
    )
    expected_manifest = completed.terminal_manifest_sha256
    object.__setattr__(submitted.session, "canonical_evidence_sha256", "f" * 64)
    object.__setattr__(submitted.receipt, "revision", 999)

    stored = await store.inspect_job(JOB_ID)

    assert stored is not None
    assert stored.terminal_manifest_sha256 == expected_manifest
    assert stored.checkpoints[0].collection.receipt.revision == 1


async def test_store_detaches_mutable_timezone_from_attempt_state() -> None:
    mutable_tz = MutableOffsetTz()
    source_now = datetime(2026, 3, 24, 22, 0, tzinfo=mutable_tz)
    store = InMemoryKrCalendarCollectionJobStore()
    created = await store.load_or_create_job(_spec(), now=source_now)
    active = await _begin(store, created, attempt_id=ATTEMPT_1, now=source_now)
    assert active.active_attempt is not None
    begun_at = active.active_attempt.begun_at
    mutable_tz.offset = timedelta(hours=9)

    stored = await store.inspect_job(JOB_ID)

    assert active.active_attempt.begun_at == begun_at
    assert active.active_attempt.begun_at.tzinfo is UTC
    assert stored is not None
    assert stored.active_attempt is not None
    assert stored.active_attempt.begun_at == begun_at


def test_spec_accepts_one_and_366_days_but_rejects_invalid_ranges() -> None:
    assert _spec().total_days == 1
    assert _spec(end_date=START_DATE + timedelta(days=365)).total_days == 366
    assert _spec(start_date=date.max, end_date=date.max).total_days == 1

    with pytest.raises(KrCalendarCollectionJobStoreError):
        _spec(end_date=START_DATE + timedelta(days=366))
    with pytest.raises(KrCalendarCollectionJobStoreError):
        _spec(start_date=START_DATE + timedelta(days=1), end_date=START_DATE)
    with pytest.raises(KrCalendarCollectionJobStoreError):
        _spec(start_date=cast(date, datetime(2026, 3, 25, tzinfo=UTC)))
    with pytest.raises(KrCalendarCollectionJobStoreError):
        _spec(trigger=cast(Any, "MANUAL"))


def test_snapshot_rejects_tampered_terminal_manifest() -> None:
    store_snapshot = _completed_snapshot()
    object.__setattr__(store_snapshot, "terminal_manifest_sha256", "f" * 64)

    with pytest.raises(KrCalendarCollectionJobStoreError):
        canonical_kr_calendar_collection_job_snapshot(store_snapshot)


@pytest.mark.parametrize("revision", [1, 2, 100])
def test_completed_snapshot_rejects_revision_not_bound_to_last_fence(
    revision: int,
) -> None:
    store_snapshot = _completed_snapshot()
    object.__setattr__(store_snapshot, "revision", revision)

    with pytest.raises(KrCalendarCollectionJobStoreError):
        canonical_kr_calendar_collection_job_snapshot(store_snapshot)


async def test_fresh_in_memory_store_has_no_restart_durability() -> None:
    first = InMemoryKrCalendarCollectionJobStore()
    second = InMemoryKrCalendarCollectionJobStore()
    await first.load_or_create_job(_spec(), now=CREATED_AT)

    assert await first.inspect_job(JOB_ID) is not None
    assert await second.inspect_job(JOB_ID) is None


async def _begin(
    store: InMemoryKrCalendarCollectionJobStore,
    snapshot: KrCalendarCollectionJobSnapshotV1,
    *,
    attempt_id: str,
    now: datetime,
) -> KrCalendarCollectionJobSnapshotV1:
    target_date = snapshot.next_date
    assert target_date is not None
    return await store.begin_date_attempt(
        job_id=snapshot.spec.job_id,
        spec_sha256=snapshot.spec.spec_sha256,
        expected_revision=snapshot.revision,
        attempt_id=attempt_id,
        holder_id=HOLDER_ID,
        target_date=target_date,
        now=now,
    )


async def _confirm(
    store: InMemoryKrCalendarCollectionJobStore,
    snapshot: KrCalendarCollectionJobSnapshotV1,
    collection: CollectedKrDailySessionObservationV1,
    *,
    now: datetime,
) -> KrCalendarCollectionJobSnapshotV1:
    return await store.confirm_date(
        **_active_args(snapshot),
        collection=collection,
        now=now,
    )


def _active_args(snapshot: KrCalendarCollectionJobSnapshotV1) -> dict[str, Any]:
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


def _spec(
    *,
    provider: str = "toss",
    start_date: date = START_DATE,
    end_date: date = START_DATE,
    trigger: Any = "manual",
) -> KrCalendarCollectionJobSpecV1:
    return KrCalendarCollectionJobSpecV1(
        job_id=JOB_ID,
        provider=provider,
        market="KR",
        start_date=start_date,
        end_date=end_date,
        trigger=trigger,
    )


def _collection(
    target_date: date,
    *,
    observed_at: datetime = CREATED_AT,
) -> CollectedKrDailySessionObservationV1:
    next_date = target_date + timedelta(days=1)
    session = PointInTimeKrDailySessionV1.create(
        provider="toss",
        market="KR",
        session_date=target_date,
        is_open=True,
        regular_start_at=datetime.combine(target_date, datetime.min.time(), tzinfo=KST)
        + timedelta(hours=9),
        regular_end_at=datetime.combine(target_date, datetime.min.time(), tzinfo=KST)
        + timedelta(hours=15, minutes=30),
        next_business_date=next_date,
        next_regular_start_at=datetime.combine(next_date, datetime.min.time(), tzinfo=KST)
        + timedelta(hours=9),
        next_regular_end_at=datetime.combine(next_date, datetime.min.time(), tzinfo=KST)
        + timedelta(hours=15, minutes=30),
        observed_at=observed_at,
        provider_contract_sha256=CONTRACT_SHA256,
    )
    receipt = CalendarObservationWriteReceipt(
        status="stored",
        calendar_idempotency_key=session.idempotency_key,
        canonical_evidence_sha256=session.canonical_evidence_sha256,
        revision=1,
        revision_inserted=True,
        occurrence_id=uuid4(),
        occurrence_inserted=True,
        observed_at=session.observed_at,
    )
    return CollectedKrDailySessionObservationV1(session=session, receipt=receipt)


def _completed_snapshot() -> KrCalendarCollectionJobSnapshotV1:
    import asyncio as _asyncio

    async def build() -> KrCalendarCollectionJobSnapshotV1:
        store = InMemoryKrCalendarCollectionJobStore()
        created = await store.load_or_create_job(_spec(), now=CREATED_AT)
        active = await _begin(store, created, attempt_id=ATTEMPT_1, now=CREATED_AT)
        return await _confirm(
            store,
            active,
            _collection(START_DATE),
            now=CREATED_AT + timedelta(seconds=1),
        )

    return _asyncio.run(build())
