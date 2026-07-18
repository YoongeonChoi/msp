from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal
from uuid import UUID, uuid4

from app.application.ports.kr_calendar_collection_job_store_port import (
    KrCalendarCollectionDateAttemptV1,
    KrCalendarCollectionJobSnapshotV1,
    KrCalendarCollectionJobSpecV1,
    KrCalendarCollectionJobState,
    KrCalendarCollectionJobStorePort,
    canonical_kr_calendar_collection_job_snapshot,
    canonical_kr_calendar_collection_job_spec,
)
from app.application.ports.kr_daily_session_collection_port import (
    CollectedKrDailySessionObservationV1,
    KrDailySessionCollectorPort,
    canonical_collected_kr_daily_session_observation,
)
from app.application.use_cases.collect_kr_daily_session_observation import (
    KrDailySessionCollectionError,
)
from app.domain.common.errors import KnownFailClosedError
from app.domain.common.time import now_utc

KR_CALENDAR_DATE_RANGE_COLLECTION_RUN_SCHEMA_VERSION = "kr_calendar_date_range_collection_run.v1"
KR_CALENDAR_DATE_RANGE_COLLECTION_LIMITATIONS = (
    "manual_invocation_only",
    "one_date_per_invocation",
    "in_memory_reference_store_not_restart_durable",
    "durable_supabase_job_store_not_implemented",
    "runtime_and_scheduler_not_connected",
    "provider_authenticity_and_finality_not_proven",
    "official_exchange_calendar_completeness_not_proven",
    "full_data_quality_not_certified",
    "calendar_dataset_research_feature_backtest_strategy_order_use_not_authorized",
    "production_live_use_not_authorized",
)

KrCalendarDateRangeCollectionAction = Literal[
    "advanced",
    "completed",
    "completed_replay",
]

_RETRYABLE_PRE_WRITE_CODES = frozenset(
    {
        "kr_daily_session_collection_clock_invalid",
        "kr_daily_session_collection_clock_moved_backwards",
        "kr_daily_session_collection_observed_at_outside_read",
        "kr_daily_session_collection_session_date_mismatch",
        "kr_daily_session_collection_source_evidence_invalid",
        "kr_daily_session_collection_source_failed",
        "kr_daily_session_collection_target_date_invalid",
    }
)


class KrCalendarDateRangeCollectionJobError(KnownFailClosedError):
    def __init__(self, safe_message: str) -> None:
        super().__init__("kr_calendar_date_range_collection_job", safe_message)


@dataclass(frozen=True, slots=True, init=False)
class KrCalendarDateRangeCollectionRunResultV1:
    schema_version: str
    job_id: str
    spec_sha256: str
    provider: str
    market: str
    action: KrCalendarDateRangeCollectionAction
    processed_date: date | None
    job_state: KrCalendarCollectionJobState
    job_revision: int
    total_date_count: int
    confirmed_date_count: int
    remaining_date_count: int
    terminal_manifest_sha256: str | None
    manual_execution_only: bool
    automatic_retry_allowed: bool
    durable_runtime_configured: bool
    full_calendar_certified: bool
    limitations: tuple[str, ...]

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise KrCalendarDateRangeCollectionJobError(
            "kr_calendar_date_range_collection_result_requires_gate"
        )


class RunKrCalendarDateRangeCollectionJob:
    def __init__(
        self,
        collector: KrDailySessionCollectorPort,
        job_store: KrCalendarCollectionJobStorePort,
        *,
        holder_id: str,
        clock: Callable[[], datetime] = now_utc,
        attempt_id_factory: Callable[[], UUID] = uuid4,
        manual_execution_enabled: bool = False,
    ) -> None:
        self.collector = collector
        self.job_store = job_store
        self.holder_id = _uuid4_string(holder_id, "holder_id")
        self.clock = clock
        self.attempt_id_factory = attempt_id_factory
        self.manual_execution_enabled = manual_execution_enabled

    async def execute(
        self,
        spec: KrCalendarCollectionJobSpecV1,
    ) -> KrCalendarDateRangeCollectionRunResultV1:
        if self.manual_execution_enabled is not True:
            raise KrCalendarDateRangeCollectionJobError(
                "kr_calendar_date_range_collection_manual_execution_disabled"
            )
        canonical_spec = _spec(spec)
        transition_at = _read_clock(self.clock)
        snapshot = await self._load(canonical_spec, now=transition_at)

        if snapshot.state == "completed":
            return _run_result(
                snapshot,
                action="completed_replay",
                processed_date=None,
            )
        if snapshot.state in {"collecting", "blocked_unknown"}:
            raise KrCalendarDateRangeCollectionJobError(
                "kr_calendar_date_range_collection_unresolved_attempt"
            )
        if snapshot.state not in {"ready", "paused_retryable"}:
            raise KrCalendarDateRangeCollectionJobError(
                "kr_calendar_date_range_collection_snapshot_invalid"
            )
        target_date = snapshot.next_date
        if target_date is None:
            raise KrCalendarDateRangeCollectionJobError(
                "kr_calendar_date_range_collection_snapshot_invalid"
            )

        attempt_id = _new_attempt_id(self.attempt_id_factory)
        active = await self._begin(
            snapshot,
            attempt_id=attempt_id,
            target_date=target_date,
            now=transition_at,
        )

        failure_kind: Literal["retryable", "unknown"] | None = None
        try:
            source_collection = await self.collector.execute_with_evidence(target_date)
        except KrDailySessionCollectionError as exc:
            if (
                type(exc) is KrDailySessionCollectionError
                and exc.write_outcome == "not_attempted"
                and exc.safe_message in _RETRYABLE_PRE_WRITE_CODES
            ):
                failure_kind = "retryable"
            else:
                failure_kind = "unknown"
        except Exception:
            failure_kind = "unknown"

        if failure_kind == "retryable":
            await self._pause(active, reason_code="collection_failed_before_write")
            raise KrCalendarDateRangeCollectionJobError(
                "kr_calendar_date_range_collection_paused_retryable"
            )
        if failure_kind == "unknown":
            await self._block(active, reason_code="collection_write_outcome_unknown")
            raise KrCalendarDateRangeCollectionJobError(
                "kr_calendar_date_range_collection_blocked_unknown"
            )

        collection: CollectedKrDailySessionObservationV1 | None = None
        with suppress(Exception):
            collection = _collection(
                source_collection,
                spec=canonical_spec,
                target_date=target_date,
            )
        if collection is None:
            await self._block(active, reason_code="collection_evidence_invalid")
            raise KrCalendarDateRangeCollectionJobError(
                "kr_calendar_date_range_collection_collection_evidence_invalid"
            )
        confirmed_at = _read_clock(self.clock)
        confirmed = await self._confirm(
            active,
            collection=collection,
            now=confirmed_at,
        )
        action: KrCalendarDateRangeCollectionAction = (
            "completed" if confirmed.state == "completed" else "advanced"
        )
        return _run_result(
            confirmed,
            action=action,
            processed_date=target_date,
        )

    async def _load(
        self,
        spec: KrCalendarCollectionJobSpecV1,
        *,
        now: datetime,
    ) -> KrCalendarCollectionJobSnapshotV1:
        snapshot: KrCalendarCollectionJobSnapshotV1 | None = None
        store_spec = _spec(spec)
        try:
            source_snapshot = await self.job_store.load_or_create_job(
                store_spec,
                now=now,
            )
            snapshot = _snapshot(source_snapshot, spec=spec)
        except Exception:
            pass
        if snapshot is None:
            raise KrCalendarDateRangeCollectionJobError(
                "kr_calendar_date_range_collection_job_load_outcome_unknown"
            )
        return snapshot

    async def _begin(
        self,
        snapshot: KrCalendarCollectionJobSnapshotV1,
        *,
        attempt_id: str,
        target_date: date,
        now: datetime,
    ) -> KrCalendarCollectionJobSnapshotV1:
        begun: KrCalendarCollectionJobSnapshotV1 | None = None
        try:
            source_snapshot = await self.job_store.begin_date_attempt(
                job_id=snapshot.spec.job_id,
                spec_sha256=snapshot.spec.spec_sha256,
                expected_revision=snapshot.revision,
                attempt_id=attempt_id,
                holder_id=self.holder_id,
                target_date=target_date,
                now=now,
            )
            candidate = _snapshot(source_snapshot, spec=snapshot.spec)
            if _valid_begin_transition(
                before=snapshot,
                after=candidate,
                attempt_id=attempt_id,
                holder_id=self.holder_id,
                target_date=target_date,
                transition_at=now,
            ):
                begun = candidate
        except Exception:
            pass
        if begun is None:
            raise KrCalendarDateRangeCollectionJobError(
                "kr_calendar_date_range_collection_begin_outcome_unknown"
            )
        return begun

    async def _pause(
        self,
        snapshot: KrCalendarCollectionJobSnapshotV1,
        *,
        reason_code: str,
    ) -> KrCalendarCollectionJobSnapshotV1:
        attempt = _active_attempt(snapshot)
        paused: KrCalendarCollectionJobSnapshotV1 | None = None
        now = _read_clock(self.clock)
        try:
            source_snapshot = await self.job_store.pause_retryable(
                job_id=snapshot.spec.job_id,
                spec_sha256=snapshot.spec.spec_sha256,
                expected_revision=snapshot.revision,
                attempt_id=attempt.attempt_id,
                holder_id=attempt.holder_id,
                target_date=attempt.target_date,
                reason_code=reason_code,
                now=now,
            )
            candidate = _snapshot(source_snapshot, spec=snapshot.spec)
            if _valid_terminal_attempt_transition(
                before=snapshot,
                after=candidate,
                expected_state="paused_retryable",
                expected_reason=reason_code,
                keep_active=False,
                transition_at=now,
            ):
                paused = candidate
        except Exception:
            pass
        if paused is None:
            raise KrCalendarDateRangeCollectionJobError(
                "kr_calendar_date_range_collection_pause_outcome_unknown"
            )
        return paused

    async def _block(
        self,
        snapshot: KrCalendarCollectionJobSnapshotV1,
        *,
        reason_code: str,
    ) -> KrCalendarCollectionJobSnapshotV1:
        attempt = _active_attempt(snapshot)
        blocked: KrCalendarCollectionJobSnapshotV1 | None = None
        now = _read_clock(self.clock)
        try:
            source_snapshot = await self.job_store.block_unknown(
                job_id=snapshot.spec.job_id,
                spec_sha256=snapshot.spec.spec_sha256,
                expected_revision=snapshot.revision,
                attempt_id=attempt.attempt_id,
                holder_id=attempt.holder_id,
                target_date=attempt.target_date,
                reason_code=reason_code,
                now=now,
            )
            candidate = _snapshot(source_snapshot, spec=snapshot.spec)
            if _valid_terminal_attempt_transition(
                before=snapshot,
                after=candidate,
                expected_state="blocked_unknown",
                expected_reason=reason_code,
                keep_active=True,
                transition_at=now,
            ):
                blocked = candidate
        except Exception:
            pass
        if blocked is None:
            raise KrCalendarDateRangeCollectionJobError(
                "kr_calendar_date_range_collection_block_outcome_unknown"
            )
        return blocked

    async def _confirm(
        self,
        snapshot: KrCalendarCollectionJobSnapshotV1,
        *,
        collection: CollectedKrDailySessionObservationV1,
        now: datetime,
    ) -> KrCalendarCollectionJobSnapshotV1:
        attempt = _active_attempt(snapshot)
        confirmed: KrCalendarCollectionJobSnapshotV1 | None = None
        store_collection = canonical_collected_kr_daily_session_observation(collection)
        try:
            source_snapshot = await self.job_store.confirm_date(
                job_id=snapshot.spec.job_id,
                spec_sha256=snapshot.spec.spec_sha256,
                expected_revision=snapshot.revision,
                attempt_id=attempt.attempt_id,
                holder_id=attempt.holder_id,
                target_date=attempt.target_date,
                collection=store_collection,
                now=now,
            )
            candidate = _snapshot(source_snapshot, spec=snapshot.spec)
            if _valid_confirm_transition(
                before=snapshot,
                after=candidate,
                collection=collection,
                confirmed_at=now,
            ):
                confirmed = candidate
        except Exception:
            pass
        if confirmed is None:
            raise KrCalendarDateRangeCollectionJobError(
                "kr_calendar_date_range_collection_checkpoint_outcome_unknown"
            )
        return confirmed


def _valid_begin_transition(
    *,
    before: KrCalendarCollectionJobSnapshotV1,
    after: KrCalendarCollectionJobSnapshotV1,
    attempt_id: str,
    holder_id: str,
    target_date: date,
    transition_at: datetime,
) -> bool:
    attempt = after.active_attempt
    return (
        after.revision == before.revision + 1
        and after.state == "collecting"
        and after.checkpoints == before.checkpoints
        and after.created_at == before.created_at
        and after.state_reason is None
        and after.terminal_manifest_sha256 is None
        and attempt is not None
        and attempt.attempt_id == attempt_id
        and attempt.holder_id == holder_id
        and attempt.target_date == target_date
        and attempt.fencing_revision == after.revision
        and attempt.begun_at == transition_at
        and after.updated_at == transition_at
    )


def _valid_terminal_attempt_transition(
    *,
    before: KrCalendarCollectionJobSnapshotV1,
    after: KrCalendarCollectionJobSnapshotV1,
    expected_state: Literal["paused_retryable", "blocked_unknown"],
    expected_reason: str,
    keep_active: bool,
    transition_at: datetime,
) -> bool:
    expected_attempt = before.active_attempt if keep_active else None
    return (
        after.revision == before.revision + 1
        and after.state == expected_state
        and after.checkpoints == before.checkpoints
        and after.active_attempt == expected_attempt
        and after.state_reason == expected_reason
        and after.created_at == before.created_at
        and after.terminal_manifest_sha256 is None
        and after.updated_at == transition_at
    )


def _valid_confirm_transition(
    *,
    before: KrCalendarCollectionJobSnapshotV1,
    after: KrCalendarCollectionJobSnapshotV1,
    collection: CollectedKrDailySessionObservationV1,
    confirmed_at: datetime,
) -> bool:
    attempt = before.active_attempt
    if attempt is None or len(after.checkpoints) != len(before.checkpoints) + 1:
        return False
    checkpoint = after.checkpoints[-1]
    completed = after.confirmed_count == after.spec.total_days
    return (
        after.revision == before.revision + 1
        and after.state == ("completed" if completed else "ready")
        and after.checkpoints[:-1] == before.checkpoints
        and checkpoint.attempt_id == attempt.attempt_id
        and checkpoint.holder_id == attempt.holder_id
        and checkpoint.target_date == attempt.target_date
        and checkpoint.fencing_revision == attempt.fencing_revision
        and checkpoint.begun_at == attempt.begun_at
        and checkpoint.collection == collection
        and checkpoint.confirmed_at == confirmed_at
        and after.updated_at == confirmed_at
        and after.active_attempt is None
        and after.state_reason is None
        and after.created_at == before.created_at
        and (after.terminal_manifest_sha256 is not None) == completed
    )


def _active_attempt(
    snapshot: KrCalendarCollectionJobSnapshotV1,
) -> KrCalendarCollectionDateAttemptV1:
    attempt = snapshot.active_attempt
    if snapshot.state != "collecting" or attempt is None:
        raise KrCalendarDateRangeCollectionJobError(
            "kr_calendar_date_range_collection_active_attempt_invalid"
        )
    return attempt


def _spec(value: object) -> KrCalendarCollectionJobSpecV1:
    canonical: KrCalendarCollectionJobSpecV1 | None = None
    with suppress(Exception):
        canonical = canonical_kr_calendar_collection_job_spec(value)
    if canonical is None:
        raise KrCalendarDateRangeCollectionJobError(
            "kr_calendar_date_range_collection_spec_invalid"
        )
    return canonical


def _snapshot(
    value: object,
    *,
    spec: KrCalendarCollectionJobSpecV1,
) -> KrCalendarCollectionJobSnapshotV1:
    canonical: KrCalendarCollectionJobSnapshotV1 | None = None
    try:
        candidate = canonical_kr_calendar_collection_job_snapshot(value)
        if candidate.spec == spec and candidate.spec.spec_sha256 == spec.spec_sha256:
            canonical = candidate
    except Exception:
        pass
    if canonical is None:
        raise KrCalendarDateRangeCollectionJobError(
            "kr_calendar_date_range_collection_snapshot_invalid"
        )
    return canonical


def _collection(
    value: object,
    *,
    spec: KrCalendarCollectionJobSpecV1,
    target_date: date,
) -> CollectedKrDailySessionObservationV1:
    canonical: CollectedKrDailySessionObservationV1 | None = None
    try:
        candidate = canonical_collected_kr_daily_session_observation(value)
        if (
            candidate.target_date == target_date
            and candidate.session.provider == spec.provider
            and candidate.session.market == spec.market
        ):
            canonical = candidate
    except Exception:
        pass
    if canonical is None:
        raise KrCalendarDateRangeCollectionJobError(
            "kr_calendar_date_range_collection_collection_evidence_invalid"
        )
    return canonical


def _read_clock(clock: Callable[[], datetime]) -> datetime:
    canonical: datetime | None = None
    try:
        value = clock()
        if type(value) is datetime and value.tzinfo is not None and value.utcoffset() is not None:
            canonical = value.astimezone(UTC)
    except Exception:
        pass
    if canonical is None:
        raise KrCalendarDateRangeCollectionJobError(
            "kr_calendar_date_range_collection_clock_invalid"
        )
    return canonical


def _new_attempt_id(factory: Callable[[], UUID]) -> str:
    canonical: str | None = None
    try:
        value = factory()
        if type(value) is UUID and value.version == 4:
            canonical = str(value)
    except Exception:
        pass
    if canonical is None:
        raise KrCalendarDateRangeCollectionJobError(
            "kr_calendar_date_range_collection_attempt_id_invalid"
        )
    return canonical


def _uuid4_string(value: object, field_name: str) -> str:
    canonical: str | None = None
    try:
        if type(value) is str:
            parsed = UUID(value)
            if parsed.version == 4 and str(parsed) == value:
                canonical = value
    except (AttributeError, TypeError, ValueError):
        pass
    if canonical is None:
        raise KrCalendarDateRangeCollectionJobError(
            f"kr_calendar_date_range_collection_{field_name}_invalid"
        )
    return canonical


def _run_result(
    snapshot: KrCalendarCollectionJobSnapshotV1,
    *,
    action: KrCalendarDateRangeCollectionAction,
    processed_date: date | None,
) -> KrCalendarDateRangeCollectionRunResultV1:
    if action == "advanced":
        if snapshot.state != "ready" or processed_date is None:
            raise KrCalendarDateRangeCollectionJobError(
                "kr_calendar_date_range_collection_result_invalid"
            )
    elif action == "completed":
        if snapshot.state != "completed" or processed_date is None:
            raise KrCalendarDateRangeCollectionJobError(
                "kr_calendar_date_range_collection_result_invalid"
            )
    elif action == "completed_replay":
        if snapshot.state != "completed" or processed_date is not None:
            raise KrCalendarDateRangeCollectionJobError(
                "kr_calendar_date_range_collection_result_invalid"
            )
    else:
        raise KrCalendarDateRangeCollectionJobError(
            "kr_calendar_date_range_collection_result_invalid"
        )
    result = object.__new__(KrCalendarDateRangeCollectionRunResultV1)
    values: dict[str, object] = {
        "schema_version": KR_CALENDAR_DATE_RANGE_COLLECTION_RUN_SCHEMA_VERSION,
        "job_id": snapshot.spec.job_id,
        "spec_sha256": snapshot.spec.spec_sha256,
        "provider": snapshot.spec.provider,
        "market": snapshot.spec.market,
        "action": action,
        "processed_date": processed_date,
        "job_state": snapshot.state,
        "job_revision": snapshot.revision,
        "total_date_count": snapshot.spec.total_days,
        "confirmed_date_count": snapshot.confirmed_count,
        "remaining_date_count": snapshot.remaining_count,
        "terminal_manifest_sha256": snapshot.terminal_manifest_sha256,
        "manual_execution_only": True,
        "automatic_retry_allowed": False,
        "durable_runtime_configured": False,
        "full_calendar_certified": False,
        "limitations": KR_CALENDAR_DATE_RANGE_COLLECTION_LIMITATIONS,
    }
    for field_name, field_value in values.items():
        object.__setattr__(result, field_name, field_value)
    return result
