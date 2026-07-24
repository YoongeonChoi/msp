from __future__ import annotations

import asyncio
import traceback
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID

import pytest

from app.adapters.persistence.in_memory_candle_observation_store import (
    InMemoryCandleObservationStore,
)
from app.adapters.persistence.in_memory_daily_candle_collection_job_store import (
    InMemoryDailyCandleCollectionJobStore,
)
from app.application.ports.candle_data_port import (
    DailyCandleReadPage,
    DailyCandleReadRequest,
)
from app.application.ports.candle_observation_store_port import (
    CandleObservationStorePersistenceKind,
    CandleObservationWriteReceipt,
)
from app.application.ports.daily_candle_collection_job_store_port import (
    DailyCandleCollectionCompletionV1,
    DailyCandleCollectionJobPersistenceKind,
    DailyCandleCollectionJobSnapshotV1,
    DailyCandleCollectionJobSpecV1,
    DailyCandleCollectionWriteEvidenceV1,
)
from app.application.ports.persistence_authority import (
    PersistenceAuthority,
    persistence_authority_fingerprint,
)
from app.application.use_cases.run_daily_candle_collection_job_once import (
    RunDailyCandleCollectionJobOnce,
    RunDailyCandleCollectionJobOnceError,
)
from app.domain.common.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderSchemaError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    ProviderUnknownError,
)
from app.domain.market_data.point_in_time import PointInTimeCandleV1

BASE = datetime(2026, 7, 23, 0, 0, tzinfo=UTC)
EVENT_AT = BASE - timedelta(days=1)
CONTRACT_SHA = "a" * 64
JOB_ID = "11111111-1111-4111-8111-111111111111"
HOLDER_ID = "22222222-2222-4222-8222-222222222222"
ATTEMPT_ID = UUID("33333333-3333-4333-8333-333333333333")
CONTENT_REVISION_ID = UUID("44444444-4444-4444-8444-444444444444")
OCCURRENCE_ID = UUID("55555555-5555-4555-8555-555555555555")
SECRET = "secret-provider-or-store-detail-must-not-leak"
PERSISTENCE_AUTHORITY = persistence_authority_fingerprint(
    namespace="test-durable",
    origin="https://primary.example.test",
    profile="worker_api",
)
OTHER_PERSISTENCE_AUTHORITY = persistence_authority_fingerprint(
    namespace="test-durable",
    origin="https://other.example.test",
    profile="worker_api",
)

FaultMode = Literal["before", "after", "cancel_before", "cancel_after"]
SourceMode = Literal[
    "success",
    "auth_failure",
    "schema_failure",
    "unknown_failure",
    "runtime_failure",
    "cancel",
    "empty",
    "multiple",
    "wrong_provider",
]


class MonotonicClock:
    def __init__(self, *, cancel_at: set[int] | None = None) -> None:
        self.calls = 0
        self.cancel_at = set() if cancel_at is None else set(cancel_at)

    def __call__(self) -> datetime:
        index = self.calls
        self.calls += 1
        if index in self.cancel_at:
            raise asyncio.CancelledError(SECRET)
        return BASE + timedelta(seconds=index)

    @property
    def next_value(self) -> datetime:
        return BASE + timedelta(seconds=self.calls)


class FakeSource:
    def __init__(
        self,
        trace: list[str],
        clock: MonotonicClock,
        *,
        mode: SourceMode = "success",
        error: BaseException | None = None,
        response_received: bool = False,
    ) -> None:
        self.trace = trace
        self.clock = clock
        self.mode = mode
        self.error = error
        self.response_received = response_received
        self.calls: list[DailyCandleReadRequest] = []
        self.dispatches = 0
        self.responses = 0

    async def read_daily_candle_page(
        self,
        request: DailyCandleReadRequest,
    ) -> DailyCandleReadPage:
        self.trace.append("provider.call")
        self.calls.append(request)
        self.dispatches += 1
        if self.error is not None:
            if self.response_received:
                self.responses += 1
            raise self.error
        if self.mode == "auth_failure":
            self.responses += 1
            raise ProviderAuthError("toss", SECRET)
        if self.mode == "schema_failure":
            self.responses += 1
            raise ProviderSchemaError("toss", SECRET)
        if self.mode == "unknown_failure":
            raise ProviderUnavailableError("toss", SECRET)
        if self.mode == "runtime_failure":
            raise RuntimeError(SECRET)
        if self.mode == "cancel":
            raise asyncio.CancelledError(SECRET)

        observed_at = self.clock.next_value
        candle = _candle(
            request=request,
            observed_at=observed_at,
            provider="other" if self.mode == "wrong_provider" else "toss",
        )
        if self.mode == "empty":
            candles: tuple[PointInTimeCandleV1, ...] = ()
        elif self.mode == "multiple":
            candles = (
                candle,
                _candle(
                    request=request,
                    observed_at=observed_at,
                    event_at=EVENT_AT - timedelta(days=1),
                ),
            )
        else:
            candles = (candle,)
        self.responses += 1
        return DailyCandleReadPage(
            candles=candles,
            next_before=EVENT_AT,
            observed_at=observed_at,
        )


class BlockingSource(FakeSource):
    def __init__(self, trace: list[str], clock: MonotonicClock) -> None:
        super().__init__(trace, clock)
        self.entered = asyncio.Event()

    async def read_daily_candle_page(
        self,
        request: DailyCandleReadRequest,
    ) -> DailyCandleReadPage:
        self.trace.append("provider.call")
        self.calls.append(request)
        self.dispatches += 1
        self.entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class DurableObservationStoreHarness(InMemoryCandleObservationStore):
    persistence_kind: CandleObservationStorePersistenceKind = "durable"
    persistence_authority: PersistenceAuthority = PERSISTENCE_AUTHORITY

    def __init__(
        self,
        trace: list[str],
        *,
        fault: FaultMode | Literal["bad_receipt"] | None = None,
        authority: PersistenceAuthority = PERSISTENCE_AUTHORITY,
    ) -> None:
        super().__init__()
        self.trace = trace
        self.fault = fault
        self.persistence_authority = authority
        self.append_calls = 0

    async def append_observation(
        self,
        candle: PointInTimeCandleV1,
    ) -> CandleObservationWriteReceipt:
        self.append_calls += 1
        self.trace.append("append.call")
        if self.fault == "before":
            raise RuntimeError(SECRET)
        if self.fault == "cancel_before":
            raise asyncio.CancelledError(SECRET)
        receipt = await super().append_observation(candle)
        self.trace.append("append.commit")
        if self.fault == "after":
            raise RuntimeError(SECRET)
        if self.fault == "cancel_after":
            raise asyncio.CancelledError(SECRET)
        if self.fault == "bad_receipt":
            return CandleObservationWriteReceipt(
                idempotency_key=receipt.idempotency_key,
                canonical_observation_sha256="b" * 64,
                revision=receipt.revision,
                inserted=receipt.inserted,
                stored_observed_at=receipt.stored_observed_at,
            )
        return receipt


class CancellingAuthorityObservationStore(DurableObservationStoreHarness):
    def __getattribute__(self, name: str) -> Any:
        if name == "persistence_authority":
            raise asyncio.CancelledError(SECRET)
        return super().__getattribute__(name)


class DurableJobStoreHarness(InMemoryDailyCandleCollectionJobStore):
    persistence_kind: DailyCandleCollectionJobPersistenceKind = "durable"
    persistence_authority: PersistenceAuthority = PERSISTENCE_AUTHORITY

    def __init__(
        self,
        trace: list[str],
        *,
        faults: dict[str, FaultMode] | None = None,
        authority: PersistenceAuthority = PERSISTENCE_AUTHORITY,
    ) -> None:
        super().__init__()
        self.trace = trace
        self.faults = {} if faults is None else dict(faults)
        self.persistence_authority = authority

    def _before(self, operation: str) -> None:
        self.trace.append(f"{operation}.call")
        fault = self.faults.get(operation)
        if fault == "before":
            raise RuntimeError(SECRET)
        if fault == "cancel_before":
            raise asyncio.CancelledError(SECRET)

    def _after(
        self,
        operation: str,
        snapshot: DailyCandleCollectionJobSnapshotV1,
    ) -> DailyCandleCollectionJobSnapshotV1:
        self.trace.append(f"{operation}.commit")
        durable = _durable_snapshot(snapshot)
        fault = self.faults.get(operation)
        if fault == "after":
            raise RuntimeError(SECRET)
        if fault == "cancel_after":
            raise asyncio.CancelledError(SECRET)
        return durable

    async def load_or_create_job(
        self,
        spec: DailyCandleCollectionJobSpecV1,
        *,
        now: datetime,
    ) -> DailyCandleCollectionJobSnapshotV1:
        self._before("load")
        result = await super().load_or_create_job(spec, now=now)
        return self._after("load", result)

    async def begin_attempt(
        self,
        *,
        job_id: str,
        spec_sha256: str,
        expected_revision: int,
        attempt_id: str,
        holder_id: str,
        now: datetime,
    ) -> DailyCandleCollectionJobSnapshotV1:
        self._before("begin")
        result = await super().begin_attempt(
            job_id=job_id,
            spec_sha256=spec_sha256,
            expected_revision=expected_revision,
            attempt_id=attempt_id,
            holder_id=holder_id,
            now=now,
        )
        return self._after("begin", result)

    async def fence_candidate(
        self,
        *,
        job_id: str,
        spec_sha256: str,
        expected_revision: int,
        attempt_id: str,
        holder_id: str,
        fencing_revision: int,
        candle: PointInTimeCandleV1,
        now: datetime,
    ) -> DailyCandleCollectionJobSnapshotV1:
        self._before("fence")
        result = await super().fence_candidate(
            job_id=job_id,
            spec_sha256=spec_sha256,
            expected_revision=expected_revision,
            attempt_id=attempt_id,
            holder_id=holder_id,
            fencing_revision=fencing_revision,
            candle=candle,
            now=now,
        )
        return self._after("fence", result)

    async def pause_retryable(
        self,
        *,
        job_id: str,
        spec_sha256: str,
        expected_revision: int,
        attempt_id: str,
        holder_id: str,
        fencing_revision: int,
        reason_code: str,
        now: datetime,
    ) -> DailyCandleCollectionJobSnapshotV1:
        self._before("pause")
        result = await super().pause_retryable(
            job_id=job_id,
            spec_sha256=spec_sha256,
            expected_revision=expected_revision,
            attempt_id=attempt_id,
            holder_id=holder_id,
            fencing_revision=fencing_revision,
            reason_code=reason_code,
            now=now,
        )
        return self._after("pause", result)

    async def block_unknown(
        self,
        *,
        job_id: str,
        spec_sha256: str,
        expected_revision: int,
        attempt_id: str,
        holder_id: str,
        fencing_revision: int,
        reason_code: str,
        now: datetime,
    ) -> DailyCandleCollectionJobSnapshotV1:
        self._before("block")
        result = await super().block_unknown(
            job_id=job_id,
            spec_sha256=spec_sha256,
            expected_revision=expected_revision,
            attempt_id=attempt_id,
            holder_id=holder_id,
            fencing_revision=fencing_revision,
            reason_code=reason_code,
            now=now,
        )
        return self._after("block", result)

    async def confirm_candidate(
        self,
        *,
        job_id: str,
        spec_sha256: str,
        expected_revision: int,
        attempt_id: str,
        holder_id: str,
        fencing_revision: int,
        receipt: CandleObservationWriteReceipt,
        now: datetime,
    ) -> DailyCandleCollectionJobSnapshotV1:
        self._before("confirm")
        result = await super().confirm_candidate(
            job_id=job_id,
            spec_sha256=spec_sha256,
            expected_revision=expected_revision,
            attempt_id=attempt_id,
            holder_id=holder_id,
            fencing_revision=fencing_revision,
            receipt=receipt,
            now=now,
        )
        return self._after("confirm", result)

    async def inspect_job(
        self,
        job_id: str,
    ) -> DailyCandleCollectionJobSnapshotV1 | None:
        result = await super().inspect_job(job_id)
        return None if result is None else _durable_snapshot(result)


def _durable_snapshot(
    snapshot: DailyCandleCollectionJobSnapshotV1,
) -> DailyCandleCollectionJobSnapshotV1:
    completion = snapshot.completion
    if snapshot.state != "completed" or completion is None:
        return snapshot
    durable_completion = DailyCandleCollectionCompletionV1(
        candidate=completion.candidate,
        write_evidence=DailyCandleCollectionWriteEvidenceV1(
            persistence_kind="durable",
            receipt=completion.write_evidence.receipt,
            content_revision_id=CONTENT_REVISION_ID,
            occurrence_id=OCCURRENCE_ID,
            occurrence_observed_at=completion.candidate.candle.observed_at,
        ),
        confirmed_at=completion.confirmed_at,
    )
    return DailyCandleCollectionJobSnapshotV1(
        spec=snapshot.spec,
        revision=snapshot.revision,
        state=snapshot.state,
        active_attempt=snapshot.active_attempt,
        fenced_candidate=snapshot.fenced_candidate,
        completion=durable_completion,
        state_reason=snapshot.state_reason,
        created_at=snapshot.created_at,
        updated_at=snapshot.updated_at,
    )


def _spec(**overrides: object) -> DailyCandleCollectionJobSpecV1:
    values: dict[str, object] = {
        "job_id": JOB_ID,
        "provider": "toss",
        "symbol": "005930",
        "market": "KR",
        "interval": "1d",
        "adjusted": True,
        "before": BASE,
        "provider_contract_sha256": CONTRACT_SHA,
        "trigger": "manual",
    }
    values.update(overrides)
    return DailyCandleCollectionJobSpecV1(**cast(dict[str, Any], values))


def _candle(
    *,
    request: DailyCandleReadRequest,
    observed_at: datetime,
    provider: str = "toss",
    event_at: datetime = EVENT_AT,
) -> PointInTimeCandleV1:
    return PointInTimeCandleV1.create(
        provider=provider,
        symbol=request.symbol,
        market="KR",
        interval="1d",
        adjusted=request.adjusted,
        provider_event_at=event_at,
        observed_at=observed_at,
        currency="KRW",
        open_krw=71_600,
        high_krw=72_300,
        low_krw=71_500,
        close_krw=72_000,
        volume=3_521_000,
        provider_contract_sha256=CONTRACT_SHA,
    )


def _runner(
    source: FakeSource,
    observation_store: DurableObservationStoreHarness,
    job_store: DurableJobStoreHarness,
    clock: MonotonicClock,
    *,
    enabled: bool = True,
) -> RunDailyCandleCollectionJobOnce:
    return RunDailyCandleCollectionJobOnce(
        source,
        observation_store,
        job_store,
        holder_id=HOLDER_ID,
        clock=clock,
        attempt_id_factory=lambda: ATTEMPT_ID,
        manual_execution_enabled=enabled,
    )


def _assert_secret_absent_from_exception_chain(error: BaseException) -> None:
    rendered = "".join(traceback.format_exception(error))
    assert SECRET not in rendered
    seen: set[int] = set()
    remaining = [error]
    while remaining:
        current = remaining.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        assert SECRET not in str(current)
        if current.__cause__ is not None:
            remaining.append(current.__cause__)
        if current.__context__ is not None:
            remaining.append(current.__context__)


def _assert_sanitized_cancellation(error: BaseException) -> None:
    assert isinstance(error, asyncio.CancelledError)
    assert str(error) == ""
    assert error.__cause__ is None
    assert error.__context__ is None
    _assert_secret_absent_from_exception_chain(error)


async def test_manual_gates_are_zero_io() -> None:
    trace: list[str] = []
    clock = MonotonicClock()
    source = FakeSource(trace, clock)
    observation_store = DurableObservationStoreHarness(trace)
    job_store = DurableJobStoreHarness(trace)

    with pytest.raises(
        RunDailyCandleCollectionJobOnceError,
        match="daily_candle_collection_manual_execution_disabled",
    ):
        await _runner(source, observation_store, job_store, clock, enabled=False).execute(
            _spec(),
            manual_confirmation=True,
        )
    with pytest.raises(
        RunDailyCandleCollectionJobOnceError,
        match="daily_candle_collection_manual_confirmation_required",
    ):
        await _runner(source, observation_store, job_store, clock).execute(_spec())

    assert trace == []
    assert clock.calls == 0


@pytest.mark.parametrize("reference_store", ["job", "observation"])
async def test_reference_store_is_rejected_before_any_io(reference_store: str) -> None:
    trace: list[str] = []
    clock = MonotonicClock()
    source = FakeSource(trace, clock)
    durable_observation = DurableObservationStoreHarness(trace)
    durable_job = DurableJobStoreHarness(trace)
    observation_store = (
        InMemoryCandleObservationStore()
        if reference_store == "observation"
        else durable_observation
    )
    job_store = InMemoryDailyCandleCollectionJobStore() if reference_store == "job" else durable_job
    runner = RunDailyCandleCollectionJobOnce(
        source,
        observation_store,
        job_store,
        holder_id=HOLDER_ID,
        clock=clock,
        attempt_id_factory=lambda: ATTEMPT_ID,
        manual_execution_enabled=True,
    )

    with pytest.raises(
        RunDailyCandleCollectionJobOnceError,
        match=rf"daily_candle_collection_{reference_store}_store_must_be_durable",
    ):
        await runner.execute(_spec(), manual_confirmation=True)

    assert trace == []
    assert clock.calls == 0


async def test_mismatched_durable_store_authority_is_rejected_before_any_io() -> None:
    trace: list[str] = []
    clock = MonotonicClock()
    source = FakeSource(trace, clock)
    observation_store = DurableObservationStoreHarness(
        trace,
        authority=OTHER_PERSISTENCE_AUTHORITY,
    )
    job_store = DurableJobStoreHarness(trace)

    with pytest.raises(
        RunDailyCandleCollectionJobOnceError,
        match="^daily_candle_collection_store_authority_mismatch$",
    ):
        await _runner(source, observation_store, job_store, clock).execute(
            _spec(),
            manual_confirmation=True,
        )

    assert trace == []
    assert clock.calls == 0
    assert source.calls == []
    assert observation_store.append_calls == 0


@pytest.mark.parametrize("invalid_store", ["job", "observation"])
async def test_invalid_durable_store_authority_is_rejected_before_any_io(
    invalid_store: str,
) -> None:
    trace: list[str] = []
    clock = MonotonicClock()
    source = FakeSource(trace, clock)
    observation_store = DurableObservationStoreHarness(trace)
    job_store = DurableJobStoreHarness(trace)
    target: object = job_store if invalid_store == "job" else observation_store
    cast(Any, target).persistence_authority = "not-an-authority"

    with pytest.raises(
        RunDailyCandleCollectionJobOnceError,
        match="^daily_candle_collection_store_authority_invalid$",
    ):
        await _runner(source, observation_store, job_store, clock).execute(
            _spec(),
            manual_confirmation=True,
        )

    assert trace == []
    assert clock.calls == 0
    assert source.calls == []
    assert observation_store.append_calls == 0


async def test_authority_lookup_cancellation_is_sanitized_before_any_io() -> None:
    trace: list[str] = []
    clock = MonotonicClock()
    source = FakeSource(trace, clock)
    observation_store = CancellingAuthorityObservationStore(trace)
    job_store = DurableJobStoreHarness(trace)

    with pytest.raises(asyncio.CancelledError) as captured:
        await _runner(source, observation_store, job_store, clock).execute(
            _spec(),
            manual_confirmation=True,
        )

    _assert_sanitized_cancellation(captured.value)
    assert trace == []
    assert clock.calls == 0
    assert source.calls == []
    assert observation_store.append_calls == 0


async def test_initial_clock_cancellation_is_sanitized_before_store_io() -> None:
    trace: list[str] = []
    clock = MonotonicClock(cancel_at={0})
    source = FakeSource(trace, clock)
    observation_store = DurableObservationStoreHarness(trace)
    job_store = DurableJobStoreHarness(trace)

    with pytest.raises(asyncio.CancelledError) as captured:
        await _runner(source, observation_store, job_store, clock).execute(
            _spec(),
            manual_confirmation=True,
        )

    _assert_sanitized_cancellation(captured.value)
    assert trace == []
    assert clock.calls == 1
    assert source.calls == []
    assert observation_store.append_calls == 0


async def test_attempt_factory_cancellation_is_sanitized_before_begin() -> None:
    trace: list[str] = []
    clock = MonotonicClock()
    source = FakeSource(trace, clock)
    observation_store = DurableObservationStoreHarness(trace)
    job_store = DurableJobStoreHarness(trace)

    def cancel_attempt_factory() -> UUID:
        raise asyncio.CancelledError(SECRET)

    runner = RunDailyCandleCollectionJobOnce(
        source,
        observation_store,
        job_store,
        holder_id=HOLDER_ID,
        clock=clock,
        attempt_id_factory=cancel_attempt_factory,
        manual_execution_enabled=True,
    )
    with pytest.raises(asyncio.CancelledError) as captured:
        await runner.execute(_spec(), manual_confirmation=True)

    snapshot = await job_store.inspect_job(JOB_ID)
    assert snapshot is not None
    assert snapshot.state == "ready"
    _assert_sanitized_cancellation(captured.value)
    assert trace == ["load.call", "load.commit"]
    assert source.calls == []
    assert observation_store.append_calls == 0


async def test_success_orders_fence_before_append_and_replays_without_external_io() -> None:
    trace: list[str] = []
    clock = MonotonicClock()
    source = FakeSource(trace, clock)
    observation_store = DurableObservationStoreHarness(trace)
    job_store = DurableJobStoreHarness(trace)
    runner = _runner(source, observation_store, job_store, clock)

    result = await runner.execute(_spec(), manual_confirmation=True)

    assert result.state == "completed"
    assert result.revision == 4
    assert result.completion is not None
    assert result.completion.write_evidence.persistence_kind == "durable"
    assert result.completion.write_evidence.content_revision_id == CONTENT_REVISION_ID
    assert result.completion.write_evidence.occurrence_id == OCCURRENCE_ID
    assert trace == [
        "load.call",
        "load.commit",
        "begin.call",
        "begin.commit",
        "provider.call",
        "fence.call",
        "fence.commit",
        "append.call",
        "append.commit",
        "confirm.call",
        "confirm.commit",
    ]
    assert len(source.calls) == 1
    assert source.calls[0] == DailyCandleReadRequest(
        symbol="005930",
        before=BASE,
        count=1,
        adjusted=True,
    )
    assert observation_store.append_calls == 1

    replay = await runner.execute(_spec(), manual_confirmation=True)

    assert replay.state == "completed"
    assert len(source.calls) == 1
    assert observation_store.append_calls == 1
    assert trace[-2:] == ["load.call", "load.commit"]


@pytest.mark.parametrize(
    "provider_error",
    [
        ProviderAuthError("toss", SECRET),
        ProviderRateLimitError("toss", SECRET),
    ],
)
async def test_known_provider_failure_pauses_without_automatic_retry(
    provider_error: ProviderError,
) -> None:
    trace: list[str] = []
    clock = MonotonicClock()
    source = FakeSource(
        trace,
        clock,
        error=provider_error,
        response_received=True,
    )
    observation_store = DurableObservationStoreHarness(trace)
    job_store = DurableJobStoreHarness(trace)
    runner = _runner(source, observation_store, job_store, clock)

    with pytest.raises(
        RunDailyCandleCollectionJobOnceError,
        match="^daily_candle_collection_paused_retryable$",
    ) as captured:
        await runner.execute(_spec(), manual_confirmation=True)

    snapshot = await job_store.inspect_job(JOB_ID)
    assert snapshot is not None
    assert snapshot.state == "paused_retryable"
    assert snapshot.state_reason == "provider_read_failed_before_candidate"
    assert observation_store.append_calls == 0
    assert source.dispatches == 1
    assert source.responses == 1
    _assert_secret_absent_from_exception_chain(captured.value)

    replacement_source = FakeSource(trace, clock)
    replacement_runner = _runner(
        replacement_source,
        observation_store,
        job_store,
        clock,
    )
    with pytest.raises(
        RunDailyCandleCollectionJobOnceError,
        match="daily_candle_collection_job_state_not_executable",
    ):
        await replacement_runner.execute(_spec(), manual_confirmation=True)
    assert replacement_source.calls == []
    assert observation_store.append_calls == 0


@pytest.mark.parametrize(
    "provider_error",
    [
        ProviderTimeoutError("toss", SECRET),
        ProviderUnavailableError("toss", SECRET),
        ProviderUnknownError("toss", SECRET),
        ProviderError("toss", SECRET),
        RuntimeError(SECRET),
    ],
)
async def test_ambiguous_provider_failure_blocks_and_never_appends(
    provider_error: BaseException,
) -> None:
    trace: list[str] = []
    clock = MonotonicClock()
    source = FakeSource(trace, clock, error=provider_error)
    observation_store = DurableObservationStoreHarness(trace)
    job_store = DurableJobStoreHarness(trace)

    with pytest.raises(
        RunDailyCandleCollectionJobOnceError,
        match="^daily_candle_collection_blocked_unknown$",
    ) as captured:
        await _runner(source, observation_store, job_store, clock).execute(
            _spec(),
            manual_confirmation=True,
        )

    snapshot = await job_store.inspect_job(JOB_ID)
    assert snapshot is not None
    assert snapshot.state == "blocked_unknown"
    assert snapshot.state_reason == "provider_read_outcome_unknown_before_candidate"
    assert observation_store.append_calls == 0
    assert source.dispatches == 1
    assert source.responses == 0
    _assert_secret_absent_from_exception_chain(captured.value)


@pytest.mark.parametrize(
    "mode",
    ["schema_failure", "empty", "multiple", "wrong_provider"],
)
async def test_invalid_provider_candidate_blocks_before_fence(mode: SourceMode) -> None:
    trace: list[str] = []
    clock = MonotonicClock()
    source = FakeSource(trace, clock, mode=mode)
    observation_store = DurableObservationStoreHarness(trace)
    job_store = DurableJobStoreHarness(trace)

    with pytest.raises(
        RunDailyCandleCollectionJobOnceError,
        match="daily_candle_collection_blocked_unknown",
    ):
        await _runner(source, observation_store, job_store, clock).execute(
            _spec(),
            manual_confirmation=True,
        )

    snapshot = await job_store.inspect_job(JOB_ID)
    assert snapshot is not None
    assert snapshot.state_reason == "unexpected_failure_before_candidate"
    assert "fence.call" not in trace
    assert observation_store.append_calls == 0
    assert source.dispatches == 1
    assert source.responses == 1


async def test_provider_cancellation_is_persisted_and_re_raised() -> None:
    trace: list[str] = []
    clock = MonotonicClock()
    source = FakeSource(trace, clock, mode="cancel")
    observation_store = DurableObservationStoreHarness(trace)
    job_store = DurableJobStoreHarness(trace)

    with pytest.raises(asyncio.CancelledError) as captured:
        await _runner(source, observation_store, job_store, clock).execute(
            _spec(),
            manual_confirmation=True,
        )

    snapshot = await job_store.inspect_job(JOB_ID)
    assert snapshot is not None
    assert snapshot.state == "blocked_unknown"
    assert snapshot.state_reason == "cancelled_before_candidate"
    assert observation_store.append_calls == 0
    _assert_sanitized_cancellation(captured.value)


async def test_cancellation_after_provider_response_blocks_before_fence() -> None:
    trace: list[str] = []
    clock = MonotonicClock(cancel_at={2})
    source = FakeSource(trace, clock)
    observation_store = DurableObservationStoreHarness(trace)
    job_store = DurableJobStoreHarness(trace)

    with pytest.raises(asyncio.CancelledError) as captured:
        await _runner(source, observation_store, job_store, clock).execute(
            _spec(),
            manual_confirmation=True,
        )

    snapshot = await job_store.inspect_job(JOB_ID)
    assert snapshot is not None
    assert snapshot.state == "blocked_unknown"
    assert snapshot.state_reason == "cancelled_before_candidate"
    assert source.dispatches == 1
    assert source.responses == 1
    assert trace.count("block.call") == 1
    assert "fence.call" not in trace
    assert observation_store.append_calls == 0
    _assert_sanitized_cancellation(captured.value)


@pytest.mark.parametrize(
    ("block_fault", "expected_state"),
    [
        ("cancel_before", "collecting"),
        ("cancel_after", "blocked_unknown"),
    ],
)
async def test_cleanup_cancellation_is_sanitized_without_retry(
    block_fault: FaultMode,
    expected_state: str,
) -> None:
    trace: list[str] = []
    clock = MonotonicClock()
    source = FakeSource(trace, clock, mode="cancel")
    observation_store = DurableObservationStoreHarness(trace)
    job_store = DurableJobStoreHarness(trace, faults={"block": block_fault})

    with pytest.raises(asyncio.CancelledError) as captured:
        await _runner(source, observation_store, job_store, clock).execute(
            _spec(),
            manual_confirmation=True,
        )

    snapshot = await job_store.inspect_job(JOB_ID)
    assert snapshot is not None
    assert snapshot.state == expected_state
    _assert_sanitized_cancellation(captured.value)
    assert source.dispatches == 1
    assert trace.count("block.call") == 1
    assert observation_store.append_calls == 0


async def test_task_cancellation_during_provider_read_persists_cleanup() -> None:
    trace: list[str] = []
    clock = MonotonicClock()
    source = BlockingSource(trace, clock)
    observation_store = DurableObservationStoreHarness(trace)
    job_store = DurableJobStoreHarness(trace)
    task = asyncio.create_task(
        _runner(source, observation_store, job_store, clock).execute(
            _spec(),
            manual_confirmation=True,
        )
    )

    await source.entered.wait()
    assert task.cancel()
    with pytest.raises(asyncio.CancelledError) as captured:
        await task

    snapshot = await job_store.inspect_job(JOB_ID)
    assert snapshot is not None
    assert snapshot.state == "blocked_unknown"
    assert snapshot.state_reason == "cancelled_before_candidate"
    assert source.dispatches == 1
    assert source.responses == 0
    assert observation_store.append_calls == 0
    assert trace[-2:] == ["block.call", "block.commit"]
    _assert_sanitized_cancellation(captured.value)


@pytest.mark.parametrize(
    ("fault", "expected_state", "cancelled"),
    [
        ("before", None, False),
        ("after", "ready", False),
        ("cancel_before", None, True),
        ("cancel_after", "ready", True),
    ],
)
async def test_load_failure_or_cancellation_stops_before_provider_without_retry(
    fault: FaultMode,
    expected_state: str | None,
    cancelled: bool,
) -> None:
    trace: list[str] = []
    clock = MonotonicClock()
    source = FakeSource(trace, clock)
    observation_store = DurableObservationStoreHarness(trace)
    job_store = DurableJobStoreHarness(trace, faults={"load": fault})

    error_type: type[BaseException] = (
        asyncio.CancelledError if cancelled else RunDailyCandleCollectionJobOnceError
    )
    with pytest.raises(error_type) as captured:
        await _runner(source, observation_store, job_store, clock).execute(
            _spec(),
            manual_confirmation=True,
        )

    snapshot = await job_store.inspect_job(JOB_ID)
    assert (None if snapshot is None else snapshot.state) == expected_state
    if cancelled:
        _assert_sanitized_cancellation(captured.value)
    else:
        assert str(captured.value) == "daily_candle_collection_job_load_outcome_unknown"
        _assert_secret_absent_from_exception_chain(captured.value)
    assert trace.count("load.call") == 1
    assert source.calls == []
    assert observation_store.append_calls == 0


@pytest.mark.parametrize(
    ("fault", "expected_state", "cancelled"),
    [
        ("before", "blocked_unknown", False),
        ("after", "candidate_fenced", False),
        ("cancel_before", "blocked_unknown", True),
        ("cancel_after", "candidate_fenced", True),
    ],
)
async def test_fence_response_loss_never_reaches_append(
    fault: FaultMode,
    expected_state: str,
    cancelled: bool,
) -> None:
    trace: list[str] = []
    clock = MonotonicClock()
    source = FakeSource(trace, clock)
    observation_store = DurableObservationStoreHarness(trace)
    job_store = DurableJobStoreHarness(trace, faults={"fence": fault})
    runner = _runner(source, observation_store, job_store, clock)

    error_type: type[BaseException] = (
        asyncio.CancelledError if cancelled else RunDailyCandleCollectionJobOnceError
    )
    with pytest.raises(error_type) as captured:
        await runner.execute(_spec(), manual_confirmation=True)

    snapshot = await job_store.inspect_job(JOB_ID)
    assert snapshot is not None
    assert snapshot.state == expected_state
    assert trace.count("fence.call") == 1
    assert observation_store.append_calls == 0
    assert "append.call" not in trace
    if not cancelled:
        _assert_secret_absent_from_exception_chain(captured.value)
    else:
        _assert_sanitized_cancellation(captured.value)

    fresh_source = FakeSource(trace, clock)
    with pytest.raises(RunDailyCandleCollectionJobOnceError):
        await _runner(
            fresh_source,
            observation_store,
            job_store,
            clock,
        ).execute(_spec(), manual_confirmation=True)
    assert fresh_source.calls == []


@pytest.mark.parametrize(
    ("fault", "expected_occurrences", "cancelled", "reason"),
    [
        ("before", 0, False, "append_outcome_unknown"),
        ("after", 1, False, "append_outcome_unknown"),
        ("bad_receipt", 1, False, "append_outcome_unknown"),
        ("cancel_before", 0, True, "cancelled_after_candidate"),
        ("cancel_after", 1, True, "cancelled_after_candidate"),
    ],
)
async def test_append_fault_is_blocked_without_retry(
    fault: FaultMode | Literal["bad_receipt"],
    expected_occurrences: int,
    cancelled: bool,
    reason: str,
) -> None:
    trace: list[str] = []
    clock = MonotonicClock()
    source = FakeSource(trace, clock)
    observation_store = DurableObservationStoreHarness(trace, fault=fault)
    job_store = DurableJobStoreHarness(trace)
    runner = _runner(source, observation_store, job_store, clock)

    error_type: type[BaseException] = (
        asyncio.CancelledError if cancelled else RunDailyCandleCollectionJobOnceError
    )
    with pytest.raises(error_type) as captured:
        await runner.execute(_spec(), manual_confirmation=True)

    snapshot = await job_store.inspect_job(JOB_ID)
    assert snapshot is not None
    assert snapshot.state == "blocked_unknown"
    assert snapshot.state_reason == reason
    assert snapshot.fenced_candidate is not None
    identity = snapshot.fenced_candidate.candle.idempotency_key
    assert len(observation_store.revisions_for(identity)) == expected_occurrences
    assert observation_store.append_calls == 1
    assert "confirm.call" not in trace
    _assert_secret_absent_from_exception_chain(captured.value)
    if cancelled:
        _assert_sanitized_cancellation(captured.value)


@pytest.mark.parametrize(
    ("fault", "expected_state", "cancelled"),
    [
        ("before", "blocked_unknown", False),
        ("after", "completed", False),
        ("cancel_before", "blocked_unknown", True),
        ("cancel_after", "completed", True),
    ],
)
async def test_confirm_response_loss_never_retries_append_or_confirm(
    fault: FaultMode,
    expected_state: str,
    cancelled: bool,
) -> None:
    trace: list[str] = []
    clock = MonotonicClock()
    source = FakeSource(trace, clock)
    observation_store = DurableObservationStoreHarness(trace)
    job_store = DurableJobStoreHarness(trace, faults={"confirm": fault})
    runner = _runner(source, observation_store, job_store, clock)

    error_type: type[BaseException] = (
        asyncio.CancelledError if cancelled else RunDailyCandleCollectionJobOnceError
    )
    with pytest.raises(error_type) as captured:
        await runner.execute(_spec(), manual_confirmation=True)

    snapshot = await job_store.inspect_job(JOB_ID)
    assert snapshot is not None
    assert snapshot.state == expected_state
    if expected_state == "blocked_unknown":
        assert snapshot.state_reason == (
            "cancelled_after_candidate" if cancelled else "confirm_outcome_unknown"
        )
    assert observation_store.append_calls == 1
    assert trace.count("confirm.call") == 1
    if not cancelled:
        _assert_secret_absent_from_exception_chain(captured.value)
    else:
        _assert_sanitized_cancellation(captured.value)

    fresh_source = FakeSource(trace, clock)
    if expected_state == "completed":
        replay = await _runner(
            fresh_source,
            observation_store,
            job_store,
            clock,
        ).execute(_spec(), manual_confirmation=True)
        assert replay.state == "completed"
    else:
        with pytest.raises(RunDailyCandleCollectionJobOnceError):
            await _runner(
                fresh_source,
                observation_store,
                job_store,
                clock,
            ).execute(_spec(), manual_confirmation=True)
    assert fresh_source.calls == []
    assert observation_store.append_calls == 1
    assert trace.count("confirm.call") == 1


@pytest.mark.parametrize(
    ("fault", "expected_state", "cancelled"),
    [
        ("before", "ready", False),
        ("after", "collecting", False),
        ("cancel_before", "ready", True),
        ("cancel_after", "collecting", True),
    ],
)
async def test_begin_failure_or_cancellation_stops_before_provider(
    fault: FaultMode,
    expected_state: str,
    cancelled: bool,
) -> None:
    trace: list[str] = []
    clock = MonotonicClock()
    source = FakeSource(trace, clock)
    observation_store = DurableObservationStoreHarness(trace)
    job_store = DurableJobStoreHarness(trace, faults={"begin": fault})

    error_type: type[BaseException] = (
        asyncio.CancelledError if cancelled else RunDailyCandleCollectionJobOnceError
    )
    with pytest.raises(error_type) as captured:
        await _runner(source, observation_store, job_store, clock).execute(
            _spec(),
            manual_confirmation=True,
        )

    snapshot = await job_store.inspect_job(JOB_ID)
    assert snapshot is not None
    assert snapshot.state == expected_state
    assert trace.count("begin.call") == 1
    assert source.calls == []
    assert observation_store.append_calls == 0
    if not cancelled:
        assert isinstance(captured.value, RunDailyCandleCollectionJobOnceError)
        assert str(captured.value) == "daily_candle_collection_begin_outcome_unknown"
        _assert_secret_absent_from_exception_chain(captured.value)
    else:
        _assert_sanitized_cancellation(captured.value)


@pytest.mark.parametrize(
    ("pause_fault", "expected_state", "cancelled"),
    [
        ("before", "collecting", False),
        ("after", "paused_retryable", False),
        ("cancel_before", "collecting", True),
        ("cancel_after", "paused_retryable", True),
    ],
)
async def test_pause_response_loss_never_authorizes_an_automatic_retry(
    pause_fault: FaultMode,
    expected_state: str,
    cancelled: bool,
) -> None:
    trace: list[str] = []
    clock = MonotonicClock()
    source = FakeSource(trace, clock, mode="auth_failure")
    observation_store = DurableObservationStoreHarness(trace)
    job_store = DurableJobStoreHarness(trace, faults={"pause": pause_fault})

    error_type: type[BaseException] = (
        asyncio.CancelledError if cancelled else RunDailyCandleCollectionJobOnceError
    )
    with pytest.raises(error_type) as captured:
        await _runner(source, observation_store, job_store, clock).execute(
            _spec(),
            manual_confirmation=True,
        )

    snapshot = await job_store.inspect_job(JOB_ID)
    assert snapshot is not None
    assert snapshot.state == expected_state
    assert trace.count("pause.call") == 1
    replacement_source = FakeSource(trace, clock)
    with pytest.raises(RunDailyCandleCollectionJobOnceError):
        await _runner(
            replacement_source,
            observation_store,
            job_store,
            clock,
        ).execute(_spec(), manual_confirmation=True)
    assert replacement_source.calls == []
    assert observation_store.append_calls == 0
    if cancelled:
        _assert_sanitized_cancellation(captured.value)
    else:
        assert str(captured.value) == "daily_candle_collection_pause_outcome_unknown"
        _assert_secret_absent_from_exception_chain(captured.value)


@pytest.mark.parametrize(
    ("block_fault", "expected_state", "cancelled"),
    [
        ("before", "candidate_fenced", False),
        ("after", "blocked_unknown", False),
        ("cancel_before", "candidate_fenced", True),
        ("cancel_after", "blocked_unknown", True),
    ],
)
async def test_block_response_loss_preserves_a_non_executable_state(
    block_fault: FaultMode,
    expected_state: str,
    cancelled: bool,
) -> None:
    trace: list[str] = []
    clock = MonotonicClock()
    source = FakeSource(trace, clock)
    observation_store = DurableObservationStoreHarness(trace, fault="after")
    job_store = DurableJobStoreHarness(trace, faults={"block": block_fault})

    error_type: type[BaseException] = (
        asyncio.CancelledError if cancelled else RunDailyCandleCollectionJobOnceError
    )
    with pytest.raises(error_type) as captured:
        await _runner(source, observation_store, job_store, clock).execute(
            _spec(),
            manual_confirmation=True,
        )

    snapshot = await job_store.inspect_job(JOB_ID)
    assert snapshot is not None
    assert snapshot.state == expected_state
    assert trace.count("block.call") == 1
    assert observation_store.append_calls == 1
    replacement_source = FakeSource(trace, clock)
    with pytest.raises(RunDailyCandleCollectionJobOnceError):
        await _runner(
            replacement_source,
            observation_store,
            job_store,
            clock,
        ).execute(_spec(), manual_confirmation=True)
    assert replacement_source.calls == []
    assert observation_store.append_calls == 1
    if cancelled:
        _assert_sanitized_cancellation(captured.value)
    else:
        assert str(captured.value) == "daily_candle_collection_block_outcome_unknown"
        _assert_secret_absent_from_exception_chain(captured.value)
        assert captured.value.__context__ is None


@pytest.mark.parametrize(
    ("cancel_at", "reason", "append_calls"),
    [
        (3, "cancelled_before_candidate", 0),
        (4, "cancelled_after_candidate", 1),
    ],
)
async def test_cancellation_between_phases_persists_exact_scope(
    cancel_at: int,
    reason: str,
    append_calls: int,
) -> None:
    trace: list[str] = []
    clock = MonotonicClock(cancel_at={cancel_at})
    source = FakeSource(trace, clock)
    observation_store = DurableObservationStoreHarness(trace)
    job_store = DurableJobStoreHarness(trace)

    with pytest.raises(asyncio.CancelledError) as captured:
        await _runner(source, observation_store, job_store, clock).execute(
            _spec(),
            manual_confirmation=True,
        )

    snapshot = await job_store.inspect_job(JOB_ID)
    assert snapshot is not None
    assert snapshot.state == "blocked_unknown"
    assert snapshot.state_reason == reason
    assert observation_store.append_calls == append_calls
    assert "confirm.call" not in trace
    _assert_sanitized_cancellation(captured.value)
