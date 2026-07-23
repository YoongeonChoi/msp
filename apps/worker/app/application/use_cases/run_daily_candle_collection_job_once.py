from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.application.ports.candle_data_port import (
    DailyCandleReadPage,
    DailyCandleReadRequest,
    DailyCandleSourcePort,
)
from app.application.ports.candle_observation_store_port import (
    CandleObservationStorePort,
    CandleObservationWriteReceipt,
)
from app.application.ports.daily_candle_collection_job_store_port import (
    DailyCandleCollectionAttemptV1,
    DailyCandleCollectionJobSnapshotV1,
    DailyCandleCollectionJobSpecV1,
    DailyCandleCollectionJobStorePort,
    canonical_daily_candle_collection_job_snapshot,
    canonical_daily_candle_collection_job_spec,
)
from app.application.ports.persistence_authority import is_persistence_authority
from app.application.services.data_collection_service import (
    CandleCollectionError,
    DataCollectionService,
)
from app.domain.common.errors import KnownFailClosedError
from app.domain.common.time import now_utc
from app.domain.market_data.point_in_time import PointInTimeCandleV1


class RunDailyCandleCollectionJobOnceError(KnownFailClosedError):
    def __init__(self, safe_message: str) -> None:
        super().__init__("run_daily_candle_collection_job_once", safe_message)


class RunDailyCandleCollectionJobOnce:
    """Run one explicit, durable, fenced daily-candle collection attempt.

    This use case is intentionally absent from the normal Worker runtime. It
    never retries a provider read, append, or confirmation, and it accepts only
    a fresh ready job. Paused or unresolved jobs require a separate recovery
    assessment before a future use case may make them executable.
    """

    def __init__(
        self,
        source: DailyCandleSourcePort,
        observation_store: CandleObservationStorePort,
        job_store: DailyCandleCollectionJobStorePort,
        *,
        holder_id: str,
        clock: Callable[[], datetime] = now_utc,
        attempt_id_factory: Callable[[], UUID] = uuid4,
        manual_execution_enabled: bool = False,
    ) -> None:
        self.source = source
        self.observation_store = observation_store
        self.job_store = job_store
        self.holder_id = _uuid4_text(holder_id, "holder_id")
        self.clock = clock
        self.attempt_id_factory = attempt_id_factory
        self.manual_execution_enabled = manual_execution_enabled
        self.collector = DataCollectionService(source, clock=clock)

    async def execute(
        self,
        spec: DailyCandleCollectionJobSpecV1,
        *,
        manual_confirmation: bool = False,
    ) -> DailyCandleCollectionJobSnapshotV1:
        result: DailyCandleCollectionJobSnapshotV1 | None = None
        cancelled = False
        try:
            result = await self._execute(
                spec,
                manual_confirmation=manual_confirmation,
            )
        except asyncio.CancelledError:
            cancelled = True
        if cancelled:
            raise asyncio.CancelledError
        if result is None:
            raise RunDailyCandleCollectionJobOnceError(
                "daily_candle_collection_execution_outcome_unknown"
            )
        return result

    async def _execute(
        self,
        spec: DailyCandleCollectionJobSpecV1,
        *,
        manual_confirmation: bool,
    ) -> DailyCandleCollectionJobSnapshotV1:
        if self.manual_execution_enabled is not True:
            raise RunDailyCandleCollectionJobOnceError(
                "daily_candle_collection_manual_execution_disabled"
            )
        if manual_confirmation is not True:
            raise RunDailyCandleCollectionJobOnceError(
                "daily_candle_collection_manual_confirmation_required"
            )
        _require_durable_store(self.job_store, "job")
        _require_durable_store(self.observation_store, "observation")
        _require_shared_persistence_authority(
            job_store=self.job_store,
            observation_store=self.observation_store,
        )
        canonical_spec = _spec(spec)

        transition_at = _read_clock(self.clock)
        snapshot = await self._load(canonical_spec, now=transition_at)
        if snapshot.state == "completed":
            _require_durable_completion(snapshot)
            return snapshot
        if snapshot.state != "ready":
            raise RunDailyCandleCollectionJobOnceError(
                "daily_candle_collection_job_state_not_executable"
            )

        attempt_id = _new_attempt_id(self.attempt_id_factory)
        active = await self._begin(
            snapshot,
            attempt_id=attempt_id,
            now=transition_at,
        )
        page = await self._read_one(active)
        candle = await self._candidate_or_block(active, page)

        fenced_at: datetime | None = None
        clock_cancelled = False
        clock_failed = False
        try:
            fenced_at = _read_clock(self.clock)
        except asyncio.CancelledError:
            clock_cancelled = True
        except RunDailyCandleCollectionJobOnceError:
            clock_failed = True
        if clock_cancelled:
            await self._best_effort_block(
                active,
                reason_code="cancelled_before_candidate",
            )
            raise asyncio.CancelledError
        if clock_failed or fenced_at is None:
            await self._block(
                active,
                reason_code="unexpected_failure_before_candidate",
            )
            raise RunDailyCandleCollectionJobOnceError(
                "daily_candle_collection_blocked_unknown"
            ) from None

        fenced: DailyCandleCollectionJobSnapshotV1 | None = None
        fence_cancelled = False
        try:
            fenced = await self._fence(active, candle=candle, now=fenced_at)
        except asyncio.CancelledError:
            fence_cancelled = True
        except Exception:
            pass
        if fence_cancelled:
            await self._best_effort_block(
                active,
                reason_code="cancelled_before_candidate",
            )
            raise asyncio.CancelledError
        if fenced is None:
            await self._best_effort_block(
                active,
                reason_code="unexpected_failure_before_candidate",
            )
            raise RunDailyCandleCollectionJobOnceError(
                "daily_candle_collection_fence_outcome_unknown"
            ) from None

        receipt = await self._append(fenced)
        confirmed_at: datetime | None = None
        confirm_clock_cancelled = False
        confirm_clock_failed = False
        try:
            confirmed_at = _read_clock(self.clock)
        except asyncio.CancelledError:
            confirm_clock_cancelled = True
        except RunDailyCandleCollectionJobOnceError:
            confirm_clock_failed = True
        if confirm_clock_cancelled:
            await self._best_effort_block(
                fenced,
                reason_code="cancelled_after_candidate",
            )
            raise asyncio.CancelledError
        if confirm_clock_failed or confirmed_at is None:
            await self._block(
                fenced,
                reason_code="unexpected_failure_after_candidate",
            )
            raise RunDailyCandleCollectionJobOnceError(
                "daily_candle_collection_blocked_unknown"
            ) from None

        confirmed: DailyCandleCollectionJobSnapshotV1 | None = None
        confirm_cancelled = False
        try:
            confirmed = await self._confirm(
                fenced,
                receipt=receipt,
                now=confirmed_at,
            )
        except asyncio.CancelledError:
            confirm_cancelled = True
        except Exception:
            pass
        if confirm_cancelled:
            await self._best_effort_block(
                fenced,
                reason_code="cancelled_after_candidate",
            )
            raise asyncio.CancelledError
        if confirmed is None:
            await self._best_effort_block(
                fenced,
                reason_code="confirm_outcome_unknown",
            )
            raise RunDailyCandleCollectionJobOnceError(
                "daily_candle_collection_confirm_outcome_unknown"
            ) from None
        return confirmed

    async def _read_one(
        self,
        active: DailyCandleCollectionJobSnapshotV1,
    ) -> DailyCandleReadPage:
        request = DailyCandleReadRequest(
            symbol=active.spec.symbol,
            before=active.spec.before,
            count=1,
            adjusted=active.spec.adjusted,
        )
        page: DailyCandleReadPage | None = None
        read_outcome: object = None
        read_cancelled = False
        unexpected_failure = False
        try:
            page = await self.collector.collect_daily_candle_page(request)
        except asyncio.CancelledError:
            read_cancelled = True
        except CandleCollectionError as exc:
            read_outcome = exc.read_outcome
        except Exception:
            unexpected_failure = True
        if read_cancelled:
            await self._best_effort_block(
                active,
                reason_code="cancelled_before_candidate",
            )
            raise asyncio.CancelledError
        if read_outcome == "failed":
            await self._pause(
                active,
                reason_code="provider_read_failed_before_candidate",
            )
            raise RunDailyCandleCollectionJobOnceError(
                "daily_candle_collection_paused_retryable"
            ) from None
        if read_outcome is not None:
            reason = (
                "provider_read_outcome_unknown_before_candidate"
                if read_outcome == "unknown"
                else "unexpected_failure_before_candidate"
            )
            await self._block(active, reason_code=reason)
            raise RunDailyCandleCollectionJobOnceError(
                "daily_candle_collection_blocked_unknown"
            ) from None
        if unexpected_failure or page is None:
            await self._block(
                active,
                reason_code="unexpected_failure_before_candidate",
            )
            raise RunDailyCandleCollectionJobOnceError(
                "daily_candle_collection_blocked_unknown"
            ) from None
        return page

    async def _candidate_or_block(
        self,
        active: DailyCandleCollectionJobSnapshotV1,
        page: DailyCandleReadPage,
    ) -> PointInTimeCandleV1:
        candidate: PointInTimeCandleV1 | None = None
        candidate_cancelled = False
        try:
            candidate = _single_candidate(page, spec=active.spec)
        except asyncio.CancelledError:
            candidate_cancelled = True
        except Exception:
            pass
        if candidate_cancelled:
            await self._best_effort_block(
                active,
                reason_code="cancelled_before_candidate",
            )
            raise asyncio.CancelledError
        if candidate is not None:
            return candidate
        await self._block(
            active,
            reason_code="unexpected_failure_before_candidate",
        )
        raise RunDailyCandleCollectionJobOnceError("daily_candle_collection_blocked_unknown")

    async def _load(
        self,
        spec: DailyCandleCollectionJobSpecV1,
        *,
        now: datetime,
    ) -> DailyCandleCollectionJobSnapshotV1:
        loaded: DailyCandleCollectionJobSnapshotV1 | None = None
        try:
            source = await self.job_store.load_or_create_job(spec, now=now)
            loaded = _snapshot(source, spec=spec)
        except Exception:
            pass
        if loaded is None:
            raise RunDailyCandleCollectionJobOnceError(
                "daily_candle_collection_job_load_outcome_unknown"
            )
        return loaded

    async def _begin(
        self,
        snapshot: DailyCandleCollectionJobSnapshotV1,
        *,
        attempt_id: str,
        now: datetime,
    ) -> DailyCandleCollectionJobSnapshotV1:
        begun: DailyCandleCollectionJobSnapshotV1 | None = None
        try:
            source = await self.job_store.begin_attempt(
                job_id=snapshot.spec.job_id,
                spec_sha256=snapshot.spec.spec_sha256,
                expected_revision=snapshot.revision,
                attempt_id=attempt_id,
                holder_id=self.holder_id,
                now=now,
            )
            candidate = _snapshot(source, spec=snapshot.spec)
            if _valid_begin(
                before=snapshot,
                after=candidate,
                attempt_id=attempt_id,
                holder_id=self.holder_id,
                transition_at=now,
            ):
                begun = candidate
        except Exception:
            pass
        if begun is None:
            raise RunDailyCandleCollectionJobOnceError(
                "daily_candle_collection_begin_outcome_unknown"
            )
        return begun

    async def _fence(
        self,
        snapshot: DailyCandleCollectionJobSnapshotV1,
        *,
        candle: PointInTimeCandleV1,
        now: datetime,
    ) -> DailyCandleCollectionJobSnapshotV1:
        attempt = _active_attempt(snapshot, expected_state="collecting")
        source = await self.job_store.fence_candidate(
            job_id=snapshot.spec.job_id,
            spec_sha256=snapshot.spec.spec_sha256,
            expected_revision=snapshot.revision,
            attempt_id=attempt.attempt_id,
            holder_id=attempt.holder_id,
            fencing_revision=attempt.fencing_revision,
            candle=candle,
            now=now,
        )
        candidate = _snapshot(source, spec=snapshot.spec)
        if not _valid_fence(
            before=snapshot,
            after=candidate,
            candle=candle,
            transition_at=now,
        ):
            raise RunDailyCandleCollectionJobOnceError(
                "daily_candle_collection_fence_outcome_unknown"
            )
        return candidate

    async def _append(
        self,
        snapshot: DailyCandleCollectionJobSnapshotV1,
    ) -> CandleObservationWriteReceipt:
        candidate = snapshot.fenced_candidate
        if snapshot.state != "candidate_fenced" or candidate is None:
            raise RunDailyCandleCollectionJobOnceError(
                "daily_candle_collection_fenced_candidate_invalid"
            )
        receipt: CandleObservationWriteReceipt | None = None
        append_cancelled = False
        try:
            source = await self.observation_store.append_observation(candidate.candle)
            receipt = _receipt(source, candle=candidate.candle)
        except asyncio.CancelledError:
            append_cancelled = True
        except Exception:
            pass
        if append_cancelled:
            await self._best_effort_block(
                snapshot,
                reason_code="cancelled_after_candidate",
            )
            raise asyncio.CancelledError
        if receipt is None:
            await self._block(
                snapshot,
                reason_code="append_outcome_unknown",
            )
            raise RunDailyCandleCollectionJobOnceError(
                "daily_candle_collection_append_outcome_unknown"
            ) from None
        return receipt

    async def _pause(
        self,
        snapshot: DailyCandleCollectionJobSnapshotV1,
        *,
        reason_code: str,
    ) -> DailyCandleCollectionJobSnapshotV1:
        attempt = _active_attempt(snapshot, expected_state="collecting")
        transition_at = _read_clock(self.clock)
        paused: DailyCandleCollectionJobSnapshotV1 | None = None
        try:
            source = await self.job_store.pause_retryable(
                job_id=snapshot.spec.job_id,
                spec_sha256=snapshot.spec.spec_sha256,
                expected_revision=snapshot.revision,
                attempt_id=attempt.attempt_id,
                holder_id=attempt.holder_id,
                fencing_revision=attempt.fencing_revision,
                reason_code=reason_code,
                now=transition_at,
            )
            candidate = _snapshot(source, spec=snapshot.spec)
            if _valid_terminal_transition(
                before=snapshot,
                after=candidate,
                expected_state="paused_retryable",
                expected_reason=reason_code,
                keep_active=False,
                transition_at=transition_at,
            ):
                paused = candidate
        except Exception:
            pass
        if paused is None:
            raise RunDailyCandleCollectionJobOnceError(
                "daily_candle_collection_pause_outcome_unknown"
            )
        return paused

    async def _block(
        self,
        snapshot: DailyCandleCollectionJobSnapshotV1,
        *,
        reason_code: str,
    ) -> DailyCandleCollectionJobSnapshotV1:
        attempt = _active_attempt(
            snapshot,
            expected_state=("collecting" if snapshot.state == "collecting" else "candidate_fenced"),
        )
        transition_at = _read_clock(self.clock)
        blocked: DailyCandleCollectionJobSnapshotV1 | None = None
        try:
            source = await self.job_store.block_unknown(
                job_id=snapshot.spec.job_id,
                spec_sha256=snapshot.spec.spec_sha256,
                expected_revision=snapshot.revision,
                attempt_id=attempt.attempt_id,
                holder_id=attempt.holder_id,
                fencing_revision=attempt.fencing_revision,
                reason_code=reason_code,
                now=transition_at,
            )
            candidate = _snapshot(source, spec=snapshot.spec)
            if _valid_terminal_transition(
                before=snapshot,
                after=candidate,
                expected_state="blocked_unknown",
                expected_reason=reason_code,
                keep_active=True,
                transition_at=transition_at,
            ):
                blocked = candidate
        except Exception:
            pass
        if blocked is None:
            raise RunDailyCandleCollectionJobOnceError(
                "daily_candle_collection_block_outcome_unknown"
            )
        return blocked

    async def _best_effort_block(
        self,
        snapshot: DailyCandleCollectionJobSnapshotV1,
        *,
        reason_code: str,
    ) -> None:
        with suppress(Exception):
            await self._block(snapshot, reason_code=reason_code)

    async def _confirm(
        self,
        snapshot: DailyCandleCollectionJobSnapshotV1,
        *,
        receipt: CandleObservationWriteReceipt,
        now: datetime,
    ) -> DailyCandleCollectionJobSnapshotV1:
        attempt = _active_attempt(snapshot, expected_state="candidate_fenced")
        source = await self.job_store.confirm_candidate(
            job_id=snapshot.spec.job_id,
            spec_sha256=snapshot.spec.spec_sha256,
            expected_revision=snapshot.revision,
            attempt_id=attempt.attempt_id,
            holder_id=attempt.holder_id,
            fencing_revision=attempt.fencing_revision,
            receipt=receipt,
            now=now,
        )
        candidate = _snapshot(source, spec=snapshot.spec)
        if not _valid_confirm(
            before=snapshot,
            after=candidate,
            receipt=receipt,
            confirmed_at=now,
        ):
            raise RunDailyCandleCollectionJobOnceError(
                "daily_candle_collection_confirm_outcome_unknown"
            )
        return candidate


def _require_durable_store(
    value: DailyCandleCollectionJobStorePort | CandleObservationStorePort,
    store_name: str,
) -> None:
    kind: object = None
    with suppress(Exception):
        kind = value.persistence_kind
    if type(kind) is not str or kind != "durable":
        raise RunDailyCandleCollectionJobOnceError(
            f"daily_candle_collection_{store_name}_store_must_be_durable"
        )


def _require_shared_persistence_authority(
    *,
    job_store: DailyCandleCollectionJobStorePort,
    observation_store: CandleObservationStorePort,
) -> None:
    job_authority: object = None
    observation_authority: object = None
    with suppress(Exception):
        job_authority = job_store.persistence_authority
    with suppress(Exception):
        observation_authority = observation_store.persistence_authority
    if not is_persistence_authority(job_authority) or not is_persistence_authority(
        observation_authority
    ):
        raise RunDailyCandleCollectionJobOnceError(
            "daily_candle_collection_store_authority_invalid"
        )
    if job_authority != observation_authority:
        raise RunDailyCandleCollectionJobOnceError(
            "daily_candle_collection_store_authority_mismatch"
        )


def _spec(value: object) -> DailyCandleCollectionJobSpecV1:
    canonical: DailyCandleCollectionJobSpecV1 | None = None
    with suppress(Exception):
        canonical = canonical_daily_candle_collection_job_spec(value)
    if canonical is None:
        raise RunDailyCandleCollectionJobOnceError("daily_candle_collection_job_spec_invalid")
    return canonical


def _snapshot(
    value: object,
    *,
    spec: DailyCandleCollectionJobSpecV1,
) -> DailyCandleCollectionJobSnapshotV1:
    canonical: DailyCandleCollectionJobSnapshotV1 | None = None
    with suppress(Exception):
        candidate = canonical_daily_candle_collection_job_snapshot(value)
        if candidate.spec == spec and candidate.spec.spec_sha256 == spec.spec_sha256:
            canonical = candidate
    if canonical is None:
        raise RunDailyCandleCollectionJobOnceError("daily_candle_collection_job_snapshot_invalid")
    return canonical


def _single_candidate(
    page: object,
    *,
    spec: DailyCandleCollectionJobSpecV1,
) -> PointInTimeCandleV1:
    if type(page) is not DailyCandleReadPage or len(page.candles) != 1:
        raise RunDailyCandleCollectionJobOnceError("daily_candle_collection_candidate_invalid")
    candle = page.candles[0]
    if type(candle) is not PointInTimeCandleV1:
        raise RunDailyCandleCollectionJobOnceError("daily_candle_collection_candidate_invalid")
    canonical: PointInTimeCandleV1 | None = None
    with suppress(Exception):
        candidate = PointInTimeCandleV1.from_payload(candle.to_payload())
        if candidate == candle:
            canonical = candidate
    if canonical is None or (
        canonical.provider != spec.provider
        or canonical.symbol != spec.symbol
        or canonical.market != spec.market
        or canonical.interval != spec.interval
        or canonical.adjusted is not spec.adjusted
        or canonical.provider_event_at > spec.before
        or canonical.provider_contract_sha256 != spec.provider_contract_sha256
        or canonical.observed_at != page.observed_at
    ):
        raise RunDailyCandleCollectionJobOnceError(
            "daily_candle_collection_candidate_scope_mismatch"
        )
    return canonical


def _receipt(
    value: object,
    *,
    candle: PointInTimeCandleV1,
) -> CandleObservationWriteReceipt:
    canonical: CandleObservationWriteReceipt | None = None
    if type(value) is CandleObservationWriteReceipt:
        with suppress(Exception):
            candidate = CandleObservationWriteReceipt(
                idempotency_key=value.idempotency_key,
                canonical_observation_sha256=value.canonical_observation_sha256,
                revision=value.revision,
                inserted=value.inserted,
                stored_observed_at=value.stored_observed_at,
            )
            if candidate == value:
                canonical = candidate
    if canonical is None or (
        canonical.idempotency_key != candle.idempotency_key
        or canonical.canonical_observation_sha256 != candle.canonical_observation_sha256
        or canonical.stored_observed_at > candle.observed_at
    ):
        raise RunDailyCandleCollectionJobOnceError("daily_candle_collection_receipt_invalid")
    return canonical


def _active_attempt(
    snapshot: DailyCandleCollectionJobSnapshotV1,
    *,
    expected_state: str,
) -> DailyCandleCollectionAttemptV1:
    attempt = snapshot.active_attempt
    if snapshot.state != expected_state or attempt is None:
        raise RunDailyCandleCollectionJobOnceError("daily_candle_collection_active_attempt_invalid")
    return attempt


def _valid_begin(
    *,
    before: DailyCandleCollectionJobSnapshotV1,
    after: DailyCandleCollectionJobSnapshotV1,
    attempt_id: str,
    holder_id: str,
    transition_at: datetime,
) -> bool:
    attempt = after.active_attempt
    return (
        before.state == "ready"
        and after.revision == before.revision + 1
        and after.state == "collecting"
        and attempt is not None
        and attempt.attempt_id == attempt_id
        and attempt.holder_id == holder_id
        and attempt.fencing_revision == after.revision
        and attempt.begun_at == transition_at
        and after.fenced_candidate is None
        and after.completion is None
        and after.state_reason is None
        and after.created_at == before.created_at
        and after.updated_at == transition_at
    )


def _valid_fence(
    *,
    before: DailyCandleCollectionJobSnapshotV1,
    after: DailyCandleCollectionJobSnapshotV1,
    candle: PointInTimeCandleV1,
    transition_at: datetime,
) -> bool:
    candidate = after.fenced_candidate
    return (
        before.state == "collecting"
        and after.revision == before.revision + 1
        and after.state == "candidate_fenced"
        and after.active_attempt == before.active_attempt
        and candidate is not None
        and candidate.attempt == before.active_attempt
        and candidate.candle == candle
        and candidate.fenced_at == transition_at
        and after.completion is None
        and after.state_reason is None
        and after.created_at == before.created_at
        and after.updated_at == transition_at
    )


def _valid_terminal_transition(
    *,
    before: DailyCandleCollectionJobSnapshotV1,
    after: DailyCandleCollectionJobSnapshotV1,
    expected_state: str,
    expected_reason: str,
    keep_active: bool,
    transition_at: datetime,
) -> bool:
    return (
        after.revision == before.revision + 1
        and after.state == expected_state
        and after.active_attempt == (before.active_attempt if keep_active else None)
        and after.fenced_candidate == (before.fenced_candidate if keep_active else None)
        and after.completion is None
        and after.state_reason == expected_reason
        and after.created_at == before.created_at
        and after.updated_at == transition_at
    )


def _valid_confirm(
    *,
    before: DailyCandleCollectionJobSnapshotV1,
    after: DailyCandleCollectionJobSnapshotV1,
    receipt: CandleObservationWriteReceipt,
    confirmed_at: datetime,
) -> bool:
    candidate = before.fenced_candidate
    completion = after.completion
    return (
        before.state == "candidate_fenced"
        and candidate is not None
        and after.revision == before.revision + 1
        and after.state == "completed"
        and after.active_attempt is None
        and after.fenced_candidate is None
        and completion is not None
        and completion.candidate == candidate
        and completion.write_evidence.persistence_kind == "durable"
        and completion.write_evidence.receipt == receipt
        and completion.confirmed_at == confirmed_at
        and after.state_reason is None
        and after.created_at == before.created_at
        and after.updated_at == confirmed_at
    )


def _require_durable_completion(snapshot: DailyCandleCollectionJobSnapshotV1) -> None:
    completion = snapshot.completion
    if (
        snapshot.state != "completed"
        or completion is None
        or completion.write_evidence.persistence_kind != "durable"
    ):
        raise RunDailyCandleCollectionJobOnceError(
            "daily_candle_collection_completed_evidence_invalid"
        )


def _read_clock(clock: Callable[[], datetime]) -> datetime:
    canonical: datetime | None = None
    with suppress(Exception):
        value = clock()
        if type(value) is datetime and value.tzinfo is not None and value.utcoffset() is not None:
            canonical = value.astimezone(UTC)
    if canonical is None:
        raise RunDailyCandleCollectionJobOnceError("daily_candle_collection_clock_invalid")
    return canonical


def _new_attempt_id(factory: Callable[[], UUID]) -> str:
    canonical: str | None = None
    with suppress(Exception):
        value = factory()
        if type(value) is UUID and value.version == 4:
            canonical = str(value)
    if canonical is None:
        raise RunDailyCandleCollectionJobOnceError("daily_candle_collection_attempt_id_invalid")
    return canonical


def _uuid4_text(value: object, field_name: str) -> str:
    canonical: str | None = None
    with suppress(AttributeError, TypeError, ValueError):
        if type(value) is str:
            parsed = UUID(value)
            if parsed.version == 4 and str(parsed) == value:
                canonical = value
    if canonical is None:
        raise RunDailyCandleCollectionJobOnceError(f"daily_candle_collection_{field_name}_invalid")
    return canonical
