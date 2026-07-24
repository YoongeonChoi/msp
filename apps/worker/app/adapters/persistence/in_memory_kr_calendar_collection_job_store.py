from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, date, datetime
from uuid import UUID

from app.application.ports.kr_calendar_collection_job_store_port import (
    KrCalendarCollectionDateAttemptV1,
    KrCalendarCollectionDateCheckpointV1,
    KrCalendarCollectionJobPersistenceKind,
    KrCalendarCollectionJobSnapshotV1,
    KrCalendarCollectionJobSpecV1,
    KrCalendarCollectionJobState,
    KrCalendarCollectionJobStoreError,
    canonical_kr_calendar_collection_job_snapshot,
    canonical_kr_calendar_collection_job_spec,
    kr_calendar_collection_job_manifest_sha256,
)
from app.application.ports.kr_daily_session_collection_port import (
    CollectedKrDailySessionObservationV1,
    canonical_collected_kr_daily_session_observation,
)


class InMemoryKrCalendarCollectionJobStore:
    """Reference CAS/fencing semantics without process-restart durability.

    This adapter is intentionally absent from the runtime container. It has no
    TTL, lease takeover, or automatic retry path for unresolved attempts.
    """

    persistence_kind: KrCalendarCollectionJobPersistenceKind = "reference"

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._jobs: dict[str, KrCalendarCollectionJobSnapshotV1] = {}
        self._used_attempt_ids: set[str] = set()

    async def load_or_create_job(
        self,
        spec: KrCalendarCollectionJobSpecV1,
        *,
        now: datetime,
    ) -> KrCalendarCollectionJobSnapshotV1:
        canonical_spec = canonical_kr_calendar_collection_job_spec(spec)
        canonical_now = _utc(now)
        async with self._lock:
            existing = self._jobs.get(canonical_spec.job_id)
            if existing is not None:
                if existing.spec != canonical_spec:
                    raise KrCalendarCollectionJobStoreError(
                        "kr_calendar_collection_job_spec_conflict"
                    )
                return _clone(existing)
            created = KrCalendarCollectionJobSnapshotV1(
                spec=canonical_spec,
                revision=1,
                state="ready",
                checkpoints=(),
                active_attempt=None,
                state_reason=None,
                terminal_manifest_sha256=None,
                created_at=canonical_now,
                updated_at=canonical_now,
            )
            self._jobs[canonical_spec.job_id] = _clone(created)
            return _clone(created)

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
        canonical_now = _utc(now)
        async with self._lock:
            current = self._current(
                job_id=job_id,
                spec_sha256=spec_sha256,
                expected_revision=expected_revision,
            )
            if current.state not in {"ready", "paused_retryable"}:
                raise KrCalendarCollectionJobStoreError(
                    "kr_calendar_collection_job_begin_state_invalid"
                )
            if current.next_date != target_date or type(target_date) is not date:
                raise KrCalendarCollectionJobStoreError(
                    "kr_calendar_collection_job_begin_target_invalid"
                )
            if attempt_id in self._used_attempt_ids:
                raise KrCalendarCollectionJobStoreError("kr_calendar_collection_job_attempt_reused")
            if canonical_now < current.updated_at:
                raise KrCalendarCollectionJobStoreError(
                    "kr_calendar_collection_job_clock_regressed"
                )
            attempt = KrCalendarCollectionDateAttemptV1(
                attempt_id=attempt_id,
                holder_id=holder_id,
                target_date=target_date,
                fencing_revision=expected_revision + 1,
                begun_at=canonical_now,
            )
            updated = KrCalendarCollectionJobSnapshotV1(
                spec=current.spec,
                revision=expected_revision + 1,
                state="collecting",
                checkpoints=current.checkpoints,
                active_attempt=attempt,
                state_reason=None,
                terminal_manifest_sha256=None,
                created_at=current.created_at,
                updated_at=canonical_now,
            )
            self._used_attempt_ids.add(attempt_id)
            self._jobs[job_id] = _clone(updated)
            return _clone(updated)

    async def pause_retryable(
        self,
        *,
        job_id: str,
        spec_sha256: str,
        expected_revision: int,
        attempt_id: str,
        holder_id: str,
        target_date: date,
        reason_code: str,
        now: datetime,
    ) -> KrCalendarCollectionJobSnapshotV1:
        canonical_now = _utc(now)
        async with self._lock:
            current = self._active_current(
                job_id=job_id,
                spec_sha256=spec_sha256,
                expected_revision=expected_revision,
                attempt_id=attempt_id,
                holder_id=holder_id,
                target_date=target_date,
            )
            updated = self._transition_active(
                current,
                state="paused_retryable",
                active_attempt=None,
                state_reason=reason_code,
                now=canonical_now,
            )
            self._jobs[job_id] = _clone(updated)
            return _clone(updated)

    async def block_unknown(
        self,
        *,
        job_id: str,
        spec_sha256: str,
        expected_revision: int,
        attempt_id: str,
        holder_id: str,
        target_date: date,
        reason_code: str,
        now: datetime,
    ) -> KrCalendarCollectionJobSnapshotV1:
        canonical_now = _utc(now)
        async with self._lock:
            current = self._active_current(
                job_id=job_id,
                spec_sha256=spec_sha256,
                expected_revision=expected_revision,
                attempt_id=attempt_id,
                holder_id=holder_id,
                target_date=target_date,
            )
            updated = self._transition_active(
                current,
                state="blocked_unknown",
                active_attempt=current.active_attempt,
                state_reason=reason_code,
                now=canonical_now,
            )
            self._jobs[job_id] = _clone(updated)
            return _clone(updated)

    async def confirm_date(
        self,
        *,
        job_id: str,
        spec_sha256: str,
        expected_revision: int,
        attempt_id: str,
        holder_id: str,
        target_date: date,
        collection: CollectedKrDailySessionObservationV1,
        now: datetime,
    ) -> KrCalendarCollectionJobSnapshotV1:
        canonical_collection = canonical_collected_kr_daily_session_observation(collection)
        canonical_now = _utc(now)
        async with self._lock:
            current = self._active_current(
                job_id=job_id,
                spec_sha256=spec_sha256,
                expected_revision=expected_revision,
                attempt_id=attempt_id,
                holder_id=holder_id,
                target_date=target_date,
            )
            attempt = current.active_attempt
            if attempt is None:
                raise KrCalendarCollectionJobStoreError(
                    "kr_calendar_collection_job_attempt_missing"
                )
            if (
                canonical_collection.target_date != target_date
                or canonical_collection.session.provider != current.spec.provider
                or canonical_collection.session.market != current.spec.market
            ):
                raise KrCalendarCollectionJobStoreError(
                    "kr_calendar_collection_job_collection_scope_mismatch"
                )
            if canonical_now < current.updated_at:
                raise KrCalendarCollectionJobStoreError(
                    "kr_calendar_collection_job_clock_regressed"
                )
            checkpoint = KrCalendarCollectionDateCheckpointV1(
                attempt_id=attempt.attempt_id,
                holder_id=attempt.holder_id,
                target_date=attempt.target_date,
                fencing_revision=attempt.fencing_revision,
                begun_at=attempt.begun_at,
                collection=canonical_collection,
                confirmed_at=canonical_now,
            )
            checkpoints = (*current.checkpoints, checkpoint)
            completed = len(checkpoints) == current.spec.total_days
            manifest = (
                kr_calendar_collection_job_manifest_sha256(current.spec, checkpoints)
                if completed
                else None
            )
            updated = KrCalendarCollectionJobSnapshotV1(
                spec=current.spec,
                revision=expected_revision + 1,
                state="completed" if completed else "ready",
                checkpoints=checkpoints,
                active_attempt=None,
                state_reason=None,
                terminal_manifest_sha256=manifest,
                created_at=current.created_at,
                updated_at=canonical_now,
            )
            self._jobs[job_id] = _clone(updated)
            return _clone(updated)

    async def inspect_job(
        self,
        job_id: str,
    ) -> KrCalendarCollectionJobSnapshotV1 | None:
        canonical_job_id = _uuid4_text(job_id)
        async with self._lock:
            current = self._jobs.get(canonical_job_id)
            return None if current is None else _clone(current)

    def _current(
        self,
        *,
        job_id: str,
        spec_sha256: str,
        expected_revision: int,
    ) -> KrCalendarCollectionJobSnapshotV1:
        if type(expected_revision) is not int or expected_revision <= 0:
            raise KrCalendarCollectionJobStoreError(
                "kr_calendar_collection_job_expected_revision_invalid"
            )
        current = self._jobs.get(job_id)
        if current is None:
            raise KrCalendarCollectionJobStoreError("kr_calendar_collection_job_not_found")
        if current.spec.spec_sha256 != spec_sha256:
            raise KrCalendarCollectionJobStoreError("kr_calendar_collection_job_spec_hash_mismatch")
        if current.revision != expected_revision:
            raise KrCalendarCollectionJobStoreError("kr_calendar_collection_job_revision_conflict")
        return _clone(current)

    def _active_current(
        self,
        *,
        job_id: str,
        spec_sha256: str,
        expected_revision: int,
        attempt_id: str,
        holder_id: str,
        target_date: date,
    ) -> KrCalendarCollectionJobSnapshotV1:
        current = self._current(
            job_id=job_id,
            spec_sha256=spec_sha256,
            expected_revision=expected_revision,
        )
        attempt = current.active_attempt
        if current.state != "collecting" or attempt is None:
            raise KrCalendarCollectionJobStoreError(
                "kr_calendar_collection_job_active_state_invalid"
            )
        if (
            attempt.attempt_id != attempt_id
            or attempt.holder_id != holder_id
            or attempt.target_date != target_date
            or attempt.fencing_revision != expected_revision
        ):
            raise KrCalendarCollectionJobStoreError(
                "kr_calendar_collection_job_attempt_fence_mismatch"
            )
        return current

    @staticmethod
    def _transition_active(
        current: KrCalendarCollectionJobSnapshotV1,
        *,
        state: KrCalendarCollectionJobState,
        active_attempt: KrCalendarCollectionDateAttemptV1 | None,
        state_reason: str,
        now: datetime,
    ) -> KrCalendarCollectionJobSnapshotV1:
        if now < current.updated_at:
            raise KrCalendarCollectionJobStoreError("kr_calendar_collection_job_clock_regressed")
        if state not in {"paused_retryable", "blocked_unknown"}:
            raise KrCalendarCollectionJobStoreError("kr_calendar_collection_job_transition_invalid")
        return KrCalendarCollectionJobSnapshotV1(
            spec=current.spec,
            revision=current.revision + 1,
            state=state,
            checkpoints=current.checkpoints,
            active_attempt=active_attempt,
            state_reason=state_reason,
            terminal_manifest_sha256=None,
            created_at=current.created_at,
            updated_at=now,
        )


def _clone(
    snapshot: KrCalendarCollectionJobSnapshotV1,
) -> KrCalendarCollectionJobSnapshotV1:
    return canonical_kr_calendar_collection_job_snapshot(snapshot)


def _utc(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None:
        raise KrCalendarCollectionJobStoreError("kr_calendar_collection_job_store_clock_invalid")
    converted: datetime | None = None
    try:
        if value.utcoffset() is not None:
            converted = value.astimezone(UTC)
    except (OverflowError, RuntimeError, TypeError, ValueError):
        pass
    if converted is None:
        raise KrCalendarCollectionJobStoreError("kr_calendar_collection_job_store_clock_invalid")
    return converted


def _uuid4_text(value: object) -> str:
    parsed: UUID | None = None
    if type(value) is str:
        with suppress(AttributeError, TypeError, ValueError):
            parsed = UUID(value)
    if parsed is None or parsed.version != 4 or str(parsed) != value:
        raise KrCalendarCollectionJobStoreError(
            "kr_calendar_collection_job_store_job_id_invalid"
        )
    return str(parsed)
