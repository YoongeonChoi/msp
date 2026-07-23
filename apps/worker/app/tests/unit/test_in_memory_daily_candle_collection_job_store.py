from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from typing import Any, cast

import pytest

from app.adapters.persistence.in_memory_daily_candle_collection_job_store import (
    InMemoryDailyCandleCollectionJobStore,
)
from app.application.ports.candle_observation_store_port import (
    CandleObservationWriteReceipt,
)
from app.application.ports.daily_candle_collection_job_store_port import (
    DAILY_CANDLE_COLLECTION_JOB_MAX_BEGIN_EXPECTED_REVISION,
    DAILY_CANDLE_COLLECTION_JOB_MAX_BLOCK_EXPECTED_REVISION,
    DAILY_CANDLE_COLLECTION_JOB_MAX_CONFIRM_EXPECTED_REVISION,
    DAILY_CANDLE_COLLECTION_JOB_MAX_DATABASE_REVISION,
    DAILY_CANDLE_COLLECTION_JOB_MAX_FENCE_EXPECTED_REVISION,
    DAILY_CANDLE_COLLECTION_JOB_MAX_PAUSE_EXPECTED_REVISION,
    DailyCandleCollectionJobInspectorPort,
    DailyCandleCollectionJobSnapshotV1,
    DailyCandleCollectionJobSpecV1,
    DailyCandleCollectionJobStoreError,
    DailyCandleCollectionJobStorePort,
    canonical_daily_candle_collection_job_snapshot,
)
from app.domain.market_data.point_in_time import PointInTimeCandleV1

JOB_ID = "00000000-0000-4000-8000-000000000201"
HOLDER_ID = "00000000-0000-4000-8000-000000000202"
ATTEMPT_1 = "00000000-0000-4000-8000-000000000203"
ATTEMPT_2 = "00000000-0000-4000-8000-000000000204"
PROVIDER_EVENT_AT = datetime(2026, 3, 25, 6, 30, tzinfo=UTC)
CREATED_AT = datetime(2026, 3, 25, 6, 31, tzinfo=UTC)
OBSERVED_AT = CREATED_AT + timedelta(seconds=1)
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


class EqualsEverything:
    def __eq__(self, _other: object) -> bool:
        return True

    def __ne__(self, _other: object) -> bool:
        return False


async def test_store_fences_exact_candidate_before_confirming_receipt() -> None:
    store = InMemoryDailyCandleCollectionJobStore()
    created = await store.load_or_create_job(_spec(), now=CREATED_AT)
    collecting = await _begin(store, created, attempt_id=ATTEMPT_1, now=CREATED_AT)
    candle = _candle()
    fenced = await _fence(
        store,
        collecting,
        candle,
        now=OBSERVED_AT + timedelta(seconds=1),
    )
    assert created.state == "ready"
    assert created.revision == 1
    assert collecting.state == "collecting"
    assert collecting.revision == 2
    assert collecting.active_attempt is not None
    assert collecting.active_attempt.fencing_revision == 2
    assert fenced.state == "candidate_fenced"
    assert fenced.revision == 3
    assert fenced.fenced_candidate is not None
    assert fenced.fenced_candidate.candle == candle
    with pytest.raises(DailyCandleCollectionJobStoreError):
        await _begin(
            store,
            fenced,
            attempt_id=ATTEMPT_2,
            now=OBSERVED_AT + timedelta(seconds=1, microseconds=1),
        )
    completed = await _confirm(
        store,
        fenced,
        _receipt(candle),
        now=OBSERVED_AT + timedelta(seconds=2),
    )
    assert completed.state == "completed"
    assert completed.revision == 4
    assert completed.active_attempt is None
    assert completed.fenced_candidate is None
    assert completed.completion is not None
    assert completed.completion.candidate.candle == candle
    assert completed.completion.write_evidence.persistence_kind == "reference"
    assert completed.completion.write_evidence.receipt == _receipt(candle)
    assert completed.completion.write_evidence.content_revision_id.version == 4
    assert completed.completion.write_evidence.occurrence_id.version == 4
    assert (
        completed.completion.write_evidence.content_revision_id
        != completed.completion.write_evidence.occurrence_id
    )
    assert completed.automatic_retry_allowed is False
    with pytest.raises(DailyCandleCollectionJobStoreError):
        await _begin(
            store,
            completed,
            attempt_id=ATTEMPT_2,
            now=OBSERVED_AT + timedelta(days=3650),
        )

    object.__setattr__(
        completed,
        "created_at",
        completed.completion.candidate.attempt.begun_at + timedelta(microseconds=1),
    )
    with pytest.raises(
        DailyCandleCollectionJobStoreError,
        match="daily_candle_collection_job_snapshot_invalid",
    ):
        canonical_daily_candle_collection_job_snapshot(completed)


async def test_pause_before_candidate_requires_new_manual_attempt_and_fence() -> None:
    store = InMemoryDailyCandleCollectionJobStore()
    created = await store.load_or_create_job(_spec(), now=CREATED_AT)
    collecting = await _begin(store, created, attempt_id=ATTEMPT_1, now=CREATED_AT)
    paused = await store.pause_retryable(
        **_active_args(collecting),
        reason_code="provider_read_failed_before_candidate",
        now=CREATED_AT + timedelta(seconds=1),
    )
    with pytest.raises(
        DailyCandleCollectionJobStoreError,
        match="daily_candle_collection_job_attempt_reused",
    ):
        await _begin(
            store,
            paused,
            attempt_id=ATTEMPT_1,
            now=CREATED_AT + timedelta(seconds=2),
        )
    retried = await _begin(
        store,
        paused,
        attempt_id=ATTEMPT_2,
        now=CREATED_AT + timedelta(seconds=3),
    )

    assert paused.state == "paused_retryable"
    assert paused.revision == 3
    assert paused.active_attempt is None
    assert paused.automatic_retry_allowed is False
    assert retried.state == "collecting"
    assert retried.revision == 4
    assert retried.active_attempt is not None
    assert retried.active_attempt.fencing_revision == 4


async def test_pause_rejects_unclassified_reason_without_state_change() -> None:
    store = InMemoryDailyCandleCollectionJobStore()
    created = await store.load_or_create_job(_spec(), now=CREATED_AT)
    collecting = await _begin(store, created, attempt_id=ATTEMPT_1, now=CREATED_AT)

    with pytest.raises(
        DailyCandleCollectionJobStoreError,
        match="daily_candle_collection_job_pause_reason_invalid",
    ):
        await store.pause_retryable(
            **_active_args(collecting),
            reason_code="unexpected_failure_before_candidate",
            now=CREATED_AT + timedelta(seconds=1),
        )

    assert await store.inspect_job(JOB_ID) == collecting


async def test_active_transition_rejects_non_string_identity_objects() -> None:
    store = InMemoryDailyCandleCollectionJobStore()
    created = await store.load_or_create_job(_spec(), now=CREATED_AT)
    collecting = await _begin(store, created, attempt_id=ATTEMPT_1, now=CREATED_AT)
    arguments = _active_args(collecting)
    arguments["attempt_id"] = EqualsEverything()
    arguments["holder_id"] = EqualsEverything()

    with pytest.raises(
        DailyCandleCollectionJobStoreError,
        match="daily_candle_collection_job_store_attempt_id_invalid",
    ):
        await store.pause_retryable(
            **arguments,
            reason_code="provider_read_failed_before_candidate",
            now=CREATED_AT + timedelta(seconds=1),
        )

    assert await store.inspect_job(JOB_ID) == collecting


async def test_transition_revision_headroom_rejects_without_state_change() -> None:
    store = InMemoryDailyCandleCollectionJobStore()
    created = await store.load_or_create_job(_spec(), now=CREATED_AT)

    with pytest.raises(
        DailyCandleCollectionJobStoreError,
        match="daily_candle_collection_job_revision_exhausted",
    ):
        await store.begin_attempt(
            job_id=JOB_ID,
            spec_sha256=created.spec.spec_sha256,
            expected_revision=(DAILY_CANDLE_COLLECTION_JOB_MAX_BEGIN_EXPECTED_REVISION + 1),
            attempt_id=ATTEMPT_1,
            holder_id=HOLDER_ID,
            now=CREATED_AT,
        )
    assert await store.inspect_job(JOB_ID) == created

    collecting = await _begin(store, created, attempt_id=ATTEMPT_1, now=CREATED_AT)
    transitions = (
        (
            store.fence_candidate,
            DAILY_CANDLE_COLLECTION_JOB_MAX_FENCE_EXPECTED_REVISION + 1,
            {"candle": _candle()},
        ),
        (
            store.pause_retryable,
            DAILY_CANDLE_COLLECTION_JOB_MAX_PAUSE_EXPECTED_REVISION + 1,
            {"reason_code": "provider_read_failed_before_candidate"},
        ),
        (
            store.block_unknown,
            DAILY_CANDLE_COLLECTION_JOB_MAX_BLOCK_EXPECTED_REVISION + 1,
            {"reason_code": "unexpected_failure_before_candidate"},
        ),
        (
            store.confirm_candidate,
            DAILY_CANDLE_COLLECTION_JOB_MAX_CONFIRM_EXPECTED_REVISION + 1,
            {"receipt": _receipt(_candle())},
        ),
    )
    for transition, exhausted_revision, extra in transitions:
        arguments = _active_args(collecting)
        arguments["expected_revision"] = exhausted_revision
        with pytest.raises(
            DailyCandleCollectionJobStoreError,
            match="daily_candle_collection_job_revision_exhausted",
        ):
            await transition(
                **arguments,
                **extra,
                now=CREATED_AT + timedelta(seconds=1),
            )

    assert await store.inspect_job(JOB_ID) == collecting


async def test_transition_rejects_unreachable_database_max_revision() -> None:
    store = InMemoryDailyCandleCollectionJobStore()
    created = await store.load_or_create_job(_spec(), now=CREATED_AT)

    with pytest.raises(
        DailyCandleCollectionJobStoreError,
        match="daily_candle_collection_job_expected_revision_invalid",
    ):
        await store.begin_attempt(
            job_id=JOB_ID,
            spec_sha256=created.spec.spec_sha256,
            expected_revision=DAILY_CANDLE_COLLECTION_JOB_MAX_DATABASE_REVISION,
            attempt_id=ATTEMPT_1,
            holder_id=HOLDER_ID,
            now=CREATED_AT,
        )

    assert await store.inspect_job(JOB_ID) == created


async def test_unknown_append_preserves_candidate_without_ttl_or_takeover() -> None:
    store = InMemoryDailyCandleCollectionJobStore()
    created = await store.load_or_create_job(_spec(), now=CREATED_AT)
    collecting = await _begin(store, created, attempt_id=ATTEMPT_1, now=CREATED_AT)
    fenced = await _fence(
        store,
        collecting,
        _candle(),
        now=OBSERVED_AT + timedelta(seconds=1),
    )
    blocked = await store.block_unknown(
        **_active_args(fenced),
        reason_code="append_outcome_unknown",
        now=OBSERVED_AT + timedelta(seconds=2),
    )

    assert blocked.state == "blocked_unknown"
    assert blocked.revision == 4
    assert blocked.active_attempt == fenced.active_attempt
    assert blocked.fenced_candidate == fenced.fenced_candidate
    assert blocked.automatic_retry_allowed is False
    with pytest.raises(DailyCandleCollectionJobStoreError):
        await _begin(
            store,
            blocked,
            attempt_id=ATTEMPT_2,
            now=CREATED_AT + timedelta(days=3650),
        )
    with pytest.raises(DailyCandleCollectionJobStoreError):
        await _confirm(
            store,
            blocked,
            _receipt(_candle()),
            now=CREATED_AT + timedelta(days=3650, seconds=1),
        )


async def test_unexpected_pre_candidate_failure_blocks_without_retry_or_takeover() -> None:
    store = InMemoryDailyCandleCollectionJobStore()
    created = await store.load_or_create_job(_spec(), now=CREATED_AT)
    collecting = await _begin(store, created, attempt_id=ATTEMPT_1, now=CREATED_AT)
    blocked = await store.block_unknown(
        **_active_args(collecting),
        reason_code="unexpected_failure_before_candidate",
        now=CREATED_AT + timedelta(seconds=1),
    )

    assert blocked.state == "blocked_unknown"
    assert blocked.revision == 3
    assert blocked.active_attempt == collecting.active_attempt
    assert blocked.fenced_candidate is None
    assert blocked.automatic_retry_allowed is False
    with pytest.raises(DailyCandleCollectionJobStoreError):
        await _begin(
            store,
            blocked,
            attempt_id=ATTEMPT_2,
            now=CREATED_AT + timedelta(days=3650),
        )


@pytest.mark.parametrize(
    ("after_candidate", "reason_code"),
    [
        (False, "append_outcome_unknown"),
        (True, "unexpected_failure_before_candidate"),
        (False, "unclassified_unknown"),
    ],
)
async def test_block_rejects_wrong_phase_or_unclassified_reason_without_mutation(
    after_candidate: bool,
    reason_code: str,
) -> None:
    store = InMemoryDailyCandleCollectionJobStore()
    created = await store.load_or_create_job(_spec(), now=CREATED_AT)
    current = await _begin(store, created, attempt_id=ATTEMPT_1, now=CREATED_AT)
    transition_at = CREATED_AT + timedelta(seconds=1)
    if after_candidate:
        current = await _fence(
            store,
            current,
            _candle(),
            now=OBSERVED_AT + timedelta(seconds=1),
        )
        transition_at = OBSERVED_AT + timedelta(seconds=2)

    with pytest.raises(
        DailyCandleCollectionJobStoreError,
        match="daily_candle_collection_job_block_reason_invalid",
    ):
        await store.block_unknown(
            **_active_args(current),
            reason_code=reason_code,
            now=transition_at,
        )

    assert await store.inspect_job(JOB_ID) == current


async def test_concurrent_begin_has_exactly_one_cas_winner() -> None:
    store = InMemoryDailyCandleCollectionJobStore()
    created = await store.load_or_create_job(_spec(), now=CREATED_AT)

    outcomes = await asyncio.gather(
        _begin(store, created, attempt_id=ATTEMPT_1, now=CREATED_AT),
        _begin(store, created, attempt_id=ATTEMPT_2, now=CREATED_AT),
        return_exceptions=True,
    )

    assert sum(isinstance(item, DailyCandleCollectionJobSnapshotV1) for item in outcomes) == 1
    assert sum(isinstance(item, DailyCandleCollectionJobStoreError) for item in outcomes) == 1
    stored = await store.inspect_job(JOB_ID)
    assert stored is not None
    assert stored.state == "collecting"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("expected_revision", 999),
        ("expected_revision", True),
        ("attempt_id", ATTEMPT_2),
        ("holder_id", "00000000-0000-4000-8000-000000000299"),
        ("fencing_revision", 999),
        ("fencing_revision", True),
        ("spec_sha256", "f" * 64),
    ],
)
async def test_candidate_mutations_reject_stale_cas_and_fence_without_change(
    field: str,
    value: object,
) -> None:
    store = InMemoryDailyCandleCollectionJobStore()
    created = await store.load_or_create_job(_spec(), now=CREATED_AT)
    collecting = await _begin(store, created, attempt_id=ATTEMPT_1, now=CREATED_AT)
    fenced = await _fence(
        store,
        collecting,
        _candle(),
        now=OBSERVED_AT + timedelta(seconds=1),
    )
    args: dict[str, Any] = _active_args(fenced)
    args[field] = value

    with pytest.raises(DailyCandleCollectionJobStoreError):
        await store.confirm_candidate(
            **args,
            receipt=_receipt(_candle()),
            now=OBSERVED_AT + timedelta(seconds=2),
        )

    assert await store.inspect_job(JOB_ID) == fenced


async def test_fence_rejects_candle_outside_exact_spec_without_mutation() -> None:
    store = InMemoryDailyCandleCollectionJobStore()
    created = await store.load_or_create_job(_spec(), now=CREATED_AT)
    collecting = await _begin(store, created, attempt_id=ATTEMPT_1, now=CREATED_AT)
    wrong_event = _candle(provider_event_at=CREATED_AT + timedelta(microseconds=1))

    with pytest.raises(
        DailyCandleCollectionJobStoreError,
        match="daily_candle_collection_job_candidate_scope_mismatch",
    ):
        await _fence(
            store,
            collecting,
            wrong_event,
            now=OBSERVED_AT + timedelta(seconds=1),
        )

    assert await store.inspect_job(JOB_ID) == collecting


@pytest.mark.parametrize(
    "mismatch",
    ["identity", "observation", "stored_observed_at"],
)
async def test_confirm_requires_receipt_to_match_exact_fenced_candle(
    mismatch: str,
) -> None:
    store = InMemoryDailyCandleCollectionJobStore()
    created = await store.load_or_create_job(_spec(), now=CREATED_AT)
    collecting = await _begin(store, created, attempt_id=ATTEMPT_1, now=CREATED_AT)
    candle = _candle()
    fenced = await _fence(
        store,
        collecting,
        candle,
        now=OBSERVED_AT + timedelta(seconds=1),
    )
    receipt = _receipt(candle)
    if mismatch == "identity":
        receipt = _receipt(candle, idempotency_key="f" * 64)
    elif mismatch == "observation":
        receipt = _receipt(candle, canonical_observation_sha256="f" * 64)
    else:
        receipt = _receipt(
            candle,
            stored_observed_at=OBSERVED_AT + timedelta(seconds=1),
        )

    with pytest.raises(
        DailyCandleCollectionJobStoreError,
        match="daily_candle_collection_job_receipt_mismatch",
    ):
        await _confirm(
            store,
            fenced,
            receipt,
            now=OBSERVED_AT + timedelta(seconds=2),
        )

    assert await store.inspect_job(JOB_ID) == fenced


async def test_exact_content_replay_accepts_older_stored_revision_observation() -> None:
    store = InMemoryDailyCandleCollectionJobStore()
    created = await store.load_or_create_job(_spec(), now=CREATED_AT)
    collecting = await _begin(store, created, attempt_id=ATTEMPT_1, now=CREATED_AT)
    candle = _candle()
    fenced = await _fence(
        store,
        collecting,
        candle,
        now=OBSERVED_AT + timedelta(seconds=1),
    )
    earlier_stored_at = candle.observed_at - timedelta(microseconds=1)
    completed = await _confirm(
        store,
        fenced,
        _receipt(candle, stored_observed_at=earlier_stored_at),
        now=OBSERVED_AT + timedelta(seconds=2),
    )

    assert completed.completion is not None
    assert completed.completion.write_evidence.receipt.stored_observed_at == earlier_stored_at
    assert completed.completion.write_evidence.occurrence_observed_at == candle.observed_at


async def test_returned_snapshots_and_submitted_candidate_are_detached() -> None:
    store = InMemoryDailyCandleCollectionJobStore()
    spec = _spec()
    created = await store.load_or_create_job(spec, now=CREATED_AT)
    collecting = await _begin(store, created, attempt_id=ATTEMPT_1, now=CREATED_AT)
    submitted = _candle()
    fenced = await _fence(
        store,
        collecting,
        submitted,
        now=OBSERVED_AT + timedelta(seconds=1),
    )
    object.__setattr__(spec, "symbol", "999999")
    object.__setattr__(submitted, "close_krw", 999_999)
    object.__setattr__(fenced, "revision", 999)
    assert fenced.fenced_candidate is not None
    object.__setattr__(fenced.fenced_candidate.candle, "volume", 999_999)

    stored = await store.inspect_job(JOB_ID)

    assert stored is not None
    assert stored.revision == 3
    assert stored.spec.symbol == "005930"
    assert stored.fenced_candidate is not None
    assert stored.fenced_candidate.candle.close_krw == 70_500
    assert stored.fenced_candidate.candle.volume == 1_000_000


async def test_store_detaches_mutable_timezone_from_spec_and_attempt() -> None:
    mutable_tz = MutableOffsetTz()
    before = datetime(2026, 3, 25, 6, 31, tzinfo=mutable_tz)
    source_now = datetime(2026, 3, 25, 6, 31, tzinfo=mutable_tz)
    store = InMemoryDailyCandleCollectionJobStore()
    spec = _spec(before=before)
    created = await store.load_or_create_job(spec, now=source_now)
    collecting = await _begin(store, created, attempt_id=ATTEMPT_1, now=source_now)
    mutable_tz.offset = timedelta(hours=9)

    stored = await store.inspect_job(JOB_ID)

    assert stored is not None
    assert stored.spec.before == CREATED_AT
    assert stored.spec.before.tzinfo is UTC
    assert stored.active_attempt is not None
    assert stored.active_attempt.begun_at == CREATED_AT
    assert stored.active_attempt.begun_at.tzinfo is UTC
    assert collecting.active_attempt is not None
    assert collecting.active_attempt.begun_at == CREATED_AT


async def test_same_job_id_rejects_conflicting_spec_without_mutation() -> None:
    store = InMemoryDailyCandleCollectionJobStore()
    original = await store.load_or_create_job(_spec(), now=CREATED_AT)

    with pytest.raises(
        DailyCandleCollectionJobStoreError,
        match="daily_candle_collection_job_spec_conflict",
    ):
        await store.load_or_create_job(
            _spec(symbol="000660"),
            now=CREATED_AT + timedelta(days=1),
        )

    assert await store.inspect_job(JOB_ID) == original


def test_spec_and_snapshot_reject_noncanonical_types_and_tampering() -> None:
    with pytest.raises(DailyCandleCollectionJobStoreError):
        _spec(adjusted=cast(Any, 1))
    with pytest.raises(DailyCandleCollectionJobStoreError):
        _spec(count=cast(Any, True))
    with pytest.raises(DailyCandleCollectionJobStoreError):
        _spec(pagination_allowed=cast(Any, True))
    with pytest.raises(DailyCandleCollectionJobStoreError):
        _spec(automatic_retry_allowed=cast(Any, True))
    with pytest.raises(DailyCandleCollectionJobStoreError):
        _spec(job_id="00000000-0000-1000-8000-000000000201")

    snapshot = DailyCandleCollectionJobSnapshotV1(
        spec=_spec(),
        revision=1,
        state="ready",
        active_attempt=None,
        fenced_candidate=None,
        completion=None,
        state_reason=None,
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
    )
    object.__setattr__(snapshot, "automatic_retry_allowed", True)
    with pytest.raises(DailyCandleCollectionJobStoreError):
        canonical_daily_candle_collection_job_snapshot(snapshot)


def test_spec_hash_binds_variable_fields_and_normalizes_equivalent_timezone() -> None:
    baseline = _spec()
    equivalent = _spec(
        before=baseline.before.astimezone(timezone(timedelta(hours=9))),
    )
    variants = (
        _spec(job_id="00000000-0000-4000-8000-000000000299"),
        _spec(provider="toss-alt"),
        _spec(symbol="000660"),
        _spec(adjusted=False),
        _spec(before=baseline.before - timedelta(microseconds=1)),
        _spec(provider_contract_sha256="d" * 64),
    )

    assert equivalent.spec_sha256 == baseline.spec_sha256
    assert all(candidate.spec_sha256 != baseline.spec_sha256 for candidate in variants)


async def test_fresh_reference_store_has_no_restart_durability() -> None:
    first = InMemoryDailyCandleCollectionJobStore()
    second = InMemoryDailyCandleCollectionJobStore()
    await first.load_or_create_job(_spec(), now=CREATED_AT)

    assert await first.inspect_job(JOB_ID) is not None
    assert await second.inspect_job(JOB_ID) is None


async def test_store_implements_operation_and_inspector_ports() -> None:
    store: DailyCandleCollectionJobStorePort = InMemoryDailyCandleCollectionJobStore()
    inspector: DailyCandleCollectionJobInspectorPort = cast(
        InMemoryDailyCandleCollectionJobStore,
        store,
    )

    with pytest.raises(
        DailyCandleCollectionJobStoreError,
        match="job_id_invalid",
    ):
        await inspector.inspect_job("00000000-0000-1000-8000-000000000201")

    assert store.persistence_kind == "reference"
    assert await inspector.inspect_job(JOB_ID) is None


async def _begin(
    store: InMemoryDailyCandleCollectionJobStore,
    snapshot: DailyCandleCollectionJobSnapshotV1,
    *,
    attempt_id: str,
    now: datetime,
) -> DailyCandleCollectionJobSnapshotV1:
    return await store.begin_attempt(
        job_id=snapshot.spec.job_id,
        spec_sha256=snapshot.spec.spec_sha256,
        expected_revision=snapshot.revision,
        attempt_id=attempt_id,
        holder_id=HOLDER_ID,
        now=now,
    )


async def _fence(
    store: InMemoryDailyCandleCollectionJobStore,
    snapshot: DailyCandleCollectionJobSnapshotV1,
    candle: PointInTimeCandleV1,
    *,
    now: datetime,
) -> DailyCandleCollectionJobSnapshotV1:
    return await store.fence_candidate(
        **_active_args(snapshot),
        candle=candle,
        now=now,
    )


async def _confirm(
    store: InMemoryDailyCandleCollectionJobStore,
    snapshot: DailyCandleCollectionJobSnapshotV1,
    receipt: CandleObservationWriteReceipt,
    *,
    now: datetime,
) -> DailyCandleCollectionJobSnapshotV1:
    return await store.confirm_candidate(
        **_active_args(snapshot),
        receipt=receipt,
        now=now,
    )


def _active_args(snapshot: DailyCandleCollectionJobSnapshotV1) -> dict[str, Any]:
    attempt = snapshot.active_attempt
    assert attempt is not None
    return {
        "job_id": snapshot.spec.job_id,
        "spec_sha256": snapshot.spec.spec_sha256,
        "expected_revision": snapshot.revision,
        "attempt_id": attempt.attempt_id,
        "holder_id": attempt.holder_id,
        "fencing_revision": attempt.fencing_revision,
    }


def _spec(
    *,
    job_id: str = JOB_ID,
    provider: str = "toss",
    symbol: str = "005930",
    adjusted: Any = True,
    before: datetime = CREATED_AT,
    count: Any = 1,
    pagination_allowed: Any = False,
    automatic_retry_allowed: Any = False,
    provider_contract_sha256: str = CONTRACT_SHA256,
) -> DailyCandleCollectionJobSpecV1:
    return DailyCandleCollectionJobSpecV1(
        job_id=job_id,
        provider=provider,
        symbol=symbol,
        market="KR",
        interval="1d",
        adjusted=adjusted,
        before=before,
        provider_contract_sha256=provider_contract_sha256,
        trigger="manual",
        count=count,
        pagination_allowed=pagination_allowed,
        automatic_retry_allowed=automatic_retry_allowed,
    )


def _candle(
    *,
    provider_event_at: datetime = PROVIDER_EVENT_AT,
    observed_at: datetime = OBSERVED_AT,
) -> PointInTimeCandleV1:
    return PointInTimeCandleV1.create(
        provider="toss",
        symbol="005930",
        market="KR",
        interval="1d",
        adjusted=True,
        provider_event_at=provider_event_at,
        observed_at=observed_at,
        currency="KRW",
        open_krw=70_000,
        high_krw=71_000,
        low_krw=69_500,
        close_krw=70_500,
        volume=1_000_000,
        provider_contract_sha256=CONTRACT_SHA256,
    )


def _receipt(
    candle: PointInTimeCandleV1,
    *,
    idempotency_key: str | None = None,
    canonical_observation_sha256: str | None = None,
    stored_observed_at: datetime | None = None,
) -> CandleObservationWriteReceipt:
    return CandleObservationWriteReceipt(
        idempotency_key=idempotency_key or candle.idempotency_key,
        canonical_observation_sha256=(
            canonical_observation_sha256 or candle.canonical_observation_sha256
        ),
        revision=1,
        inserted=True,
        stored_observed_at=stored_observed_at or candle.observed_at,
    )
