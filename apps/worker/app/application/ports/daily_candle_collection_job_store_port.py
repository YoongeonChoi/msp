from __future__ import annotations

import hashlib
import json
import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol
from uuid import UUID

from app.application.ports.candle_observation_store_port import (
    CandleObservationWriteReceipt,
)
from app.application.ports.persistence_authority import PersistenceAuthority
from app.domain.common.errors import KnownFailClosedError
from app.domain.common.json import JsonObject
from app.domain.market_data.point_in_time import PointInTimeCandleV1

DAILY_CANDLE_COLLECTION_JOB_SCHEMA_VERSION = "daily_candle_collection_job.v1"
DAILY_CANDLE_COLLECTION_RETRYABLE_REASONS: frozenset[str] = frozenset(
    {"provider_read_failed_before_candidate"}
)
DAILY_CANDLE_COLLECTION_PRE_CANDIDATE_BLOCK_REASONS: frozenset[str] = frozenset(
    {
        "provider_read_outcome_unknown_before_candidate",
        "unexpected_failure_before_candidate",
        "cancelled_before_candidate",
    }
)
DAILY_CANDLE_COLLECTION_POST_CANDIDATE_BLOCK_REASONS: frozenset[str] = frozenset(
    {
        "append_outcome_unknown",
        "confirm_outcome_unknown",
        "unexpected_failure_after_candidate",
        "cancelled_after_candidate",
    }
)

DAILY_CANDLE_COLLECTION_JOB_MAX_DATABASE_REVISION = 9_223_372_036_854_775_807
# Leave enough revision space for the longest remaining safe path.
DAILY_CANDLE_COLLECTION_JOB_MAX_BEGIN_EXPECTED_REVISION = (
    DAILY_CANDLE_COLLECTION_JOB_MAX_DATABASE_REVISION - 4
)
DAILY_CANDLE_COLLECTION_JOB_MAX_FENCE_EXPECTED_REVISION = (
    DAILY_CANDLE_COLLECTION_JOB_MAX_DATABASE_REVISION - 3
)
DAILY_CANDLE_COLLECTION_JOB_MAX_PAUSE_EXPECTED_REVISION = (
    DAILY_CANDLE_COLLECTION_JOB_MAX_DATABASE_REVISION - 5
)
DAILY_CANDLE_COLLECTION_JOB_MAX_BLOCK_EXPECTED_REVISION = (
    DAILY_CANDLE_COLLECTION_JOB_MAX_DATABASE_REVISION - 2
)
DAILY_CANDLE_COLLECTION_JOB_MAX_CONFIRM_EXPECTED_REVISION = (
    DAILY_CANDLE_COLLECTION_JOB_MAX_DATABASE_REVISION - 2
)

DailyCandleCollectionJobState = Literal[
    "ready",
    "collecting",
    "candidate_fenced",
    "paused_retryable",
    "blocked_unknown",
    "completed",
]
DailyCandleCollectionJobPersistenceKind = Literal["reference", "durable"]

_PROVIDER_RE = re.compile(r"[a-z][a-z0-9._-]{0,63}")
_KR_SYMBOL_RE = re.compile(r"[0-9]{6}")
_REASON_RE = re.compile(r"[a-z][a-z0-9_]{0,127}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class DailyCandleCollectionJobStoreError(KnownFailClosedError):
    def __init__(self, safe_message: str) -> None:
        super().__init__("daily_candle_collection_job_store", safe_message)


@dataclass(frozen=True, slots=True)
class DailyCandleCollectionJobSpecV1:
    """Immutable identity for exactly one daily candle collection request."""

    job_id: str
    provider: str
    symbol: str
    market: Literal["KR"]
    interval: Literal["1d"]
    adjusted: bool
    before: datetime
    provider_contract_sha256: str
    trigger: Literal["manual"]
    count: Literal[1] = 1
    pagination_allowed: Literal[False] = False
    automatic_retry_allowed: Literal[False] = False
    schema_version: str = DAILY_CANDLE_COLLECTION_JOB_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_uuid4(self.job_id, "job_id")
        if type(self.provider) is not str or _PROVIDER_RE.fullmatch(self.provider) is None:
            raise DailyCandleCollectionJobStoreError("daily_candle_collection_job_provider_invalid")
        if type(self.symbol) is not str or _KR_SYMBOL_RE.fullmatch(self.symbol) is None:
            raise DailyCandleCollectionJobStoreError("daily_candle_collection_job_symbol_invalid")
        if type(self.market) is not str or self.market != "KR":
            raise DailyCandleCollectionJobStoreError("daily_candle_collection_job_market_invalid")
        if type(self.interval) is not str or self.interval != "1d":
            raise DailyCandleCollectionJobStoreError("daily_candle_collection_job_interval_invalid")
        if type(self.adjusted) is not bool:
            raise DailyCandleCollectionJobStoreError("daily_candle_collection_job_adjusted_invalid")
        object.__setattr__(
            self,
            "before",
            _utc(self.before, "before"),
        )
        _require_sha256(self.provider_contract_sha256, "provider_contract_sha256")
        if type(self.trigger) is not str or self.trigger != "manual":
            raise DailyCandleCollectionJobStoreError("daily_candle_collection_job_trigger_invalid")
        if type(self.count) is not int or self.count != 1:
            raise DailyCandleCollectionJobStoreError("daily_candle_collection_job_count_invalid")
        if self.pagination_allowed is not False:
            raise DailyCandleCollectionJobStoreError(
                "daily_candle_collection_job_pagination_forbidden"
            )
        if self.automatic_retry_allowed is not False:
            raise DailyCandleCollectionJobStoreError(
                "daily_candle_collection_job_automatic_retry_forbidden"
            )
        if (
            type(self.schema_version) is not str
            or self.schema_version != DAILY_CANDLE_COLLECTION_JOB_SCHEMA_VERSION
        ):
            raise DailyCandleCollectionJobStoreError(
                "daily_candle_collection_job_schema_version_invalid"
            )

    @property
    def spec_sha256(self) -> str:
        return _payload_sha256(_spec_payload(self))


@dataclass(frozen=True, slots=True)
class DailyCandleCollectionAttemptV1:
    attempt_id: str
    holder_id: str
    fencing_revision: int
    begun_at: datetime

    def __post_init__(self) -> None:
        _require_uuid4(self.attempt_id, "attempt_id")
        _require_uuid4(self.holder_id, "holder_id")
        if (
            type(self.fencing_revision) is not int
            or self.fencing_revision <= 1
            or self.fencing_revision % 2 != 0
            or self.fencing_revision > DAILY_CANDLE_COLLECTION_JOB_MAX_FENCE_EXPECTED_REVISION
        ):
            raise DailyCandleCollectionJobStoreError(
                "daily_candle_collection_job_attempt_revision_invalid"
            )
        object.__setattr__(self, "begun_at", _utc(self.begun_at, "attempt_begun_at"))


@dataclass(frozen=True, slots=True)
class DailyCandleCollectionCandidateV1:
    attempt: DailyCandleCollectionAttemptV1
    candle: PointInTimeCandleV1
    fenced_at: datetime

    def __post_init__(self) -> None:
        canonical_attempt = canonical_daily_candle_collection_attempt(self.attempt)
        canonical_candle = _canonical_candle(self.candle)
        fenced_at = _utc(self.fenced_at, "candidate_fenced_at")
        if (
            canonical_candle.observed_at < canonical_attempt.begun_at
            or fenced_at < canonical_candle.observed_at
        ):
            raise DailyCandleCollectionJobStoreError(
                "daily_candle_collection_job_candidate_clock_invalid"
            )
        object.__setattr__(self, "attempt", canonical_attempt)
        object.__setattr__(self, "candle", canonical_candle)
        object.__setattr__(self, "fenced_at", fenced_at)


@dataclass(frozen=True, slots=True)
class DailyCandleCollectionWriteEvidenceV1:
    """Store-bound identities labeled by their persistence authority."""

    persistence_kind: DailyCandleCollectionJobPersistenceKind
    receipt: CandleObservationWriteReceipt
    content_revision_id: UUID
    occurrence_id: UUID
    occurrence_observed_at: datetime

    def __post_init__(self) -> None:
        if type(self.persistence_kind) is not str or self.persistence_kind not in {
            "reference",
            "durable",
        }:
            raise DailyCandleCollectionJobStoreError(
                "daily_candle_collection_job_write_evidence_persistence_invalid"
            )
        canonical_receipt = _canonical_receipt(self.receipt)
        content_revision_id = _canonical_uuid4(
            self.content_revision_id,
            "content_revision_id",
        )
        occurrence_id = _canonical_uuid4(self.occurrence_id, "occurrence_id")
        if occurrence_id == content_revision_id:
            raise DailyCandleCollectionJobStoreError(
                "daily_candle_collection_job_write_evidence_identity_invalid"
            )
        object.__setattr__(self, "receipt", canonical_receipt)
        object.__setattr__(self, "content_revision_id", content_revision_id)
        object.__setattr__(self, "occurrence_id", occurrence_id)
        object.__setattr__(
            self,
            "occurrence_observed_at",
            _utc(self.occurrence_observed_at, "occurrence_observed_at"),
        )


@dataclass(frozen=True, slots=True)
class DailyCandleCollectionCompletionV1:
    candidate: DailyCandleCollectionCandidateV1
    write_evidence: DailyCandleCollectionWriteEvidenceV1
    confirmed_at: datetime

    def __post_init__(self) -> None:
        canonical_candidate = canonical_daily_candle_collection_candidate(self.candidate)
        canonical_evidence = canonical_daily_candle_collection_write_evidence(self.write_evidence)
        confirmed_at = _utc(self.confirmed_at, "completion_confirmed_at")
        candle = canonical_candidate.candle
        canonical_receipt = canonical_evidence.receipt
        if (
            canonical_receipt.idempotency_key != candle.idempotency_key
            or canonical_receipt.canonical_observation_sha256 != candle.canonical_observation_sha256
            or canonical_receipt.stored_observed_at > candle.observed_at
            or canonical_evidence.occurrence_observed_at != candle.observed_at
        ):
            raise DailyCandleCollectionJobStoreError("daily_candle_collection_job_receipt_mismatch")
        if confirmed_at < canonical_candidate.fenced_at:
            raise DailyCandleCollectionJobStoreError(
                "daily_candle_collection_job_completion_clock_invalid"
            )
        object.__setattr__(self, "candidate", canonical_candidate)
        object.__setattr__(self, "write_evidence", canonical_evidence)
        object.__setattr__(self, "confirmed_at", confirmed_at)


@dataclass(frozen=True, slots=True)
class DailyCandleCollectionJobSnapshotV1:
    spec: DailyCandleCollectionJobSpecV1
    revision: int
    state: DailyCandleCollectionJobState
    active_attempt: DailyCandleCollectionAttemptV1 | None
    fenced_candidate: DailyCandleCollectionCandidateV1 | None
    completion: DailyCandleCollectionCompletionV1 | None
    state_reason: str | None
    created_at: datetime
    updated_at: datetime
    automatic_retry_allowed: bool = False

    def __post_init__(self) -> None:
        canonical_spec = canonical_daily_candle_collection_job_spec(self.spec)
        if (
            type(self.revision) is not int
            or self.revision <= 0
            or self.revision > DAILY_CANDLE_COLLECTION_JOB_MAX_DATABASE_REVISION
        ):
            raise DailyCandleCollectionJobStoreError("daily_candle_collection_job_revision_invalid")
        if type(self.state) is not str or self.state not in {
            "ready",
            "collecting",
            "candidate_fenced",
            "paused_retryable",
            "blocked_unknown",
            "completed",
        }:
            raise DailyCandleCollectionJobStoreError("daily_candle_collection_job_state_invalid")
        canonical_attempt = (
            None
            if self.active_attempt is None
            else canonical_daily_candle_collection_attempt(self.active_attempt)
        )
        canonical_candidate = (
            None
            if self.fenced_candidate is None
            else canonical_daily_candle_collection_candidate(self.fenced_candidate)
        )
        canonical_completion = (
            None
            if self.completion is None
            else canonical_daily_candle_collection_completion(self.completion)
        )
        created_at = _utc(self.created_at, "created_at")
        updated_at = _utc(self.updated_at, "updated_at")
        if updated_at < created_at:
            raise DailyCandleCollectionJobStoreError("daily_candle_collection_job_clock_invalid")
        if self.automatic_retry_allowed is not False:
            raise DailyCandleCollectionJobStoreError(
                "daily_candle_collection_job_automatic_retry_forbidden"
            )
        _validate_snapshot_state(
            spec=canonical_spec,
            revision=self.revision,
            state=self.state,
            active_attempt=canonical_attempt,
            fenced_candidate=canonical_candidate,
            completion=canonical_completion,
            state_reason=self.state_reason,
            created_at=created_at,
            updated_at=updated_at,
        )
        object.__setattr__(self, "spec", canonical_spec)
        object.__setattr__(self, "active_attempt", canonical_attempt)
        object.__setattr__(self, "fenced_candidate", canonical_candidate)
        object.__setattr__(self, "completion", canonical_completion)
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "updated_at", updated_at)


class DailyCandleCollectionJobStorePort(Protocol):
    @property
    def persistence_kind(self) -> DailyCandleCollectionJobPersistenceKind: ...

    @property
    def persistence_authority(self) -> PersistenceAuthority: ...

    async def load_or_create_job(
        self,
        spec: DailyCandleCollectionJobSpecV1,
        *,
        now: datetime,
    ) -> DailyCandleCollectionJobSnapshotV1: ...

    async def begin_attempt(
        self,
        *,
        job_id: str,
        spec_sha256: str,
        expected_revision: int,
        attempt_id: str,
        holder_id: str,
        now: datetime,
    ) -> DailyCandleCollectionJobSnapshotV1: ...

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
    ) -> DailyCandleCollectionJobSnapshotV1: ...

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
    ) -> DailyCandleCollectionJobSnapshotV1: ...

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
    ) -> DailyCandleCollectionJobSnapshotV1: ...

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
    ) -> DailyCandleCollectionJobSnapshotV1: ...


class DailyCandleCollectionJobInspectorPort(Protocol):
    async def inspect_job(
        self,
        job_id: str,
    ) -> DailyCandleCollectionJobSnapshotV1 | None: ...


def canonical_daily_candle_collection_job_spec(
    value: object,
) -> DailyCandleCollectionJobSpecV1:
    if type(value) is not DailyCandleCollectionJobSpecV1:
        raise DailyCandleCollectionJobStoreError("daily_candle_collection_job_spec_invalid")
    canonical: DailyCandleCollectionJobSpecV1 | None = None
    with suppress(Exception):
        canonical = DailyCandleCollectionJobSpecV1(
            job_id=value.job_id,
            provider=value.provider,
            symbol=value.symbol,
            market=value.market,
            interval=value.interval,
            adjusted=value.adjusted,
            before=value.before,
            provider_contract_sha256=value.provider_contract_sha256,
            trigger=value.trigger,
            count=value.count,
            pagination_allowed=value.pagination_allowed,
            automatic_retry_allowed=value.automatic_retry_allowed,
            schema_version=value.schema_version,
        )
    if canonical is None or canonical != value:
        raise DailyCandleCollectionJobStoreError("daily_candle_collection_job_spec_invalid")
    return canonical


def canonical_daily_candle_collection_attempt(
    value: object,
) -> DailyCandleCollectionAttemptV1:
    if type(value) is not DailyCandleCollectionAttemptV1:
        raise DailyCandleCollectionJobStoreError("daily_candle_collection_job_attempt_invalid")
    canonical: DailyCandleCollectionAttemptV1 | None = None
    with suppress(Exception):
        canonical = DailyCandleCollectionAttemptV1(
            attempt_id=value.attempt_id,
            holder_id=value.holder_id,
            fencing_revision=value.fencing_revision,
            begun_at=value.begun_at,
        )
    if canonical is None or canonical != value:
        raise DailyCandleCollectionJobStoreError("daily_candle_collection_job_attempt_invalid")
    return canonical


def canonical_daily_candle_collection_candidate(
    value: object,
) -> DailyCandleCollectionCandidateV1:
    if type(value) is not DailyCandleCollectionCandidateV1:
        raise DailyCandleCollectionJobStoreError("daily_candle_collection_job_candidate_invalid")
    canonical: DailyCandleCollectionCandidateV1 | None = None
    with suppress(Exception):
        canonical = DailyCandleCollectionCandidateV1(
            attempt=value.attempt,
            candle=value.candle,
            fenced_at=value.fenced_at,
        )
    if canonical is None or canonical != value:
        raise DailyCandleCollectionJobStoreError("daily_candle_collection_job_candidate_invalid")
    return canonical


def canonical_daily_candle_collection_write_evidence(
    value: object,
) -> DailyCandleCollectionWriteEvidenceV1:
    if type(value) is not DailyCandleCollectionWriteEvidenceV1:
        raise DailyCandleCollectionJobStoreError(
            "daily_candle_collection_job_write_evidence_invalid"
        )
    canonical: DailyCandleCollectionWriteEvidenceV1 | None = None
    with suppress(Exception):
        canonical = DailyCandleCollectionWriteEvidenceV1(
            persistence_kind=value.persistence_kind,
            receipt=value.receipt,
            content_revision_id=value.content_revision_id,
            occurrence_id=value.occurrence_id,
            occurrence_observed_at=value.occurrence_observed_at,
        )
    if canonical is None or canonical != value:
        raise DailyCandleCollectionJobStoreError(
            "daily_candle_collection_job_write_evidence_invalid"
        )
    return canonical


def canonical_daily_candle_collection_completion(
    value: object,
) -> DailyCandleCollectionCompletionV1:
    if type(value) is not DailyCandleCollectionCompletionV1:
        raise DailyCandleCollectionJobStoreError("daily_candle_collection_job_completion_invalid")
    canonical: DailyCandleCollectionCompletionV1 | None = None
    with suppress(Exception):
        canonical = DailyCandleCollectionCompletionV1(
            candidate=value.candidate,
            write_evidence=value.write_evidence,
            confirmed_at=value.confirmed_at,
        )
    if canonical is None or canonical != value:
        raise DailyCandleCollectionJobStoreError("daily_candle_collection_job_completion_invalid")
    return canonical


def canonical_daily_candle_collection_job_snapshot(
    value: object,
) -> DailyCandleCollectionJobSnapshotV1:
    if type(value) is not DailyCandleCollectionJobSnapshotV1:
        raise DailyCandleCollectionJobStoreError("daily_candle_collection_job_snapshot_invalid")
    canonical: DailyCandleCollectionJobSnapshotV1 | None = None
    with suppress(Exception):
        canonical = DailyCandleCollectionJobSnapshotV1(
            spec=value.spec,
            revision=value.revision,
            state=value.state,
            active_attempt=value.active_attempt,
            fenced_candidate=value.fenced_candidate,
            completion=value.completion,
            state_reason=value.state_reason,
            created_at=value.created_at,
            updated_at=value.updated_at,
            automatic_retry_allowed=value.automatic_retry_allowed,
        )
    if canonical is None or canonical != value:
        raise DailyCandleCollectionJobStoreError("daily_candle_collection_job_snapshot_invalid")
    return canonical


def _validate_snapshot_state(
    *,
    spec: DailyCandleCollectionJobSpecV1,
    revision: int,
    state: DailyCandleCollectionJobState,
    active_attempt: DailyCandleCollectionAttemptV1 | None,
    fenced_candidate: DailyCandleCollectionCandidateV1 | None,
    completion: DailyCandleCollectionCompletionV1 | None,
    state_reason: str | None,
    created_at: datetime,
    updated_at: datetime,
) -> None:
    if state_reason is not None and (
        type(state_reason) is not str or _REASON_RE.fullmatch(state_reason) is None
    ):
        raise DailyCandleCollectionJobStoreError("daily_candle_collection_job_state_reason_invalid")
    if active_attempt is not None and not (created_at <= active_attempt.begun_at <= updated_at):
        raise DailyCandleCollectionJobStoreError(
            "daily_candle_collection_job_active_attempt_invalid"
        )
    if fenced_candidate is not None:
        _validate_candidate_scope(spec, fenced_candidate)
        if fenced_candidate.fenced_at > updated_at:
            raise DailyCandleCollectionJobStoreError(
                "daily_candle_collection_job_candidate_clock_invalid"
            )
    if completion is not None:
        _validate_candidate_scope(spec, completion.candidate)
        if (
            completion.candidate.attempt.begun_at < created_at
            or completion.confirmed_at > updated_at
        ):
            raise DailyCandleCollectionJobStoreError(
                "daily_candle_collection_job_completion_clock_invalid"
            )

    if state == "ready":
        valid = (
            revision == 1
            and active_attempt is None
            and fenced_candidate is None
            and completion is None
            and state_reason is None
        )
    elif state == "collecting":
        valid = (
            active_attempt is not None
            and revision == active_attempt.fencing_revision
            and fenced_candidate is None
            and completion is None
            and state_reason is None
        )
    elif state == "candidate_fenced":
        valid = (
            active_attempt is not None
            and fenced_candidate is not None
            and fenced_candidate.attempt == active_attempt
            and revision == active_attempt.fencing_revision + 1
            and completion is None
            and state_reason is None
        )
    elif state == "paused_retryable":
        valid = (
            revision >= 3
            and revision % 2 == 1
            and revision <= DAILY_CANDLE_COLLECTION_JOB_MAX_BEGIN_EXPECTED_REVISION
            and active_attempt is None
            and fenced_candidate is None
            and completion is None
            and state_reason in DAILY_CANDLE_COLLECTION_RETRYABLE_REASONS
        )
    elif state == "blocked_unknown":
        pre_candidate_blocked = (
            active_attempt is not None
            and fenced_candidate is None
            and revision == active_attempt.fencing_revision + 1
            and state_reason in DAILY_CANDLE_COLLECTION_PRE_CANDIDATE_BLOCK_REASONS
        )
        post_candidate_blocked = (
            active_attempt is not None
            and fenced_candidate is not None
            and fenced_candidate.attempt == active_attempt
            and revision == active_attempt.fencing_revision + 2
            and state_reason in DAILY_CANDLE_COLLECTION_POST_CANDIDATE_BLOCK_REASONS
        )
        valid = (
            (pre_candidate_blocked or post_candidate_blocked)
            and completion is None
            and state_reason is not None
        )
    else:
        valid = (
            completion is not None
            and revision == completion.candidate.attempt.fencing_revision + 2
            and active_attempt is None
            and fenced_candidate is None
            and state_reason is None
        )
    if not valid:
        raise DailyCandleCollectionJobStoreError(
            "daily_candle_collection_job_state_contract_invalid"
        )


def _validate_candidate_scope(
    spec: DailyCandleCollectionJobSpecV1,
    candidate: DailyCandleCollectionCandidateV1,
) -> None:
    candle = candidate.candle
    if (
        candle.provider != spec.provider
        or candle.symbol != spec.symbol
        or candle.market != spec.market
        or candle.interval != spec.interval
        or candle.adjusted is not spec.adjusted
        or candle.provider_event_at > spec.before
        or candle.provider_contract_sha256 != spec.provider_contract_sha256
    ):
        raise DailyCandleCollectionJobStoreError(
            "daily_candle_collection_job_candidate_scope_mismatch"
        )


def _canonical_candle(value: object) -> PointInTimeCandleV1:
    if type(value) is not PointInTimeCandleV1:
        raise DailyCandleCollectionJobStoreError("daily_candle_collection_job_candle_invalid")
    canonical: PointInTimeCandleV1 | None = None
    with suppress(Exception):
        canonical = PointInTimeCandleV1.from_payload(value.to_payload())
    if canonical is None or canonical != value:
        raise DailyCandleCollectionJobStoreError("daily_candle_collection_job_candle_invalid")
    return canonical


def _canonical_receipt(value: object) -> CandleObservationWriteReceipt:
    if type(value) is not CandleObservationWriteReceipt:
        raise DailyCandleCollectionJobStoreError("daily_candle_collection_job_receipt_invalid")
    canonical: CandleObservationWriteReceipt | None = None
    with suppress(Exception):
        canonical = CandleObservationWriteReceipt(
            idempotency_key=value.idempotency_key,
            canonical_observation_sha256=value.canonical_observation_sha256,
            revision=value.revision,
            inserted=value.inserted,
            stored_observed_at=_utc(value.stored_observed_at, "receipt_stored_observed_at"),
        )
    if canonical is None or canonical != value:
        raise DailyCandleCollectionJobStoreError("daily_candle_collection_job_receipt_invalid")
    return canonical


def _spec_payload(spec: DailyCandleCollectionJobSpecV1) -> JsonObject:
    return {
        "schema_version": spec.schema_version,
        "job_id": spec.job_id,
        "provider": spec.provider,
        "symbol": spec.symbol,
        "market": spec.market,
        "interval": spec.interval,
        "adjusted": spec.adjusted,
        "before": _canonical_timestamp(spec.before),
        "provider_contract_sha256": spec.provider_contract_sha256,
        "trigger": spec.trigger,
        "count": spec.count,
        "pagination_allowed": spec.pagination_allowed,
        "automatic_retry_allowed": spec.automatic_retry_allowed,
    }


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
        raise DailyCandleCollectionJobStoreError("daily_candle_collection_job_manifest_invalid")
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_timestamp(value: datetime) -> str:
    return _utc(value, "manifest_timestamp").isoformat().replace("+00:00", "Z")


def _require_uuid4(value: object, field_name: str) -> str:
    parsed: UUID | None = None
    if type(value) is str:
        with suppress(AttributeError, TypeError, ValueError):
            parsed = UUID(value)
    if parsed is None or parsed.version != 4 or str(parsed) != value:
        raise DailyCandleCollectionJobStoreError(
            f"daily_candle_collection_job_{field_name}_invalid"
        )
    return str(parsed)


def _canonical_uuid4(value: object, field_name: str) -> UUID:
    if type(value) is not UUID or value.version != 4:
        raise DailyCandleCollectionJobStoreError(
            f"daily_candle_collection_job_{field_name}_invalid"
        )
    return UUID(str(value))


def _require_sha256(value: object, field_name: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise DailyCandleCollectionJobStoreError(
            f"daily_candle_collection_job_{field_name}_invalid"
        )
    return value


def _utc(value: object, field_name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None:
        raise DailyCandleCollectionJobStoreError(
            f"daily_candle_collection_job_{field_name}_invalid"
        )
    converted: datetime | None = None
    with suppress(OverflowError, RuntimeError, TypeError, ValueError):
        if value.utcoffset() is not None:
            converted = value.astimezone(UTC)
    if converted is None:
        raise DailyCandleCollectionJobStoreError(
            f"daily_candle_collection_job_{field_name}_invalid"
        )
    return converted
