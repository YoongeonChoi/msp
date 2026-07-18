#!/usr/bin/env python3
"""Verify the durable, manually fenced KR calendar collection job store."""

from __future__ import annotations

import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent
WORKER_ROOT = ROOT.parent / "apps" / "worker"
sys.path.insert(0, str(WORKER_ROOT))

from app.application.ports.kr_calendar_collection_job_store_port import (  # noqa: E402
    KrCalendarCollectionJobSpecV1,
)
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
    snapshot = envelope["snapshot"]
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
    if type(active_attempt) is dict:
        require_canonical_timestamp(
            active_attempt.get("begun_at"),
            "snapshot.active_attempt.begun_at",
        )
    checkpoints = snapshot["checkpoints"]
    if type(checkpoints) is list:
        for index, checkpoint in enumerate(checkpoints):
            if type(checkpoint) is not dict:
                raise VerificationError("snapshot checkpoint is not an object")
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
            require_canonical_timestamp(
                receipt.get("observed_at"),
                f"snapshot.checkpoints[{index}].receipt.observed_at",
            )
    return snapshot


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
    expanded_spec = dict(snapshot["spec"])
    expanded_spec["maximum_inclusive_days"] = 366
    expanded_spec["automatic_retry_allowed"] = False
    manifest_payload = {
        "schema_version": "kr_calendar_collection_job_manifest.v1",
        "spec": expanded_spec,
        "spec_sha256": snapshot["spec_sha256"],
        "confirmed_count": 2,
        "checkpoints": checkpoints,
    }
    expected_manifest = hashlib.sha256(
        json.dumps(
            manifest_payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
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


def main() -> int:
    suffix = uuid4().hex[:10]
    fresh = f"msp-kr-calendar-job-fresh-{suffix}"
    upgrade = f"msp-kr-calendar-job-upgrade-{suffix}"
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
        verify_fresh(fresh)
        verify_concurrent_begin(fresh)
        verify_pause_rebegin_block(fresh)
        verify_completion_manifest(fresh)
        verify_malformed_fail_closed(fresh)
        verify_security_contract(fresh)
        if domain_snapshot(fresh) != domain_before:
            raise VerificationError("collection jobs changed trading/order domain rows")
        print("PASS success and failure paths write zero trading/order rows")
        verify_populated_upgrade(upgrade)
        print("FINAL=PASS kr_calendar_collection_job_store_verifier")
        return 0
    except (VerificationError, OSError, json.JSONDecodeError, ValueError) as error:
        print(f"FINAL=FAIL {error}", file=sys.stderr)
        return 1
    finally:
        run(["docker", "rm", "-f", upgrade], check=False)
        run(["docker", "rm", "-f", fresh], check=False)


if __name__ == "__main__":
    raise SystemExit(main())
