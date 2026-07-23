from __future__ import annotations

import asyncio
import re
from contextlib import suppress
from datetime import UTC, datetime
from uuid import UUID, uuid4

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
    DAILY_CANDLE_COLLECTION_POST_CANDIDATE_BLOCK_REASONS,
    DAILY_CANDLE_COLLECTION_PRE_CANDIDATE_BLOCK_REASONS,
    DAILY_CANDLE_COLLECTION_RETRYABLE_REASONS,
    DailyCandleCollectionAttemptV1,
    DailyCandleCollectionCandidateV1,
    DailyCandleCollectionCompletionV1,
    DailyCandleCollectionJobPersistenceKind,
    DailyCandleCollectionJobSnapshotV1,
    DailyCandleCollectionJobSpecV1,
    DailyCandleCollectionJobStoreError,
    DailyCandleCollectionWriteEvidenceV1,
    canonical_daily_candle_collection_job_snapshot,
    canonical_daily_candle_collection_job_spec,
)
from app.domain.market_data.point_in_time import PointInTimeCandleV1

_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class InMemoryDailyCandleCollectionJobStore:
    """Reference CAS/fence semantics without process-restart durability.

    The adapter is intentionally not wired into runtime. A fenced candidate is
    preserved before any external append may begin, and an unknown append has
    no TTL, takeover, or automatic retry path.
    """

    persistence_kind: DailyCandleCollectionJobPersistenceKind = "reference"

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._jobs: dict[str, DailyCandleCollectionJobSnapshotV1] = {}
        self._used_attempt_ids: set[str] = set()

    async def load_or_create_job(
        self,
        spec: DailyCandleCollectionJobSpecV1,
        *,
        now: datetime,
    ) -> DailyCandleCollectionJobSnapshotV1:
        canonical_spec = canonical_daily_candle_collection_job_spec(spec)
        canonical_now = _utc(now)
        async with self._lock:
            existing = self._jobs.get(canonical_spec.job_id)
            if existing is not None:
                if existing.spec != canonical_spec:
                    raise DailyCandleCollectionJobStoreError(
                        "daily_candle_collection_job_spec_conflict"
                    )
                return _clone(existing)
            created = DailyCandleCollectionJobSnapshotV1(
                spec=canonical_spec,
                revision=1,
                state="ready",
                active_attempt=None,
                fenced_candidate=None,
                completion=None,
                state_reason=None,
                created_at=canonical_now,
                updated_at=canonical_now,
            )
            self._jobs[canonical_spec.job_id] = _clone(created)
            return _clone(created)

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
        canonical_now = _utc(now)
        _require_revision_headroom(
            expected_revision,
            DAILY_CANDLE_COLLECTION_JOB_MAX_BEGIN_EXPECTED_REVISION,
        )
        async with self._lock:
            current = self._current(
                job_id=job_id,
                spec_sha256=spec_sha256,
                expected_revision=expected_revision,
            )
            if current.state not in {"ready", "paused_retryable"}:
                raise DailyCandleCollectionJobStoreError(
                    "daily_candle_collection_job_begin_state_invalid"
                )
            _uuid4_text(attempt_id, "attempt_id")
            _uuid4_text(holder_id, "holder_id")
            if attempt_id in self._used_attempt_ids:
                raise DailyCandleCollectionJobStoreError(
                    "daily_candle_collection_job_attempt_reused"
                )
            if canonical_now < current.updated_at:
                raise DailyCandleCollectionJobStoreError(
                    "daily_candle_collection_job_clock_regressed"
                )
            attempt = DailyCandleCollectionAttemptV1(
                attempt_id=attempt_id,
                holder_id=holder_id,
                fencing_revision=expected_revision + 1,
                begun_at=canonical_now,
            )
            updated = DailyCandleCollectionJobSnapshotV1(
                spec=current.spec,
                revision=attempt.fencing_revision,
                state="collecting",
                active_attempt=attempt,
                fenced_candidate=None,
                completion=None,
                state_reason=None,
                created_at=current.created_at,
                updated_at=canonical_now,
            )
            self._used_attempt_ids.add(attempt_id)
            self._jobs[current.spec.job_id] = _clone(updated)
            return _clone(updated)

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
        canonical_now = _utc(now)
        _require_revision_headroom(
            expected_revision,
            DAILY_CANDLE_COLLECTION_JOB_MAX_FENCE_EXPECTED_REVISION,
        )
        async with self._lock:
            current = self._active_current(
                job_id=job_id,
                spec_sha256=spec_sha256,
                expected_revision=expected_revision,
                attempt_id=attempt_id,
                holder_id=holder_id,
                fencing_revision=fencing_revision,
                allowed_states={"collecting"},
            )
            if canonical_now < current.updated_at:
                raise DailyCandleCollectionJobStoreError(
                    "daily_candle_collection_job_clock_regressed"
                )
            attempt = current.active_attempt
            if attempt is None:
                raise DailyCandleCollectionJobStoreError(
                    "daily_candle_collection_job_attempt_missing"
                )
            candidate = DailyCandleCollectionCandidateV1(
                attempt=attempt,
                candle=candle,
                fenced_at=canonical_now,
            )
            updated = DailyCandleCollectionJobSnapshotV1(
                spec=current.spec,
                revision=expected_revision + 1,
                state="candidate_fenced",
                active_attempt=attempt,
                fenced_candidate=candidate,
                completion=None,
                state_reason=None,
                created_at=current.created_at,
                updated_at=canonical_now,
            )
            self._jobs[current.spec.job_id] = _clone(updated)
            return _clone(updated)

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
        canonical_now = _utc(now)
        _require_revision_headroom(
            expected_revision,
            DAILY_CANDLE_COLLECTION_JOB_MAX_PAUSE_EXPECTED_REVISION,
        )
        async with self._lock:
            current = self._active_current(
                job_id=job_id,
                spec_sha256=spec_sha256,
                expected_revision=expected_revision,
                attempt_id=attempt_id,
                holder_id=holder_id,
                fencing_revision=fencing_revision,
                allowed_states={"collecting"},
            )
            if reason_code not in DAILY_CANDLE_COLLECTION_RETRYABLE_REASONS:
                raise DailyCandleCollectionJobStoreError(
                    "daily_candle_collection_job_pause_reason_invalid"
                )
            if canonical_now < current.updated_at:
                raise DailyCandleCollectionJobStoreError(
                    "daily_candle_collection_job_clock_regressed"
                )
            updated = DailyCandleCollectionJobSnapshotV1(
                spec=current.spec,
                revision=expected_revision + 1,
                state="paused_retryable",
                active_attempt=None,
                fenced_candidate=None,
                completion=None,
                state_reason=reason_code,
                created_at=current.created_at,
                updated_at=canonical_now,
            )
            self._jobs[current.spec.job_id] = _clone(updated)
            return _clone(updated)

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
        canonical_now = _utc(now)
        _require_revision_headroom(
            expected_revision,
            DAILY_CANDLE_COLLECTION_JOB_MAX_BLOCK_EXPECTED_REVISION,
        )
        async with self._lock:
            current = self._active_current(
                job_id=job_id,
                spec_sha256=spec_sha256,
                expected_revision=expected_revision,
                attempt_id=attempt_id,
                holder_id=holder_id,
                fencing_revision=fencing_revision,
                allowed_states={"collecting", "candidate_fenced"},
            )
            if canonical_now < current.updated_at:
                raise DailyCandleCollectionJobStoreError(
                    "daily_candle_collection_job_clock_regressed"
                )
            allowed_reasons = (
                DAILY_CANDLE_COLLECTION_PRE_CANDIDATE_BLOCK_REASONS
                if current.state == "collecting"
                else DAILY_CANDLE_COLLECTION_POST_CANDIDATE_BLOCK_REASONS
            )
            if reason_code not in allowed_reasons:
                raise DailyCandleCollectionJobStoreError(
                    "daily_candle_collection_job_block_reason_invalid"
                )
            updated = DailyCandleCollectionJobSnapshotV1(
                spec=current.spec,
                revision=expected_revision + 1,
                state="blocked_unknown",
                active_attempt=current.active_attempt,
                fenced_candidate=current.fenced_candidate,
                completion=None,
                state_reason=reason_code,
                created_at=current.created_at,
                updated_at=canonical_now,
            )
            self._jobs[current.spec.job_id] = _clone(updated)
            return _clone(updated)

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
        canonical_now = _utc(now)
        _require_revision_headroom(
            expected_revision,
            DAILY_CANDLE_COLLECTION_JOB_MAX_CONFIRM_EXPECTED_REVISION,
        )
        async with self._lock:
            current = self._active_current(
                job_id=job_id,
                spec_sha256=spec_sha256,
                expected_revision=expected_revision,
                attempt_id=attempt_id,
                holder_id=holder_id,
                fencing_revision=fencing_revision,
                allowed_states={"candidate_fenced"},
            )
            if canonical_now < current.updated_at:
                raise DailyCandleCollectionJobStoreError(
                    "daily_candle_collection_job_clock_regressed"
                )
            candidate = current.fenced_candidate
            if candidate is None:
                raise DailyCandleCollectionJobStoreError(
                    "daily_candle_collection_job_candidate_missing"
                )
            completion = DailyCandleCollectionCompletionV1(
                candidate=candidate,
                write_evidence=DailyCandleCollectionWriteEvidenceV1(
                    persistence_kind="reference",
                    receipt=receipt,
                    content_revision_id=uuid4(),
                    occurrence_id=uuid4(),
                    occurrence_observed_at=candidate.candle.observed_at,
                ),
                confirmed_at=canonical_now,
            )
            updated = DailyCandleCollectionJobSnapshotV1(
                spec=current.spec,
                revision=expected_revision + 1,
                state="completed",
                active_attempt=None,
                fenced_candidate=None,
                completion=completion,
                state_reason=None,
                created_at=current.created_at,
                updated_at=canonical_now,
            )
            self._jobs[current.spec.job_id] = _clone(updated)
            return _clone(updated)

    async def inspect_job(
        self,
        job_id: str,
    ) -> DailyCandleCollectionJobSnapshotV1 | None:
        canonical_job_id = _uuid4_text(job_id, "job_id")
        async with self._lock:
            current = self._jobs.get(canonical_job_id)
            return None if current is None else _clone(current)

    def _current(
        self,
        *,
        job_id: str,
        spec_sha256: str,
        expected_revision: int,
    ) -> DailyCandleCollectionJobSnapshotV1:
        canonical_job_id = _uuid4_text(job_id, "job_id")
        if type(spec_sha256) is not str or _SHA256_RE.fullmatch(spec_sha256) is None:
            raise DailyCandleCollectionJobStoreError(
                "daily_candle_collection_job_spec_sha256_invalid"
            )
        if type(expected_revision) is not int or expected_revision <= 0:
            raise DailyCandleCollectionJobStoreError(
                "daily_candle_collection_job_expected_revision_invalid"
            )
        current = self._jobs.get(canonical_job_id)
        if current is None:
            raise DailyCandleCollectionJobStoreError("daily_candle_collection_job_not_found")
        if current.spec.spec_sha256 != spec_sha256:
            raise DailyCandleCollectionJobStoreError(
                "daily_candle_collection_job_spec_hash_mismatch"
            )
        if current.revision != expected_revision:
            raise DailyCandleCollectionJobStoreError(
                "daily_candle_collection_job_revision_conflict"
            )
        return _clone(current)

    def _active_current(
        self,
        *,
        job_id: str,
        spec_sha256: str,
        expected_revision: int,
        attempt_id: str,
        holder_id: str,
        fencing_revision: int,
        allowed_states: set[str],
    ) -> DailyCandleCollectionJobSnapshotV1:
        current = self._current(
            job_id=job_id,
            spec_sha256=spec_sha256,
            expected_revision=expected_revision,
        )
        attempt = current.active_attempt
        if current.state not in allowed_states or attempt is None:
            raise DailyCandleCollectionJobStoreError(
                "daily_candle_collection_job_active_state_invalid"
            )
        canonical_attempt_id = _uuid4_text(attempt_id, "attempt_id")
        canonical_holder_id = _uuid4_text(holder_id, "holder_id")
        if (
            type(fencing_revision) is not int
            or attempt.attempt_id != canonical_attempt_id
            or attempt.holder_id != canonical_holder_id
            or attempt.fencing_revision != fencing_revision
        ):
            raise DailyCandleCollectionJobStoreError(
                "daily_candle_collection_job_attempt_fence_mismatch"
            )
        return current


def _clone(
    snapshot: DailyCandleCollectionJobSnapshotV1,
) -> DailyCandleCollectionJobSnapshotV1:
    return canonical_daily_candle_collection_job_snapshot(snapshot)


def _require_revision_headroom(value: object, maximum: int) -> None:
    if (
        type(value) is not int
        or value <= 0
        or value >= DAILY_CANDLE_COLLECTION_JOB_MAX_DATABASE_REVISION
    ):
        raise DailyCandleCollectionJobStoreError(
            "daily_candle_collection_job_expected_revision_invalid"
        )
    if value > maximum:
        raise DailyCandleCollectionJobStoreError("daily_candle_collection_job_revision_exhausted")


def _utc(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None:
        raise DailyCandleCollectionJobStoreError("daily_candle_collection_job_store_clock_invalid")
    converted: datetime | None = None
    with suppress(OverflowError, RuntimeError, TypeError, ValueError):
        if value.utcoffset() is not None:
            converted = value.astimezone(UTC)
    if converted is None:
        raise DailyCandleCollectionJobStoreError("daily_candle_collection_job_store_clock_invalid")
    return converted


def _uuid4_text(value: object, field_name: str) -> str:
    parsed: UUID | None = None
    if type(value) is str:
        with suppress(AttributeError, TypeError, ValueError):
            parsed = UUID(value)
    if parsed is None or parsed.version != 4 or str(parsed) != value:
        raise DailyCandleCollectionJobStoreError(
            f"daily_candle_collection_job_store_{field_name}_invalid"
        )
    return str(parsed)
