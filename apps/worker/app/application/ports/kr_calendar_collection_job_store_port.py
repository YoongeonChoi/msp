from __future__ import annotations

import hashlib
import json
import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal, Protocol
from uuid import UUID

from app.application.ports.kr_daily_session_collection_port import (
    CollectedKrDailySessionObservationV1,
    canonical_collected_kr_daily_session_observation,
)
from app.domain.common.errors import KnownFailClosedError
from app.domain.common.json import JsonObject, JsonValue

KR_CALENDAR_COLLECTION_JOB_SCHEMA_VERSION = "kr_calendar_collection_job.v1"
KR_CALENDAR_COLLECTION_JOB_MANIFEST_SCHEMA_VERSION = "kr_calendar_collection_job_manifest.v1"
KR_CALENDAR_COLLECTION_JOB_MAX_DAYS = 366

KrCalendarCollectionJobState = Literal[
    "ready",
    "collecting",
    "paused_retryable",
    "blocked_unknown",
    "completed",
]

_PROVIDER_RE = re.compile(r"[a-z][a-z0-9._-]{0,63}")
_REASON_RE = re.compile(r"[a-z][a-z0-9_]{0,127}")


class KrCalendarCollectionJobStoreError(KnownFailClosedError):
    def __init__(self, safe_message: str) -> None:
        super().__init__("kr_calendar_collection_job_store", safe_message)


@dataclass(frozen=True, slots=True)
class KrCalendarCollectionJobSpecV1:
    job_id: str
    provider: str
    market: str
    start_date: date
    end_date: date
    trigger: Literal["manual"]
    schema_version: str = KR_CALENDAR_COLLECTION_JOB_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_uuid4(self.job_id, "job_id")
        if type(self.provider) is not str or _PROVIDER_RE.fullmatch(self.provider) is None:
            raise KrCalendarCollectionJobStoreError("kr_calendar_collection_job_provider_invalid")
        if type(self.market) is not str or self.market != "KR":
            raise KrCalendarCollectionJobStoreError("kr_calendar_collection_job_market_invalid")
        _require_date(self.start_date, "start_date")
        _require_date(self.end_date, "end_date")
        if self.end_date < self.start_date:
            raise KrCalendarCollectionJobStoreError("kr_calendar_collection_job_date_range_invalid")
        if self.total_days > KR_CALENDAR_COLLECTION_JOB_MAX_DAYS:
            raise KrCalendarCollectionJobStoreError(
                "kr_calendar_collection_job_date_range_too_large"
            )
        if type(self.trigger) is not str or self.trigger != "manual":
            raise KrCalendarCollectionJobStoreError("kr_calendar_collection_job_trigger_invalid")
        if (
            type(self.schema_version) is not str
            or self.schema_version != KR_CALENDAR_COLLECTION_JOB_SCHEMA_VERSION
        ):
            raise KrCalendarCollectionJobStoreError(
                "kr_calendar_collection_job_schema_version_invalid"
            )

    @property
    def total_days(self) -> int:
        return (self.end_date - self.start_date).days + 1

    @property
    def spec_sha256(self) -> str:
        return _payload_sha256(_spec_payload(self))


@dataclass(frozen=True, slots=True)
class KrCalendarCollectionDateAttemptV1:
    attempt_id: str
    holder_id: str
    target_date: date
    fencing_revision: int
    begun_at: datetime

    def __post_init__(self) -> None:
        _require_uuid4(self.attempt_id, "attempt_id")
        _require_uuid4(self.holder_id, "holder_id")
        _require_date(self.target_date, "attempt_target_date")
        if type(self.fencing_revision) is not int or self.fencing_revision <= 1:
            raise KrCalendarCollectionJobStoreError(
                "kr_calendar_collection_job_attempt_revision_invalid"
            )
        object.__setattr__(
            self,
            "begun_at",
            _utc(self.begun_at, "attempt_begun_at"),
        )


@dataclass(frozen=True, slots=True)
class KrCalendarCollectionDateCheckpointV1:
    attempt_id: str
    holder_id: str
    target_date: date
    fencing_revision: int
    begun_at: datetime
    collection: CollectedKrDailySessionObservationV1
    confirmed_at: datetime

    def __post_init__(self) -> None:
        _require_uuid4(self.attempt_id, "checkpoint_attempt_id")
        _require_uuid4(self.holder_id, "checkpoint_holder_id")
        _require_date(self.target_date, "checkpoint_target_date")
        if type(self.fencing_revision) is not int or self.fencing_revision <= 1:
            raise KrCalendarCollectionJobStoreError(
                "kr_calendar_collection_job_checkpoint_revision_invalid"
            )
        begun_at = _utc(self.begun_at, "checkpoint_begun_at")
        canonical_collection = canonical_collected_kr_daily_session_observation(self.collection)
        if canonical_collection.target_date != self.target_date:
            raise KrCalendarCollectionJobStoreError(
                "kr_calendar_collection_job_checkpoint_date_mismatch"
            )
        object.__setattr__(self, "collection", canonical_collection)
        object.__setattr__(self, "begun_at", begun_at)
        object.__setattr__(
            self,
            "confirmed_at",
            _utc(self.confirmed_at, "checkpoint_confirmed_at"),
        )
        if self.confirmed_at < self.begun_at:
            raise KrCalendarCollectionJobStoreError(
                "kr_calendar_collection_job_checkpoint_clock_invalid"
            )
        if not (self.begun_at <= canonical_collection.session.observed_at <= self.confirmed_at):
            raise KrCalendarCollectionJobStoreError(
                "kr_calendar_collection_job_checkpoint_observation_clock_invalid"
            )


@dataclass(frozen=True, slots=True)
class KrCalendarCollectionJobSnapshotV1:
    spec: KrCalendarCollectionJobSpecV1
    revision: int
    state: KrCalendarCollectionJobState
    checkpoints: tuple[KrCalendarCollectionDateCheckpointV1, ...]
    active_attempt: KrCalendarCollectionDateAttemptV1 | None
    state_reason: str | None
    terminal_manifest_sha256: str | None
    created_at: datetime
    updated_at: datetime
    automatic_retry_allowed: bool = False

    def __post_init__(self) -> None:
        canonical_spec = canonical_kr_calendar_collection_job_spec(self.spec)
        created_at = _utc(self.created_at, "created_at")
        updated_at = _utc(self.updated_at, "updated_at")
        if updated_at < created_at:
            raise KrCalendarCollectionJobStoreError("kr_calendar_collection_job_clock_invalid")
        if type(self.revision) is not int or self.revision <= 0:
            raise KrCalendarCollectionJobStoreError("kr_calendar_collection_job_revision_invalid")
        if type(self.state) is not str or self.state not in {
            "ready",
            "collecting",
            "paused_retryable",
            "blocked_unknown",
            "completed",
        }:
            raise KrCalendarCollectionJobStoreError("kr_calendar_collection_job_state_invalid")
        if type(self.checkpoints) is not tuple:
            raise KrCalendarCollectionJobStoreError(
                "kr_calendar_collection_job_checkpoints_invalid"
            )
        canonical_checkpoints = tuple(
            canonical_kr_calendar_collection_checkpoint(item) for item in self.checkpoints
        )
        if len(canonical_checkpoints) > canonical_spec.total_days:
            raise KrCalendarCollectionJobStoreError(
                "kr_calendar_collection_job_checkpoints_invalid"
            )
        seen_attempt_ids: set[str] = set()
        seen_occurrence_ids: set[UUID] = set()
        seen_calendar_keys: set[str] = set()
        previous_confirmed_at = created_at
        previous_fencing_revision = 1
        for index, checkpoint in enumerate(canonical_checkpoints):
            expected_date = canonical_spec.start_date + timedelta(days=index)
            collection = checkpoint.collection
            if checkpoint.target_date != expected_date:
                raise KrCalendarCollectionJobStoreError(
                    "kr_calendar_collection_job_checkpoint_sequence_invalid"
                )
            if checkpoint.attempt_id in seen_attempt_ids:
                raise KrCalendarCollectionJobStoreError(
                    "kr_calendar_collection_job_checkpoint_attempt_reused"
                )
            seen_attempt_ids.add(checkpoint.attempt_id)
            receipt = collection.receipt
            if (
                receipt.occurrence_id in seen_occurrence_ids
                or receipt.calendar_idempotency_key in seen_calendar_keys
            ):
                raise KrCalendarCollectionJobStoreError(
                    "kr_calendar_collection_job_checkpoint_lineage_reused"
                )
            seen_occurrence_ids.add(receipt.occurrence_id)
            seen_calendar_keys.add(receipt.calendar_idempotency_key)
            if (
                checkpoint.fencing_revision <= previous_fencing_revision
                or checkpoint.fencing_revision % 2 != 0
                or checkpoint.begun_at < previous_confirmed_at
                or checkpoint.confirmed_at > updated_at
            ):
                raise KrCalendarCollectionJobStoreError(
                    "kr_calendar_collection_job_checkpoint_sequence_invalid"
                )
            previous_fencing_revision = checkpoint.fencing_revision
            previous_confirmed_at = checkpoint.confirmed_at
            if (
                collection.session.provider != canonical_spec.provider
                or collection.session.market != canonical_spec.market
            ):
                raise KrCalendarCollectionJobStoreError(
                    "kr_calendar_collection_job_checkpoint_scope_mismatch"
                )

        canonical_attempt = (
            None
            if self.active_attempt is None
            else canonical_kr_calendar_collection_attempt(self.active_attempt)
        )
        if canonical_attempt is not None and (
            canonical_attempt.fencing_revision <= previous_fencing_revision
            or canonical_attempt.fencing_revision % 2 != 0
            or canonical_attempt.begun_at < previous_confirmed_at
            or canonical_attempt.begun_at > updated_at
        ):
            raise KrCalendarCollectionJobStoreError(
                "kr_calendar_collection_job_active_attempt_invalid"
            )
        if self.automatic_retry_allowed is not False:
            raise KrCalendarCollectionJobStoreError(
                "kr_calendar_collection_job_automatic_retry_forbidden"
            )
        _validate_snapshot_state(
            spec=canonical_spec,
            revision=self.revision,
            state=self.state,
            checkpoints=canonical_checkpoints,
            active_attempt=canonical_attempt,
            state_reason=self.state_reason,
            terminal_manifest_sha256=self.terminal_manifest_sha256,
        )
        object.__setattr__(self, "spec", canonical_spec)
        object.__setattr__(self, "checkpoints", canonical_checkpoints)
        object.__setattr__(self, "active_attempt", canonical_attempt)
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "updated_at", updated_at)

    @property
    def confirmed_count(self) -> int:
        return len(self.checkpoints)

    @property
    def remaining_count(self) -> int:
        return self.spec.total_days - self.confirmed_count

    @property
    def next_date(self) -> date | None:
        if self.remaining_count == 0:
            return None
        return self.spec.start_date + timedelta(days=self.confirmed_count)


class KrCalendarCollectionJobStorePort(Protocol):
    async def load_or_create_job(
        self,
        spec: KrCalendarCollectionJobSpecV1,
        *,
        now: datetime,
    ) -> KrCalendarCollectionJobSnapshotV1: ...

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
    ) -> KrCalendarCollectionJobSnapshotV1: ...

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
    ) -> KrCalendarCollectionJobSnapshotV1: ...

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
    ) -> KrCalendarCollectionJobSnapshotV1: ...

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
    ) -> KrCalendarCollectionJobSnapshotV1: ...


class KrCalendarCollectionJobInspectorPort(Protocol):
    async def inspect_job(
        self,
        job_id: str,
    ) -> KrCalendarCollectionJobSnapshotV1 | None: ...


def canonical_kr_calendar_collection_job_spec(
    value: object,
) -> KrCalendarCollectionJobSpecV1:
    if type(value) is not KrCalendarCollectionJobSpecV1:
        raise KrCalendarCollectionJobStoreError("kr_calendar_collection_job_spec_invalid")
    canonical: KrCalendarCollectionJobSpecV1 | None = None
    with suppress(Exception):
        canonical = KrCalendarCollectionJobSpecV1(
            job_id=value.job_id,
            provider=value.provider,
            market=value.market,
            start_date=value.start_date,
            end_date=value.end_date,
            trigger=value.trigger,
            schema_version=value.schema_version,
        )
    if canonical is None or canonical != value:
        raise KrCalendarCollectionJobStoreError("kr_calendar_collection_job_spec_invalid")
    return canonical


def canonical_kr_calendar_collection_attempt(
    value: object,
) -> KrCalendarCollectionDateAttemptV1:
    if type(value) is not KrCalendarCollectionDateAttemptV1:
        raise KrCalendarCollectionJobStoreError("kr_calendar_collection_job_attempt_invalid")
    canonical: KrCalendarCollectionDateAttemptV1 | None = None
    with suppress(Exception):
        canonical = KrCalendarCollectionDateAttemptV1(
            attempt_id=value.attempt_id,
            holder_id=value.holder_id,
            target_date=value.target_date,
            fencing_revision=value.fencing_revision,
            begun_at=value.begun_at,
        )
    if canonical is None or canonical != value:
        raise KrCalendarCollectionJobStoreError("kr_calendar_collection_job_attempt_invalid")
    return canonical


def canonical_kr_calendar_collection_checkpoint(
    value: object,
) -> KrCalendarCollectionDateCheckpointV1:
    if type(value) is not KrCalendarCollectionDateCheckpointV1:
        raise KrCalendarCollectionJobStoreError("kr_calendar_collection_job_checkpoint_invalid")
    canonical: KrCalendarCollectionDateCheckpointV1 | None = None
    with suppress(Exception):
        canonical = KrCalendarCollectionDateCheckpointV1(
            attempt_id=value.attempt_id,
            holder_id=value.holder_id,
            target_date=value.target_date,
            fencing_revision=value.fencing_revision,
            begun_at=value.begun_at,
            collection=value.collection,
            confirmed_at=value.confirmed_at,
        )
    if canonical is None or canonical != value:
        raise KrCalendarCollectionJobStoreError("kr_calendar_collection_job_checkpoint_invalid")
    return canonical


def canonical_kr_calendar_collection_job_snapshot(
    value: object,
) -> KrCalendarCollectionJobSnapshotV1:
    if type(value) is not KrCalendarCollectionJobSnapshotV1:
        raise KrCalendarCollectionJobStoreError("kr_calendar_collection_job_snapshot_invalid")
    canonical: KrCalendarCollectionJobSnapshotV1 | None = None
    with suppress(Exception):
        canonical = KrCalendarCollectionJobSnapshotV1(
            spec=value.spec,
            revision=value.revision,
            state=value.state,
            checkpoints=value.checkpoints,
            active_attempt=value.active_attempt,
            state_reason=value.state_reason,
            terminal_manifest_sha256=value.terminal_manifest_sha256,
            created_at=value.created_at,
            updated_at=value.updated_at,
            automatic_retry_allowed=value.automatic_retry_allowed,
        )
    if canonical is None or canonical != value:
        raise KrCalendarCollectionJobStoreError("kr_calendar_collection_job_snapshot_invalid")
    return canonical


def kr_calendar_collection_job_manifest_sha256(
    spec: KrCalendarCollectionJobSpecV1,
    checkpoints: tuple[KrCalendarCollectionDateCheckpointV1, ...],
) -> str:
    canonical_spec = canonical_kr_calendar_collection_job_spec(spec)
    if type(checkpoints) is not tuple:
        raise KrCalendarCollectionJobStoreError("kr_calendar_collection_job_checkpoints_invalid")
    canonical_checkpoints = tuple(
        canonical_kr_calendar_collection_checkpoint(item) for item in checkpoints
    )
    _validate_manifest_checkpoints(canonical_spec, canonical_checkpoints)
    payload: JsonObject = {
        "schema_version": KR_CALENDAR_COLLECTION_JOB_MANIFEST_SCHEMA_VERSION,
        "spec": _spec_payload(canonical_spec),
        "spec_sha256": canonical_spec.spec_sha256,
        "confirmed_count": len(canonical_checkpoints),
        "checkpoints": [_checkpoint_payload(checkpoint) for checkpoint in canonical_checkpoints],
    }
    return _payload_sha256(payload)


def _validate_snapshot_state(
    *,
    spec: KrCalendarCollectionJobSpecV1,
    revision: int,
    state: KrCalendarCollectionJobState,
    checkpoints: tuple[KrCalendarCollectionDateCheckpointV1, ...],
    active_attempt: KrCalendarCollectionDateAttemptV1 | None,
    state_reason: str | None,
    terminal_manifest_sha256: str | None,
) -> None:
    complete = len(checkpoints) == spec.total_days
    last_checkpoint_revision = checkpoints[-1].fencing_revision if checkpoints else 0
    stable_revision = last_checkpoint_revision + 1 if checkpoints else 1
    if state in {"ready", "completed"} and revision != stable_revision:
        raise KrCalendarCollectionJobStoreError(
            "kr_calendar_collection_job_revision_sequence_invalid"
        )
    if state == "paused_retryable" and (
        revision <= stable_revision or (revision - stable_revision) % 2 != 0
    ):
        raise KrCalendarCollectionJobStoreError(
            "kr_calendar_collection_job_revision_sequence_invalid"
        )
    if state == "collecting" and revision % 2 != 0:
        raise KrCalendarCollectionJobStoreError(
            "kr_calendar_collection_job_revision_sequence_invalid"
        )
    if state == "blocked_unknown" and revision % 2 != 1:
        raise KrCalendarCollectionJobStoreError(
            "kr_calendar_collection_job_revision_sequence_invalid"
        )
    if state_reason is not None and (
        type(state_reason) is not str or _REASON_RE.fullmatch(state_reason) is None
    ):
        raise KrCalendarCollectionJobStoreError("kr_calendar_collection_job_state_reason_invalid")
    if state in {"collecting", "blocked_unknown"}:
        if active_attempt is None or complete:
            raise KrCalendarCollectionJobStoreError(
                "kr_calendar_collection_job_active_attempt_invalid"
            )
        expected_date = spec.start_date + timedelta(days=len(checkpoints))
        if (
            active_attempt.target_date != expected_date
            or (state == "collecting" and active_attempt.fencing_revision != revision)
            or (state == "blocked_unknown" and active_attempt.fencing_revision != revision - 1)
        ):
            raise KrCalendarCollectionJobStoreError(
                "kr_calendar_collection_job_active_attempt_invalid"
            )
    elif active_attempt is not None:
        raise KrCalendarCollectionJobStoreError("kr_calendar_collection_job_active_attempt_invalid")
    if state in {"paused_retryable", "blocked_unknown"}:
        if state_reason is None:
            raise KrCalendarCollectionJobStoreError(
                "kr_calendar_collection_job_state_reason_invalid"
            )
    elif state_reason is not None:
        raise KrCalendarCollectionJobStoreError("kr_calendar_collection_job_state_reason_invalid")
    if state == "completed":
        expected_manifest = kr_calendar_collection_job_manifest_sha256(
            spec,
            checkpoints,
        )
        if (
            not complete
            or type(terminal_manifest_sha256) is not str
            or terminal_manifest_sha256 != expected_manifest
        ):
            raise KrCalendarCollectionJobStoreError(
                "kr_calendar_collection_job_terminal_manifest_invalid"
            )
    else:
        if complete or terminal_manifest_sha256 is not None:
            raise KrCalendarCollectionJobStoreError(
                "kr_calendar_collection_job_terminal_manifest_invalid"
            )


def _validate_manifest_checkpoints(
    spec: KrCalendarCollectionJobSpecV1,
    checkpoints: tuple[KrCalendarCollectionDateCheckpointV1, ...],
) -> None:
    if len(checkpoints) != spec.total_days:
        raise KrCalendarCollectionJobStoreError(
            "kr_calendar_collection_job_terminal_manifest_requires_complete_range"
        )
    seen_attempts: set[str] = set()
    seen_occurrences: set[UUID] = set()
    seen_calendar_keys: set[str] = set()
    previous_fencing_revision = 1
    previous_confirmed_at: datetime | None = None
    for index, checkpoint in enumerate(checkpoints):
        collection = checkpoint.collection
        receipt = collection.receipt
        if (
            checkpoint.target_date != spec.start_date + timedelta(days=index)
            or collection.session.provider != spec.provider
            or collection.session.market != spec.market
            or checkpoint.fencing_revision <= previous_fencing_revision
            or checkpoint.fencing_revision % 2 != 0
            or (previous_confirmed_at is not None and checkpoint.begun_at < previous_confirmed_at)
            or checkpoint.attempt_id in seen_attempts
            or receipt.occurrence_id in seen_occurrences
            or receipt.calendar_idempotency_key in seen_calendar_keys
        ):
            raise KrCalendarCollectionJobStoreError(
                "kr_calendar_collection_job_terminal_manifest_checkpoints_invalid"
            )
        previous_fencing_revision = checkpoint.fencing_revision
        previous_confirmed_at = checkpoint.confirmed_at
        seen_attempts.add(checkpoint.attempt_id)
        seen_occurrences.add(receipt.occurrence_id)
        seen_calendar_keys.add(receipt.calendar_idempotency_key)


def _spec_payload(spec: KrCalendarCollectionJobSpecV1) -> JsonObject:
    return {
        "schema_version": spec.schema_version,
        "job_id": spec.job_id,
        "provider": spec.provider,
        "market": spec.market,
        "start_date": spec.start_date.isoformat(),
        "end_date": spec.end_date.isoformat(),
        "trigger": spec.trigger,
        "maximum_inclusive_days": KR_CALENDAR_COLLECTION_JOB_MAX_DAYS,
        "automatic_retry_allowed": False,
    }


def _checkpoint_payload(
    checkpoint: KrCalendarCollectionDateCheckpointV1,
) -> JsonObject:
    collection = checkpoint.collection
    receipt = collection.receipt
    receipt_payload: JsonObject = {
        "status": receipt.status,
        "calendar_idempotency_key": receipt.calendar_idempotency_key,
        "canonical_evidence_sha256": receipt.canonical_evidence_sha256,
        "revision": receipt.revision,
        "revision_inserted": receipt.revision_inserted,
        "occurrence_id": str(receipt.occurrence_id),
        "occurrence_inserted": receipt.occurrence_inserted,
        "observed_at": _canonical_timestamp(receipt.observed_at),
    }
    value: dict[str, JsonValue] = {
        "attempt_id": checkpoint.attempt_id,
        "holder_id": checkpoint.holder_id,
        "target_date": checkpoint.target_date.isoformat(),
        "fencing_revision": checkpoint.fencing_revision,
        "begun_at": _canonical_timestamp(checkpoint.begun_at),
        "session": collection.session.to_payload(),
        "receipt": receipt_payload,
        "confirmed_at": _canonical_timestamp(checkpoint.confirmed_at),
    }
    return value


def _payload_sha256(value: JsonObject) -> str:
    canonical: str | None = None
    with suppress(TypeError, ValueError):
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    if canonical is None:
        raise KrCalendarCollectionJobStoreError("kr_calendar_collection_job_manifest_invalid")
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_timestamp(value: datetime) -> str:
    return _utc(value, "manifest_timestamp").isoformat().replace("+00:00", "Z")


def _require_uuid4(value: object, field_name: str) -> str:
    if type(value) is not str:
        raise KrCalendarCollectionJobStoreError(f"kr_calendar_collection_job_{field_name}_invalid")
    parsed: UUID | None = None
    with suppress(AttributeError, TypeError, ValueError):
        parsed = UUID(value)
    if parsed is None or parsed.version != 4 or str(parsed) != value:
        raise KrCalendarCollectionJobStoreError(f"kr_calendar_collection_job_{field_name}_invalid")
    return value


def _require_date(value: object, field_name: str) -> date:
    if type(value) is not date:
        raise KrCalendarCollectionJobStoreError(f"kr_calendar_collection_job_{field_name}_invalid")
    return value


def _utc(value: object, field_name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None:
        raise KrCalendarCollectionJobStoreError(f"kr_calendar_collection_job_{field_name}_invalid")
    converted: datetime | None = None
    try:
        if value.utcoffset() is not None:
            converted = value.astimezone(UTC)
    except (OverflowError, RuntimeError, TypeError, ValueError):
        pass
    if converted is None:
        raise KrCalendarCollectionJobStoreError(f"kr_calendar_collection_job_{field_name}_invalid")
    return converted
