#!/usr/bin/env python3
"""Verify the durable, manually fenced KR calendar collection job store."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parent
WORKER_ROOT = ROOT.parent / "apps" / "worker"
sys.path.insert(0, str(WORKER_ROOT))

from verify_g1_g2_migration import (  # noqa: E402
    DB_PASSWORD,
    JWT_SECRET,
    MIGRATIONS,
    POSTGRES_IMAGE,
    POSTGREST_IMAGE,
    SEED,
    VerificationError,
    apply_repository,
    bootstrap_sql,
    expect_failure,
    jwt_claim_sql,
    jwt_token,
    psql,
    run,
    wait_for_postgres,
)
from verify_pit_calendar_observation_store import (  # noqa: E402
    append_calendar,
    calendar_fixture,
    domain_snapshot,
)
from verify_pit_daily_candle_timing_store import (  # noqa: E402
    WORKER_ID,
    jsonb_literal,
    scalar,
    sql_text,
)

from app.application.ports.kr_calendar_collection_job_store_port import (  # noqa: E402
    KrCalendarCollectionJobSpecV1,
)

MIGRATION_NAME = "20260719060000_kr_calendar_collection_job_store.sql"
SNAPSHOT_FIELDS = {
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
SPEC_FIELDS = {
    "schema_version",
    "job_id",
    "provider",
    "market",
    "start_date",
    "end_date",
    "trigger",
}
TABLES = (
    "kr_calendar_collection_jobs",
    "kr_calendar_collection_attempt_ledger",
)
RPC_NAMES = (
    "load_or_create_kr_calendar_collection_job_v1",
    "begin_kr_calendar_collection_date_attempt_v1",
    "pause_kr_calendar_collection_date_attempt_v1",
    "block_kr_calendar_collection_date_attempt_v1",
    "confirm_kr_calendar_collection_date_v1",
)
CHECKPOINT_FIELDS = {
    "attempt_id",
    "holder_id",
    "target_date",
    "fencing_revision",
    "begun_at",
    "session",
    "receipt",
    "confirmed_at",
}
ACTIVE_ATTEMPT_FIELDS = {
    "attempt_id",
    "holder_id",
    "target_date",
    "fencing_revision",
    "begun_at",
}
CALENDAR_RECEIPT_FIELDS = {
    "status",
    "calendar_idempotency_key",
    "canonical_evidence_sha256",
    "revision",
    "revision_inserted",
    "occurrence_id",
    "occurrence_inserted",
    "observed_at",
    "quarantine_id",
    "reason_code",
}
MAX_RPC_RESPONSE_BYTES = 4 * 1024 * 1024
MINIMUM_RESPONSE_HEADROOM_BYTES = 64 * 1024


def canonical_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def require_canonical_timestamp(value: object, field_name: str) -> None:
    if type(value) is not str:
        raise VerificationError(f"{field_name} is not timestamp text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise VerificationError(f"{field_name} is not a timestamp") from error
    if parsed.tzinfo is None or canonical_timestamp(parsed) != value:
        raise VerificationError(f"{field_name} is not canonical UTC: {value}")


def spec_payload(spec: KrCalendarCollectionJobSpecV1) -> dict[str, object]:
    return {
        "schema_version": spec.schema_version,
        "job_id": spec.job_id,
        "provider": spec.provider,
        "market": spec.market,
        "start_date": spec.start_date.isoformat(),
        "end_date": spec.end_date.isoformat(),
        "trigger": spec.trigger,
    }


def job_spec(*, start: date, days: int = 2) -> KrCalendarCollectionJobSpecV1:
    return KrCalendarCollectionJobSpecV1(
        job_id=str(uuid4()),
        provider="toss",
        market="KR",
        start_date=start,
        end_date=start + timedelta(days=days - 1),
        trigger="manual",
    )


def validate_snapshot(snapshot: object) -> dict[str, object]:
    if type(snapshot) is not dict or set(snapshot) != SNAPSHOT_FIELDS:
        raise VerificationError(f"snapshot shape mismatch: {snapshot}")
    if (
        snapshot["schema_version"] != "kr_calendar_collection_job_snapshot.v1"
        or snapshot["automatic_retry_allowed"] is not False
        or type(snapshot["spec"]) is not dict
        or set(snapshot["spec"]) != SPEC_FIELDS
    ):
        raise VerificationError(f"snapshot contract mismatch: {snapshot}")
    require_canonical_timestamp(snapshot["created_at"], "snapshot.created_at")
    require_canonical_timestamp(snapshot["updated_at"], "snapshot.updated_at")
    active_attempt = snapshot["active_attempt"]
    if active_attempt is not None:
        if type(active_attempt) is not dict or set(active_attempt) != ACTIVE_ATTEMPT_FIELDS:
            raise VerificationError("snapshot active attempt shape mismatch")
        require_canonical_timestamp(
            active_attempt.get("begun_at"),
            "snapshot.active_attempt.begun_at",
        )
    checkpoints = snapshot["checkpoints"]
    if type(checkpoints) is not list:
        raise VerificationError("snapshot checkpoints are not an array")
    for index, checkpoint in enumerate(checkpoints):
        if type(checkpoint) is not dict or set(checkpoint) != CHECKPOINT_FIELDS:
            raise VerificationError("snapshot checkpoint shape mismatch")
        for key in ("begun_at", "confirmed_at"):
            require_canonical_timestamp(
                checkpoint.get(key),
                f"snapshot.checkpoints[{index}].{key}",
            )
        session = checkpoint.get("session")
        receipt = checkpoint.get("receipt")
        if type(session) is not dict or type(receipt) is not dict:
            raise VerificationError("snapshot checkpoint evidence is invalid")
        require_canonical_timestamp(
            session.get("observed_at"),
            f"snapshot.checkpoints[{index}].session.observed_at",
        )
        for key in (
            "regular_start_at",
            "regular_end_at",
            "next_regular_start_at",
            "next_regular_end_at",
        ):
            value = session.get(key)
            if value is not None:
                require_canonical_timestamp(
                    value,
                    f"snapshot.checkpoints[{index}].session.{key}",
                )
        require_canonical_timestamp(
            receipt.get("observed_at"),
            f"snapshot.checkpoints[{index}].receipt.observed_at",
        )
    return snapshot


def _snapshot(container: str, call_sql: str) -> dict[str, object]:
    rows = psql(
        container,
        jwt_claim_sql(WORKER_ID, role="service_role")
        + f"select row_to_json(result) from ({call_sql}) as result;",
    ).stdout.strip().splitlines()
    if not rows:
        raise VerificationError(f"RPC did not return one row: {rows}")
    envelope = json.loads(rows[-1])
    if type(envelope) is not dict or set(envelope) != {"snapshot"}:
        raise VerificationError(f"RPC row shape mismatch: {envelope}")
    return validate_snapshot(envelope["snapshot"])


def load_or_create(
    container: str,
    spec: KrCalendarCollectionJobSpecV1,
    now: datetime,
) -> dict[str, object]:
    return _snapshot(
        container,
        "select * from worker_api.load_or_create_kr_calendar_collection_job_v1("
        f"{jsonb_literal(spec_payload(spec), 'job_spec')},"
        f"{sql_text(now.isoformat())}::timestamptz)",
    )


def transition_call(
    rpc: str,
    *,
    spec: KrCalendarCollectionJobSpecV1,
    revision: int,
    attempt_id: str,
    holder_id: str,
    target_date: date,
    now: datetime,
    reason: str | None = None,
    session: dict[str, object] | None = None,
    receipt: dict[str, object] | None = None,
) -> str:
    values = [
        sql_text(spec.job_id),
        sql_text(spec.spec_sha256),
        str(revision),
        sql_text(attempt_id),
        sql_text(holder_id),
        f"{sql_text(target_date.isoformat())}::date",
    ]
    if reason is not None:
        values.append(sql_text(reason))
    if session is not None and receipt is not None:
        values.extend(
            [
                jsonb_literal(session, "confirm_session"),
                jsonb_literal(receipt, "confirm_receipt"),
            ]
        )
    values.append(f"{sql_text(now.isoformat())}::timestamptz")
    return f"select * from worker_api.{rpc}({','.join(values)})"


def begin(
    container: str,
    spec: KrCalendarCollectionJobSpecV1,
    revision: int,
    attempt_id: str,
    holder_id: str,
    target_date: date,
    now: datetime,
) -> dict[str, object]:
    return _snapshot(
        container,
        transition_call(
            "begin_kr_calendar_collection_date_attempt_v1",
            spec=spec,
            revision=revision,
            attempt_id=attempt_id,
            holder_id=holder_id,
            target_date=target_date,
            now=now,
        ),
    )


def receipt_payload(receipt: dict[str, object]) -> dict[str, object]:
    keys = {
        "status",
        "calendar_idempotency_key",
        "canonical_evidence_sha256",
        "revision",
        "revision_inserted",
        "occurrence_id",
        "occurrence_inserted",
        "observed_at",
    }
    value = {key: receipt[key] for key in keys}
    observed_at = value["observed_at"]
    if type(observed_at) is not str:
        raise VerificationError("calendar receipt observed_at is not text")
    value["observed_at"] = canonical_timestamp(
        datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    )
    return value


def load_rpc_payload(
    spec: KrCalendarCollectionJobSpecV1,
    now: datetime,
) -> dict[str, object]:
    return {
        "p_spec": spec_payload(spec),
        "p_now": canonical_timestamp(now),
    }


def transition_rpc_payload(
    *,
    spec: KrCalendarCollectionJobSpecV1,
    revision: int,
    attempt_id: str,
    holder_id: str,
    target_date: date,
    now: datetime,
    reason: str | None = None,
    session: dict[str, object] | None = None,
    receipt: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "p_job_id": spec.job_id,
        "p_spec_sha256": spec.spec_sha256,
        "p_expected_revision": revision,
        "p_attempt_id": attempt_id,
        "p_holder_id": holder_id,
        "p_target_date": target_date.isoformat(),
        "p_now": canonical_timestamp(now),
    }
    if reason is not None:
        payload["p_reason_code"] = reason
    if session is not None and receipt is not None:
        payload["p_session"] = session
        payload["p_receipt"] = receipt
    return payload


def http_post_rpc(
    root: str,
    rpc: str,
    token: str | None,
    body: dict[str, object],
    *,
    profile: str = "worker_api",
) -> tuple[int, bytes, dict[str, str]]:
    if rpc not in RPC_NAMES:
        raise VerificationError(f"unexpected PostgREST RPC name: {rpc}")
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "Accept-Profile": profile,
        "Content-Profile": profile,
        "Content-Type": "application/json",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(
        f"{root}/rpc/{rpc}",
        data=json.dumps(
            body,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            status = response.status
            response_headers = {
                key.lower(): value for key, value in response.headers.items()
            }
            raw = response.read(MAX_RPC_RESPONSE_BYTES + 1)
    except HTTPError as error:
        status = error.code
        response_headers = {
            key.lower(): value for key, value in error.headers.items()
        }
        raw = error.read(MAX_RPC_RESPONSE_BYTES + 1)
    except (OSError, URLError) as error:
        raise VerificationError(
            f"PostgREST request transport failed for {rpc}: "
            f"{type(error).__name__}"
        ) from None
    if len(raw) > MAX_RPC_RESPONSE_BYTES:
        raise VerificationError("PostgREST response exceeded the 4 MiB contract")
    if response_headers.get("content-encoding") not in (None, "identity"):
        raise VerificationError("PostgREST returned non-identity content encoding")
    if "application/json" not in response_headers.get("content-type", ""):
        raise VerificationError("PostgREST response was not JSON")
    return status, raw, response_headers


def require_postgrest_error(
    status: int,
    raw: bytes,
    *,
    expected_status: int,
    expected_code: str,
    expected_message: str | None = None,
    require_null_context: bool = True,
) -> dict[str, object]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise VerificationError("PostgREST error was not canonical JSON") from error
    if (
        status != expected_status
        or type(payload) is not dict
        or set(payload) != {"code", "details", "hint", "message"}
        or payload["code"] != expected_code
        or (
            require_null_context
            and (payload["details"] is not None or payload["hint"] is not None)
        )
        or (
            expected_message is not None
            and payload["message"] != expected_message
        )
    ):
        raise VerificationError(
            "PostgREST safe error contract mismatch: "
            f"status={status}, payload={payload}"
        )
    return payload


def postgrest_snapshot(
    root: str,
    rpc: str,
    token: str,
    body: dict[str, object],
) -> tuple[dict[str, object], int]:
    status, raw, _ = http_post_rpc(root, rpc, token, body)
    if status != 200:
        raise VerificationError(f"PostgREST RPC {rpc} failed with HTTP {status}")
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise VerificationError("PostgREST success was not canonical JSON") from error
    if (
        type(envelope) is not list
        or len(envelope) != 1
        or type(envelope[0]) is not dict
        or set(envelope[0]) != {"snapshot"}
    ):
        raise VerificationError(f"PostgREST RPC envelope mismatch: {envelope}")
    return validate_snapshot(envelope[0]["snapshot"]), len(raw)


def start_postgrest(pg: str, network: str, container: str) -> str:
    run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            container,
            "--network",
            network,
            "-p",
            "127.0.0.1::3000",
            "-e",
            f"PGRST_DB_URI=postgres://authenticator:{DB_PASSWORD}@{pg}:5432/postgres",
            "-e",
            "PGRST_DB_SCHEMAS=api,worker_api",
            "-e",
            "PGRST_DB_ANON_ROLE=anon",
            "-e",
            f"PGRST_JWT_SECRET={JWT_SECRET}",
            POSTGREST_IMAGE,
        ]
    )
    port_text = run(["docker", "port", container, "3000/tcp"]).stdout.strip()
    port_lines = port_text.splitlines()
    if len(port_lines) != 1 or not port_lines[0].startswith("127.0.0.1:"):
        raise VerificationError(
            f"PostgREST published port shape mismatch: {port_text!r}"
        )
    port = port_lines[0].removeprefix("127.0.0.1:")
    if not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise VerificationError(
            f"PostgREST published port is invalid: {port_text!r}"
        )
    host = os.environ.get("KR_CALENDAR_POSTGREST_HOST", "127.0.0.1")
    root = f"http://{host}:{port}"
    for _ in range(60):
        try:
            request = Request(root, headers={"Accept-Encoding": "identity"})
            with urlopen(request, timeout=1) as response:
                response.read(1)
            return root
        except HTTPError as error:
            if error.code < 500:
                return root
            time.sleep(0.5)
        except (OSError, URLError):
            time.sleep(0.5)
    logs = run(["docker", "logs", container], check=False)
    raise VerificationError(
        "PostgREST did not become ready:\n" + logs.stdout + logs.stderr
    )


def python_terminal_manifest(snapshot: dict[str, object]) -> str:
    checkpoints = snapshot["checkpoints"]
    if type(checkpoints) is not list:
        raise VerificationError("terminal snapshot checkpoints are not an array")
    expanded_spec = dict(snapshot["spec"])
    expanded_spec["maximum_inclusive_days"] = 366
    expanded_spec["automatic_retry_allowed"] = False
    manifest_payload = {
        "schema_version": "kr_calendar_collection_job_manifest.v1",
        "spec": expanded_spec,
        "spec_sha256": snapshot["spec_sha256"],
        "confirmed_count": len(checkpoints),
        "checkpoints": checkpoints,
    }
    return hashlib.sha256(
        json.dumps(
            manifest_payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def verify_fresh(container: str) -> None:
    session = calendar_fixture(day_offset=0)
    spec = job_spec(start=session.session_date)
    now = session.observed_at - timedelta(minutes=10)
    before = domain_snapshot(container)
    created = load_or_create(container, spec, now)
    replayed = load_or_create(container, spec, now + timedelta(minutes=1))
    if (
        created != replayed
        or created["revision"] != 1
        or created["state"] != "ready"
        or created["spec_sha256"] != spec.spec_sha256
        or created["checkpoints"] != []
        or created["active_attempt"] is not None
    ):
        raise VerificationError(f"load/create idempotency mismatch: {created}")
    # Each psql invocation reconnects, proving the row is not process memory.
    if load_or_create(container, spec, now + timedelta(minutes=2)) != created:
        raise VerificationError("reconnected load did not preserve the job")
    if domain_snapshot(container) != before:
        raise VerificationError("job creation changed trading/order domain rows")

    concurrent = job_spec(start=session.session_date + timedelta(days=2), days=1)

    def invoke_load() -> int:
        result = psql(
            container,
            jwt_claim_sql(WORKER_ID, role="service_role")
            + "select * from "
            "worker_api.load_or_create_kr_calendar_collection_job_v1("
            f"{jsonb_literal(spec_payload(concurrent), 'concurrent_spec')},"
            f"{sql_text(now.isoformat())}::timestamptz);",
            check=False,
        )
        return result.returncode

    with ThreadPoolExecutor(max_workers=2) as executor:
        create_results = list(executor.map(lambda _: invoke_load(), range(2)))
    if create_results != [0, 0]:
        raise VerificationError(
            f"concurrent idempotent create failed: {create_results}"
        )
    conflicting = KrCalendarCollectionJobSpecV1(
        job_id=concurrent.job_id,
        provider=concurrent.provider,
        market=concurrent.market,
        start_date=concurrent.start_date,
        end_date=concurrent.end_date + timedelta(days=1),
        trigger="manual",
    )
    expect_failure(
        container,
        jwt_claim_sql(WORKER_ID, role="service_role")
        + "select * from "
        "worker_api.load_or_create_kr_calendar_collection_job_v1("
        f"{jsonb_literal(spec_payload(conflicting), 'conflicting_spec')},"
        f"{sql_text(now.isoformat())}::timestamptz);",
        "kr_calendar_collection_job_spec_conflict",
    )
    maximum = job_spec(
        start=session.session_date + timedelta(days=10),
        days=366,
    )
    maximum_snapshot = load_or_create(container, maximum, now)
    if maximum_snapshot["spec"]["end_date"] != maximum.end_date.isoformat():
        raise VerificationError("maximum 366-day inclusive range was not retained")
    print(
        "PASS fresh/concurrent create, 366-day bound, exact hash and reconnect durability"
    )


def verify_concurrent_begin(container: str) -> None:
    session = calendar_fixture(day_offset=10)
    spec = job_spec(start=session.session_date, days=1)
    now = session.observed_at - timedelta(minutes=5)
    load_or_create(container, spec, now - timedelta(minutes=1))
    holder = str(uuid4())
    attempts = (str(uuid4()), str(uuid4()))

    def invoke(attempt: str) -> int:
        result = psql(
            container,
            jwt_claim_sql(WORKER_ID, role="service_role")
            + transition_call(
                "begin_kr_calendar_collection_date_attempt_v1",
                spec=spec,
                revision=1,
                attempt_id=attempt,
                holder_id=holder,
                target_date=spec.start_date,
                now=now,
            )
            + ";",
            check=False,
        )
        return result.returncode

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(invoke, attempts))
    if sorted(results) != [0, 3]:
        raise VerificationError(f"concurrent begin did not have one winner: {results}")
    snapshot = load_or_create(container, spec, now + timedelta(minutes=1))
    if snapshot["state"] != "collecting" or snapshot["revision"] != 2:
        raise VerificationError(f"concurrent begin state mismatch: {snapshot}")
    if scalar(
        container,
        "select count(*) from private.kr_calendar_collection_attempt_ledger "
        f"where job_id={sql_text(spec.job_id)}::uuid;",
    ) != "1":
        raise VerificationError("losing concurrent begin wrote an attempt row")
    stale_before = scalar(
        container,
        "select concat_ws('|',revision,state) from "
        "private.kr_calendar_collection_jobs "
        f"where job_id={sql_text(spec.job_id)}::uuid;",
    )
    expect_failure(
        container,
        jwt_claim_sql(WORKER_ID, role="service_role")
        + transition_call(
            "begin_kr_calendar_collection_date_attempt_v1",
            spec=spec,
            revision=1,
            attempt_id=str(uuid4()),
            holder_id=holder,
            target_date=spec.start_date,
            now=now + timedelta(minutes=2),
        )
        + ";",
        "kr_calendar_collection_job_revision_conflict",
    )
    stale_after = scalar(
        container,
        "select concat_ws('|',revision,state) from "
        "private.kr_calendar_collection_jobs "
        f"where job_id={sql_text(spec.job_id)}::uuid;",
    )
    if stale_after != stale_before:
        raise VerificationError("stale CAS changed the job")
    print("PASS concurrent begin one winner and stale CAS zero-change")


def verify_pause_rebegin_block(container: str) -> None:
    session = calendar_fixture(day_offset=20)
    spec = job_spec(start=session.session_date, days=1)
    created_at = session.observed_at - timedelta(minutes=10)
    load_or_create(container, spec, created_at)
    holder = str(uuid4())
    first_attempt = str(uuid4())
    collecting = begin(
        container,
        spec,
        1,
        first_attempt,
        holder,
        spec.start_date,
        created_at + timedelta(minutes=1),
    )
    paused = _snapshot(
        container,
        transition_call(
            "pause_kr_calendar_collection_date_attempt_v1",
            spec=spec,
            revision=2,
            attempt_id=first_attempt,
            holder_id=holder,
            target_date=spec.start_date,
            reason="provider_temporarily_unavailable",
            now=created_at + timedelta(minutes=2),
        ),
    )
    if (
        collecting["state"] != "collecting"
        or paused["state"] != "paused_retryable"
        or paused["revision"] != 3
        or paused["active_attempt"] is not None
        or paused["state_reason"] != "provider_temporarily_unavailable"
    ):
        raise VerificationError(f"pause transition mismatch: {paused}")
    expect_failure(
        container,
        jwt_claim_sql(WORKER_ID, role="service_role")
        + transition_call(
            "begin_kr_calendar_collection_date_attempt_v1",
            spec=spec,
            revision=3,
            attempt_id=first_attempt,
            holder_id=holder,
            target_date=spec.start_date,
            now=created_at + timedelta(minutes=3),
        )
        + ";",
        "kr_calendar_collection_job_attempt_reused",
    )
    second_attempt = str(uuid4())
    begin(
        container,
        spec,
        3,
        second_attempt,
        holder,
        spec.start_date,
        created_at + timedelta(minutes=4),
    )
    blocked = _snapshot(
        container,
        transition_call(
            "block_kr_calendar_collection_date_attempt_v1",
            spec=spec,
            revision=4,
            attempt_id=second_attempt,
            holder_id=holder,
            target_date=spec.start_date,
            reason="provider_result_unknown",
            now=created_at + timedelta(minutes=5),
        ),
    )
    if (
        blocked["state"] != "blocked_unknown"
        or blocked["revision"] != 5
        or blocked["active_attempt"] is None
        or blocked["active_attempt"]["fencing_revision"] != 4
        or blocked["automatic_retry_allowed"] is not False
    ):
        raise VerificationError(f"blocked state mismatch: {blocked}")
    expect_failure(
        container,
        jwt_claim_sql(WORKER_ID, role="service_role")
        + transition_call(
            "begin_kr_calendar_collection_date_attempt_v1",
            spec=spec,
            revision=5,
            attempt_id=str(uuid4()),
            holder_id=holder,
            target_date=spec.start_date,
            now=created_at + timedelta(minutes=6),
        )
        + ";",
        "kr_calendar_collection_job_begin_state_invalid",
    )
    print("PASS explicit pause/rebegin and blocked no-takeover semantics")


def verify_completion_manifest(container: str) -> None:
    sessions = (calendar_fixture(day_offset=30), calendar_fixture(day_offset=31))
    receipts = tuple(append_calendar(container, item) for item in sessions)
    spec = job_spec(start=sessions[0].session_date, days=2)
    created_at = sessions[0].observed_at - timedelta(minutes=10)
    load_or_create(container, spec, created_at)
    holder = str(uuid4())
    snapshot: dict[str, object] | None = None
    revision = 1
    for index, (session, raw_receipt) in enumerate(zip(sessions, receipts, strict=True)):
        attempt = str(uuid4())
        begun_at = session.observed_at - timedelta(minutes=5)
        confirm_at = session.observed_at + timedelta(minutes=5)
        begin(
            container,
            spec,
            revision,
            attempt,
            holder,
            session.session_date,
            begun_at,
        )
        revision += 1
        snapshot = _snapshot(
            container,
            transition_call(
                "confirm_kr_calendar_collection_date_v1",
                spec=spec,
                revision=revision,
                attempt_id=attempt,
                holder_id=holder,
                target_date=session.session_date,
                session=session.to_payload(),
                receipt=receipt_payload(raw_receipt),
                now=confirm_at,
            ),
        )
        revision += 1
        expected_state = "completed" if index == 1 else "ready"
        if snapshot["state"] != expected_state or snapshot["revision"] != revision:
            raise VerificationError(f"confirm transition mismatch: {snapshot}")
    if snapshot is None:
        raise VerificationError("completion snapshot missing")
    checkpoints = snapshot["checkpoints"]
    if type(checkpoints) is not list or len(checkpoints) != 2:
        raise VerificationError(f"checkpoint range is not contiguous: {snapshot}")
    expected_manifest = python_terminal_manifest(snapshot)
    if snapshot["terminal_manifest_sha256"] != expected_manifest:
        raise VerificationError(
            "terminal manifest differs from Python canonical JSON: "
            f"{snapshot['terminal_manifest_sha256']} != {expected_manifest}"
        )
    print("PASS contiguous confirmations and Python/SQL terminal manifest parity")


def verify_malformed_fail_closed(container: str) -> None:
    invalid_spec = {
        "schema_version": "kr_calendar_collection_job.v1",
        "job_id": str(uuid4()),
        "provider": "toss",
        "market": "KR",
        "start_date": "2026-08-01",
        "end_date": "2027-08-02",
        "trigger": "manual",
    }
    expect_failure(
        container,
        jwt_claim_sql(WORKER_ID, role="service_role")
        + "select * from worker_api.load_or_create_kr_calendar_collection_job_v1("
        f"{jsonb_literal(invalid_spec, 'invalid_job_spec')},"
        "'2026-08-01T00:00:00Z'::timestamptz);",
        "kr_calendar_collection_job_argument_invalid",
    )

    session = calendar_fixture(day_offset=40)
    raw_receipt = append_calendar(container, session)
    spec = job_spec(start=session.session_date, days=1)
    created_at = session.observed_at - timedelta(minutes=10)
    load_or_create(container, spec, created_at)
    holder = str(uuid4())
    attempt = str(uuid4())
    begin(
        container,
        spec,
        1,
        attempt,
        holder,
        spec.start_date,
        session.observed_at - timedelta(minutes=5),
    )
    before = scalar(
        container,
        "select concat_ws('|',job.revision,job.state,(select count(*) from "
        "private.kr_calendar_collection_attempt_ledger event where "
        "event.job_id=job.job_id)) from private.kr_calendar_collection_jobs job "
        f"where job.job_id={sql_text(spec.job_id)}::uuid;",
    )
    forged = receipt_payload(raw_receipt)
    forged["occurrence_id"] = str(uuid4())
    expect_failure(
        container,
        jwt_claim_sql(WORKER_ID, role="service_role")
        + transition_call(
            "confirm_kr_calendar_collection_date_v1",
            spec=spec,
            revision=2,
            attempt_id=attempt,
            holder_id=holder,
            target_date=spec.start_date,
            session=session.to_payload(),
            receipt=forged,
            now=session.observed_at + timedelta(minutes=5),
        )
        + ";",
        "kr_calendar_collection_job_collection_scope_mismatch",
    )
    after = scalar(
        container,
        "select concat_ws('|',job.revision,job.state,(select count(*) from "
        "private.kr_calendar_collection_attempt_ledger event where "
        "event.job_id=job.job_id)) from private.kr_calendar_collection_jobs job "
        f"where job.job_id={sql_text(spec.job_id)}::uuid;",
    )
    if after != before:
        raise VerificationError(f"malformed confirm changed state: {before} -> {after}")
    print("PASS malformed range and forged immutable lineage fail closed")


def verify_security_contract(container: str) -> None:
    catalog = scalar(
        container,
        """
select concat_ws('|',
  (select count(*) from pg_class c where c.oid in (
    'private.kr_calendar_collection_jobs'::regclass,
    'private.kr_calendar_collection_attempt_ledger'::regclass
  ) and c.relrowsecurity and c.relforcerowsecurity),
  (select count(*) from pg_policy p where p.polrelid in (
    'private.kr_calendar_collection_jobs'::regclass,
    'private.kr_calendar_collection_attempt_ledger'::regclass
  )),
  has_table_privilege('service_role','private.kr_calendar_collection_jobs','SELECT'),
  has_table_privilege('service_role','private.kr_calendar_collection_attempt_ledger','SELECT'),
  has_function_privilege(
    'service_role',
    'worker_api.load_or_create_kr_calendar_collection_job_v1(jsonb,timestamptz)',
    'EXECUTE'
  ),
  has_function_privilege(
    'service_role',
    'private.kr_calendar_collection_snapshot_v1(uuid)',
    'EXECUTE'
  ));
""",
    )
    if catalog != "2|0|f|f|t|f":
        raise VerificationError(f"RLS/ACL catalog mismatch: {catalog}")
    for role in ("anon", "authenticated", "authenticator"):
        expect_failure(
            container,
            f"set role {role}; select * from "
            "worker_api.load_or_create_kr_calendar_collection_job_v1("
            "'{}'::jsonb,clock_timestamp());",
            "permission denied",
        )
    expect_failure(
        container,
        "set role service_role; select private.kr_calendar_collection_snapshot_v1("
        "'11111111-1111-4111-8111-111111111111'::uuid);",
        "permission denied",
    )
    if scalar(
        container,
        "select count(*) from private.kr_calendar_collection_attempt_ledger;",
    ) != "0":
        expect_failure(
            container,
            "update private.kr_calendar_collection_attempt_ledger "
            "set occurred_at=occurred_at where true;",
            "append_only_table_mutation_forbidden",
        )
        expect_failure(
            container,
            "delete from private.kr_calendar_collection_attempt_ledger where true;",
            "append_only_table_mutation_forbidden",
        )
    print("PASS service-only RPC, helper/table denial, forced RLS and append-only ledger")


def job_store_counts(container: str) -> str:
    return scalar(
        container,
        "select concat_ws('|',"
        "(select count(*) from private.kr_calendar_collection_jobs),"
        "(select count(*) from private.kr_calendar_collection_attempt_ledger));",
    )


def job_state_fingerprint(container: str, job_id: str) -> str:
    return scalar(
        container,
        "select private.pit_sha256_text_v1("
        "private.kr_calendar_collection_canonical_json_v1("
        "jsonb_build_object('job',to_jsonb(job),'ledger',coalesce("
        "(select jsonb_agg(to_jsonb(event) order by event.job_revision,event.event_id) "
        "from private.kr_calendar_collection_attempt_ledger as event "
        "where event.job_id=job.job_id),'[]'::jsonb)))) "
        "from private.kr_calendar_collection_jobs as job "
        f"where job.job_id={sql_text(job_id)}::uuid;",
    )


def verify_postgrest_contract(container: str, root: str) -> None:
    service = jwt_token("service_role", WORKER_ID)
    authenticated = jwt_token("authenticated", str(uuid4()))
    session = calendar_fixture(day_offset=60)
    created_at = session.observed_at - timedelta(minutes=10)
    denied_spec = job_spec(start=session.session_date + timedelta(days=2), days=1)
    denied_body = load_rpc_payload(denied_spec, created_at)
    before_denials = job_store_counts(container)

    status, raw, _ = http_post_rpc(
        root,
        "load_or_create_kr_calendar_collection_job_v1",
        None,
        denied_body,
    )
    require_postgrest_error(
        status,
        raw,
        expected_status=401,
        expected_code="42501",
    )
    status, raw, _ = http_post_rpc(
        root,
        "load_or_create_kr_calendar_collection_job_v1",
        authenticated,
        denied_body,
    )
    require_postgrest_error(
        status,
        raw,
        expected_status=403,
        expected_code="42501",
    )
    status, raw, _ = http_post_rpc(
        root,
        "load_or_create_kr_calendar_collection_job_v1",
        service,
        denied_body,
        profile="api",
    )
    require_postgrest_error(
        status,
        raw,
        expected_status=404,
        expected_code="PGRST202",
        require_null_context=False,
    )
    if job_store_counts(container) != before_denials:
        raise VerificationError("denied PostgREST calls changed durable job state")

    domain_before = domain_snapshot(container)
    spec = job_spec(start=session.session_date, days=1)
    created, _ = postgrest_snapshot(
        root,
        "load_or_create_kr_calendar_collection_job_v1",
        service,
        load_rpc_payload(spec, created_at),
    )
    if created["state"] != "ready" or created["revision"] != 1:
        raise VerificationError(f"PostgREST load transition mismatch: {created}")
    raw_receipt = append_calendar(container, session)
    receipt = receipt_payload(raw_receipt)
    holder = str(uuid4())
    attempt = str(uuid4())
    begun_at = session.observed_at - timedelta(minutes=5)
    collecting, _ = postgrest_snapshot(
        root,
        "begin_kr_calendar_collection_date_attempt_v1",
        service,
        transition_rpc_payload(
            spec=spec,
            revision=1,
            attempt_id=attempt,
            holder_id=holder,
            target_date=spec.start_date,
            now=begun_at,
        ),
    )
    if collecting["state"] != "collecting" or collecting["revision"] != 2:
        raise VerificationError(f"PostgREST begin transition mismatch: {collecting}")
    stale_before = job_state_fingerprint(container, spec.job_id)
    stale_calls = (
        (
            "begin_kr_calendar_collection_date_attempt_v1",
            transition_rpc_payload(
                spec=spec,
                revision=1,
                attempt_id=str(uuid4()),
                holder_id=holder,
                target_date=spec.start_date,
                now=begun_at + timedelta(minutes=1),
            ),
        ),
        (
            "pause_kr_calendar_collection_date_attempt_v1",
            transition_rpc_payload(
                spec=spec,
                revision=1,
                attempt_id=attempt,
                holder_id=holder,
                target_date=spec.start_date,
                reason="provider_temporarily_unavailable",
                now=begun_at + timedelta(minutes=1),
            ),
        ),
        (
            "block_kr_calendar_collection_date_attempt_v1",
            transition_rpc_payload(
                spec=spec,
                revision=1,
                attempt_id=attempt,
                holder_id=holder,
                target_date=spec.start_date,
                reason="provider_result_unknown",
                now=begun_at + timedelta(minutes=1),
            ),
        ),
        (
            "confirm_kr_calendar_collection_date_v1",
            transition_rpc_payload(
                spec=spec,
                revision=1,
                attempt_id=attempt,
                holder_id=holder,
                target_date=spec.start_date,
                session=session.to_payload(),
                receipt=receipt,
                now=session.observed_at + timedelta(minutes=5),
            ),
        ),
    )
    for rpc, payload in stale_calls:
        status, raw, _ = http_post_rpc(root, rpc, service, payload)
        require_postgrest_error(
            status,
            raw,
            expected_status=409,
            expected_code="PT409",
            expected_message="kr_calendar_collection_job_revision_conflict",
        )
    if job_state_fingerprint(container, spec.job_id) != stale_before:
        raise VerificationError("stale PostgREST CAS changed durable job state")

    completed, _ = postgrest_snapshot(
        root,
        "confirm_kr_calendar_collection_date_v1",
        service,
        transition_rpc_payload(
            spec=spec,
            revision=2,
            attempt_id=attempt,
            holder_id=holder,
            target_date=spec.start_date,
            session=session.to_payload(),
            receipt=receipt,
            now=session.observed_at + timedelta(minutes=5),
        ),
    )
    if (
        completed["state"] != "completed"
        or completed["revision"] != 3
        or completed["terminal_manifest_sha256"]
        != python_terminal_manifest(completed)
    ):
        raise VerificationError(f"PostgREST confirm transition mismatch: {completed}")

    pause_session = calendar_fixture(day_offset=70)
    pause_spec = job_spec(start=pause_session.session_date, days=1)
    pause_created_at = pause_session.observed_at - timedelta(minutes=10)
    postgrest_snapshot(
        root,
        "load_or_create_kr_calendar_collection_job_v1",
        service,
        load_rpc_payload(pause_spec, pause_created_at),
    )
    pause_holder = str(uuid4())
    first_attempt = str(uuid4())
    postgrest_snapshot(
        root,
        "begin_kr_calendar_collection_date_attempt_v1",
        service,
        transition_rpc_payload(
            spec=pause_spec,
            revision=1,
            attempt_id=first_attempt,
            holder_id=pause_holder,
            target_date=pause_spec.start_date,
            now=pause_created_at + timedelta(minutes=1),
        ),
    )
    paused, _ = postgrest_snapshot(
        root,
        "pause_kr_calendar_collection_date_attempt_v1",
        service,
        transition_rpc_payload(
            spec=pause_spec,
            revision=2,
            attempt_id=first_attempt,
            holder_id=pause_holder,
            target_date=pause_spec.start_date,
            reason="provider_temporarily_unavailable",
            now=pause_created_at + timedelta(minutes=2),
        ),
    )
    if paused["state"] != "paused_retryable" or paused["revision"] != 3:
        raise VerificationError(f"PostgREST pause transition mismatch: {paused}")
    second_attempt = str(uuid4())
    postgrest_snapshot(
        root,
        "begin_kr_calendar_collection_date_attempt_v1",
        service,
        transition_rpc_payload(
            spec=pause_spec,
            revision=3,
            attempt_id=second_attempt,
            holder_id=pause_holder,
            target_date=pause_spec.start_date,
            now=pause_created_at + timedelta(minutes=3),
        ),
    )
    blocked, _ = postgrest_snapshot(
        root,
        "block_kr_calendar_collection_date_attempt_v1",
        service,
        transition_rpc_payload(
            spec=pause_spec,
            revision=4,
            attempt_id=second_attempt,
            holder_id=pause_holder,
            target_date=pause_spec.start_date,
            reason="provider_result_unknown",
            now=pause_created_at + timedelta(minutes=4),
        ),
    )
    if (
        blocked["state"] != "blocked_unknown"
        or blocked["revision"] != 5
        or blocked["active_attempt"] is None
        or blocked["automatic_retry_allowed"] is not False
    ):
        raise VerificationError(f"PostgREST block transition mismatch: {blocked}")
    if domain_snapshot(container) != domain_before:
        raise VerificationError("PostgREST job flows changed trading/order domain rows")
    print(
        "PASS actual PostgREST worker_api profiles, role boundary, five RPC "
        "envelopes and stale CAS safe error"
    )


def append_calendar_batch(
    container: str,
    payloads: list[dict[str, object]],
) -> list[dict[str, object]]:
    statements = [
        jwt_claim_sql(WORKER_ID, role="service_role"),
        "begin;",
        "set local lock_timeout = '5s';",
        "set local statement_timeout = '90s';",
    ]
    for index, payload in enumerate(payloads):
        statements.append(
            "select row_to_json(receipt) from "
            "worker_api.append_pit_kr_daily_session_observation_v1("
            f"{jsonb_literal(payload, f'maximum_calendar_{index}')}) as receipt;"
        )
    statements.append("commit;")
    rows = psql(container, "\n".join(statements)).stdout.strip().splitlines()
    if len(rows) < len(payloads):
        raise VerificationError("batched calendar append returned too few receipts")
    receipts: list[dict[str, object]] = []
    for raw in rows[-len(payloads) :]:
        parsed = json.loads(raw)
        if type(parsed) is not dict or set(parsed) != CALENDAR_RECEIPT_FIELDS:
            raise VerificationError("batched calendar receipt shape mismatch")
        if parsed["status"] != "stored" or parsed["quarantine_id"] is not None:
            raise VerificationError("batched calendar evidence was not stored")
        receipts.append(receipt_payload(parsed))
    return receipts


def perform_transition(call_sql: str) -> str:
    prefix = "select * from "
    if not call_sql.startswith(prefix):
        raise VerificationError("unexpected transition SQL shape")
    return "perform * from " + call_sql.removeprefix(prefix) + ";"


def verify_maximum_postgrest_snapshot(container: str, root: str) -> None:
    started_at = time.monotonic()
    sessions = [calendar_fixture(day_offset=100 + index) for index in range(366)]
    payloads = [session.to_payload() for session in sessions]
    receipts = append_calendar_batch(container, payloads)
    append_seconds = time.monotonic() - started_at
    spec = job_spec(start=sessions[0].session_date, days=366)
    created_at = sessions[0].observed_at - timedelta(minutes=10)
    load_or_create(container, spec, created_at)
    holder = str(uuid4())
    statements: list[str] = []
    revision = 1
    for session, receipt in zip(sessions, receipts, strict=True):
        attempt = str(uuid4())
        statements.append(
            perform_transition(
                transition_call(
                    "begin_kr_calendar_collection_date_attempt_v1",
                    spec=spec,
                    revision=revision,
                    attempt_id=attempt,
                    holder_id=holder,
                    target_date=session.session_date,
                    now=session.observed_at - timedelta(minutes=5),
                )
            )
        )
        revision += 1
        statements.append(
            perform_transition(
                transition_call(
                    "confirm_kr_calendar_collection_date_v1",
                    spec=spec,
                    revision=revision,
                    attempt_id=attempt,
                    holder_id=holder,
                    target_date=session.session_date,
                    session=session.to_payload(),
                    receipt=receipt,
                    now=session.observed_at + timedelta(minutes=5),
                )
            )
        )
        revision += 1
    transition_started_at = time.monotonic()
    batch_sql = (
        jwt_claim_sql(WORKER_ID, role="service_role")
        + "\nbegin;"
        + "\nset local lock_timeout = '5s';"
        + "\nset local statement_timeout = '180s';"
        + "\ndo $kr_calendar_job$\nbegin\n"
        + "\n".join(statements)
        + "\nend\n$kr_calendar_job$;"
        + "\ncommit;"
    )
    psql(container, batch_sql)
    transition_seconds = time.monotonic() - transition_started_at
    if revision != 733:
        raise VerificationError(f"maximum job revision arithmetic failed: {revision}")

    ledger_contract = scalar(
        container,
        "select concat_ws('|',count(*),"
        "count(distinct attempt_id) filter (where event_kind='begun'),"
        "count(distinct receipt_payload->>'occurrence_id') "
        "filter (where event_kind='confirmed')) "
        "from private.kr_calendar_collection_attempt_ledger "
        f"where job_id={sql_text(spec.job_id)}::uuid;",
    )
    if ledger_contract != "732|366|366":
        raise VerificationError(f"maximum job ledger mismatch: {ledger_contract}")

    service = jwt_token("service_role", WORKER_ID)
    reload_body = load_rpc_payload(
        spec,
        sessions[-1].observed_at + timedelta(minutes=6),
    )
    reload_started_at = time.monotonic()
    terminal, response_bytes = postgrest_snapshot(
        root,
        "load_or_create_kr_calendar_collection_job_v1",
        service,
        reload_body,
    )
    reload_seconds = time.monotonic() - reload_started_at
    checkpoints = terminal["checkpoints"]
    if (
        terminal["revision"] != 733
        or terminal["state"] != "completed"
        or terminal["active_attempt"] is not None
        or terminal["state_reason"] is not None
        or type(checkpoints) is not list
        or len(checkpoints) != 366
    ):
        raise VerificationError("maximum terminal snapshot state mismatch")
    attempt_ids: set[object] = set()
    occurrence_ids: set[object] = set()
    for index, checkpoint in enumerate(checkpoints):
        expected_date = spec.start_date + timedelta(days=index)
        if (
            checkpoint["target_date"] != expected_date.isoformat()
            or checkpoint["fencing_revision"] != 2 + (index * 2)
        ):
            raise VerificationError(f"maximum checkpoint {index} is not contiguous")
        attempt_ids.add(checkpoint["attempt_id"])
        occurrence_ids.add(checkpoint["receipt"]["occurrence_id"])
    if len(attempt_ids) != 366 or len(occurrence_ids) != 366:
        raise VerificationError("maximum checkpoint identities are not unique")
    expected_manifest = python_terminal_manifest(terminal)
    if terminal["terminal_manifest_sha256"] != expected_manifest:
        raise VerificationError("maximum Python/SQL terminal manifest mismatch")
    headroom_bytes = MAX_RPC_RESPONSE_BYTES - response_bytes
    if headroom_bytes < MINIMUM_RESPONSE_HEADROOM_BYTES:
        raise VerificationError(
            f"maximum response lacks 4 MiB headroom: {response_bytes} bytes"
        )

    ledger_before = scalar(
        container,
        "select count(*) from private.kr_calendar_collection_attempt_ledger "
        f"where job_id={sql_text(spec.job_id)}::uuid;",
    )
    replayed, replay_bytes = postgrest_snapshot(
        root,
        "load_or_create_kr_calendar_collection_job_v1",
        service,
        load_rpc_payload(
            spec,
            sessions[-1].observed_at + timedelta(minutes=7),
        ),
    )
    ledger_after = scalar(
        container,
        "select count(*) from private.kr_calendar_collection_attempt_ledger "
        f"where job_id={sql_text(spec.job_id)}::uuid;",
    )
    if replayed != terminal or ledger_before != ledger_after or replay_bytes != response_bytes:
        raise VerificationError("completed maximum job reload was not zero-write stable")
    print(
        "PASS 366 contiguous checkpoints, revision 733, 732 ledger rows, "
        "Python/SQL manifest parity and identity response "
        f"response_bytes={response_bytes} headroom_bytes={headroom_bytes} "
        f"append_seconds={append_seconds:.3f} "
        f"transition_seconds={transition_seconds:.3f} "
        f"reload_seconds={reload_seconds:.3f}"
    )


def verify_populated_upgrade(container: str) -> None:
    psql(container, bootstrap_sql())
    target = MIGRATIONS / MIGRATION_NAME
    migrations = sorted(MIGRATIONS.glob("*.sql"))
    try:
        target_index = migrations.index(target)
    except ValueError as exc:
        raise VerificationError("calendar collection job migration missing") from exc
    for migration in migrations[:target_index]:
        psql(container, migration.read_text(encoding="utf-8"))
    psql(container, SEED.read_text(encoding="utf-8"))

    session = calendar_fixture(day_offset=50)
    append_calendar(container, session)
    source_count = scalar(
        container,
        "select count(*) from private.pit_calendar_observation_occurrences;",
    )
    psql(container, target.read_text(encoding="utf-8"))
    spec = job_spec(start=session.session_date, days=1)
    created = load_or_create(
        container,
        spec,
        session.observed_at - timedelta(minutes=10),
    )
    reloaded = load_or_create(
        container,
        spec,
        session.observed_at - timedelta(minutes=9),
    )
    if created != reloaded or source_count == "0":
        raise VerificationError("populated upgrade did not preserve durable source/job state")
    for migration in migrations[target_index + 1 :]:
        psql(container, migration.read_text(encoding="utf-8"))
    verify_security_contract(container)
    print("PASS populated upgrade and reconnect durability")


def cleanup_disposable_resources(
    *,
    containers: tuple[str, ...],
    network: str,
) -> str | None:
    try:
        for container in containers:
            run(["docker", "rm", "-f", container], check=False)
        run(["docker", "network", "rm", network], check=False)
        container_list = run(
            ["docker", "ps", "-a", "--format", "{{.Names}}"],
            check=False,
        )
        network_list = run(
            ["docker", "network", "ls", "--format", "{{.Name}}"],
            check=False,
        )
    except OSError:
        return "disposable_resource_cleanup_command_unavailable"
    if container_list.returncode != 0 or network_list.returncode != 0:
        return "disposable_resource_cleanup_verification_failed"
    remaining_containers = set(container_list.stdout.splitlines()).intersection(containers)
    remaining_networks = set(network_list.stdout.splitlines()).intersection({network})
    if remaining_containers or remaining_networks:
        return "disposable_resource_cleanup_incomplete"
    return None


def main() -> int:
    suffix = uuid4().hex[:10]
    fresh = f"msp-kr-calendar-job-fresh-{suffix}"
    upgrade = f"msp-kr-calendar-job-upgrade-{suffix}"
    postgrest = f"msp-kr-calendar-job-rest-{suffix}"
    network = f"msp-kr-calendar-job-net-{suffix}"
    failure: str | None = None
    try:
        run(["docker", "info"])
        run(["docker", "network", "create", network])
        for container in (fresh, upgrade):
            run(
                [
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    container,
                    "--network",
                    network,
                    "-e",
                    f"POSTGRES_PASSWORD={DB_PASSWORD}",
                    POSTGRES_IMAGE,
                ]
            )
            wait_for_postgres(container)
        apply_repository(fresh)
        domain_before = domain_snapshot(fresh)
        verify_fresh(fresh)
        verify_concurrent_begin(fresh)
        verify_pause_rebegin_block(fresh)
        verify_completion_manifest(fresh)
        verify_malformed_fail_closed(fresh)
        verify_security_contract(fresh)
        postgrest_root = start_postgrest(fresh, network, postgrest)
        verify_postgrest_contract(fresh, postgrest_root)
        verify_maximum_postgrest_snapshot(fresh, postgrest_root)
        if domain_snapshot(fresh) != domain_before:
            raise VerificationError("collection jobs changed trading/order domain rows")
        print("PASS success and failure paths write zero trading/order rows")
        verify_populated_upgrade(upgrade)
    except (VerificationError, OSError, json.JSONDecodeError, ValueError) as error:
        failure = str(error)
    finally:
        cleanup_failure = cleanup_disposable_resources(
            containers=(postgrest, upgrade, fresh),
            network=network,
        )
    if cleanup_failure is not None:
        failure = cleanup_failure if failure is None else f"{failure}; {cleanup_failure}"
    if failure is not None:
        print(f"FINAL=FAIL {failure}", file=sys.stderr)
        return 1
    print("FINAL=PASS kr_calendar_collection_job_store_verifier")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
