from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal, cast
from uuid import UUID, uuid4

from app.application.ports.kr_calendar_collection_job_store_port import (
    KrCalendarCollectionDateAttemptV1,
    KrCalendarCollectionJobPersistenceKind,
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
from app.application.services.kr_calendar_collection_recovery_assessment import (
    KR_CALENDAR_COLLECTION_RECOVERY_ASSESSMENT_LIMITATIONS,
    KR_CALENDAR_COLLECTION_RECOVERY_ASSESSMENT_SCHEMA_VERSION,
    KR_CALENDAR_COLLECTION_RETRYABLE_STATE_REASON,
    KrCalendarCollectionRecoveryAssessmentV1,
)
from app.application.use_cases.collect_kr_daily_session_observation import (
    KrDailySessionCollectionError,
)
from app.domain.common.errors import KnownFailClosedError
from app.domain.common.time import now_utc

KR_CALENDAR_DATE_RANGE_COLLECTION_RUN_SCHEMA_VERSION = "kr_calendar_date_range_collection_run.v1"
KR_CALENDAR_DATE_RANGE_COLLECTION_REFERENCE_LIMITATIONS = (
    "manual_invocation_only",
    "one_date_per_invocation",
    "job_store_durability_depends_on_explicit_adapter_configuration",
    "durable_supabase_job_store_not_runtime_configured",
    "worker_loop_and_scheduler_not_connected",
    "provider_authenticity_and_finality_not_proven",
    "official_exchange_calendar_completeness_not_proven",
    "full_data_quality_not_certified",
    "calendar_dataset_research_feature_backtest_strategy_order_use_not_authorized",
    "production_live_use_not_authorized",
)
KR_CALENDAR_DATE_RANGE_COLLECTION_DURABLE_LIMITATIONS = (
    "manual_invocation_only",
    "one_date_per_invocation",
    "automatic_retry_not_authorized",
    "durable_store_scope_limited_to_collection_checkpoint_state",
    "worker_loop_and_scheduler_not_connected",
    "provider_authenticity_and_finality_not_proven",
    "official_exchange_calendar_completeness_not_proven",
    "full_data_quality_not_certified",
    "calendar_dataset_research_feature_backtest_strategy_order_use_not_authorized",
    "production_live_use_not_authorized",
)
# Backward-compatible name for the reference adapter result contract.
KR_CALENDAR_DATE_RANGE_COLLECTION_LIMITATIONS = (
    KR_CALENDAR_DATE_RANGE_COLLECTION_REFERENCE_LIMITATIONS
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
    processed_checkpoint_attempt_id: str | None
    processed_checkpoint_holder_id: str | None
    processed_checkpoint_fencing_revision: int | None
    processed_checkpoint_begun_at: datetime | None
    processed_checkpoint_confirmed_at: datetime | None
    processed_receipt_status: str | None
    processed_receipt_calendar_idempotency_key: str | None
    processed_receipt_canonical_evidence_sha256: str | None
    processed_receipt_revision: int | None
    processed_receipt_revision_inserted: bool | None
    processed_receipt_occurrence_id: str | None
    processed_receipt_occurrence_inserted: bool | None
    processed_receipt_observed_at: datetime | None
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
        self.persistence_kind = _persistence_kind(job_store)

    async def execute(
        self,
        spec: KrCalendarCollectionJobSpecV1,
    ) -> KrCalendarDateRangeCollectionRunResultV1:
        return await self._execute(
            spec,
            recovery_assessment=None,
            paused_retry_confirmation=False,
        )

    async def execute_assessed(
        self,
        spec: KrCalendarCollectionJobSpecV1,
        recovery_assessment: KrCalendarCollectionRecoveryAssessmentV1,
        *,
        paused_retry_confirmation: bool = False,
    ) -> KrCalendarDateRangeCollectionRunResultV1:
        return await self._execute(
            spec,
            recovery_assessment=recovery_assessment,
            paused_retry_confirmation=paused_retry_confirmation,
        )

    async def _execute(
        self,
        spec: KrCalendarCollectionJobSpecV1,
        *,
        recovery_assessment: KrCalendarCollectionRecoveryAssessmentV1 | None,
        paused_retry_confirmation: bool,
    ) -> KrCalendarDateRangeCollectionRunResultV1:
        if self.manual_execution_enabled is not True:
            raise KrCalendarDateRangeCollectionJobError(
                "kr_calendar_date_range_collection_manual_execution_disabled"
            )
        canonical_spec = _spec(spec)
        assessment = _execution_assessment(
            recovery_assessment,
            spec=canonical_spec,
            persistence_kind=self.persistence_kind,
            paused_retry_confirmation=paused_retry_confirmation,
        )
        transition_at = _read_clock(self.clock)
        snapshot = await self._load(canonical_spec, now=transition_at)
        _bind_loaded_snapshot_to_assessment(
            snapshot,
            assessment=assessment,
            persistence_kind=self.persistence_kind,
            loaded_at=transition_at,
        )

        if snapshot.state == "completed":
            return _run_result(
                snapshot,
                action="completed_replay",
                processed_date=None,
                persistence_kind=self.persistence_kind,
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
            await self._pause(
                active,
                reason_code=KR_CALENDAR_COLLECTION_RETRYABLE_STATE_REASON,
            )
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
            persistence_kind=self.persistence_kind,
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


def _persistence_kind(
    store: KrCalendarCollectionJobStorePort,
) -> KrCalendarCollectionJobPersistenceKind:
    kind: object = None
    with suppress(Exception):
        kind = store.persistence_kind
    if type(kind) is not str or kind not in {"reference", "durable"}:
        raise KrCalendarDateRangeCollectionJobError(
            "kr_calendar_date_range_collection_job_store_persistence_kind_invalid"
        )
    return cast(KrCalendarCollectionJobPersistenceKind, kind)


def _execution_assessment(
    value: object,
    *,
    spec: KrCalendarCollectionJobSpecV1,
    persistence_kind: KrCalendarCollectionJobPersistenceKind,
    paused_retry_confirmation: object,
) -> KrCalendarCollectionRecoveryAssessmentV1 | None:
    if persistence_kind == "reference":
        if value is not None:
            raise KrCalendarDateRangeCollectionJobError(
                "kr_calendar_date_range_collection_recovery_assessment_requires_durable_store"
            )
        return None
    if value is None:
        raise KrCalendarDateRangeCollectionJobError(
            "kr_calendar_date_range_collection_recovery_assessment_required"
        )
    assessment = _canonical_execution_assessment(value, spec=spec)
    if assessment.classification not in {"missing", "ready", "paused_retryable"}:
        raise KrCalendarDateRangeCollectionJobError(
            "kr_calendar_date_range_collection_recovery_assessment_not_executable"
        )
    if assessment.classification == "paused_retryable":
        if paused_retry_confirmation is not True:
            raise KrCalendarDateRangeCollectionJobError(
                "kr_calendar_date_range_collection_paused_retry_confirmation_required"
            )
    elif paused_retry_confirmation is not False:
        raise KrCalendarDateRangeCollectionJobError(
            "kr_calendar_date_range_collection_paused_retry_confirmation_unexpected"
        )
    return assessment


def _canonical_execution_assessment(
    value: object,
    *,
    spec: KrCalendarCollectionJobSpecV1,
) -> KrCalendarCollectionRecoveryAssessmentV1:
    if type(value) is not KrCalendarCollectionRecoveryAssessmentV1:
        raise KrCalendarDateRangeCollectionJobError(
            "kr_calendar_date_range_collection_recovery_assessment_invalid"
        )
    assessment = value
    valid = True
    try:
        classification = assessment.classification
        if classification not in {
            "missing",
            "ready",
            "paused_retryable",
            "paused_unrecognized",
            "collecting",
            "blocked_unknown",
            "completed",
        }:
            raise ValueError("assessment_classification_invalid")
        expected_action = {
            "missing": "create_job_by_explicit_manual_invocation",
            "ready": "advance_next_date_by_explicit_manual_invocation",
            "paused_retryable": (
                "review_pre_write_failure_before_explicit_manual_invocation"
            ),
            "paused_unrecognized": "investigate_unrecognized_pause_without_retry",
            "collecting": "investigate_in_flight_attempt_without_retry",
            "blocked_unknown": "reconcile_unknown_write_outcome_without_retry",
            "completed": "no_action_completed",
        }[classification]
        missing = classification == "missing"
        completed = classification == "completed"
        unresolved_attempt = classification in {"collecting", "blocked_unknown"}
        unresolved_write_outcome = classification in {
            "paused_unrecognized",
            "collecting",
            "blocked_unknown",
        }
        explicit_candidate = classification in {"missing", "ready", "paused_retryable"}
        expected_job_state = (
            "paused_retryable" if classification == "paused_unrecognized" else classification
        )
        state_reason = assessment.state_reason
        if classification == "paused_retryable":
            state_reason_valid = (
                state_reason == KR_CALENDAR_COLLECTION_RETRYABLE_STATE_REASON
            )
        elif classification in {"paused_unrecognized", "blocked_unknown"}:
            state_reason_valid = (
                type(state_reason) is str
                and bool(state_reason)
                and (
                    classification != "paused_unrecognized"
                    or state_reason != KR_CALENDAR_COLLECTION_RETRYABLE_STATE_REASON
                )
            )
        else:
            state_reason_valid = state_reason is None
        confirmed_count = assessment.confirmed_date_count
        if (
            type(confirmed_count) is not int
            or confirmed_count < 0
            or confirmed_count > spec.total_days
            or (completed and confirmed_count != spec.total_days)
            or (not completed and confirmed_count >= spec.total_days)
        ):
            raise ValueError("assessment_count_invalid")
        expected_next_date = (
            None if completed else spec.start_date + timedelta(days=confirmed_count)
        )
        expected_revision = assessment.job_revision
        if missing:
            if expected_revision is not None or confirmed_count != 0:
                raise ValueError("assessment_missing_state_invalid")
        elif type(expected_revision) is not int or expected_revision <= 0:
            raise ValueError("assessment_revision_invalid")
        valid = all(
            (
                type(assessment.schema_version) is str,
                assessment.schema_version
                == KR_CALENDAR_COLLECTION_RECOVERY_ASSESSMENT_SCHEMA_VERSION,
                type(assessment.job_id) is str,
                assessment.job_id == spec.job_id,
                type(assessment.spec_sha256) is str,
                assessment.spec_sha256 == spec.spec_sha256,
                type(assessment.classification) is str,
                assessment.job_state is None
                or type(assessment.job_state) is str,
                assessment.job_state == (None if missing else expected_job_state),
                state_reason_valid,
                type(assessment.remaining_date_count) is int,
                assessment.remaining_date_count == spec.total_days - confirmed_count,
                assessment.next_date is None or type(assessment.next_date) is date,
                assessment.next_date == expected_next_date,
                assessment.unresolved_attempt_present is unresolved_attempt,
                assessment.recommended_operator_action == expected_action,
                assessment.explicit_manual_invocation_candidate is explicit_candidate,
                assessment.operator_review_required is (not completed),
                assessment.unresolved_write_outcome is unresolved_write_outcome,
                type(assessment.recommended_operator_action) is str,
                assessment.read_only is True,
                assessment.automatic_retry_allowed is False,
                assessment.mutation_performed is False,
                assessment.mutation_authorized is False,
                assessment.retry_authorized is False,
                assessment.manual_execution_authorized is False,
                assessment.manual_recovery_authorized is False,
                assessment.production_live_authorized is False,
                assessment.full_calendar_certified is False,
                type(assessment.limitations) is tuple,
                assessment.limitations
                == KR_CALENDAR_COLLECTION_RECOVERY_ASSESSMENT_LIMITATIONS,
            )
        )
    except Exception:
        valid = False
    if not valid:
        raise KrCalendarDateRangeCollectionJobError(
            "kr_calendar_date_range_collection_recovery_assessment_invalid"
        )
    return assessment


def _bind_loaded_snapshot_to_assessment(
    snapshot: KrCalendarCollectionJobSnapshotV1,
    *,
    assessment: KrCalendarCollectionRecoveryAssessmentV1 | None,
    persistence_kind: KrCalendarCollectionJobPersistenceKind,
    loaded_at: datetime,
) -> None:
    if persistence_kind == "reference":
        if assessment is not None:
            raise KrCalendarDateRangeCollectionJobError(
                "kr_calendar_date_range_collection_recovery_assessment_invalid"
            )
        return
    if assessment is None:
        raise KrCalendarDateRangeCollectionJobError(
            "kr_calendar_date_range_collection_recovery_assessment_required"
        )
    if assessment.classification == "missing":
        bound = (
            snapshot.state == "ready"
            and snapshot.revision == 1
            and snapshot.checkpoints == ()
            and snapshot.confirmed_count == 0
            and snapshot.remaining_count == snapshot.spec.total_days
            and snapshot.next_date == snapshot.spec.start_date
            and snapshot.active_attempt is None
            and snapshot.state_reason is None
            and snapshot.created_at == loaded_at
            and snapshot.updated_at == loaded_at
        )
    else:
        bound = (
            snapshot.state == assessment.classification
            and snapshot.revision == assessment.job_revision
            and snapshot.confirmed_count == assessment.confirmed_date_count
            and snapshot.remaining_count == assessment.remaining_date_count
            and snapshot.next_date == assessment.next_date
            and (snapshot.active_attempt is not None)
            is assessment.unresolved_attempt_present
            and snapshot.state_reason == assessment.state_reason
        )
    if not bound:
        raise KrCalendarDateRangeCollectionJobError(
            "kr_calendar_date_range_collection_recovery_assessment_stale"
        )


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
    persistence_kind: KrCalendarCollectionJobPersistenceKind,
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
    if processed_date is not None and not snapshot.checkpoints:
        raise KrCalendarDateRangeCollectionJobError(
            "kr_calendar_date_range_collection_result_invalid"
        )
    checkpoint = None if processed_date is None else snapshot.checkpoints[-1]
    if checkpoint is not None and checkpoint.target_date != processed_date:
        raise KrCalendarDateRangeCollectionJobError(
            "kr_calendar_date_range_collection_result_invalid"
        )
    receipt = None if checkpoint is None else checkpoint.collection.receipt
    durable = persistence_kind == "durable"
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
        "processed_checkpoint_attempt_id": (
            None if checkpoint is None else checkpoint.attempt_id
        ),
        "processed_checkpoint_holder_id": (
            None if checkpoint is None else checkpoint.holder_id
        ),
        "processed_checkpoint_fencing_revision": (
            None if checkpoint is None else checkpoint.fencing_revision
        ),
        "processed_checkpoint_begun_at": (
            None if checkpoint is None else checkpoint.begun_at
        ),
        "processed_checkpoint_confirmed_at": (
            None if checkpoint is None else checkpoint.confirmed_at
        ),
        "processed_receipt_status": None if receipt is None else receipt.status,
        "processed_receipt_calendar_idempotency_key": (
            None if receipt is None else receipt.calendar_idempotency_key
        ),
        "processed_receipt_canonical_evidence_sha256": (
            None if receipt is None else receipt.canonical_evidence_sha256
        ),
        "processed_receipt_revision": None if receipt is None else receipt.revision,
        "processed_receipt_revision_inserted": (
            None if receipt is None else receipt.revision_inserted
        ),
        "processed_receipt_occurrence_id": (
            None if receipt is None else str(receipt.occurrence_id)
        ),
        "processed_receipt_occurrence_inserted": (
            None if receipt is None else receipt.occurrence_inserted
        ),
        "processed_receipt_observed_at": (
            None if receipt is None else receipt.observed_at
        ),
        "manual_execution_only": True,
        "automatic_retry_allowed": False,
        "durable_runtime_configured": durable,
        "full_calendar_certified": False,
        "limitations": (
            KR_CALENDAR_DATE_RANGE_COLLECTION_DURABLE_LIMITATIONS
            if durable
            else KR_CALENDAR_DATE_RANGE_COLLECTION_REFERENCE_LIMITATIONS
        ),
    }
    for field_name, field_value in values.items():
        object.__setattr__(result, field_name, field_value)
    return result
