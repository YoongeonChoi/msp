from __future__ import annotations

import re
from contextlib import suppress
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID

import httpx

from app.application.ports.candle_observation_store_port import (
    CandleObservationWriteReceipt,
)
from app.application.ports.daily_candle_collection_job_store_port import (
    DAILY_CANDLE_COLLECTION_JOB_MAX_BEGIN_EXPECTED_REVISION,
    DAILY_CANDLE_COLLECTION_JOB_MAX_BLOCK_EXPECTED_REVISION,
    DAILY_CANDLE_COLLECTION_JOB_MAX_CONFIRM_EXPECTED_REVISION,
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
    DailyCandleCollectionJobState,
    DailyCandleCollectionJobStoreError,
    DailyCandleCollectionWriteEvidenceV1,
    canonical_daily_candle_collection_job_snapshot,
    canonical_daily_candle_collection_job_spec,
)
from app.config import Settings
from app.domain.common.json import JsonObject
from app.domain.market_data.point_in_time import PointInTimeCandleV1
from app.infrastructure.bounded_json import (
    BoundedJsonError,
    bounded_json_response,
)
from app.infrastructure.supabase_headers import supabase_api_headers

DailyCandleCollectionJobRpc = Literal[
    "load_or_create_pit_daily_candle_collection_job_v1",
    "inspect_pit_daily_candle_collection_job_v1",
    "begin_pit_daily_candle_collection_attempt_v1",
    "fence_pit_daily_candle_collection_candidate_v1",
    "pause_pit_daily_candle_collection_attempt_v1",
    "block_pit_daily_candle_collection_attempt_v1",
    "confirm_pit_daily_candle_collection_attempt_v1",
]

DAILY_CANDLE_COLLECTION_JOB_RPC_ALLOWLIST: frozenset[str] = frozenset(
    {
        "load_or_create_pit_daily_candle_collection_job_v1",
        "inspect_pit_daily_candle_collection_job_v1",
        "begin_pit_daily_candle_collection_attempt_v1",
        "fence_pit_daily_candle_collection_candidate_v1",
        "pause_pit_daily_candle_collection_attempt_v1",
        "block_pit_daily_candle_collection_attempt_v1",
        "confirm_pit_daily_candle_collection_attempt_v1",
    }
)

# Only exact migration-owned messages may cross the PostgREST trust boundary.
DAILY_CANDLE_COLLECTION_JOB_SAFE_DATABASE_ERRORS: frozenset[str] = frozenset(
    {
        "daily_candle_collection_job_attempt_fence_mismatch",
        "daily_candle_collection_job_attempt_reused",
        "daily_candle_collection_job_clock_regressed",
        "daily_candle_collection_job_not_found",
        "daily_candle_collection_job_revision_exhausted",
        "daily_candle_collection_job_revision_conflict",
        "daily_candle_collection_job_spec_conflict",
        "daily_candle_collection_job_spec_hash_mismatch",
    }
)

_SAFE_DATABASE_ERROR_MAP = {
    f"pit_{safe_message}": safe_message
    for safe_message in DAILY_CANDLE_COLLECTION_JOB_SAFE_DATABASE_ERRORS
}

DAILY_CANDLE_COLLECTION_JOB_SNAPSHOT_SCHEMA_VERSION = "daily_candle_collection_job_snapshot.v1"
DAILY_CANDLE_COLLECTION_JOB_MAX_RPC_RESPONSE_BYTES = 64 * 1024

_MAX_DATABASE_BIGINT = 9_223_372_036_854_775_807
_MAX_TRANSITION_REVISION = _MAX_DATABASE_BIGINT - 1
_REASON_RE = re.compile(r"[a-z][a-z0-9_]{0,127}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SNAPSHOT_FIELDS = frozenset(
    {
        "schema_version",
        "spec_sha256",
        "spec",
        "revision",
        "state",
        "active_attempt",
        "candidate",
        "completion",
        "state_reason",
        "created_at",
        "updated_at",
        "automatic_retry_allowed",
    }
)
_SPEC_FIELDS = frozenset(
    {
        "schema_version",
        "job_id",
        "provider",
        "symbol",
        "market",
        "interval",
        "adjusted",
        "before",
        "provider_contract_sha256",
        "trigger",
        "count",
        "pagination_allowed",
        "automatic_retry_allowed",
    }
)
_ATTEMPT_FIELDS = frozenset({"attempt_id", "holder_id", "fencing_revision", "begun_at"})
_CANDIDATE_FIELDS = frozenset(
    {
        "attempt_id",
        "holder_id",
        "fencing_revision",
        "begun_at",
        "idempotency_key",
        "canonical_observation_sha256",
        "candle",
        "fenced_at",
    }
)
_COMPLETION_FIELDS = frozenset(
    {
        "persistence_kind",
        "occurrence_id",
        "content_revision_id",
        "occurrence_observed_at",
        "content_revision",
        "content_revision_observed_at",
        "idempotency_key",
        "canonical_observation_sha256",
        "receipt",
        "confirmed_at",
    }
)
_CANDLE_FIELDS = frozenset(
    {
        "schema_version",
        "provider",
        "symbol",
        "market",
        "interval",
        "adjusted",
        "provider_event_at",
        "observed_at",
        "currency",
        "open_krw",
        "high_krw",
        "low_krw",
        "close_krw",
        "volume",
        "provider_contract_sha256",
        "canonical_observation_sha256",
    }
)
_RECEIPT_FIELDS = frozenset(
    {
        "idempotency_key",
        "canonical_observation_sha256",
        "revision",
        "inserted",
        "stored_observed_at",
    }
)


class SupabaseDailyCandleCollectionJobStore:
    """Durable RPC-only state machine for one manually selected daily candle.

    This adapter performs no table CRUD, retry, scheduler, strategy, or order
    operation. A caller must explicitly select it and drive every transition.
    """

    persistence_kind: DailyCandleCollectionJobPersistenceKind = "durable"

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not settings.supabase_url or settings.supabase_secret_key is None:
            raise DailyCandleCollectionJobStoreError(
                "daily_candle_collection_job_store_credentials_missing"
            )
        secret = settings.supabase_secret_key.get_secret_value()
        self.base_url = settings.supabase_url.rstrip("/") + "/rest/v1/rpc"
        self.headers = supabase_api_headers(secret) | {
            "accept-profile": "worker_api",
            "accept-encoding": "identity",
            "content-profile": "worker_api",
            "content-type": "application/json",
        }
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=10.0,
            headers=self.headers,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def load_or_create_job(
        self,
        spec: DailyCandleCollectionJobSpecV1,
        *,
        now: datetime,
    ) -> DailyCandleCollectionJobSnapshotV1:
        canonical_spec = _canonical_spec(spec)
        canonical_now = _utc_input(now, "now")
        snapshot = _snapshot_from_rpc(
            await self._rpc(
                "load_or_create_pit_daily_candle_collection_job_v1",
                {
                    "p_spec": _spec_payload(canonical_spec),
                    "p_now": _timestamp(canonical_now),
                },
            )
        )
        if snapshot.spec != canonical_spec:
            _binding_error()
        return snapshot

    async def inspect_job(
        self,
        job_id: str,
    ) -> DailyCandleCollectionJobSnapshotV1 | None:
        canonical_job_id = _uuid4_text(job_id, "job_id")
        snapshot = _inspection_from_rpc(
            await self._rpc(
                "inspect_pit_daily_candle_collection_job_v1",
                {"p_job_id": canonical_job_id},
            )
        )
        if snapshot is not None and snapshot.spec.job_id != canonical_job_id:
            _binding_error()
        return snapshot

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
        inputs = _transition_inputs(
            job_id=job_id,
            spec_sha256=spec_sha256,
            expected_revision=expected_revision,
            attempt_id=attempt_id,
            holder_id=holder_id,
            now=now,
            maximum_expected_revision=DAILY_CANDLE_COLLECTION_JOB_MAX_BEGIN_EXPECTED_REVISION,
        )
        snapshot = _snapshot_from_rpc(
            await self._rpc(
                "begin_pit_daily_candle_collection_attempt_v1",
                inputs.payload,
            )
        )
        attempt = snapshot.active_attempt
        if (
            not _identity_matches(snapshot, inputs.job_id, inputs.spec_sha256)
            or snapshot.revision != inputs.expected_revision + 1
            or snapshot.state != "collecting"
            or snapshot.updated_at != inputs.now
            or attempt is None
            or attempt.attempt_id != inputs.attempt_id
            or attempt.holder_id != inputs.holder_id
            or attempt.fencing_revision != inputs.expected_revision + 1
            or attempt.begun_at != inputs.now
        ):
            _binding_error()
        return snapshot

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
        inputs = _transition_inputs(
            job_id=job_id,
            spec_sha256=spec_sha256,
            expected_revision=expected_revision,
            attempt_id=attempt_id,
            holder_id=holder_id,
            now=now,
            maximum_expected_revision=DAILY_CANDLE_COLLECTION_JOB_MAX_FENCE_EXPECTED_REVISION,
        )
        fence = _fencing_revision(fencing_revision)
        if fence != inputs.expected_revision:
            raise DailyCandleCollectionJobStoreError(
                "daily_candle_collection_job_store_fencing_revision_invalid"
            )
        canonical_candle = _canonical_candle(candle)
        snapshot = _snapshot_from_rpc(
            await self._rpc(
                "fence_pit_daily_candle_collection_candidate_v1",
                inputs.payload
                | {
                    "p_fencing_revision": fence,
                    "p_candidate": canonical_candle.to_payload(),
                },
            )
        )
        attempt = snapshot.active_attempt
        candidate = snapshot.fenced_candidate
        if (
            not _identity_matches(snapshot, inputs.job_id, inputs.spec_sha256)
            or snapshot.revision != inputs.expected_revision + 1
            or snapshot.state != "candidate_fenced"
            or snapshot.updated_at != inputs.now
            or attempt is None
            or candidate is None
            or candidate.attempt != attempt
            or attempt.attempt_id != inputs.attempt_id
            or attempt.holder_id != inputs.holder_id
            or attempt.fencing_revision != fence
            or candidate.candle != canonical_candle
            or candidate.fenced_at != inputs.now
        ):
            _binding_error()
        return snapshot

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
        inputs = _transition_inputs(
            job_id=job_id,
            spec_sha256=spec_sha256,
            expected_revision=expected_revision,
            attempt_id=attempt_id,
            holder_id=holder_id,
            now=now,
            maximum_expected_revision=DAILY_CANDLE_COLLECTION_JOB_MAX_PAUSE_EXPECTED_REVISION,
        )
        fence = _fencing_revision(fencing_revision)
        if fence != inputs.expected_revision:
            raise DailyCandleCollectionJobStoreError(
                "daily_candle_collection_job_store_fencing_revision_invalid"
            )
        reason = _reason(reason_code)
        if reason not in DAILY_CANDLE_COLLECTION_RETRYABLE_REASONS:
            raise DailyCandleCollectionJobStoreError(
                "daily_candle_collection_job_store_pause_reason_invalid"
            )
        snapshot = _snapshot_from_rpc(
            await self._rpc(
                "pause_pit_daily_candle_collection_attempt_v1",
                inputs.payload | {"p_fencing_revision": fence, "p_reason_code": reason},
            )
        )
        if (
            not _identity_matches(snapshot, inputs.job_id, inputs.spec_sha256)
            or snapshot.revision != inputs.expected_revision + 1
            or snapshot.state != "paused_retryable"
            or snapshot.active_attempt is not None
            or snapshot.fenced_candidate is not None
            or snapshot.completion is not None
            or snapshot.state_reason != reason
            or snapshot.updated_at != inputs.now
        ):
            _binding_error()
        return snapshot

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
        inputs = _transition_inputs(
            job_id=job_id,
            spec_sha256=spec_sha256,
            expected_revision=expected_revision,
            attempt_id=attempt_id,
            holder_id=holder_id,
            now=now,
            maximum_expected_revision=DAILY_CANDLE_COLLECTION_JOB_MAX_BLOCK_EXPECTED_REVISION,
        )
        fence = _fencing_revision(fencing_revision)
        if inputs.expected_revision not in {fence, fence + 1}:
            raise DailyCandleCollectionJobStoreError(
                "daily_candle_collection_job_store_fencing_revision_invalid"
            )
        reason = _reason(reason_code)
        allowed_reasons = (
            DAILY_CANDLE_COLLECTION_PRE_CANDIDATE_BLOCK_REASONS
            if inputs.expected_revision == fence
            else DAILY_CANDLE_COLLECTION_POST_CANDIDATE_BLOCK_REASONS
        )
        if reason not in allowed_reasons:
            raise DailyCandleCollectionJobStoreError(
                "daily_candle_collection_job_store_block_reason_invalid"
            )
        snapshot = _snapshot_from_rpc(
            await self._rpc(
                "block_pit_daily_candle_collection_attempt_v1",
                inputs.payload | {"p_fencing_revision": fence, "p_reason_code": reason},
            )
        )
        attempt = snapshot.active_attempt
        candidate = snapshot.fenced_candidate
        candidate_shape_matches = (
            candidate is None
            if inputs.expected_revision == fence
            else candidate is not None and candidate.attempt == attempt
        )
        if (
            not _identity_matches(snapshot, inputs.job_id, inputs.spec_sha256)
            or snapshot.revision != inputs.expected_revision + 1
            or snapshot.state != "blocked_unknown"
            or snapshot.state_reason != reason
            or snapshot.updated_at != inputs.now
            or attempt is None
            or not candidate_shape_matches
            or attempt.attempt_id != inputs.attempt_id
            or attempt.holder_id != inputs.holder_id
            or attempt.fencing_revision != fence
        ):
            _binding_error()
        return snapshot

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
        inputs = _transition_inputs(
            job_id=job_id,
            spec_sha256=spec_sha256,
            expected_revision=expected_revision,
            attempt_id=attempt_id,
            holder_id=holder_id,
            now=now,
            maximum_expected_revision=DAILY_CANDLE_COLLECTION_JOB_MAX_CONFIRM_EXPECTED_REVISION,
        )
        fence = _fencing_revision(fencing_revision)
        if inputs.expected_revision != fence + 1:
            raise DailyCandleCollectionJobStoreError(
                "daily_candle_collection_job_store_fencing_revision_invalid"
            )
        canonical_receipt = _canonical_receipt(receipt)
        snapshot = _snapshot_from_rpc(
            await self._rpc(
                "confirm_pit_daily_candle_collection_attempt_v1",
                inputs.payload
                | {
                    "p_fencing_revision": fence,
                    "p_receipt": _receipt_payload(canonical_receipt),
                },
            )
        )
        completion = snapshot.completion
        if (
            not _identity_matches(snapshot, inputs.job_id, inputs.spec_sha256)
            or snapshot.revision != inputs.expected_revision + 1
            or snapshot.state != "completed"
            or snapshot.active_attempt is not None
            or snapshot.fenced_candidate is not None
            or snapshot.state_reason is not None
            or snapshot.updated_at != inputs.now
            or completion is None
            or completion.candidate.attempt.attempt_id != inputs.attempt_id
            or completion.candidate.attempt.holder_id != inputs.holder_id
            or completion.candidate.attempt.fencing_revision != fence
            or completion.write_evidence.receipt != canonical_receipt
            or completion.confirmed_at != inputs.now
        ):
            _binding_error()
        return snapshot

    async def _rpc(
        self,
        rpc: DailyCandleCollectionJobRpc,
        payload: JsonObject,
    ) -> object:
        if type(rpc) is not str or rpc not in DAILY_CANDLE_COLLECTION_JOB_RPC_ALLOWLIST:
            raise DailyCandleCollectionJobStoreError(
                "daily_candle_collection_job_store_rpc_not_allowed"
            )
        if type(payload) is not dict:
            raise DailyCandleCollectionJobStoreError(
                "daily_candle_collection_job_store_rpc_payload_invalid"
            )
        result: object = None
        failure: str | None = None
        try:
            async with self.client.stream(
                "POST",
                f"{self.base_url}/{rpc}",
                headers=self.headers,
                json=payload,
            ) as response:
                parsed: object = None
                try:
                    parsed = await bounded_json_response(
                        response,
                        max_bytes=DAILY_CANDLE_COLLECTION_JOB_MAX_RPC_RESPONSE_BYTES,
                    )
                except BoundedJsonError:
                    failure = (
                        "daily_candle_collection_job_store_rpc_failed_or_returned_invalid_json"
                    )
                if failure is None:
                    if response.is_success:
                        result = parsed
                    else:
                        failure = _safe_database_error(parsed) or (
                            "daily_candle_collection_job_store_rpc_failed_or_returned_invalid_json"
                        )
        except Exception:
            failure = "daily_candle_collection_job_store_rpc_failed_or_returned_invalid_json"
        if failure is not None:
            raise DailyCandleCollectionJobStoreError(failure) from None
        return result


class _TransitionInputs:
    __slots__ = (
        "job_id",
        "spec_sha256",
        "expected_revision",
        "attempt_id",
        "holder_id",
        "now",
        "payload",
    )

    def __init__(
        self,
        *,
        job_id: str,
        spec_sha256: str,
        expected_revision: int,
        attempt_id: str,
        holder_id: str,
        now: datetime,
    ) -> None:
        self.job_id = job_id
        self.spec_sha256 = spec_sha256
        self.expected_revision = expected_revision
        self.attempt_id = attempt_id
        self.holder_id = holder_id
        self.now = now
        self.payload: JsonObject = {
            "p_job_id": job_id,
            "p_spec_sha256": spec_sha256,
            "p_expected_revision": expected_revision,
            "p_attempt_id": attempt_id,
            "p_holder_id": holder_id,
            "p_now": _timestamp(now),
        }


def _transition_inputs(
    *,
    job_id: object,
    spec_sha256: object,
    expected_revision: object,
    attempt_id: object,
    holder_id: object,
    now: object,
    maximum_expected_revision: int,
) -> _TransitionInputs:
    return _TransitionInputs(
        job_id=_uuid4_text(job_id, "job_id"),
        spec_sha256=_sha256_value(spec_sha256, "spec_sha256"),
        expected_revision=_revision(expected_revision, maximum_expected_revision),
        attempt_id=_uuid4_text(attempt_id, "attempt_id"),
        holder_id=_uuid4_text(holder_id, "holder_id"),
        now=_utc_input(now, "now"),
    )


def _canonical_spec(value: object) -> DailyCandleCollectionJobSpecV1:
    try:
        canonical = canonical_daily_candle_collection_job_spec(value)
    except Exception:
        canonical = None
    if canonical is None:
        raise DailyCandleCollectionJobStoreError(
            "daily_candle_collection_job_store_spec_invalid"
        ) from None
    return canonical


def _canonical_candle(value: object) -> PointInTimeCandleV1:
    if type(value) is not PointInTimeCandleV1 or not _candle_object_fields_exact(value):
        raise DailyCandleCollectionJobStoreError("daily_candle_collection_job_store_candle_invalid")
    canonical: PointInTimeCandleV1 | None = None
    with suppress(Exception):
        canonical = PointInTimeCandleV1.from_payload(value.to_payload())
    if canonical is None or canonical != value:
        raise DailyCandleCollectionJobStoreError("daily_candle_collection_job_store_candle_invalid")
    return canonical


def _canonical_receipt(value: object) -> CandleObservationWriteReceipt:
    if type(value) is not CandleObservationWriteReceipt:
        raise DailyCandleCollectionJobStoreError(
            "daily_candle_collection_job_store_receipt_invalid"
        )
    canonical: CandleObservationWriteReceipt | None = None
    with suppress(Exception):
        canonical = CandleObservationWriteReceipt(
            idempotency_key=value.idempotency_key,
            canonical_observation_sha256=value.canonical_observation_sha256,
            revision=value.revision,
            inserted=value.inserted,
            stored_observed_at=value.stored_observed_at,
        )
    if canonical is None or canonical != value:
        raise DailyCandleCollectionJobStoreError(
            "daily_candle_collection_job_store_receipt_invalid"
        )
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
        "before": _timestamp(spec.before),
        "provider_contract_sha256": spec.provider_contract_sha256,
        "trigger": spec.trigger,
        "count": spec.count,
        "pagination_allowed": spec.pagination_allowed,
        "automatic_retry_allowed": spec.automatic_retry_allowed,
    }


def _receipt_payload(receipt: CandleObservationWriteReceipt) -> JsonObject:
    return {
        "idempotency_key": receipt.idempotency_key,
        "canonical_observation_sha256": receipt.canonical_observation_sha256,
        "revision": receipt.revision,
        "inserted": receipt.inserted,
        "stored_observed_at": _timestamp(receipt.stored_observed_at),
    }


def _snapshot_from_rpc(value: object) -> DailyCandleCollectionJobSnapshotV1:
    row = _exact_singleton(value, frozenset({"snapshot"}))
    return _snapshot_from_payload(row["snapshot"])


def _inspection_from_rpc(
    value: object,
) -> DailyCandleCollectionJobSnapshotV1 | None:
    row = _exact_singleton(value, frozenset({"found", "snapshot"}))
    found = row["found"]
    if type(found) is not bool:
        raise DailyCandleCollectionJobStoreError(
            "daily_candle_collection_job_store_inspection_invalid"
        )
    snapshot_value = row["snapshot"]
    if found:
        if snapshot_value is None:
            raise DailyCandleCollectionJobStoreError(
                "daily_candle_collection_job_store_inspection_invalid"
            )
        return _snapshot_from_payload(snapshot_value)
    if snapshot_value is not None:
        raise DailyCandleCollectionJobStoreError(
            "daily_candle_collection_job_store_inspection_invalid"
        )
    return None


def _snapshot_from_payload(value: object) -> DailyCandleCollectionJobSnapshotV1:
    snapshot: DailyCandleCollectionJobSnapshotV1 | None = None
    try:
        snapshot = _parse_snapshot(value)
        snapshot = canonical_daily_candle_collection_job_snapshot(snapshot)
    except Exception:
        snapshot = None
    if snapshot is None:
        raise DailyCandleCollectionJobStoreError(
            "daily_candle_collection_job_store_snapshot_invalid"
        ) from None
    return snapshot


def _parse_snapshot(value: object) -> DailyCandleCollectionJobSnapshotV1:
    payload = _exact_object(value, _SNAPSHOT_FIELDS)
    if payload["schema_version"] != DAILY_CANDLE_COLLECTION_JOB_SNAPSHOT_SCHEMA_VERSION:
        raise ValueError("snapshot_schema")
    spec = _parse_spec(payload["spec"])
    if _sha256_value(payload["spec_sha256"], "spec_sha256") != spec.spec_sha256:
        raise ValueError("snapshot_spec_hash")
    state_value = payload["state"]
    if type(state_value) is not str or state_value not in {
        "ready",
        "collecting",
        "candidate_fenced",
        "paused_retryable",
        "blocked_unknown",
        "completed",
    }:
        raise ValueError("snapshot_state")
    reason_value = payload["state_reason"]
    if reason_value is not None:
        reason_value = _reason(reason_value)
    if payload["automatic_retry_allowed"] is not False:
        raise ValueError("snapshot_retry")
    state = cast(DailyCandleCollectionJobState, state_value)
    active_attempt = (
        None if payload["active_attempt"] is None else _parse_attempt(payload["active_attempt"])
    )
    candidate = None if payload["candidate"] is None else _parse_candidate(payload["candidate"])
    completion = (
        None
        if payload["completion"] is None
        else _parse_completion(payload["completion"], candidate)
    )
    if not _wire_snapshot_state_shape_valid(
        state=state,
        active_attempt=active_attempt,
        candidate=candidate,
        completion=completion,
    ):
        raise ValueError("snapshot_state_shape")
    return DailyCandleCollectionJobSnapshotV1(
        spec=spec,
        revision=_positive_int(payload["revision"]),
        state=state,
        active_attempt=active_attempt,
        fenced_candidate=(candidate if state in {"candidate_fenced", "blocked_unknown"} else None),
        completion=completion,
        state_reason=reason_value,
        created_at=_utc_response(payload["created_at"], "created_at"),
        updated_at=_utc_response(payload["updated_at"], "updated_at"),
        automatic_retry_allowed=False,
    )


def _parse_spec(value: object) -> DailyCandleCollectionJobSpecV1:
    payload = _exact_object(value, _SPEC_FIELDS)
    if payload["automatic_retry_allowed"] is not False:
        raise ValueError("spec_retry")
    return DailyCandleCollectionJobSpecV1(
        job_id=_uuid4_text(payload["job_id"], "job_id"),
        provider=_text(payload["provider"]),
        symbol=_text(payload["symbol"]),
        market=cast(Literal["KR"], _text(payload["market"])),
        interval=cast(Literal["1d"], _text(payload["interval"])),
        adjusted=_boolean(payload["adjusted"]),
        before=_utc_response(payload["before"], "before"),
        provider_contract_sha256=_sha256_value(
            payload["provider_contract_sha256"],
            "provider_contract_sha256",
        ),
        trigger=cast(Literal["manual"], _text(payload["trigger"])),
        count=cast(Literal[1], _one(payload["count"])),
        pagination_allowed=cast(Literal[False], _false(payload["pagination_allowed"])),
        automatic_retry_allowed=cast(
            Literal[False],
            _false(payload["automatic_retry_allowed"]),
        ),
        schema_version=_text(payload["schema_version"]),
    )


def _parse_attempt(value: object) -> DailyCandleCollectionAttemptV1:
    payload = _exact_object(value, _ATTEMPT_FIELDS)
    return DailyCandleCollectionAttemptV1(
        attempt_id=_uuid4_text(payload["attempt_id"], "attempt_id"),
        holder_id=_uuid4_text(payload["holder_id"], "holder_id"),
        fencing_revision=_fencing_revision(payload["fencing_revision"]),
        begun_at=_utc_response(payload["begun_at"], "begun_at"),
    )


def _parse_candidate(value: object) -> DailyCandleCollectionCandidateV1:
    payload = _exact_object(value, _CANDIDATE_FIELDS)
    candle = _parse_candle(payload["candle"])
    if (
        _sha256_value(payload["idempotency_key"], "idempotency_key") != candle.idempotency_key
        or _sha256_value(
            payload["canonical_observation_sha256"],
            "canonical_observation_sha256",
        )
        != candle.canonical_observation_sha256
    ):
        raise ValueError("candidate_identity")
    return DailyCandleCollectionCandidateV1(
        attempt=DailyCandleCollectionAttemptV1(
            attempt_id=_uuid4_text(payload["attempt_id"], "attempt_id"),
            holder_id=_uuid4_text(payload["holder_id"], "holder_id"),
            fencing_revision=_fencing_revision(payload["fencing_revision"]),
            begun_at=_utc_response(payload["begun_at"], "begun_at"),
        ),
        candle=candle,
        fenced_at=_utc_response(payload["fenced_at"], "fenced_at"),
    )


def _parse_completion(
    value: object,
    candidate: DailyCandleCollectionCandidateV1 | None,
) -> DailyCandleCollectionCompletionV1:
    if candidate is None:
        raise ValueError("completion_candidate")
    payload = _exact_object(value, _COMPLETION_FIELDS)
    if type(payload["persistence_kind"]) is not str or payload["persistence_kind"] != "durable":
        raise ValueError("completion_persistence")
    receipt = _parse_receipt(payload["receipt"])
    occurrence_observed_at = _utc_response(
        payload["occurrence_observed_at"],
        "occurrence_observed_at",
    )
    if (
        _positive_int(payload["content_revision"]) != receipt.revision
        or _utc_response(
            payload["content_revision_observed_at"],
            "content_revision_observed_at",
        )
        != receipt.stored_observed_at
        or _sha256_value(payload["idempotency_key"], "idempotency_key") != receipt.idempotency_key
        or _sha256_value(
            payload["canonical_observation_sha256"],
            "canonical_observation_sha256",
        )
        != receipt.canonical_observation_sha256
    ):
        raise ValueError("completion_evidence")
    return DailyCandleCollectionCompletionV1(
        candidate=candidate,
        write_evidence=DailyCandleCollectionWriteEvidenceV1(
            persistence_kind="durable",
            receipt=receipt,
            content_revision_id=_uuid4_value(
                payload["content_revision_id"],
                "content_revision_id",
            ),
            occurrence_id=_uuid4_value(payload["occurrence_id"], "occurrence_id"),
            occurrence_observed_at=occurrence_observed_at,
        ),
        confirmed_at=_utc_response(payload["confirmed_at"], "confirmed_at"),
    )


def _parse_candle(value: object) -> PointInTimeCandleV1:
    payload = _exact_object(value, _CANDLE_FIELDS)
    if not _candle_payload_fields_exact(payload):
        raise ValueError("candle_fields")
    return PointInTimeCandleV1.from_payload(payload)


def _parse_receipt(value: object) -> CandleObservationWriteReceipt:
    payload = _exact_object(value, _RECEIPT_FIELDS)
    return CandleObservationWriteReceipt(
        idempotency_key=_sha256_value(payload["idempotency_key"], "idempotency_key"),
        canonical_observation_sha256=_sha256_value(
            payload["canonical_observation_sha256"],
            "canonical_observation_sha256",
        ),
        revision=_positive_int(payload["revision"]),
        inserted=_boolean(payload["inserted"]),
        stored_observed_at=_utc_response(payload["stored_observed_at"], "stored_observed_at"),
    )


def _wire_snapshot_state_shape_valid(
    *,
    state: DailyCandleCollectionJobState,
    active_attempt: DailyCandleCollectionAttemptV1 | None,
    candidate: DailyCandleCollectionCandidateV1 | None,
    completion: DailyCandleCollectionCompletionV1 | None,
) -> bool:
    if state == "ready":
        return active_attempt is None and candidate is None and completion is None
    if state == "collecting":
        return active_attempt is not None and candidate is None and completion is None
    if state == "candidate_fenced":
        return active_attempt is not None and candidate is not None and completion is None
    if state == "paused_retryable":
        return active_attempt is None and candidate is None and completion is None
    if state == "blocked_unknown":
        return active_attempt is not None and completion is None
    return active_attempt is None and candidate is not None and completion is not None


def _candle_payload_fields_exact(payload: dict[str, object]) -> bool:
    return (
        type(payload["schema_version"]) is int
        and type(payload["provider"]) is str
        and type(payload["symbol"]) is str
        and type(payload["market"]) is str
        and type(payload["interval"]) is str
        and type(payload["adjusted"]) is bool
        and type(payload["provider_event_at"]) is str
        and type(payload["observed_at"]) is str
        and type(payload["currency"]) is str
        and type(payload["open_krw"]) is int
        and type(payload["high_krw"]) is int
        and type(payload["low_krw"]) is int
        and type(payload["close_krw"]) is int
        and type(payload["volume"]) is int
        and type(payload["provider_contract_sha256"]) is str
        and type(payload["canonical_observation_sha256"]) is str
    )


def _candle_object_fields_exact(value: PointInTimeCandleV1) -> bool:
    return (
        type(value.schema_version) is int
        and type(value.provider) is str
        and type(value.symbol) is str
        and type(value.market) is str
        and type(value.interval) is str
        and type(value.adjusted) is bool
        and type(value.provider_event_at) is datetime
        and value.provider_event_at.tzinfo is not None
        and value.provider_event_at.utcoffset() is not None
        and type(value.observed_at) is datetime
        and value.observed_at.tzinfo is not None
        and value.observed_at.utcoffset() is not None
        and type(value.currency) is str
        and type(value.open_krw) is int
        and type(value.high_krw) is int
        and type(value.low_krw) is int
        and type(value.close_krw) is int
        and type(value.volume) is int
        and type(value.provider_contract_sha256) is str
        and type(value.canonical_observation_sha256) is str
    )


def _identity_matches(
    snapshot: DailyCandleCollectionJobSnapshotV1,
    job_id: str,
    spec_sha256: str,
) -> bool:
    return snapshot.spec.job_id == job_id and snapshot.spec.spec_sha256 == spec_sha256


def _exact_singleton(
    value: object,
    fields: frozenset[str],
) -> dict[str, object]:
    if type(value) is not list or len(value) != 1:
        raise DailyCandleCollectionJobStoreError(
            "daily_candle_collection_job_store_rpc_result_invalid"
        )
    return _exact_object(value[0], fields)


def _exact_object(value: object, fields: frozenset[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        raise DailyCandleCollectionJobStoreError(
            "daily_candle_collection_job_store_rpc_result_shape_invalid"
        )
    if any(type(key) is not str for key in value):
        raise DailyCandleCollectionJobStoreError(
            "daily_candle_collection_job_store_rpc_result_shape_invalid"
        )
    return value


def _safe_database_error(value: object) -> str | None:
    if type(value) is not dict:
        return None
    message = value.get("message")
    if type(message) is str:
        return _SAFE_DATABASE_ERROR_MAP.get(message)
    return None


def _binding_error() -> None:
    raise DailyCandleCollectionJobStoreError(
        "daily_candle_collection_job_store_response_binding_invalid"
    )


def _text(value: object) -> str:
    if type(value) is not str or not value:
        raise ValueError("text")
    return value


def _uuid4_text(value: object, field_name: str) -> str:
    parsed: UUID | None = None
    if type(value) is str:
        with suppress(AttributeError, TypeError, ValueError):
            parsed = UUID(value)
    if parsed is None or parsed.version != 4 or str(parsed) != value:
        raise DailyCandleCollectionJobStoreError(
            f"daily_candle_collection_job_store_{field_name}_invalid"
        )
    return cast(str, value)


def _sha256_value(value: object, field_name: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise DailyCandleCollectionJobStoreError(
            f"daily_candle_collection_job_store_{field_name}_invalid"
        )
    return value


def _revision(
    value: object,
    maximum: int = _MAX_TRANSITION_REVISION,
) -> int:
    if type(value) is not int or value <= 0 or value > _MAX_TRANSITION_REVISION:
        raise DailyCandleCollectionJobStoreError(
            "daily_candle_collection_job_store_revision_invalid"
        )
    if value > maximum:
        raise DailyCandleCollectionJobStoreError("daily_candle_collection_job_revision_exhausted")
    return value


def _positive_int(value: object) -> int:
    if type(value) is not int or value <= 0 or value > _MAX_DATABASE_BIGINT:
        raise ValueError("positive_int")
    return value


def _fencing_revision(value: object) -> int:
    revision = _revision(
        value,
        DAILY_CANDLE_COLLECTION_JOB_MAX_FENCE_EXPECTED_REVISION,
    )
    if revision <= 1 or revision % 2 != 0:
        raise DailyCandleCollectionJobStoreError(
            "daily_candle_collection_job_store_fencing_revision_invalid"
        )
    return revision


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        raise ValueError("boolean")
    return value


def _one(value: object) -> int:
    if type(value) is not int or value != 1:
        raise ValueError("one")
    return value


def _false(value: object) -> bool:
    if value is not False:
        raise ValueError("false")
    return False


def _uuid4_value(value: object, field_name: str) -> UUID:
    text = _uuid4_text(value, field_name)
    return UUID(text)


def _reason(value: object) -> str:
    if type(value) is not str or _REASON_RE.fullmatch(value) is None:
        raise DailyCandleCollectionJobStoreError("daily_candle_collection_job_store_reason_invalid")
    return value


def _utc_input(value: object, field_name: str) -> datetime:
    converted = _utc(value)
    if converted is None:
        raise DailyCandleCollectionJobStoreError(
            f"daily_candle_collection_job_store_{field_name}_invalid"
        )
    return converted


def _utc_response(value: object, field_name: str) -> datetime:
    if type(value) is not str:
        raise ValueError(field_name)
    parsed: datetime | None = None
    with suppress(ValueError):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed is None or _timestamp(parsed) != value:
        raise ValueError(field_name)
    return parsed.astimezone(UTC)


def _utc(value: object) -> datetime | None:
    if type(value) is not datetime or value.tzinfo is None:
        return None
    converted: datetime | None = None
    with suppress(OverflowError, RuntimeError, TypeError, ValueError):
        if value.utcoffset() is not None:
            converted = value.astimezone(UTC)
    return converted


def _timestamp(value: datetime) -> str:
    converted = _utc(value)
    if converted is None:
        raise ValueError("timestamp")
    return converted.isoformat().replace("+00:00", "Z")
