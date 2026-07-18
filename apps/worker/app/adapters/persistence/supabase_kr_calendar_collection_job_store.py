from __future__ import annotations

import json
import re
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from typing import Literal, cast
from uuid import UUID

import httpx

from app.application.ports.calendar_observation_store_port import (
    CalendarObservationStatus,
    CalendarObservationWriteReceipt,
)
from app.application.ports.kr_calendar_collection_job_store_port import (
    KR_CALENDAR_COLLECTION_JOB_SCHEMA_VERSION,
    KrCalendarCollectionDateAttemptV1,
    KrCalendarCollectionDateCheckpointV1,
    KrCalendarCollectionJobSnapshotV1,
    KrCalendarCollectionJobSpecV1,
    KrCalendarCollectionJobState,
    KrCalendarCollectionJobStoreError,
    canonical_kr_calendar_collection_job_snapshot,
    canonical_kr_calendar_collection_job_spec,
)
from app.application.ports.kr_daily_session_collection_port import (
    CollectedKrDailySessionObservationV1,
    canonical_collected_kr_daily_session_observation,
)
from app.config import Settings
from app.domain.common.json import JsonObject
from app.domain.market_data.point_in_time_calendar import (
    PointInTimeKrDailySessionV1,
)
from app.infrastructure.supabase_headers import supabase_api_headers

KrCalendarCollectionJobRpc = Literal[
    "load_or_create_kr_calendar_collection_job_v1",
    "begin_kr_calendar_collection_date_attempt_v1",
    "pause_kr_calendar_collection_date_attempt_v1",
    "block_kr_calendar_collection_date_attempt_v1",
    "confirm_kr_calendar_collection_date_v1",
]

KR_CALENDAR_COLLECTION_JOB_RPC_ALLOWLIST: frozenset[str] = frozenset(
    {
        "load_or_create_kr_calendar_collection_job_v1",
        "begin_kr_calendar_collection_date_attempt_v1",
        "pause_kr_calendar_collection_date_attempt_v1",
        "block_kr_calendar_collection_date_attempt_v1",
        "confirm_kr_calendar_collection_date_v1",
    }
)

# Only exact, migration-owned messages may cross the PostgREST trust boundary.
# The migration verifier keeps this set synchronized with the database contract.
KR_CALENDAR_COLLECTION_JOB_SAFE_DATABASE_ERRORS: frozenset[str] = frozenset(
    {
        "kr_calendar_collection_job_active_state_invalid",
        "kr_calendar_collection_job_attempt_fence_mismatch",
        "kr_calendar_collection_job_attempt_reused",
        "kr_calendar_collection_job_begin_state_invalid",
        "kr_calendar_collection_job_begin_target_invalid",
        "kr_calendar_collection_job_clock_regressed",
        "kr_calendar_collection_job_collection_scope_mismatch",
        "kr_calendar_collection_job_not_found",
        "kr_calendar_collection_job_revision_conflict",
        "kr_calendar_collection_job_spec_conflict",
        "kr_calendar_collection_job_spec_hash_mismatch",
    }
)

KR_CALENDAR_COLLECTION_JOB_SNAPSHOT_SCHEMA_VERSION = (
    "kr_calendar_collection_job_snapshot.v1"
)

_MAX_RPC_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_DATABASE_REVISION = 9_223_372_036_854_775_806
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_REASON_RE = re.compile(r"[a-z][a-z0-9_]{0,127}")
_SNAPSHOT_FIELDS = frozenset(
    {
        "schema_version",
        "spec_sha256",
        "spec",
        "revision",
        "state",
        "checkpoints",
        "active_attempt",
        "state_reason",
        "terminal_manifest_sha256",
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
        "market",
        "start_date",
        "end_date",
        "trigger",
    }
)
_ATTEMPT_FIELDS = frozenset(
    {
        "attempt_id",
        "holder_id",
        "target_date",
        "fencing_revision",
        "begun_at",
    }
)
_CHECKPOINT_FIELDS = frozenset(
    {
        "attempt_id",
        "holder_id",
        "target_date",
        "fencing_revision",
        "begun_at",
        "session",
        "receipt",
        "confirmed_at",
    }
)
_RECEIPT_FIELDS = frozenset(
    {
        "status",
        "calendar_idempotency_key",
        "canonical_evidence_sha256",
        "revision",
        "revision_inserted",
        "occurrence_id",
        "occurrence_inserted",
        "observed_at",
    }
)


class SupabaseKrCalendarCollectionJobStore:
    """Durable RPC-only store for manually fenced KR calendar jobs.

    Construction is explicit. This adapter has no table CRUD, retry, lease
    takeover, scheduler, runtime-container, research, strategy, or order path.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not settings.supabase_url or settings.supabase_secret_key is None:
            raise KrCalendarCollectionJobStoreError(
                "kr_calendar_collection_job_store_credentials_missing"
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
        spec: KrCalendarCollectionJobSpecV1,
        *,
        now: datetime,
    ) -> KrCalendarCollectionJobSnapshotV1:
        canonical_spec = _canonical_spec(spec)
        canonical_now = _utc_input(now, "now")
        snapshot = _snapshot_from_rpc(
            await self._rpc(
                "load_or_create_kr_calendar_collection_job_v1",
                {
                    "p_spec": _spec_payload(canonical_spec),
                    "p_now": _canonical_timestamp(canonical_now),
                },
            )
        )
        if snapshot.spec != canonical_spec:
            raise KrCalendarCollectionJobStoreError(
                "kr_calendar_collection_job_store_response_binding_invalid"
            )
        return snapshot

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
        inputs = _transition_inputs(
            job_id=job_id,
            spec_sha256=spec_sha256,
            expected_revision=expected_revision,
            attempt_id=attempt_id,
            holder_id=holder_id,
            target_date=target_date,
            now=now,
        )
        snapshot = _snapshot_from_rpc(
            await self._rpc(
                "begin_kr_calendar_collection_date_attempt_v1",
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
            or attempt.target_date != inputs.target_date
            or attempt.fencing_revision != inputs.expected_revision + 1
            or attempt.begun_at != inputs.now
        ):
            raise KrCalendarCollectionJobStoreError(
                "kr_calendar_collection_job_store_response_binding_invalid"
            )
        return snapshot

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
        inputs = _transition_inputs(
            job_id=job_id,
            spec_sha256=spec_sha256,
            expected_revision=expected_revision,
            attempt_id=attempt_id,
            holder_id=holder_id,
            target_date=target_date,
            now=now,
        )
        reason = _reason(reason_code)
        payload = inputs.payload | {"p_reason_code": reason}
        snapshot = _snapshot_from_rpc(
            await self._rpc(
                "pause_kr_calendar_collection_date_attempt_v1",
                payload,
            )
        )
        if (
            not _identity_matches(snapshot, inputs.job_id, inputs.spec_sha256)
            or snapshot.revision != inputs.expected_revision + 1
            or snapshot.state != "paused_retryable"
            or snapshot.active_attempt is not None
            or snapshot.state_reason != reason
            or snapshot.updated_at != inputs.now
            or snapshot.next_date != inputs.target_date
        ):
            raise KrCalendarCollectionJobStoreError(
                "kr_calendar_collection_job_store_response_binding_invalid"
            )
        return snapshot

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
        inputs = _transition_inputs(
            job_id=job_id,
            spec_sha256=spec_sha256,
            expected_revision=expected_revision,
            attempt_id=attempt_id,
            holder_id=holder_id,
            target_date=target_date,
            now=now,
        )
        reason = _reason(reason_code)
        payload = inputs.payload | {"p_reason_code": reason}
        snapshot = _snapshot_from_rpc(
            await self._rpc(
                "block_kr_calendar_collection_date_attempt_v1",
                payload,
            )
        )
        attempt = snapshot.active_attempt
        if (
            not _identity_matches(snapshot, inputs.job_id, inputs.spec_sha256)
            or snapshot.revision != inputs.expected_revision + 1
            or snapshot.state != "blocked_unknown"
            or snapshot.state_reason != reason
            or snapshot.updated_at != inputs.now
            or attempt is None
            or attempt.attempt_id != inputs.attempt_id
            or attempt.holder_id != inputs.holder_id
            or attempt.target_date != inputs.target_date
            or attempt.fencing_revision != inputs.expected_revision
        ):
            raise KrCalendarCollectionJobStoreError(
                "kr_calendar_collection_job_store_response_binding_invalid"
            )
        return snapshot

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
        inputs = _transition_inputs(
            job_id=job_id,
            spec_sha256=spec_sha256,
            expected_revision=expected_revision,
            attempt_id=attempt_id,
            holder_id=holder_id,
            target_date=target_date,
            now=now,
        )
        canonical_collection = _canonical_collection(collection)
        if canonical_collection.target_date != inputs.target_date:
            raise KrCalendarCollectionJobStoreError(
                "kr_calendar_collection_job_store_collection_invalid"
            )
        payload = inputs.payload | {
            "p_session": canonical_collection.session.to_payload(),
            "p_receipt": _receipt_payload(canonical_collection),
        }
        snapshot = _snapshot_from_rpc(
            await self._rpc(
                "confirm_kr_calendar_collection_date_v1",
                payload,
            )
        )
        checkpoint = snapshot.checkpoints[-1] if snapshot.checkpoints else None
        if (
            not _identity_matches(snapshot, inputs.job_id, inputs.spec_sha256)
            or snapshot.revision != inputs.expected_revision + 1
            or snapshot.state not in {"ready", "completed"}
            or snapshot.active_attempt is not None
            or snapshot.updated_at != inputs.now
            or checkpoint is None
            or checkpoint.attempt_id != inputs.attempt_id
            or checkpoint.holder_id != inputs.holder_id
            or checkpoint.target_date != inputs.target_date
            or checkpoint.fencing_revision != inputs.expected_revision
            or checkpoint.collection != canonical_collection
            or checkpoint.confirmed_at != inputs.now
        ):
            raise KrCalendarCollectionJobStoreError(
                "kr_calendar_collection_job_store_response_binding_invalid"
            )
        return snapshot

    async def _rpc(
        self,
        rpc: KrCalendarCollectionJobRpc,
        payload: JsonObject,
    ) -> object:
        if rpc not in KR_CALENDAR_COLLECTION_JOB_RPC_ALLOWLIST:
            raise KrCalendarCollectionJobStoreError(
                "kr_calendar_collection_job_store_rpc_not_allowed"
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
                content_encoding = response.headers.get(
                    "content-encoding", "identity"
                ).strip().lower()
                if content_encoding not in {"", "identity"}:
                    failure = (
                        "kr_calendar_collection_job_store_rpc_content_encoding_invalid"
                    )
                body = bytearray()
                if failure is None:
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > _MAX_RPC_RESPONSE_BYTES:
                            failure = (
                                "kr_calendar_collection_job_store_rpc_response_too_large"
                            )
                            break
                        body.extend(chunk)
                if failure is None:
                    parsed = json.loads(
                        body,
                        object_pairs_hook=_json_object_without_duplicates,
                    )
                    if not response.is_success:
                        failure = _safe_database_error(parsed) or (
                            "kr_calendar_collection_job_store_rpc_failed_or_returned_invalid_json"
                        )
                    else:
                        result = parsed
        except Exception:
            failure = (
                "kr_calendar_collection_job_store_rpc_failed_or_returned_invalid_json"
            )
        if failure is not None:
            raise KrCalendarCollectionJobStoreError(failure)
        return result


class _TransitionInputs:
    __slots__ = (
        "job_id",
        "spec_sha256",
        "expected_revision",
        "attempt_id",
        "holder_id",
        "target_date",
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
        target_date: date,
        now: datetime,
    ) -> None:
        self.job_id = job_id
        self.spec_sha256 = spec_sha256
        self.expected_revision = expected_revision
        self.attempt_id = attempt_id
        self.holder_id = holder_id
        self.target_date = target_date
        self.now = now
        self.payload: JsonObject = {
            "p_job_id": job_id,
            "p_spec_sha256": spec_sha256,
            "p_expected_revision": expected_revision,
            "p_attempt_id": attempt_id,
            "p_holder_id": holder_id,
            "p_target_date": target_date.isoformat(),
            "p_now": _canonical_timestamp(now),
        }


def _transition_inputs(
    *,
    job_id: object,
    spec_sha256: object,
    expected_revision: object,
    attempt_id: object,
    holder_id: object,
    target_date: object,
    now: object,
) -> _TransitionInputs:
    return _TransitionInputs(
        job_id=_uuid4_text(job_id, "job_id"),
        spec_sha256=_sha256_value(spec_sha256, "spec_sha256"),
        expected_revision=_revision_input(expected_revision),
        attempt_id=_uuid4_text(attempt_id, "attempt_id"),
        holder_id=_uuid4_text(holder_id, "holder_id"),
        target_date=_date_input(target_date, "target_date"),
        now=_utc_input(now, "now"),
    )


def _canonical_spec(value: object) -> KrCalendarCollectionJobSpecV1:
    canonical: KrCalendarCollectionJobSpecV1 | None = None
    with suppress(Exception):
        canonical = canonical_kr_calendar_collection_job_spec(value)
    if canonical is None:
        raise KrCalendarCollectionJobStoreError(
            "kr_calendar_collection_job_store_spec_invalid"
        )
    return canonical


def _canonical_collection(
    value: object,
) -> CollectedKrDailySessionObservationV1:
    canonical: CollectedKrDailySessionObservationV1 | None = None
    with suppress(Exception):
        canonical = canonical_collected_kr_daily_session_observation(value)
    if canonical is None:
        raise KrCalendarCollectionJobStoreError(
            "kr_calendar_collection_job_store_collection_invalid"
        )
    return canonical


def _spec_payload(spec: KrCalendarCollectionJobSpecV1) -> JsonObject:
    return {
        "schema_version": spec.schema_version,
        "job_id": spec.job_id,
        "provider": spec.provider,
        "market": spec.market,
        "start_date": spec.start_date.isoformat(),
        "end_date": spec.end_date.isoformat(),
        "trigger": spec.trigger,
    }


def _receipt_payload(
    collection: CollectedKrDailySessionObservationV1,
) -> JsonObject:
    receipt = collection.receipt
    return {
        "status": receipt.status,
        "calendar_idempotency_key": receipt.calendar_idempotency_key,
        "canonical_evidence_sha256": receipt.canonical_evidence_sha256,
        "revision": receipt.revision,
        "revision_inserted": receipt.revision_inserted,
        "occurrence_id": str(receipt.occurrence_id),
        "occurrence_inserted": receipt.occurrence_inserted,
        "observed_at": _canonical_timestamp(receipt.observed_at),
    }


def _snapshot_from_rpc(value: object) -> KrCalendarCollectionJobSnapshotV1:
    if (
        type(value) is not list
        or len(value) != 1
        or type(value[0]) is not dict
        or set(value[0]) != {"snapshot"}
    ):
        raise KrCalendarCollectionJobStoreError(
            "kr_calendar_collection_job_store_rpc_result_invalid"
        )
    snapshot: KrCalendarCollectionJobSnapshotV1 | None = None
    with suppress(Exception):
        candidate = _parse_snapshot(value[0]["snapshot"])
        snapshot = canonical_kr_calendar_collection_job_snapshot(candidate)
    if snapshot is None:
        raise KrCalendarCollectionJobStoreError(
            "kr_calendar_collection_job_store_rpc_snapshot_invalid"
        )
    return snapshot


def _parse_snapshot(value: object) -> KrCalendarCollectionJobSnapshotV1:
    row = _exact_object(value, _SNAPSHOT_FIELDS)
    if row["schema_version"] != KR_CALENDAR_COLLECTION_JOB_SNAPSHOT_SCHEMA_VERSION:
        raise ValueError("snapshot_schema_invalid")
    spec = _parse_spec(row["spec"])
    if _sha256_value(row["spec_sha256"], "response_spec_sha256") != spec.spec_sha256:
        raise ValueError("snapshot_spec_hash_invalid")
    raw_checkpoints = row["checkpoints"]
    if (
        type(raw_checkpoints) is not list
        or len(raw_checkpoints) > spec.total_days
    ):
        raise ValueError("snapshot_checkpoints_invalid")
    checkpoints = tuple(_parse_checkpoint(item) for item in raw_checkpoints)
    raw_attempt = row["active_attempt"]
    active_attempt = None if raw_attempt is None else _parse_attempt(raw_attempt)
    state = row["state"]
    if type(state) is not str or state not in {
        "ready",
        "collecting",
        "paused_retryable",
        "blocked_unknown",
        "completed",
    }:
        raise ValueError("snapshot_state_invalid")
    state_reason = row["state_reason"]
    if state_reason is not None and type(state_reason) is not str:
        raise ValueError("snapshot_state_reason_invalid")
    terminal_manifest = row["terminal_manifest_sha256"]
    if terminal_manifest is not None:
        terminal_manifest = _sha256_value(
            terminal_manifest,
            "terminal_manifest_sha256",
        )
    automatic_retry_allowed = row["automatic_retry_allowed"]
    if automatic_retry_allowed is not False:
        raise ValueError("snapshot_retry_invalid")
    return KrCalendarCollectionJobSnapshotV1(
        spec=spec,
        revision=_positive_int(row["revision"]),
        state=cast(KrCalendarCollectionJobState, state),
        checkpoints=checkpoints,
        active_attempt=active_attempt,
        state_reason=state_reason,
        terminal_manifest_sha256=terminal_manifest,
        created_at=_utc_response(row["created_at"], "created_at"),
        updated_at=_utc_response(row["updated_at"], "updated_at"),
        automatic_retry_allowed=False,
    )


def _parse_spec(value: object) -> KrCalendarCollectionJobSpecV1:
    row = _exact_object(value, _SPEC_FIELDS)
    if row["schema_version"] != KR_CALENDAR_COLLECTION_JOB_SCHEMA_VERSION:
        raise ValueError("spec_contract_invalid")
    return KrCalendarCollectionJobSpecV1(
        job_id=_uuid4_text(row["job_id"], "response_job_id"),
        provider=_required_text(row["provider"]),
        market=_required_text(row["market"]),
        start_date=_date_response(row["start_date"], "start_date"),
        end_date=_date_response(row["end_date"], "end_date"),
        trigger=cast(Literal["manual"], _required_text(row["trigger"])),
        schema_version=_required_text(row["schema_version"]),
    )


def _parse_attempt(value: object) -> KrCalendarCollectionDateAttemptV1:
    row = _exact_object(value, _ATTEMPT_FIELDS)
    return KrCalendarCollectionDateAttemptV1(
        attempt_id=_uuid4_text(row["attempt_id"], "response_attempt_id"),
        holder_id=_uuid4_text(row["holder_id"], "response_holder_id"),
        target_date=_date_response(row["target_date"], "attempt_target_date"),
        fencing_revision=_positive_int(row["fencing_revision"]),
        begun_at=_utc_response(row["begun_at"], "attempt_begun_at"),
    )


def _parse_checkpoint(
    value: object,
) -> KrCalendarCollectionDateCheckpointV1:
    row = _exact_object(value, _CHECKPOINT_FIELDS)
    return KrCalendarCollectionDateCheckpointV1(
        attempt_id=_uuid4_text(row["attempt_id"], "checkpoint_attempt_id"),
        holder_id=_uuid4_text(row["holder_id"], "checkpoint_holder_id"),
        target_date=_date_response(row["target_date"], "checkpoint_target_date"),
        fencing_revision=_positive_int(row["fencing_revision"]),
        begun_at=_utc_response(row["begun_at"], "checkpoint_begun_at"),
        collection=_parse_collection(row["session"], row["receipt"]),
        confirmed_at=_utc_response(row["confirmed_at"], "checkpoint_confirmed_at"),
    )


def _parse_collection(
    session_value: object,
    receipt_value: object,
) -> CollectedKrDailySessionObservationV1:
    if type(session_value) is not dict:
        raise ValueError("session_invalid")
    session: PointInTimeKrDailySessionV1 | None = None
    with suppress(Exception):
        session = PointInTimeKrDailySessionV1.from_payload(session_value)
    if session is None:
        raise ValueError("session_invalid")
    row = _exact_object(receipt_value, _RECEIPT_FIELDS)
    status = row["status"]
    if type(status) is not str or status not in {"stored", "replayed"}:
        raise ValueError("receipt_status_invalid")
    occurrence_id = UUID(_uuid4_text(row["occurrence_id"], "occurrence_id"))
    receipt: CalendarObservationWriteReceipt | None = None
    with suppress(Exception):
        receipt = CalendarObservationWriteReceipt(
            status=cast(CalendarObservationStatus, status),
            calendar_idempotency_key=_sha256_value(
                row["calendar_idempotency_key"],
                "calendar_idempotency_key",
            ),
            canonical_evidence_sha256=_sha256_value(
                row["canonical_evidence_sha256"],
                "canonical_evidence_sha256",
            ),
            revision=_positive_int(row["revision"]),
            revision_inserted=_boolean(row["revision_inserted"]),
            occurrence_id=occurrence_id,
            occurrence_inserted=_boolean(row["occurrence_inserted"]),
            observed_at=_utc_response(row["observed_at"], "receipt_observed_at"),
        )
    if receipt is None:
        raise ValueError("receipt_invalid")
    collection: CollectedKrDailySessionObservationV1 | None = None
    with suppress(Exception):
        collection = CollectedKrDailySessionObservationV1(
            session=session,
            receipt=receipt,
        )
    if collection is None:
        raise ValueError("collection_invalid")
    return collection


def _identity_matches(
    snapshot: KrCalendarCollectionJobSnapshotV1,
    job_id: str,
    spec_sha256: str,
) -> bool:
    return (
        snapshot.spec.job_id == job_id
        and snapshot.spec.spec_sha256 == spec_sha256
    )


def _exact_object(
    value: object,
    fields: frozenset[str],
) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        raise ValueError("object_shape_invalid")
    return cast(dict[str, object], value)


def _json_object_without_duplicates(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate_json_object_key")
        value[key] = item
    return value


def _safe_database_error(value: object) -> str | None:
    if type(value) is not dict:
        return None
    message = value.get("message")
    if (
        type(message) is str
        and message in KR_CALENDAR_COLLECTION_JOB_SAFE_DATABASE_ERRORS
    ):
        return message
    return None


def _required_text(value: object) -> str:
    if type(value) is not str or not value:
        raise ValueError("text_invalid")
    return value


def _uuid4_text(value: object, field_name: str) -> str:
    parsed: UUID | None = None
    if type(value) is str:
        with suppress(AttributeError, TypeError, ValueError):
            parsed = UUID(value)
    if parsed is None or parsed.version != 4 or str(parsed) != value:
        raise KrCalendarCollectionJobStoreError(
            f"kr_calendar_collection_job_store_{field_name}_invalid"
        )
    return value


def _sha256_value(value: object, field_name: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise KrCalendarCollectionJobStoreError(
            f"kr_calendar_collection_job_store_{field_name}_invalid"
        )
    return value


def _revision_input(value: object) -> int:
    if (
        type(value) is not int
        or value <= 0
        or value > _MAX_DATABASE_REVISION
    ):
        raise KrCalendarCollectionJobStoreError(
            "kr_calendar_collection_job_store_expected_revision_invalid"
        )
    return value


def _positive_int(value: object) -> int:
    if (
        type(value) is not int
        or value <= 0
        or value > _MAX_DATABASE_REVISION + 1
    ):
        raise ValueError("positive_int_invalid")
    return value


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        raise ValueError("boolean_invalid")
    return value


def _date_input(value: object, field_name: str) -> date:
    if type(value) is not date:
        raise KrCalendarCollectionJobStoreError(
            f"kr_calendar_collection_job_store_{field_name}_invalid"
        )
    return value


def _date_response(value: object, field_name: str) -> date:
    parsed: date | None = None
    if type(value) is str:
        with suppress(OverflowError, TypeError, ValueError):
            parsed = date.fromisoformat(value)
    if parsed is None or parsed.isoformat() != value:
        raise ValueError(f"{field_name}_invalid")
    return parsed


def _reason(value: object) -> str:
    if type(value) is not str or _REASON_RE.fullmatch(value) is None:
        raise KrCalendarCollectionJobStoreError(
            "kr_calendar_collection_job_store_reason_code_invalid"
        )
    return value


def _utc_input(value: object, field_name: str) -> datetime:
    converted = _utc(value)
    if converted is None:
        raise KrCalendarCollectionJobStoreError(
            f"kr_calendar_collection_job_store_{field_name}_invalid"
        )
    return converted


def _utc_response(value: object, field_name: str) -> datetime:
    parsed: datetime | None = None
    if type(value) is str:
        with suppress(OverflowError, TypeError, ValueError):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if (
        parsed is None
        or parsed.tzinfo is None
        or parsed.utcoffset() != timedelta(0)
    ):
        raise ValueError(f"{field_name}_invalid")
    canonical_z = parsed.isoformat().replace("+00:00", "Z")
    if value != canonical_z:
        raise ValueError(f"{field_name}_invalid")
    return parsed.astimezone(UTC)


def _utc(value: object) -> datetime | None:
    if type(value) is not datetime or value.tzinfo is None:
        return None
    converted: datetime | None = None
    with suppress(OverflowError, RuntimeError, TypeError, ValueError):
        if value.utcoffset() is not None:
            converted = value.astimezone(UTC)
    return converted


def _canonical_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
