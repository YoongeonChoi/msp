#!/usr/bin/env python3
"""Verify durable single-candle collection CAS/fence semantics."""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent
WORKER_ROOT = ROOT.parent / "apps" / "worker"
sys.path.insert(0, str(WORKER_ROOT))

from verify_g1_g2_migration import (  # noqa: E402
    DB_PASSWORD,
    MIGRATIONS,
    POSTGRES_IMAGE,
    SEED,
    VerificationError,
    apply_repository,
    bootstrap_sql,
    expect_failure,
    jwt_claim_sql,
    psql,
    run,
    wait_for_postgres,
)
from verify_pit_calendar_observation_store import domain_snapshot  # noqa: E402
from verify_pit_candle_revision_store import (  # noqa: E402
    CONTRACT_SHA256,
    append,
    candle,
    payload_literal,
)

from app.domain.market_data.point_in_time import PointInTimeCandleV1  # noqa: E402

MIGRATION_NAME = "20260719090000_pit_daily_candle_collection_job_store.sql"
WORKER_ID = "79797979-7979-4979-8979-797979797979"
BASE_EVENT_AT = datetime(2026, 3, 24, 0, 0, tzinfo=UTC)
BASE_OBSERVED_AT = datetime(2026, 3, 25, 0, 0, tzinfo=UTC)
TABLES = (
    "pit_daily_candle_collection_jobs",
    "pit_daily_candle_collection_attempt_ledger",
)
RPC_NAMES = (
    "load_or_create_pit_daily_candle_collection_job_v1",
    "inspect_pit_daily_candle_collection_job_v1",
    "begin_pit_daily_candle_collection_attempt_v1",
    "fence_pit_daily_candle_collection_candidate_v1",
    "pause_pit_daily_candle_collection_attempt_v1",
    "block_pit_daily_candle_collection_attempt_v1",
    "confirm_pit_daily_candle_collection_attempt_v1",
)
SNAPSHOT_FIELDS = {
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
SPEC_FIELDS = {
    "schema_version",
    "job_id",
    "provider",
    "symbol",
    "market",
    "interval",
    "adjusted",
    "before",
    "count",
    "pagination_allowed",
    "automatic_retry_allowed",
    "trigger",
    "provider_contract_sha256",
}
ACTIVE_ATTEMPT_FIELDS = {
    "attempt_id",
    "holder_id",
    "fencing_revision",
    "begun_at",
}
CANDIDATE_FIELDS = {
    "attempt_id",
    "holder_id",
    "fencing_revision",
    "begun_at",
    "idempotency_key",
    "canonical_observation_sha256",
    "candle",
    "fenced_at",
}
COMPLETION_FIELDS = {
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
PRE_CANDIDATE_BLOCK_REASONS = (
    "provider_read_outcome_unknown_before_candidate",
    "unexpected_failure_before_candidate",
    "cancelled_before_candidate",
)
POST_CANDIDATE_BLOCK_REASONS = (
    "append_outcome_unknown",
    "confirm_outcome_unknown",
    "unexpected_failure_after_candidate",
    "cancelled_after_candidate",
)


def canonical_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def sql_text(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def jsonb_literal(payload: dict[str, object], tag: str) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    delimiter = f"${tag}$"
    if delimiter in raw:
        raise VerificationError(f"unexpected {tag} payload delimiter")
    return f"{delimiter}{raw}{delimiter}::jsonb"


def scalar(container: str, query: str) -> str:
    return psql(container, query).stdout.strip()


def job_spec(
    *,
    job_id: str | None = None,
    symbol: str = "005930",
    before: datetime | None = None,
) -> dict[str, object]:
    return {
        "schema_version": "daily_candle_collection_job.v1",
        "job_id": job_id or str(uuid4()),
        "provider": "toss",
        "symbol": symbol,
        "market": "KR",
        "interval": "1d",
        "adjusted": True,
        "before": canonical_timestamp(before or (BASE_EVENT_AT + timedelta(days=1))),
        "count": 1,
        "pagination_allowed": False,
        "automatic_retry_allowed": False,
        "trigger": "manual",
        "provider_contract_sha256": CONTRACT_SHA256,
    }


def validate_snapshot(value: object) -> dict[str, object]:
    if type(value) is not dict or set(value) != SNAPSHOT_FIELDS:
        raise VerificationError(f"snapshot shape mismatch: {value}")
    snapshot = value
    spec = snapshot.get("spec")
    if (
        snapshot.get("schema_version") != "daily_candle_collection_job_snapshot.v1"
        or snapshot.get("automatic_retry_allowed") is not False
        or type(spec) is not dict
        or set(spec) != SPEC_FIELDS
        or spec.get("count") != 1
        or spec.get("pagination_allowed") is not False
        or spec.get("automatic_retry_allowed") is not False
        or spec.get("provider_contract_sha256") != CONTRACT_SHA256
    ):
        raise VerificationError(f"snapshot safety contract mismatch: {snapshot}")
    active = snapshot.get("active_attempt")
    if active is not None and (
        type(active) is not dict or set(active) != ACTIVE_ATTEMPT_FIELDS
    ):
        raise VerificationError(f"active attempt shape mismatch: {active}")
    candidate_value = snapshot.get("candidate")
    if candidate_value is not None and (
        type(candidate_value) is not dict or set(candidate_value) != CANDIDATE_FIELDS
    ):
        raise VerificationError(f"candidate shape mismatch: {candidate_value}")
    completion = snapshot.get("completion")
    if completion is not None and (
        type(completion) is not dict or set(completion) != COMPLETION_FIELDS
    ):
        raise VerificationError(f"completion shape mismatch: {completion}")
    return snapshot


def _snapshot(container: str, call_sql: str) -> dict[str, object]:
    rows = (
        psql(
            container,
            jwt_claim_sql(WORKER_ID, role="service_role")
            + f"\nselect snapshot from {call_sql};",
        )
        .stdout.strip()
        .splitlines()
    )
    if not rows:
        raise VerificationError(f"RPC returned no snapshot: {call_sql}")
    return validate_snapshot(json.loads(rows[-1]))


def load_or_create(
    container: str,
    spec: dict[str, object],
    now: datetime,
) -> dict[str, object]:
    return _snapshot(
        container,
        "worker_api.load_or_create_pit_daily_candle_collection_job_v1("
        f"{jsonb_literal(spec, 'spec')},"
        f"{sql_text(canonical_timestamp(now))}::timestamptz)",
    )


def inspect_job(container: str, job_id: str) -> tuple[bool, dict[str, object] | None]:
    rows = (
        psql(
            container,
            jwt_claim_sql(WORKER_ID, role="service_role")
            + "\nselect row_to_json(result) from "
            "worker_api.inspect_pit_daily_candle_collection_job_v1("
            f"{sql_text(job_id)}) as result;",
        )
        .stdout.strip()
        .splitlines()
    )
    if not rows:
        raise VerificationError("inspection RPC returned no row")
    parsed = json.loads(rows[-1])
    if type(parsed) is not dict or set(parsed) != {"found", "snapshot"}:
        raise VerificationError(f"inspection shape mismatch: {parsed}")
    snapshot = parsed["snapshot"]
    return parsed["found"] is True, (
        None if snapshot is None else validate_snapshot(snapshot)
    )


def begin_attempt(
    container: str,
    snapshot: dict[str, object],
    *,
    attempt_id: str,
    holder_id: str,
    now: datetime,
) -> dict[str, object]:
    spec = snapshot["spec"]
    assert isinstance(spec, dict)
    return _snapshot(
        container,
        "worker_api.begin_pit_daily_candle_collection_attempt_v1("
        f"{sql_text(str(spec['job_id']))},"
        f"{sql_text(str(snapshot['spec_sha256']))},"
        f"{snapshot['revision']},"
        f"{sql_text(attempt_id)},{sql_text(holder_id)},"
        f"{sql_text(canonical_timestamp(now))}::timestamptz)",
    )


def fence_candidate(
    container: str,
    snapshot: dict[str, object],
    value: PointInTimeCandleV1,
    *,
    attempt_id: str,
    holder_id: str,
    fencing_revision: int,
    now: datetime,
) -> dict[str, object]:
    spec = snapshot["spec"]
    assert isinstance(spec, dict)
    return _snapshot(
        container,
        "worker_api.fence_pit_daily_candle_collection_candidate_v1("
        f"{sql_text(str(spec['job_id']))},"
        f"{sql_text(str(snapshot['spec_sha256']))},"
        f"{snapshot['revision']},"
        f"{sql_text(attempt_id)},{sql_text(holder_id)},"
        f"{fencing_revision},{payload_literal(value)},"
        f"{sql_text(canonical_timestamp(now))}::timestamptz)",
    )


def pause_attempt(
    container: str,
    snapshot: dict[str, object],
    *,
    attempt_id: str,
    holder_id: str,
    fencing_revision: int,
    now: datetime,
) -> dict[str, object]:
    spec = snapshot["spec"]
    assert isinstance(spec, dict)
    return _snapshot(
        container,
        "worker_api.pause_pit_daily_candle_collection_attempt_v1("
        f"{sql_text(str(spec['job_id']))},"
        f"{sql_text(str(snapshot['spec_sha256']))},"
        f"{snapshot['revision']},"
        f"{sql_text(attempt_id)},{sql_text(holder_id)},"
        f"{fencing_revision},'provider_read_failed_before_candidate',"
        f"{sql_text(canonical_timestamp(now))}::timestamptz)",
    )


def block_attempt(
    container: str,
    snapshot: dict[str, object],
    *,
    attempt_id: str,
    holder_id: str,
    fencing_revision: int,
    reason_code: str,
    now: datetime,
) -> dict[str, object]:
    spec = snapshot["spec"]
    assert isinstance(spec, dict)
    return _snapshot(
        container,
        "worker_api.block_pit_daily_candle_collection_attempt_v1("
        f"{sql_text(str(spec['job_id']))},"
        f"{sql_text(str(snapshot['spec_sha256']))},"
        f"{snapshot['revision']},"
        f"{sql_text(attempt_id)},{sql_text(holder_id)},"
        f"{fencing_revision},{sql_text(reason_code)},"
        f"{sql_text(canonical_timestamp(now))}::timestamptz)",
    )


def receipt_payload(receipt: dict[str, object]) -> dict[str, object]:
    stored = receipt.get("stored_observed_at")
    if not isinstance(stored, str):
        raise VerificationError(f"append receipt clock missing: {receipt}")
    parsed_stored = datetime.fromisoformat(stored.replace("Z", "+00:00"))
    return {
        "idempotency_key": receipt["idempotency_key"],
        "canonical_observation_sha256": receipt["canonical_observation_sha256"],
        "revision": receipt["revision"],
        "inserted": receipt["inserted"],
        "stored_observed_at": canonical_timestamp(parsed_stored),
    }


def confirm_attempt(
    container: str,
    snapshot: dict[str, object],
    receipt: dict[str, object],
    *,
    attempt_id: str,
    holder_id: str,
    fencing_revision: int,
    now: datetime,
) -> dict[str, object]:
    spec = snapshot["spec"]
    assert isinstance(spec, dict)
    return _snapshot(
        container,
        "worker_api.confirm_pit_daily_candle_collection_attempt_v1("
        f"{sql_text(str(spec['job_id']))},"
        f"{sql_text(str(snapshot['spec_sha256']))},"
        f"{snapshot['revision']},"
        f"{sql_text(attempt_id)},{sql_text(holder_id)},"
        f"{fencing_revision},{jsonb_literal(receipt, 'receipt')},"
        f"{sql_text(canonical_timestamp(now))}::timestamptz)",
    )


def _claims() -> str:
    return jwt_claim_sql(WORKER_ID, role="service_role")


def verify_create_replay_and_inspect(container: str) -> None:
    spec = job_spec()
    created_at = BASE_OBSERVED_AT - timedelta(minutes=30)
    created = load_or_create(container, spec, created_at)
    replayed = load_or_create(container, spec, created_at + timedelta(minutes=1))
    if created != replayed or created["state"] != "ready" or created["revision"] != 1:
        raise VerificationError("load/create replay changed immutable job state")
    found, inspected = inspect_job(container, str(spec["job_id"]))
    if not found or inspected != created:
        raise VerificationError("inspection did not return exact durable snapshot")
    missing, missing_snapshot = inspect_job(container, str(uuid4()))
    if missing or missing_snapshot is not None:
        raise VerificationError("inspection missing result is not fail-closed")

    conflicting = dict(spec)
    conflicting["symbol"] = "000660"
    expect_failure(
        container,
        _claims() + "\nselect * from "
        "worker_api.load_or_create_pit_daily_candle_collection_job_v1("
        f"{jsonb_literal(conflicting, 'conflict')},"
        f"{sql_text(canonical_timestamp(created_at))}::timestamptz);",
        "pit_daily_candle_collection_job_spec_conflict",
    )
    malformed = dict(spec)
    malformed["pagination_allowed"] = True
    expect_failure(
        container,
        _claims() + "\nselect * from "
        "worker_api.load_or_create_pit_daily_candle_collection_job_v1("
        f"{jsonb_literal(malformed, 'malformed')},"
        f"{sql_text(canonical_timestamp(created_at))}::timestamptz);",
        "pit_daily_candle_collection_job_argument_invalid",
    )
    malformed["pagination_allowed"] = False
    malformed["schema_version"] = None
    expect_failure(
        container,
        _claims() + "\nselect * from "
        "worker_api.load_or_create_pit_daily_candle_collection_job_v1("
        f"{jsonb_literal(malformed, 'null_schema')},"
        f"{sql_text(canonical_timestamp(created_at))}::timestamptz);",
        "pit_daily_candle_collection_job_argument_invalid",
    )
    print("PASS exact 13-key create/replay/inspect contract")


def verify_completion_exact_join(container: str) -> None:
    value = candle(
        symbol="005930",
        provider_event_at=BASE_EVENT_AT,
        observed_at=BASE_OBSERVED_AT,
    )
    spec = job_spec(symbol=value.symbol)
    current = load_or_create(container, spec, value.observed_at - timedelta(minutes=30))
    attempt_id = str(uuid4())
    holder_id = str(uuid4())
    current = begin_attempt(
        container,
        current,
        attempt_id=attempt_id,
        holder_id=holder_id,
        now=value.observed_at - timedelta(minutes=20),
    )
    fencing_revision = int(current["revision"])
    current = fence_candidate(
        container,
        current,
        value,
        attempt_id=attempt_id,
        holder_id=holder_id,
        fencing_revision=fencing_revision,
        now=value.observed_at + timedelta(minutes=1),
    )
    if current["state"] != "candidate_fenced" or current["revision"] != 3:
        raise VerificationError("candidate_fenced transition was not durable")
    source_receipt = receipt_payload(append(container, value))
    # `inserted` is telemetry. Deliberately invert it: exact DB occurrence and
    # revision evidence must remain the authority for confirmation.
    source_receipt["inserted"] = not bool(source_receipt["inserted"])
    completed = confirm_attempt(
        container,
        current,
        source_receipt,
        attempt_id=attempt_id,
        holder_id=holder_id,
        fencing_revision=fencing_revision,
        now=value.observed_at + timedelta(minutes=2),
    )
    completion = completed["completion"]
    if (
        completed["state"] != "completed"
        or completed["revision"] != 4
        or completed["active_attempt"] is not None
        or type(completion) is not dict
        or completion["idempotency_key"] != value.idempotency_key
        or completion["persistence_kind"] != "durable"
        or completion["canonical_observation_sha256"]
        != value.canonical_observation_sha256
        or completion["occurrence_observed_at"]
        != canonical_timestamp(value.observed_at)
    ):
        raise VerificationError(f"completion evidence mismatch: {completed}")
    exact_join = scalar(
        container,
        "select count(*) from private.pit_candle_observation_occurrences o "
        "join private.pit_candle_observation_revisions r "
        "on r.id=o.content_revision_id "
        f"where o.id={sql_text(str(completion['occurrence_id']))}::uuid "
        f"and r.id={sql_text(str(completion['content_revision_id']))}::uuid "
        f"and o.observed_at={sql_text(canonical_timestamp(value.observed_at))}"
        "::timestamptz and o.observation_payload="
        f"{payload_literal(value)} and r.revision={completion['content_revision']};",
    )
    if exact_join != "1":
        raise VerificationError("completion IDs do not identify exact source rows")

    replay_value = candle(
        symbol="005930",
        provider_event_at=BASE_EVENT_AT,
        observed_at=BASE_OBSERVED_AT + timedelta(hours=1),
    )
    replay_spec = job_spec(symbol=replay_value.symbol)
    replay_job = load_or_create(
        container,
        replay_spec,
        replay_value.observed_at - timedelta(minutes=30),
    )
    replay_attempt = str(uuid4())
    replay_holder = str(uuid4())
    replay_job = begin_attempt(
        container,
        replay_job,
        attempt_id=replay_attempt,
        holder_id=replay_holder,
        now=replay_value.observed_at - timedelta(minutes=20),
    )
    replay_fence = int(replay_job["revision"])
    replay_job = fence_candidate(
        container,
        replay_job,
        replay_value,
        attempt_id=replay_attempt,
        holder_id=replay_holder,
        fencing_revision=replay_fence,
        now=replay_value.observed_at + timedelta(minutes=1),
    )
    replay_receipt = receipt_payload(append(container, replay_value))
    replay_completed = confirm_attempt(
        container,
        replay_job,
        replay_receipt,
        attempt_id=replay_attempt,
        holder_id=replay_holder,
        fencing_revision=replay_fence,
        now=replay_value.observed_at + timedelta(minutes=2),
    )
    replay_completion = replay_completed["completion"]
    if (
        type(replay_completion) is not dict
        or replay_completion["occurrence_observed_at"]
        != canonical_timestamp(replay_value.observed_at)
        or replay_completion["content_revision_observed_at"]
        == replay_completion["occurrence_observed_at"]
    ):
        raise VerificationError(
            "exact replay did not separate occurrence and content clocks"
        )
    print("PASS exact occurrence/revision confirmation including replay clocks")


def verify_pause_rebegin_and_aba(container: str) -> None:
    spec = job_spec(symbol="000660")
    current = load_or_create(container, spec, BASE_OBSERVED_AT - timedelta(minutes=30))
    first_attempt = str(uuid4())
    holder = str(uuid4())
    collecting = begin_attempt(
        container,
        current,
        attempt_id=first_attempt,
        holder_id=holder,
        now=BASE_OBSERVED_AT - timedelta(minutes=20),
    )
    first_fence = int(collecting["revision"])
    expect_failure(
        container,
        _claims() + "\nselect * from "
        "worker_api.pause_pit_daily_candle_collection_attempt_v1("
        f"{sql_text(str(spec['job_id']))},"
        f"{sql_text(str(collecting['spec_sha256']))},{collecting['revision']},"
        f"{sql_text(first_attempt)},{sql_text(holder)},{first_fence},"
        "'append_outcome_unknown',"
        f"{sql_text(canonical_timestamp(BASE_OBSERVED_AT))}::timestamptz);",
        "pit_daily_candle_collection_job_argument_invalid",
    )
    paused = pause_attempt(
        container,
        collecting,
        attempt_id=first_attempt,
        holder_id=holder,
        fencing_revision=first_fence,
        now=BASE_OBSERVED_AT,
    )
    if (
        paused["state"] != "paused_retryable"
        or paused["state_reason"] != "provider_read_failed_before_candidate"
        or paused["active_attempt"] is not None
    ):
        raise VerificationError("pre-candidate pause contract mismatch")
    expect_failure(
        container,
        _claims() + "\nselect * from "
        "worker_api.begin_pit_daily_candle_collection_attempt_v1("
        f"{sql_text(str(spec['job_id']))},"
        f"{sql_text(str(paused['spec_sha256']))},{paused['revision']},"
        f"{sql_text(first_attempt)},{sql_text(holder)},"
        f"{sql_text(canonical_timestamp(BASE_OBSERVED_AT + timedelta(minutes=1)))}"
        "::timestamptz);",
        "pit_daily_candle_collection_job_attempt_reused",
    )
    second_attempt = str(uuid4())
    resumed = begin_attempt(
        container,
        paused,
        attempt_id=second_attempt,
        holder_id=holder,
        now=BASE_OBSERVED_AT + timedelta(minutes=2),
    )
    stale_value = candle(
        symbol="000660",
        provider_event_at=BASE_EVENT_AT,
        observed_at=BASE_OBSERVED_AT + timedelta(minutes=3),
    )
    expect_failure(
        container,
        _claims() + "\nselect * from "
        "worker_api.fence_pit_daily_candle_collection_candidate_v1("
        f"{sql_text(str(spec['job_id']))},"
        f"{sql_text(str(paused['spec_sha256']))},{paused['revision']},"
        f"{sql_text(first_attempt)},{sql_text(holder)},{first_fence},"
        f"{payload_literal(stale_value)},"
        f"{sql_text(canonical_timestamp(BASE_OBSERVED_AT + timedelta(minutes=4)))}"
        "::timestamptz);",
        "pit_daily_candle_collection_job_revision_conflict",
    )
    if resumed["revision"] != 4 or resumed["state"] != "collecting":
        raise VerificationError("manual re-begin did not allocate a new fence")
    print("PASS exact pause reason, UUID reuse rejection, and ABA fencing")


def verify_block_reason_boundaries(container: str) -> None:
    blocked_snapshot: dict[str, object] | None = None
    blocked_attempt = ""
    blocked_holder = ""
    blocked_fence = 0
    for index, reason in enumerate(PRE_CANDIDATE_BLOCK_REASONS):
        spec = job_spec(symbol=f"{110000 + index:06d}")
        current = load_or_create(
            container, spec, BASE_OBSERVED_AT - timedelta(minutes=30)
        )
        attempt_id = str(uuid4())
        holder_id = str(uuid4())
        current = begin_attempt(
            container,
            current,
            attempt_id=attempt_id,
            holder_id=holder_id,
            now=BASE_OBSERVED_AT - timedelta(minutes=20),
        )
        fencing_revision = int(current["revision"])
        if index == 0:
            expect_failure(
                container,
                _claims() + "\nselect * from "
                "worker_api.block_pit_daily_candle_collection_attempt_v1("
                f"{sql_text(str(spec['job_id']))},"
                f"{sql_text(str(current['spec_sha256']))},{current['revision']},"
                f"{sql_text(attempt_id)},{sql_text(holder_id)},"
                f"{fencing_revision},'append_outcome_unknown',"
                f"{sql_text(canonical_timestamp(BASE_OBSERVED_AT))}"
                "::timestamptz);",
                "pit_daily_candle_collection_job_reason_scope_invalid",
            )
            expect_failure(
                container,
                _claims() + "\nselect * from "
                "worker_api.block_pit_daily_candle_collection_attempt_v1("
                f"{sql_text(str(spec['job_id']))},"
                f"{sql_text(str(current['spec_sha256']))},{current['revision']},"
                f"{sql_text(attempt_id)},{sql_text(holder_id)},"
                f"{fencing_revision},'arbitrary_retry_reason',"
                f"{sql_text(canonical_timestamp(BASE_OBSERVED_AT))}"
                "::timestamptz);",
                "pit_daily_candle_collection_job_argument_invalid",
            )
        blocked = block_attempt(
            container,
            current,
            attempt_id=attempt_id,
            holder_id=holder_id,
            fencing_revision=fencing_revision,
            reason_code=reason,
            now=BASE_OBSERVED_AT,
        )
        if (
            blocked["state"] != "blocked_unknown"
            or blocked["revision"] != fencing_revision + 1
            or blocked["candidate"] is not None
            or blocked["active_attempt"] is None
            or blocked["state_reason"] != reason
        ):
            raise VerificationError(f"pre-candidate block mismatch: {blocked}")
        blocked_snapshot = blocked
        blocked_attempt = attempt_id
        blocked_holder = holder_id
        blocked_fence = fencing_revision

    if blocked_snapshot is None:
        raise VerificationError("pre-candidate block setup missing")
    blocked_spec = blocked_snapshot["spec"]
    assert isinstance(blocked_spec, dict)
    expect_failure(
        container,
        _claims() + "\nselect * from "
        "worker_api.begin_pit_daily_candle_collection_attempt_v1("
        f"{sql_text(str(blocked_spec['job_id']))},"
        f"{sql_text(str(blocked_snapshot['spec_sha256']))},"
        f"{blocked_snapshot['revision']},{sql_text(str(uuid4()))},"
        f"{sql_text(blocked_holder)},"
        f"{sql_text(canonical_timestamp(BASE_OBSERVED_AT + timedelta(days=365)))}"
        "::timestamptz);",
        "pit_daily_candle_collection_job_begin_state_invalid",
    )

    for index, reason in enumerate(POST_CANDIDATE_BLOCK_REASONS):
        symbol = f"{120000 + index:06d}"
        value = candle(
            symbol=symbol,
            provider_event_at=BASE_EVENT_AT + timedelta(minutes=index),
            observed_at=BASE_OBSERVED_AT + timedelta(minutes=index),
        )
        spec = job_spec(
            symbol=symbol,
            before=BASE_EVENT_AT + timedelta(days=1),
        )
        current = load_or_create(
            container, spec, BASE_OBSERVED_AT - timedelta(minutes=30)
        )
        attempt_id = str(uuid4())
        holder_id = str(uuid4())
        current = begin_attempt(
            container,
            current,
            attempt_id=attempt_id,
            holder_id=holder_id,
            now=BASE_OBSERVED_AT - timedelta(minutes=20),
        )
        fencing_revision = int(current["revision"])
        current = fence_candidate(
            container,
            current,
            value,
            attempt_id=attempt_id,
            holder_id=holder_id,
            fencing_revision=fencing_revision,
            now=value.observed_at + timedelta(minutes=1),
        )
        if index == 0:
            expect_failure(
                container,
                _claims() + "\nselect * from "
                "worker_api.block_pit_daily_candle_collection_attempt_v1("
                f"{sql_text(str(spec['job_id']))},"
                f"{sql_text(str(current['spec_sha256']))},{current['revision']},"
                f"{sql_text(attempt_id)},{sql_text(holder_id)},"
                f"{fencing_revision},"
                "'provider_read_outcome_unknown_before_candidate',"
                f"{sql_text(canonical_timestamp(value.observed_at + timedelta(minutes=2)))}"
                "::timestamptz);",
                "pit_daily_candle_collection_job_reason_scope_invalid",
            )
        blocked = block_attempt(
            container,
            current,
            attempt_id=attempt_id,
            holder_id=holder_id,
            fencing_revision=fencing_revision,
            reason_code=reason,
            now=value.observed_at + timedelta(minutes=2),
        )
        candidate_value = blocked["candidate"]
        if (
            blocked["state"] != "blocked_unknown"
            or blocked["revision"] != fencing_revision + 2
            or type(candidate_value) is not dict
            or candidate_value.get("begun_at") is None
            or blocked["active_attempt"] is None
            or blocked["state_reason"] != reason
        ):
            raise VerificationError(f"post-candidate block mismatch: {blocked}")
    if not blocked_attempt or blocked_fence <= 1:
        raise VerificationError("blocked attempt evidence missing")
    print("PASS state-scoped block reason allowlists and no TTL takeover")


def verify_scope_receipt_and_fence_fail_closed(container: str) -> None:
    spec = job_spec(symbol="130000", before=BASE_EVENT_AT)
    current = load_or_create(container, spec, BASE_OBSERVED_AT - timedelta(minutes=30))
    attempt_id = str(uuid4())
    holder_id = str(uuid4())
    current = begin_attempt(
        container,
        current,
        attempt_id=attempt_id,
        holder_id=holder_id,
        now=BASE_OBSERVED_AT - timedelta(minutes=20),
    )
    fencing_revision = int(current["revision"])
    late_candidate = candle(
        symbol="130000",
        provider_event_at=BASE_EVENT_AT + timedelta(minutes=1),
        observed_at=BASE_OBSERVED_AT,
    )
    expect_failure(
        container,
        _claims() + "\nselect * from "
        "worker_api.fence_pit_daily_candle_collection_candidate_v1("
        f"{sql_text(str(spec['job_id']))},"
        f"{sql_text(str(current['spec_sha256']))},{current['revision']},"
        f"{sql_text(attempt_id)},{sql_text(holder_id)},{fencing_revision},"
        f"{payload_literal(late_candidate)},"
        f"{sql_text(canonical_timestamp(BASE_OBSERVED_AT + timedelta(minutes=1)))}"
        "::timestamptz);",
        "pit_daily_candle_collection_job_scope_mismatch",
    )
    valid_candidate = candle(
        symbol="130000",
        provider_event_at=BASE_EVENT_AT,
        observed_at=BASE_OBSERVED_AT,
    )
    expect_failure(
        container,
        _claims() + "\nselect * from "
        "worker_api.fence_pit_daily_candle_collection_candidate_v1("
        f"{sql_text(str(spec['job_id']))},null,{current['revision']},"
        f"{sql_text(attempt_id)},{sql_text(holder_id)},{fencing_revision},"
        f"{payload_literal(valid_candidate)},"
        f"{sql_text(canonical_timestamp(BASE_OBSERVED_AT + timedelta(minutes=1)))}"
        "::timestamptz);",
        "pit_daily_candle_collection_job_argument_invalid",
    )
    found, unchanged = inspect_job(container, str(spec["job_id"]))
    if not found or unchanged != current:
        raise VerificationError("null spec SHA changed durable state")
    for field in (
        "market",
        "interval",
        "currency",
        "provider_contract_sha256",
        "canonical_observation_sha256",
    ):
        candidate_payload = valid_candidate.to_payload()
        candidate_payload[field] = None
        expect_failure(
            container,
            _claims() + "\nselect * from "
            "worker_api.fence_pit_daily_candle_collection_candidate_v1("
            f"{sql_text(str(spec['job_id']))},"
            f"{sql_text(str(current['spec_sha256']))},{current['revision']},"
            f"{sql_text(attempt_id)},{sql_text(holder_id)},{fencing_revision},"
            f"{jsonb_literal(candidate_payload, f'null_{field}')},"
            f"{sql_text(canonical_timestamp(BASE_OBSERVED_AT + timedelta(minutes=1)))}"
            "::timestamptz);",
            "pit_daily_candle_collection_job_candidate_invalid",
        )
        found, unchanged = inspect_job(container, str(spec["job_id"]))
        if not found or unchanged != current:
            raise VerificationError(
                f"candidate JSON null changed durable state: {field}"
            )
    expect_failure(
        container,
        _claims() + "\nselect * from "
        "worker_api.fence_pit_daily_candle_collection_candidate_v1("
        f"{sql_text(str(spec['job_id']))},"
        f"{sql_text(str(current['spec_sha256']))},{current['revision']},"
        f"{sql_text(attempt_id)},{sql_text(str(uuid4()))},{fencing_revision},"
        f"{payload_literal(valid_candidate)},"
        f"{sql_text(canonical_timestamp(BASE_OBSERVED_AT + timedelta(minutes=1)))}"
        "::timestamptz);",
        "pit_daily_candle_collection_job_attempt_fence_mismatch",
    )
    fenced = fence_candidate(
        container,
        current,
        valid_candidate,
        attempt_id=attempt_id,
        holder_id=holder_id,
        fencing_revision=fencing_revision,
        now=BASE_OBSERVED_AT + timedelta(minutes=1),
    )
    invented_receipt = {
        "idempotency_key": valid_candidate.idempotency_key,
        "canonical_observation_sha256": valid_candidate.canonical_observation_sha256,
        "revision": 1,
        "inserted": True,
        "stored_observed_at": canonical_timestamp(valid_candidate.observed_at),
    }
    fenced_spec = fenced["spec"]
    assert isinstance(fenced_spec, dict)
    expect_failure(
        container,
        _claims() + "\nselect * from "
        "worker_api.confirm_pit_daily_candle_collection_attempt_v1("
        f"{sql_text(str(fenced_spec['job_id']))},"
        f"{sql_text(str(fenced['spec_sha256']))},{fenced['revision']},"
        f"{sql_text(attempt_id)},{sql_text(holder_id)},{fencing_revision},"
        f"{jsonb_literal(invented_receipt, 'invented')},"
        f"{sql_text(canonical_timestamp(BASE_OBSERVED_AT + timedelta(minutes=2)))}"
        "::timestamptz);",
        "pit_daily_candle_collection_job_receipt_mismatch",
    )
    found, durable = inspect_job(container, str(spec["job_id"]))
    if not found or durable != fenced:
        raise VerificationError("failed receipt validation changed durable state")
    print("PASS before/holder/fence/receipt failures leave exact state unchanged")


def verify_concurrent_begin_and_global_uuid_reuse(container: str) -> None:
    spec = job_spec(symbol="140000")
    current = load_or_create(container, spec, BASE_OBSERVED_AT - timedelta(minutes=30))
    holder = str(uuid4())
    attempts = (str(uuid4()), str(uuid4()))

    def invoke(attempt: str):  # type: ignore[no-untyped-def]
        return psql(
            container,
            _claims() + "\nselect * from "
            "worker_api.begin_pit_daily_candle_collection_attempt_v1("
            f"{sql_text(str(spec['job_id']))},"
            f"{sql_text(str(current['spec_sha256']))},{current['revision']},"
            f"{sql_text(attempt)},{sql_text(holder)},"
            f"{sql_text(canonical_timestamp(BASE_OBSERVED_AT))}::timestamptz);",
            check=False,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(invoke, attempts))
    winners = [index for index, result in enumerate(results) if result.returncode == 0]
    if len(winners) != 1:
        raise VerificationError("concurrent begin did not produce one CAS winner")
    winning_attempt = attempts[winners[0]]
    ledger_count = scalar(
        container,
        "select count(*) from "
        "private.pit_daily_candle_collection_attempt_ledger "
        f"where job_id={sql_text(str(spec['job_id']))}::uuid "
        "and event_kind='begun';",
    )
    if ledger_count != "1":
        raise VerificationError("concurrent begin wrote duplicate begin receipts")

    second_spec = job_spec(symbol="140001")
    second = load_or_create(
        container, second_spec, BASE_OBSERVED_AT - timedelta(minutes=30)
    )
    expect_failure(
        container,
        _claims() + "\nselect * from "
        "worker_api.begin_pit_daily_candle_collection_attempt_v1("
        f"{sql_text(str(second_spec['job_id']))},"
        f"{sql_text(str(second['spec_sha256']))},{second['revision']},"
        f"{sql_text(winning_attempt)},{sql_text(holder)},"
        f"{sql_text(canonical_timestamp(BASE_OBSERVED_AT))}::timestamptz);",
        "pit_daily_candle_collection_job_attempt_reused",
    )
    print("PASS concurrent CAS and global attempt UUID reuse rejection")


def verify_revision_overflow(container: str) -> None:
    spec = job_spec(symbol="150000")
    current = load_or_create(container, spec, BASE_OBSERVED_AT - timedelta(minutes=30))
    expect_failure(
        container,
        _claims() + "\nselect * from "
        "worker_api.begin_pit_daily_candle_collection_attempt_v1("
        f"{sql_text(str(spec['job_id']))},"
        f"{sql_text(str(current['spec_sha256']))},9223372036854775807,"
        f"{sql_text(str(uuid4()))},{sql_text(str(uuid4()))},"
        f"{sql_text(canonical_timestamp(BASE_OBSERVED_AT))}::timestamptz);",
        "pit_daily_candle_collection_job_argument_invalid",
    )
    expect_failure(
        container,
        _claims() + "\nselect * from "
        "worker_api.begin_pit_daily_candle_collection_attempt_v1("
        f"{sql_text(str(spec['job_id']))},"
        f"{sql_text(str(current['spec_sha256']))},9223372036854775805,"
        f"{sql_text(str(uuid4()))},{sql_text(str(uuid4()))},"
        f"{sql_text(canonical_timestamp(BASE_OBSERVED_AT))}::timestamptz);",
        "pit_daily_candle_collection_job_revision_exhausted",
    )
    found, unchanged = inspect_job(container, str(spec["job_id"]))
    if not found or unchanged != current:
        raise VerificationError("begin headroom rejection changed durable state")
    attempt_id = str(uuid4())
    holder_id = str(uuid4())
    current = begin_attempt(
        container,
        current,
        attempt_id=attempt_id,
        holder_id=holder_id,
        now=BASE_OBSERVED_AT - timedelta(minutes=20),
    )
    value = candle(
        symbol="150000",
        provider_event_at=BASE_EVENT_AT,
        observed_at=BASE_OBSERVED_AT,
    )
    expect_failure(
        container,
        _claims() + "\nselect * from "
        "worker_api.fence_pit_daily_candle_collection_candidate_v1("
        f"{sql_text(str(spec['job_id']))},{sql_text(str(current['spec_sha256']))},"
        "9223372036854775806,"
        f"{sql_text(attempt_id)},{sql_text(holder_id)},2,"
        f"{payload_literal(value)},"
        f"{sql_text(canonical_timestamp(BASE_OBSERVED_AT + timedelta(minutes=1)))}"
        "::timestamptz);",
        "pit_daily_candle_collection_job_revision_exhausted",
    )
    found, unchanged = inspect_job(container, str(spec["job_id"]))
    if not found or unchanged != current:
        raise VerificationError("fence headroom rejection changed durable state")

    expect_failure(
        container,
        _claims() + "\nselect * from "
        "worker_api.pause_pit_daily_candle_collection_attempt_v1("
        f"{sql_text(str(spec['job_id']))},{sql_text(str(current['spec_sha256']))},"
        "9223372036854775804,"
        f"{sql_text(attempt_id)},{sql_text(holder_id)},2,"
        "'provider_read_failed_before_candidate',"
        f"{sql_text(canonical_timestamp(BASE_OBSERVED_AT + timedelta(minutes=1)))}"
        "::timestamptz);",
        "pit_daily_candle_collection_job_revision_exhausted",
    )
    found, unchanged = inspect_job(container, str(spec["job_id"]))
    if not found or unchanged != current:
        raise VerificationError("pause headroom rejection changed durable state")
    expect_failure(
        container,
        _claims() + "\nselect * from "
        "worker_api.block_pit_daily_candle_collection_attempt_v1("
        f"{sql_text(str(spec['job_id']))},{sql_text(str(current['spec_sha256']))},"
        "9223372036854775806,"
        f"{sql_text(attempt_id)},{sql_text(holder_id)},2,"
        "'unexpected_failure_before_candidate',"
        f"{sql_text(canonical_timestamp(BASE_OBSERVED_AT + timedelta(minutes=1)))}"
        "::timestamptz);",
        "pit_daily_candle_collection_job_revision_exhausted",
    )
    receipt = {
        "idempotency_key": value.idempotency_key,
        "canonical_observation_sha256": value.canonical_observation_sha256,
        "revision": 1,
        "inserted": True,
        "stored_observed_at": canonical_timestamp(value.observed_at),
    }
    expect_failure(
        container,
        _claims() + "\nselect * from "
        "worker_api.confirm_pit_daily_candle_collection_attempt_v1("
        f"{sql_text(str(spec['job_id']))},{sql_text(str(current['spec_sha256']))},"
        "9223372036854775807,"
        f"{sql_text(attempt_id)},{sql_text(holder_id)},2,"
        f"{jsonb_literal(receipt, 'invalid_max_receipt')},"
        f"{sql_text(canonical_timestamp(BASE_OBSERVED_AT + timedelta(minutes=1)))}"
        "::timestamptz);",
        "pit_daily_candle_collection_job_argument_invalid",
    )
    expect_failure(
        container,
        _claims() + "\nselect * from "
        "worker_api.confirm_pit_daily_candle_collection_attempt_v1("
        f"{sql_text(str(spec['job_id']))},{sql_text(str(current['spec_sha256']))},"
        "9223372036854775806,"
        f"{sql_text(attempt_id)},{sql_text(holder_id)},2,"
        f"{jsonb_literal(receipt, 'headroom_receipt')},"
        f"{sql_text(canonical_timestamp(BASE_OBSERVED_AT + timedelta(minutes=1)))}"
        "::timestamptz);",
        "pit_daily_candle_collection_job_revision_exhausted",
    )
    found, unchanged = inspect_job(container, str(spec["job_id"]))
    if not found or unchanged != current:
        raise VerificationError("terminal headroom rejection changed durable state")
    _verify_revision_headroom_success_boundaries(container)
    print("PASS exact bigint headroom success and exhausted rejection boundaries")


def _high_collecting_fixture(
    container: str,
    *,
    symbol: str,
    fencing_revision: int,
) -> tuple[dict[str, object], str, str]:
    spec = job_spec(symbol=symbol)
    snapshot = load_or_create(
        container,
        spec,
        BASE_OBSERVED_AT - timedelta(minutes=30),
    )
    attempt_id = str(uuid4())
    holder_id = str(uuid4())
    snapshot = begin_attempt(
        container,
        snapshot,
        attempt_id=attempt_id,
        holder_id=holder_id,
        now=BASE_OBSERVED_AT - timedelta(minutes=20),
    )
    psql(
        container,
        "update private.pit_daily_candle_collection_jobs "
        f"set revision={fencing_revision},"
        f"active_fencing_revision={fencing_revision} "
        f"where job_id={sql_text(str(spec['job_id']))}::uuid;",
    )
    found, snapshot = inspect_job(container, str(spec["job_id"]))
    if not found or snapshot is None:
        raise VerificationError(
            f"maximum collecting fixture is not inspectable: {symbol}"
        )
    return snapshot, attempt_id, holder_id


def _complete_high_candidate(
    container: str,
    snapshot: dict[str, object],
    value: PointInTimeCandleV1,
    *,
    attempt_id: str,
    holder_id: str,
) -> dict[str, object]:
    fenced = fence_candidate(
        container,
        snapshot,
        value,
        attempt_id=attempt_id,
        holder_id=holder_id,
        fencing_revision=9223372036854775804,
        now=BASE_OBSERVED_AT + timedelta(minutes=1),
    )
    return confirm_attempt(
        container,
        fenced,
        receipt_payload(append(container, value)),
        attempt_id=attempt_id,
        holder_id=holder_id,
        fencing_revision=9223372036854775804,
        now=BASE_OBSERVED_AT + timedelta(minutes=2),
    )


def _verify_revision_headroom_success_boundaries(container: str) -> None:
    success_spec = job_spec(symbol="150001")
    success = load_or_create(
        container,
        success_spec,
        BASE_OBSERVED_AT - timedelta(minutes=30),
    )
    psql(
        container,
        "update private.pit_daily_candle_collection_jobs "
        "set revision=9223372036854775803,state='paused_retryable',"
        "state_reason='provider_read_failed_before_candidate' "
        f"where job_id={sql_text(str(success_spec['job_id']))}::uuid;",
    )
    found, success = inspect_job(container, str(success_spec["job_id"]))
    if not found or success is None:
        raise VerificationError("maximum begin fixture is not inspectable")
    success_attempt = str(uuid4())
    success_holder = str(uuid4())
    success = begin_attempt(
        container,
        success,
        attempt_id=success_attempt,
        holder_id=success_holder,
        now=BASE_OBSERVED_AT - timedelta(minutes=20),
    )
    success = _complete_high_candidate(
        container,
        success,
        candle(
            symbol="150001",
            provider_event_at=BASE_EVENT_AT,
            observed_at=BASE_OBSERVED_AT,
        ),
        attempt_id=success_attempt,
        holder_id=success_holder,
    )
    if success["state"] != "completed" or success["revision"] != 9223372036854775806:
        raise VerificationError("maximum begin/fence/confirm path did not complete")

    pause_state, first_attempt, first_holder = _high_collecting_fixture(
        container,
        symbol="150002",
        fencing_revision=9223372036854775802,
    )
    pause_state = pause_attempt(
        container,
        pause_state,
        attempt_id=first_attempt,
        holder_id=first_holder,
        fencing_revision=9223372036854775802,
        now=BASE_OBSERVED_AT - timedelta(minutes=10),
    )
    expect_failure(
        container,
        "update private.pit_daily_candle_collection_jobs "
        "set revision=9223372036854775802 "
        f"where job_id={sql_text(str(pause_state['spec']['job_id']))}::uuid;",
        "pit_daily_candle_collection_revision_headroom_check",
    )
    found, unchanged = inspect_job(
        container,
        str(pause_state["spec"]["job_id"]),
    )
    if not found or unchanged != pause_state:
        raise VerificationError(
            "paused revision parity rejection changed durable state"
        )
    retry_attempt = str(uuid4())
    retry_holder = str(uuid4())
    pause_state = begin_attempt(
        container,
        pause_state,
        attempt_id=retry_attempt,
        holder_id=retry_holder,
        now=BASE_OBSERVED_AT - timedelta(minutes=5),
    )
    pause_state = _complete_high_candidate(
        container,
        pause_state,
        candle(
            symbol="150002",
            provider_event_at=BASE_EVENT_AT,
            observed_at=BASE_OBSERVED_AT,
        ),
        attempt_id=retry_attempt,
        holder_id=retry_holder,
    )
    if (
        pause_state["state"] != "completed"
        or pause_state["revision"] != 9223372036854775806
    ):
        raise VerificationError("maximum pause retry path did not complete")

    for symbol, post_candidate in (("150003", False), ("150004", True)):
        block_state, block_attempt_id, block_holder_id = _high_collecting_fixture(
            container,
            symbol=symbol,
            fencing_revision=9223372036854775804,
        )
        if post_candidate:
            block_state = fence_candidate(
                container,
                block_state,
                candle(
                    symbol=symbol,
                    provider_event_at=BASE_EVENT_AT,
                    observed_at=BASE_OBSERVED_AT,
                ),
                attempt_id=block_attempt_id,
                holder_id=block_holder_id,
                fencing_revision=9223372036854775804,
                now=BASE_OBSERVED_AT + timedelta(minutes=1),
            )
        block_state = block_attempt(
            container,
            block_state,
            attempt_id=block_attempt_id,
            holder_id=block_holder_id,
            fencing_revision=9223372036854775804,
            reason_code=(
                "append_outcome_unknown"
                if post_candidate
                else "unexpected_failure_before_candidate"
            ),
            now=BASE_OBSERVED_AT + timedelta(minutes=2),
        )
        expected_revision = (
            9223372036854775806 if post_candidate else 9223372036854775805
        )
        if (
            block_state["state"] != "blocked_unknown"
            or block_state["revision"] != expected_revision
        ):
            raise VerificationError("maximum block path did not complete")


def verify_security_contract(container: str) -> None:
    rpc_names = ",".join(sql_text(name) for name in RPC_NAMES)
    catalog = scalar(
        container,
        "select concat_ws('|',"
        "(select count(*) from pg_catalog.pg_class c "
        "where c.oid in ("
        "'private.pit_daily_candle_collection_jobs'::regclass,"
        "'private.pit_daily_candle_collection_attempt_ledger'::regclass) "
        "and c.relrowsecurity and c.relforcerowsecurity),"
        "(select count(*) from pg_catalog.pg_policy p where p.polrelid in ("
        "'private.pit_daily_candle_collection_jobs'::regclass,"
        "'private.pit_daily_candle_collection_attempt_ledger'::regclass)),"
        "(select count(*) from pg_catalog.pg_trigger t "
        "where t.tgrelid="
        "'private.pit_daily_candle_collection_attempt_ledger'::regclass "
        "and not t.tgisinternal),"
        "(select count(*) from pg_catalog.pg_proc p "
        "join pg_catalog.pg_namespace n on n.oid=p.pronamespace "
        f"where n.nspname='worker_api' and p.proname in ({rpc_names})),"
        "(select count(*) from pg_catalog.pg_proc p "
        "join pg_catalog.pg_namespace n on n.oid=p.pronamespace "
        f"where n.nspname='worker_api' and p.proname in ({rpc_names}) "
        "and not p.prosecdef "
        "and p.proconfig=array['search_path=\"\"']::text[]),"
        "(select count(*) from pg_catalog.pg_proc p "
        "join pg_catalog.pg_namespace n on n.oid=p.pronamespace "
        f"where n.nspname='worker_api' and p.proname in ({rpc_names}) "
        "and has_function_privilege('service_role',p.oid,'EXECUTE')),"
        "(select count(*) from pg_catalog.pg_proc p "
        "join pg_catalog.pg_namespace n on n.oid=p.pronamespace "
        "cross join lateral pg_catalog.aclexplode(coalesce("
        "p.proacl,pg_catalog.acldefault('f',p.proowner))) acl "
        "left join pg_catalog.pg_roles r on r.oid=acl.grantee "
        "where p.proname like '%pit_daily_candle_collection%' "
        "and n.nspname in ('private','worker_api') "
        "and acl.privilege_type='EXECUTE' "
        "and (acl.grantee=0 or r.rolname in "
        "('anon','authenticated','authenticator'))),"
        "not has_table_privilege('service_role',"
        "'private.pit_daily_candle_collection_jobs',"
        "'SELECT,INSERT,UPDATE,DELETE'),"
        "not has_table_privilege('service_role',"
        "'private.pit_daily_candle_collection_attempt_ledger',"
        "'SELECT,INSERT,UPDATE,DELETE'),"
        "not has_sequence_privilege('service_role',"
        "'private.pit_daily_candle_collection_attempt_ledger_event_id_seq',"
        "'USAGE,SELECT,UPDATE'),"
        "(select count(*)=0 from pg_catalog.pg_proc p "
        "where p.proname like '%pit_daily_candle_collection%' "
        "and (p.proname like '%takeover%' or p.proname like '%expire%' "
        "or p.proname like '%reset%' or p.proname like '%retry%'))"
        ");",
    )
    if catalog != "2|0|1|7|7|7|0|t|t|t|t":
        raise VerificationError(f"security catalog mismatch: {catalog}")

    specimen = job_spec(symbol="160000")
    expect_failure(
        container,
        jwt_claim_sql(WORKER_ID, role="authenticated") + "\nselect * from "
        "worker_api.load_or_create_pit_daily_candle_collection_job_v1("
        f"{jsonb_literal(specimen, 'auth')},"
        f"{sql_text(canonical_timestamp(BASE_OBSERVED_AT))}::timestamptz);",
        "permission denied",
    )
    expect_failure(
        container,
        jwt_claim_sql(WORKER_ID, role="anon") + "\nselect * from "
        "worker_api.inspect_pit_daily_candle_collection_job_v1("
        f"{sql_text(str(uuid4()))});",
        "permission denied",
    )
    expect_failure(
        container,
        _claims() + "\nselect count(*) from private.pit_daily_candle_collection_jobs;",
        "permission denied",
    )
    ledger_rows = int(
        scalar(
            container,
            "select count(*) from private.pit_daily_candle_collection_attempt_ledger;",
        )
    )
    if ledger_rows < 1:
        raise VerificationError("append-only ledger fixture is missing")
    expect_failure(
        container,
        "update private.pit_daily_candle_collection_attempt_ledger "
        "set occurred_at=occurred_at where event_id=(select min(event_id) "
        "from private.pit_daily_candle_collection_attempt_ledger);",
        "append_only",
    )
    print("PASS forced RLS, zero policies, append-only ledger, and RPC ACLs")


def verify_populated_upgrade(container: str) -> None:
    psql(container, bootstrap_sql())
    target = MIGRATIONS / MIGRATION_NAME
    migrations = sorted(MIGRATIONS.glob("*.sql"))
    try:
        target_index = migrations.index(target)
    except ValueError as error:
        raise VerificationError("daily candle job migration missing") from error
    for migration in migrations[:target_index]:
        psql(container, migration.read_text(encoding="utf-8"))
    psql(container, SEED.read_text(encoding="utf-8"))

    source = candle(
        symbol="170000",
        provider_event_at=BASE_EVENT_AT,
        observed_at=BASE_OBSERVED_AT,
    )
    append(container, source)
    source_before = scalar(
        container,
        "select concat_ws('|',"
        "(select count(*) from private.pit_candle_observation_revisions),"
        "(select count(*) from private.pit_candle_observation_occurrences));",
    )
    psql(container, target.read_text(encoding="utf-8"))
    spec = job_spec(symbol=source.symbol)
    created = load_or_create(container, spec, BASE_OBSERVED_AT - timedelta(minutes=20))
    reloaded = load_or_create(container, spec, BASE_OBSERVED_AT - timedelta(minutes=19))
    for migration in migrations[target_index + 1 :]:
        psql(container, migration.read_text(encoding="utf-8"))
    found, inspected = inspect_job(container, str(spec["job_id"]))
    source_after = scalar(
        container,
        "select concat_ws('|',"
        "(select count(*) from private.pit_candle_observation_revisions),"
        "(select count(*) from private.pit_candle_observation_occurrences));",
    )
    if (
        created != reloaded
        or not found
        or inspected != created
        or source_before != source_after
        or source_before == "0|0"
    ):
        raise VerificationError("populated upgrade changed source or job state")
    collecting = begin_attempt(
        container,
        created,
        attempt_id=str(uuid4()),
        holder_id=str(uuid4()),
        now=BASE_OBSERVED_AT - timedelta(minutes=18),
    )
    if collecting["state"] != "collecting" or collecting["revision"] != 2:
        raise VerificationError("populated upgrade ledger fixture creation failed")
    verify_security_contract(container)
    print("PASS populated upgrade and reconnect durability")


def cleanup_disposable_resources(containers: tuple[str, ...]) -> str | None:
    try:
        for container in containers:
            run(["docker", "rm", "-f", container], check=False)
        listed = run(
            ["docker", "ps", "-a", "--format", "{{.Names}}"],
            check=False,
        )
    except OSError:
        return "disposable_resource_cleanup_command_unavailable"
    if listed.returncode != 0:
        return "disposable_resource_cleanup_verification_failed"
    remaining = set(listed.stdout.splitlines()).intersection(containers)
    return "disposable_resource_cleanup_incomplete" if remaining else None


def main() -> int:
    suffix = uuid4().hex[:10]
    fresh = f"msp-pit-candle-job-fresh-{suffix}"
    upgrade = f"msp-pit-candle-job-upgrade-{suffix}"
    failure: str | None = None
    try:
        run(["docker", "info"])
        for container in (fresh, upgrade):
            run(
                [
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    container,
                    "-e",
                    f"POSTGRES_PASSWORD={DB_PASSWORD}",
                    POSTGRES_IMAGE,
                ]
            )
            wait_for_postgres(container)
        apply_repository(fresh)
        domain_before = domain_snapshot(fresh)
        verify_create_replay_and_inspect(fresh)
        verify_completion_exact_join(fresh)
        verify_pause_rebegin_and_aba(fresh)
        verify_block_reason_boundaries(fresh)
        verify_scope_receipt_and_fence_fail_closed(fresh)
        verify_concurrent_begin_and_global_uuid_reuse(fresh)
        verify_revision_overflow(fresh)
        verify_security_contract(fresh)
        if domain_snapshot(fresh) != domain_before:
            raise VerificationError("collection jobs changed trading/order rows")
        print("PASS success and failure paths write zero trading/order rows")
        verify_populated_upgrade(upgrade)
    except (VerificationError, OSError, json.JSONDecodeError, ValueError) as error:
        failure = str(error)
    finally:
        cleanup_failure = cleanup_disposable_resources((upgrade, fresh))
    if cleanup_failure is not None:
        failure = (
            cleanup_failure if failure is None else f"{failure}; {cleanup_failure}"
        )
    if failure is not None:
        print(f"FINAL=FAIL {failure}", file=sys.stderr)
        return 1
    print("FINAL=PASS pit_daily_candle_collection_job_store_verifier")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
