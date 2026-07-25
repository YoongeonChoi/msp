#!/usr/bin/env python3
"""Verify the durable operations scheduler against disposable PostgreSQL 17.6.

This verifier never connects to a hosted database.  It exercises the scheduler
through its service-role RPC surface, uses direct catalog reads only for
assertions, verifies both a fresh replay and a populated upgrade, and removes
every disposable container even when an assertion fails.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parent
WORKER_ROOT = ROOT.parent / "apps" / "worker"
sys.path.insert(0, str(WORKER_ROOT))

from verify_g1_g2_migration import (
    DB_PASSWORD,
    MIGRATIONS,
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
from verify_pit_calendar_observation_store import domain_snapshot

POSTGRES_IMAGE = "postgres:17.6-alpine"
LOCK_FIXTURE_START_POLL_ATTEMPTS = 600
LOCK_FIXTURE_START_POLL_SECONDS = 0.05
MIGRATION_NAME = "20260724210000_durable_operations_scheduler.sql"
CONFLICT_FIX_MIGRATION_NAME = (
    "20260724234500_durable_scheduler_conflict_target.sql"
)
CHECKSUM_MANIFEST = ROOT / "migration-checksums.v1.json"
ACCOUNT_ID = "paper-primary"
HOLDER_ID = "51515151-5151-4515-8515-515151515151"
OTHER_HOLDER_ID = "52525252-5252-4525-8525-525252525252"
RECOVERY_HOLDER_ID = "53535353-5353-4535-8535-535353535353"
EFFECTFUL_RECOVERY_HOLDER_ID = "54545454-5454-4545-8545-545454545454"
EXHAUSTED_RECOVERY_HOLDER_ID = "55555555-5555-4555-8555-555555555556"
BLOCKED_SETTLEMENT_HOLDER_ID = "56565656-5656-4656-8656-565656565656"
MISSING_BARRIER_HOLDER_ID = "57575757-5757-4757-8757-575757575757"
DEFINITION_RACE_HOLDER_ID = "58585858-5858-4858-8858-585858585858"
RECONCILIATION_GATE_HOLDER_ID = "59595959-5959-4959-8959-595959595959"
LEASE_BUDGET_HOLDER_ID = "60606060-6060-4060-8060-606060606060"
FUTURE_OUTER_HOLDER_ID = "61616161-6161-4161-8161-616161616161"
CONCURRENT_CLAIM_HOLDER_ID = "62626262-6262-4626-8626-626262626262"
RELEASE_SHA = "d" * 40
OTHER_RELEASE_SHA = "e" * 40
JOB_KEYS = (
    "operations.commands",
    "operations.execution",
    "operations.settlement",
    "operations.reconciliation",
    "operations.outbox",
)
RPC_NAMES = (
    "ensure_scheduler_job_definition",
    "converge_scheduler_job_definition",
    "claim_due_scheduler_job",
    "complete_scheduler_job_run",
    "fail_scheduler_job_run",
    "inspect_scheduler_dead_letter",
    "replay_scheduler_dead_letter",
)
TABLE_NAMES = (
    "scheduler_job_definitions",
    "scheduler_job_runs",
    "scheduler_job_leases",
    "scheduler_replay_requests",
)
CONFLICT_PATCH_FUNCTIONS = (
    (
        "private.ensure_scheduler_job_definition_impl"
        "(text,text,bigint,text,text,text,integer,integer,integer,integer,"
        "integer,integer,boolean)"
    ),
    (
        "private.converge_scheduler_job_definition_impl"
        "(text,text,bigint,text,text,text,integer,integer,integer,integer,"
        "integer,integer,boolean)"
    ),
)
LEGACY_CONFLICT_FRAGMENT = "on conflict (account_id, job_key) do nothing"
CONSTRAINT_CONFLICT_FRAGMENT = (
    "on conflict on constraint "
    "scheduler_job_definitions_account_id_job_key_key do nothing"
)

# Static contract tests pin these names so deleting a behavioral proof cannot
# silently leave a green workflow behind.
VERIFICATION_MARKERS = frozenset(
    {
        "definition_digest_and_db_clock",
        "outer_lease_binding",
        "commands_priority",
        "forced_startup_command_drain",
        "execution_command_barrier",
        "reconciliation_execution_gate",
        "expired_reconciliation_cleanup_priority",
        "settlement_blocked_execution_gate",
        "effectful_block_recovery_progress",
        "expired_execution_cleanup_without_barrier",
        "concurrent_single_claim",
        "concurrent_definition_idempotency",
        "inner_lease_bounded_by_outer",
        "minimum_scheduler_lease_ttl",
        "outer_lease_remaining_budget",
        "future_outer_lease_not_yet_valid",
        "completion_exact_revision_and_idempotency",
        "job_specific_retry_matrix",
        "effectful_expiry_requires_resolution_evidence",
        "expired_lease_retry_budget",
        "restart_persistence_and_stale_takeover",
        "lock_wait_clock_revalidation",
        "manual_replay_compare_and_swap",
        "takeover_replay_receipt_recovery",
        "rolling_upgrade_definition_convergence",
        "recovery_only_no_new_cadence",
        "rls_acl_search_path_catalog",
        "single_trusted_owner_catalog",
        "zero_trading_order_side_effects",
        "populated_upgrade",
        "conflict_target_drift_rollback",
        "disposable_container_cleanup",
    }
)

DEFINITION_RECEIPT_FIELDS = {
    "definition_id",
    "account_id",
    "job_key",
    "definition_sha256",
    "revision",
    "next_due_at",
    "observed_at",
}
CLAIM_RECEIPT_FIELDS = {"claimed", "claim", "observed_at"}
CLAIM_FIELDS = {"definition", "run", "lease"}
DEFINITION_FIELDS = {
    "schema_version",
    "job_key",
    "interval_seconds",
    "lease_ttl_seconds",
    "max_attempts",
    "retry_base_seconds",
    "retry_max_seconds",
    "max_manual_replays",
    "enabled",
    "definition_sha256",
}
CONVERGENCE_DEFINITION_FIELDS = DEFINITION_FIELDS | {
    "definition_id",
    "account_id",
    "revision",
    "next_due_at",
    "scheduler_state",
}
RUN_FIELDS = {
    "run_id",
    "account_id",
    "job_key",
    "definition_sha256",
    "state",
    "revision",
    "attempt_count",
    "replay_generation",
    "replay_of_run_id",
    "scheduled_for",
    "available_at",
    "created_at",
    "updated_at",
}
LEASE_FIELDS = {
    "lease_token",
    "run_id",
    "account_id",
    "holder_id",
    "release_sha",
    "outer_fencing_token",
    "attempt_number",
    "run_revision",
    "leased_at",
    "lease_expires_at",
}
SETTLEMENT_FIELDS = {
    "run_id",
    "state",
    "run_revision",
    "attempt_count",
    "next_attempt_at",
    "failure_reason_code",
    "result_sha256",
    "observed_at",
}
INSPECTION_FIELDS = {
    "found",
    "dead_letter",
    "eligible",
    "ineligibility_reason",
    "observed_at",
}
DEAD_LETTER_FIELDS = {
    "source_run_id",
    "account_id",
    "job_key",
    "definition_sha256",
    "source_revision",
    "attempt_count",
    "failure_reason_code",
    "failure_sha256",
    "replay_generation",
    "max_manual_replays",
    "dead_lettered_at",
    "state",
}
REPLAY_FIELDS = {
    "source_run_id",
    "new_run_id",
    "replay_request_id",
    "job_key",
    "definition_sha256",
    "source_revision",
    "failure_reason_code",
    "replay_generation",
    "state",
    "created_at",
    "observed_at",
    "idempotent",
}
CONVERGENCE_FIELDS = {
    "status",
    "definition",
    "claim",
    "active_run_id",
    "next_eligible_at",
    "reason_code",
    "observed_at",
}
CONVERGENCE_STATUSES = {"converged", "claimed", "wait", "manual_resolution"}


def sql_text(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def scalar(container: str, query: str) -> str:
    return psql(container, query).stdout.strip()


def _last_json(container: str, query: str) -> dict[str, Any]:
    rows = psql(container, query).stdout.strip().splitlines()
    if not rows:
        raise VerificationError("RPC returned no row")
    value = json.loads(rows[-1])
    if type(value) is not dict:
        raise VerificationError(f"RPC row must be an object: {value!r}")
    return value


def _service_rpc(container: str, holder_id: str, call_sql: str) -> dict[str, Any]:
    return _last_json(
        container,
        jwt_claim_sql(holder_id, role="service_role")
        + f"\nselect row_to_json(result) from {call_sql} as result;",
    )


def _service_failure(
    container: str,
    holder_id: str,
    call_sql: str,
    *fragments: str,
) -> None:
    expect_failure(
        container,
        jwt_claim_sql(holder_id, role="service_role")
        + f"\nselect * from {call_sql};",
        *fragments,
    )


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise VerificationError(f"timestamp string required: {value!r}")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise VerificationError(f"timezone-aware timestamp required: {value!r}")
    return parsed.astimezone(UTC)


def database_now(container: str) -> datetime:
    return _timestamp(scalar(container, "select pg_catalog.clock_timestamp();"))


def _assert_database_clock(
    value: object,
    before: datetime,
    after: datetime,
    label: str,
) -> None:
    observed = _timestamp(value)
    if not before <= observed <= after:
        raise VerificationError(
            f"{label} is not bounded by the database clock: "
            f"before={before.isoformat()}, observed={observed.isoformat()}, "
            f"after={after.isoformat()}"
        )


def definition_spec(
    job_key: str,
    *,
    enabled: bool = True,
    interval_seconds: int = 1,
    lease_ttl_seconds: int = 30,
    max_attempts: int = 2,
    retry_base_seconds: int = 1,
    retry_max_seconds: int | None = None,
    max_manual_replays: int = 1,
) -> dict[str, object]:
    return {
        "schema_version": "durable_scheduler_job_definition.v1",
        "job_key": job_key,
        "interval_seconds": interval_seconds,
        "lease_ttl_seconds": lease_ttl_seconds,
        "max_attempts": max_attempts,
        "retry_base_seconds": retry_base_seconds,
        "retry_max_seconds": (
            retry_base_seconds if retry_max_seconds is None else retry_max_seconds
        ),
        "max_manual_replays": max_manual_replays,
        "enabled": enabled,
    }


def definition_digest(spec: dict[str, object]) -> str:
    canonical = json.dumps(
        spec,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _ensure_sql(
    spec: dict[str, object],
    digest: str | None = None,
    *,
    account_id: str = ACCOUNT_ID,
    holder_id: str = HOLDER_ID,
    release_sha: str = RELEASE_SHA,
) -> str:
    return (
        "worker_api.ensure_scheduler_job_definition("
        f"{sql_text(account_id)},{sql_text(holder_id)},{{outer_token}},"
        f"{sql_text(release_sha)},{sql_text(str(spec['job_key']))},"
        f"{sql_text(digest or definition_digest(spec))},"
        f"{int(spec['interval_seconds'])},{int(spec['lease_ttl_seconds'])},"
        f"{int(spec['max_attempts'])},{int(spec['retry_base_seconds'])},"
        f"{int(spec['retry_max_seconds'])},{int(spec['max_manual_replays'])},"
        f"{str(bool(spec['enabled'])).lower()})"
    )


def ensure_definition(
    container: str,
    outer_token: int,
    spec: dict[str, object],
    *,
    account_id: str = ACCOUNT_ID,
    holder_id: str = HOLDER_ID,
    release_sha: str = RELEASE_SHA,
) -> dict[str, Any]:
    before = database_now(container)
    receipt = _service_rpc(
        container,
        holder_id,
        _ensure_sql(
            spec,
            account_id=account_id,
            holder_id=holder_id,
            release_sha=release_sha,
        ).format(outer_token=outer_token),
    )
    after = database_now(container)
    if set(receipt) != DEFINITION_RECEIPT_FIELDS:
        raise VerificationError(f"definition receipt shape mismatch: {receipt}")
    if (
        receipt["account_id"] != account_id
        or receipt["job_key"] != spec["job_key"]
        or receipt["definition_sha256"] != definition_digest(spec)
    ):
        raise VerificationError(f"definition receipt binding mismatch: {receipt}")
    _assert_database_clock(receipt["observed_at"], before, after, "definition observed_at")
    return receipt


def _converge_sql(
    spec: dict[str, object],
    outer_token: int,
    *,
    account_id: str = ACCOUNT_ID,
    holder_id: str = HOLDER_ID,
    release_sha: str = RELEASE_SHA,
) -> str:
    return (
        "worker_api.converge_scheduler_job_definition("
        f"{sql_text(account_id)},{sql_text(holder_id)},{outer_token},"
        f"{sql_text(release_sha)},{sql_text(str(spec['job_key']))},"
        f"{sql_text(definition_digest(spec))},"
        f"{int(spec['interval_seconds'])},{int(spec['lease_ttl_seconds'])},"
        f"{int(spec['max_attempts'])},{int(spec['retry_base_seconds'])},"
        f"{int(spec['retry_max_seconds'])},{int(spec['max_manual_replays'])},"
        f"{str(bool(spec['enabled'])).lower()})"
    )


def converge_definition(
    container: str,
    outer_token: int,
    spec: dict[str, object],
    *,
    account_id: str = ACCOUNT_ID,
    holder_id: str = HOLDER_ID,
    release_sha: str = RELEASE_SHA,
) -> dict[str, Any]:
    before = database_now(container)
    receipt = _service_rpc(
        container,
        holder_id,
        _converge_sql(
            spec,
            outer_token,
            account_id=account_id,
            holder_id=holder_id,
            release_sha=release_sha,
        ),
    )
    after = database_now(container)
    if set(receipt) != CONVERGENCE_FIELDS:
        raise VerificationError(f"convergence receipt shape mismatch: {receipt}")
    if receipt.get("status") not in CONVERGENCE_STATUSES:
        raise VerificationError(f"convergence status mismatch: {receipt}")
    definition = receipt.get("definition")
    if (
        type(definition) is not dict
        or set(definition) != CONVERGENCE_DEFINITION_FIELDS
        or definition.get("account_id") != account_id
        or definition.get("job_key") != spec["job_key"]
    ):
        raise VerificationError(f"convergence definition shape mismatch: {receipt}")
    claim = receipt.get("claim")
    if claim is not None:
        if type(claim) is not dict or set(claim) != CLAIM_FIELDS:
            raise VerificationError(f"convergence claim shape mismatch: {receipt}")
        validate_claim(
            {
                "claimed": True,
                "claim": claim,
                "observed_at": receipt["observed_at"],
            },
            account_id=account_id,
        )
    reason_code = receipt.get("reason_code")
    if reason_code is not None and (
        not isinstance(reason_code, str)
        or re.fullmatch(r"[a-z][a-z0-9_]{0,127}", reason_code) is None
    ):
        raise VerificationError(f"convergence reason code mismatch: {receipt}")
    _assert_database_clock(
        receipt["observed_at"],
        before,
        after,
        "convergence observed_at",
    )
    return receipt


def acquire_outer_lease(
    container: str,
    *,
    account_id: str = ACCOUNT_ID,
    holder_id: str = HOLDER_ID,
    release_sha: str = RELEASE_SHA,
    ttl_seconds: int = 300,
) -> dict[str, Any]:
    receipt = _service_rpc(
        container,
        holder_id,
        "worker_api.acquire_worker_lease("
        f"{sql_text(account_id)},{sql_text(holder_id)},"
        f"pg_catalog.clock_timestamp(),{ttl_seconds},{sql_text(release_sha)})",
    )
    required = {"account_id", "holder_id", "fencing_token", "acquired_at", "expires_at"}
    if set(receipt) != required or receipt["account_id"] != account_id:
        raise VerificationError(f"outer lease receipt mismatch: {receipt}")
    if receipt["holder_id"] != holder_id or int(receipt["fencing_token"]) <= 0:
        raise VerificationError(f"outer lease identity mismatch: {receipt}")
    return receipt


def _claim_sql(
    outer_token: int,
    *,
    account_id: str = ACCOUNT_ID,
    holder_id: str = HOLDER_ID,
    release_sha: str = RELEASE_SHA,
) -> str:
    return (
        "worker_api.claim_due_scheduler_job("
        f"{sql_text(account_id)},{sql_text(holder_id)},{outer_token},"
        f"{sql_text(release_sha)})"
    )


def validate_claim(
    value: dict[str, Any],
    *,
    account_id: str = ACCOUNT_ID,
) -> dict[str, Any] | None:
    if set(value) != CLAIM_RECEIPT_FIELDS or type(value.get("claimed")) is not bool:
        raise VerificationError(f"claim receipt shape mismatch: {value}")
    claim = value.get("claim")
    if value["claimed"] is False:
        if claim is not None:
            raise VerificationError(f"unclaimed receipt exposed claim: {value}")
        return None
    if type(claim) is not dict or set(claim) != CLAIM_FIELDS:
        raise VerificationError(f"claim envelope shape mismatch: {claim}")
    definition = claim.get("definition")
    run_value = claim.get("run")
    lease = claim.get("lease")
    if type(definition) is not dict or set(definition) != DEFINITION_FIELDS:
        raise VerificationError(f"claim definition shape mismatch: {definition}")
    if type(run_value) is not dict or set(run_value) != RUN_FIELDS:
        raise VerificationError(f"claim run shape mismatch: {run_value}")
    if type(lease) is not dict or set(lease) != LEASE_FIELDS:
        raise VerificationError(f"claim lease shape mismatch: {lease}")
    if (
        run_value["run_id"] != lease["run_id"]
        or run_value["account_id"] != account_id
        or lease["account_id"] != account_id
        or run_value["job_key"] != definition["job_key"]
        or run_value["definition_sha256"] != definition["definition_sha256"]
        or run_value["revision"] != lease["run_revision"]
        or run_value["attempt_count"] != lease["attempt_number"]
    ):
        raise VerificationError(f"claim cross-binding mismatch: {claim}")
    return claim


def claim_due(
    container: str,
    outer_token: int,
    *,
    account_id: str = ACCOUNT_ID,
    holder_id: str = HOLDER_ID,
    release_sha: str = RELEASE_SHA,
) -> dict[str, Any]:
    before = database_now(container)
    receipt = _service_rpc(
        container,
        holder_id,
        _claim_sql(
            outer_token,
            account_id=account_id,
            holder_id=holder_id,
            release_sha=release_sha,
        ),
    )
    after = database_now(container)
    validate_claim(receipt, account_id=account_id)
    _assert_database_clock(receipt["observed_at"], before, after, "claim observed_at")
    return receipt


def _complete_sql(
    outer_token: int,
    claim: dict[str, Any],
    result_sha256: str,
    *,
    expected_revision: int | None = None,
    lease_token: str | None = None,
    account_id: str = ACCOUNT_ID,
    holder_id: str = HOLDER_ID,
    release_sha: str = RELEASE_SHA,
) -> str:
    run_value = claim["run"]
    lease = claim["lease"]
    return (
        "worker_api.complete_scheduler_job_run("
        f"{sql_text(account_id)},{sql_text(holder_id)},{outer_token},"
        f"{sql_text(release_sha)},{sql_text(str(run_value['run_id']))}::uuid,"
        f"{expected_revision if expected_revision is not None else int(run_value['revision'])},"
        f"{sql_text(str(run_value['definition_sha256']))},"
        f"{sql_text(lease_token or str(lease['lease_token']))}::uuid,"
        f"{sql_text(result_sha256)})"
    )


def complete_run(
    container: str,
    outer_token: int,
    claim: dict[str, Any],
    result_sha256: str,
    *,
    account_id: str = ACCOUNT_ID,
    holder_id: str = HOLDER_ID,
    release_sha: str = RELEASE_SHA,
) -> dict[str, Any]:
    receipt = _service_rpc(
        container,
        holder_id,
        _complete_sql(
            outer_token,
            claim,
            result_sha256,
            account_id=account_id,
            holder_id=holder_id,
            release_sha=release_sha,
        ),
    )
    if set(receipt) != SETTLEMENT_FIELDS:
        raise VerificationError(f"completion receipt shape mismatch: {receipt}")
    if (
        receipt["run_id"] != claim["run"]["run_id"]
        or receipt["state"] != "succeeded"
        or receipt["run_revision"] != int(claim["run"]["revision"]) + 1
        or receipt["attempt_count"] != claim["run"]["attempt_count"]
        or receipt["next_attempt_at"] is not None
        or receipt["failure_reason_code"] is not None
        or receipt["result_sha256"] != result_sha256
    ):
        raise VerificationError(f"completion settlement mismatch: {receipt}")
    return receipt


def _fail_sql(
    outer_token: int,
    claim: dict[str, Any],
    reason_code: str,
    failure_sha256: str,
    retryable: bool,
    *,
    account_id: str = ACCOUNT_ID,
    holder_id: str = HOLDER_ID,
    release_sha: str = RELEASE_SHA,
) -> str:
    run_value = claim["run"]
    lease = claim["lease"]
    return (
        "worker_api.fail_scheduler_job_run("
        f"{sql_text(account_id)},{sql_text(holder_id)},{outer_token},"
        f"{sql_text(release_sha)},{sql_text(str(run_value['run_id']))}::uuid,"
        f"{int(run_value['revision'])},{sql_text(str(run_value['definition_sha256']))},"
        f"{sql_text(str(lease['lease_token']))}::uuid,{sql_text(reason_code)},"
        f"{sql_text(failure_sha256)},{str(retryable).lower()})"
    )


def fail_run(
    container: str,
    outer_token: int,
    claim: dict[str, Any],
    reason_code: str,
    failure_sha256: str,
    retryable: bool,
    *,
    account_id: str = ACCOUNT_ID,
    holder_id: str = HOLDER_ID,
    release_sha: str = RELEASE_SHA,
) -> dict[str, Any]:
    receipt = _service_rpc(
        container,
        holder_id,
        _fail_sql(
            outer_token,
            claim,
            reason_code,
            failure_sha256,
            retryable,
            account_id=account_id,
            holder_id=holder_id,
            release_sha=release_sha,
        ),
    )
    if set(receipt) != SETTLEMENT_FIELDS:
        raise VerificationError(f"failure receipt shape mismatch: {receipt}")
    if (
        receipt["run_id"] != claim["run"]["run_id"]
        or receipt["run_revision"] != int(claim["run"]["revision"]) + 1
        or receipt["attempt_count"] != claim["run"]["attempt_count"]
        or receipt["failure_reason_code"] != reason_code
        or receipt["result_sha256"] != failure_sha256
    ):
        raise VerificationError(f"failure settlement mismatch: {receipt}")
    return receipt


def _inspect_sql(
    outer_token: int,
    run_id: str,
    *,
    account_id: str = ACCOUNT_ID,
    holder_id: str = HOLDER_ID,
    release_sha: str = RELEASE_SHA,
) -> str:
    return (
        "worker_api.inspect_scheduler_dead_letter("
        f"{sql_text(account_id)},{sql_text(holder_id)},{outer_token},"
        f"{sql_text(release_sha)},{sql_text(run_id)}::uuid)"
    )


def inspect_dead_letter(
    container: str,
    outer_token: int,
    run_id: str,
    *,
    account_id: str = ACCOUNT_ID,
    holder_id: str = HOLDER_ID,
    release_sha: str = RELEASE_SHA,
) -> dict[str, Any]:
    receipt = _service_rpc(
        container,
        holder_id,
        _inspect_sql(
            outer_token,
            run_id,
            account_id=account_id,
            holder_id=holder_id,
            release_sha=release_sha,
        ),
    )
    if set(receipt) != INSPECTION_FIELDS:
        raise VerificationError(f"inspection receipt shape mismatch: {receipt}")
    dead_letter = receipt.get("dead_letter")
    if receipt.get("found") is True:
        if type(dead_letter) is not dict or set(dead_letter) != DEAD_LETTER_FIELDS:
            raise VerificationError(f"dead-letter shape mismatch: {dead_letter}")
    elif dead_letter is not None:
        raise VerificationError(f"missing inspection exposed dead letter: {receipt}")
    return receipt


def _replay_sql(
    outer_token: int,
    dead_letter: dict[str, Any],
    replay_request_id: str,
    *,
    expected_revision: int | None = None,
    definition_sha256: str | None = None,
    failure_reason_code: str | None = None,
    failure_sha256: str | None = None,
    replay_generation: int | None = None,
    confirmed_reason_code: str | None = None,
    explicit_confirmation: bool = True,
    account_id: str = ACCOUNT_ID,
    holder_id: str = HOLDER_ID,
    release_sha: str = RELEASE_SHA,
) -> str:
    reason = failure_reason_code or str(dead_letter["failure_reason_code"])
    return (
        "worker_api.replay_scheduler_dead_letter("
        f"{sql_text(account_id)},{sql_text(holder_id)},{outer_token},"
        f"{sql_text(release_sha)},"
        f"{sql_text(str(dead_letter['source_run_id']))}::uuid,"
        f"{expected_revision if expected_revision is not None else int(dead_letter['source_revision'])},"
        f"{sql_text(definition_sha256 or str(dead_letter['definition_sha256']))},"
        f"{sql_text(reason)},"
        f"{sql_text(failure_sha256 or str(dead_letter['failure_sha256']))},"
        f"{replay_generation if replay_generation is not None else int(dead_letter['replay_generation'])},"
        f"{sql_text(replay_request_id)}::uuid,"
        f"{sql_text(confirmed_reason_code or reason)},"
        f"{str(explicit_confirmation).lower()})"
    )


def replay_dead_letter(
    container: str,
    outer_token: int,
    dead_letter: dict[str, Any],
    replay_request_id: str,
    *,
    account_id: str = ACCOUNT_ID,
    holder_id: str = HOLDER_ID,
    release_sha: str = RELEASE_SHA,
) -> dict[str, Any]:
    receipt = _service_rpc(
        container,
        holder_id,
        _replay_sql(
            outer_token,
            dead_letter,
            replay_request_id,
            account_id=account_id,
            holder_id=holder_id,
            release_sha=release_sha,
        ),
    )
    if set(receipt) != REPLAY_FIELDS:
        raise VerificationError(f"replay receipt shape mismatch: {receipt}")
    return receipt


def run_snapshot(container: str, run_id: str) -> dict[str, Any]:
    return _last_json(
        container,
        "select to_jsonb(run) from private.scheduler_job_runs as run "
        f"where run.run_id={sql_text(run_id)}::uuid;",
    )


def wait_for_run_deadline(container: str, run_id: str, column: str) -> None:
    if column not in {"lease_expires_at", "available_at"}:
        raise VerificationError(f"unsupported scheduler deadline: {column}")
    psql(
        container,
        "select pg_catalog.pg_sleep(least(greatest(extract(epoch from ("
        f"{column} - pg_catalog.clock_timestamp())),0)+0.25,40)) "
        "from private.scheduler_job_runs "
        f"where run_id={sql_text(run_id)}::uuid;",
    )


def verify_checksum_wiring() -> None:
    try:
        manifest = json.loads(CHECKSUM_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise VerificationError("durable scheduler checksum is not wired") from error
    for migration_name in (MIGRATION_NAME, CONFLICT_FIX_MIGRATION_NAME):
        target = MIGRATIONS / migration_name
        if not target.is_file() or target.is_symlink():
            raise VerificationError(
                f"durable scheduler migration must be a regular file: {migration_name}"
            )
        try:
            expected = manifest["migrations"][migration_name]
        except (KeyError, TypeError) as error:
            raise VerificationError(
                f"durable scheduler checksum is not wired: {migration_name}"
            ) from error
        canonical = target.read_text(encoding="utf-8").encode("utf-8")
        actual = hashlib.sha256(canonical).hexdigest()
        if expected != actual:
            raise VerificationError(
                "durable scheduler checksum mismatch: "
                f"migration={migration_name}, expected={expected!r}, actual={actual}"
            )
    print("PASS durable scheduler migration and conflict fix checksum wiring")


def open_scheduler_account_fixtures(container: str) -> None:
    psql(
        container,
        "insert into private.trading_accounts("
        "account_id,environment,broker,state,opening_capital_krw,opened_at) values "
        "('scheduler-recovery-safe','contract_test','local_contract_simulator',"
        "'open',0,pg_catalog.clock_timestamp()),"
        "('scheduler-recovery-effectful','contract_test','local_contract_simulator',"
        "'open',0,pg_catalog.clock_timestamp()),"
        "('scheduler-recovery-exhausted','contract_test','local_contract_simulator',"
        "'open',0,pg_catalog.clock_timestamp()),"
        "('scheduler-settlement-blocked','contract_test','local_contract_simulator',"
        "'open',0,pg_catalog.clock_timestamp()),"
        "('scheduler-expired-execution-missing','contract_test',"
        "'local_contract_simulator','open',0,pg_catalog.clock_timestamp()) "
        "on conflict (account_id) do nothing;"
        "insert into private.trading_accounts("
        "account_id,environment,broker,state,opening_capital_krw,opened_at) values "
        "('scheduler-definition-race','contract_test','local_contract_simulator',"
        "'open',0,pg_catalog.clock_timestamp()) "
        "on conflict (account_id) do nothing;"
        "insert into private.trading_accounts("
        "account_id,environment,broker,state,opening_capital_krw,opened_at) values "
        "('scheduler-reconciliation-gate','contract_test',"
        "'local_contract_simulator','open',0,pg_catalog.clock_timestamp()) "
        "on conflict (account_id) do nothing;"
        "insert into private.trading_accounts("
        "account_id,environment,broker,state,opening_capital_krw,opened_at) values "
        "('scheduler-lease-budget','contract_test','local_contract_simulator',"
        "'open',0,pg_catalog.clock_timestamp()),"
        "('scheduler-future-outer','contract_test','local_contract_simulator',"
        "'open',0,pg_catalog.clock_timestamp()),"
        "('scheduler-concurrent-claim','contract_test','local_contract_simulator',"
        "'open',0,pg_catalog.clock_timestamp()) "
        "on conflict (account_id) do nothing;"
        "update private.trading_accounts "
        "set state='open',opened_at=coalesce(opened_at,pg_catalog.clock_timestamp()),"
        "closed_at=null where account_id in "
        "('paper-primary','contract-test-primary');",
    )
    opened = scalar(
        container,
        "select count(*) from private.trading_accounts where account_id in "
        "('paper-primary','contract-test-primary','scheduler-recovery-safe',"
        "'scheduler-recovery-effectful','scheduler-recovery-exhausted',"
        "'scheduler-settlement-blocked','scheduler-expired-execution-missing',"
        "'scheduler-definition-race','scheduler-reconciliation-gate',"
        "'scheduler-lease-budget','scheduler-future-outer',"
        "'scheduler-concurrent-claim') "
        "and state='open' "
        "and opened_at is not null and closed_at is null;",
    )
    if opened != "12":
        raise VerificationError(f"scheduler account fixture mismatch: {opened}")


def verify_definition_digest_and_db_clock(
    container: str,
    outer: dict[str, Any],
) -> tuple[dict[str, dict[str, object]], dict[str, Any]]:
    outer_token = int(outer["fencing_token"])
    specs = {
        "operations.commands": definition_spec(
            "operations.commands",
            interval_seconds=300,
            lease_ttl_seconds=10,
            retry_base_seconds=1,
        ),
        "operations.execution": definition_spec(
            "operations.execution",
            lease_ttl_seconds=10,
        ),
        "operations.settlement": definition_spec(
            "operations.settlement",
            lease_ttl_seconds=10,
        ),
        "operations.reconciliation": definition_spec(
            "operations.reconciliation",
            retry_base_seconds=10,
            retry_max_seconds=10,
        ),
        "operations.outbox": definition_spec(
            "operations.outbox",
            retry_base_seconds=10,
            retry_max_seconds=10,
        ),
    }
    command_before = database_now(container)
    command_receipt = ensure_definition(
        container,
        outer_token,
        specs["operations.commands"],
    )
    command_after = database_now(container)
    receipts = {"operations.commands": command_receipt}
    for job_key in JOB_KEYS[1:]:
        receipts[job_key] = ensure_definition(container, outer_token, specs[job_key])

    next_due_at = _timestamp(command_receipt["next_due_at"])
    observed_at = _timestamp(command_receipt["observed_at"])
    if not command_before <= next_due_at <= observed_at <= command_after:
        raise VerificationError(
            "new definition clock ordering mismatch: "
            f"before={command_before.isoformat()}, "
            f"next_due_at={next_due_at.isoformat()}, "
            f"observed_at={observed_at.isoformat()}, "
            f"after={command_after.isoformat()}"
        )
    stored_clock = scalar(
        container,
        "select concat_ws('|',next_due_at=created_at,next_due_at=updated_at,"
        f"next_due_at={sql_text(str(command_receipt['next_due_at']))}::timestamptz) "
        "from private.scheduler_job_definitions "
        f"where definition_id={sql_text(str(command_receipt['definition_id']))}::uuid;",
    )
    if stored_clock != "t|t|t":
        raise VerificationError(
            f"new definition stored clock provenance mismatch: {stored_clock}"
        )
    repeated = ensure_definition(container, outer_token, specs["operations.commands"])
    stable_fields = DEFINITION_RECEIPT_FIELDS - {"observed_at"}
    if any(repeated[field] != command_receipt[field] for field in stable_fields):
        raise VerificationError(
            f"idempotent definition ensure changed durable state: {repeated}"
        )
    _service_failure(
        container,
        HOLDER_ID,
        _ensure_sql(specs["operations.commands"], "0" * 64).format(
            outer_token=outer_token
        ),
        "scheduler_definition_digest_mismatch",
    )

    first_receipt = claim_due(container, outer_token)
    first_claim = validate_claim(first_receipt)
    if first_claim is None or first_claim["run"]["job_key"] != "operations.commands":
        raise VerificationError(f"commands were not the first due job: {first_receipt}")
    if _timestamp(first_claim["lease"]["lease_expires_at"]) > _timestamp(
        outer["expires_at"]
    ):
        raise VerificationError("inner scheduler lease exceeded the outer worker lease")
    if first_claim["lease"]["outer_fencing_token"] != outer_token:
        raise VerificationError(f"inner lease lost outer fencing token: {first_claim}")

    # Leave only the currently leased command definition enabled.  This makes
    # concurrent probes a direct duplicate-claim test rather than a test of the
    # scheduler's ability to hand out other definitions.
    for job_key in JOB_KEYS[1:]:
        disabled = dict(specs[job_key])
        disabled["enabled"] = False
        ensure_definition(container, outer_token, disabled)
        specs[job_key] = disabled

    def concurrent_probe() -> dict[str, Any]:
        receipt = _service_rpc(container, HOLDER_ID, _claim_sql(outer_token))
        validate_claim(receipt)
        return receipt

    with ThreadPoolExecutor(max_workers=4) as pool:
        probes = list(pool.map(lambda _: concurrent_probe(), range(4)))
    if any(probe["claimed"] for probe in probes):
        raise VerificationError(f"active command run was claimed twice: {probes}")
    active_count = scalar(
        container,
        "select count(*) from private.scheduler_job_runs "
        "where state in ('pending','leased','retry_wait');",
    )
    if active_count != "1":
        raise VerificationError(f"single-active-run invariant mismatch: {active_count}")

    print(
        "PASS definition_digest_and_db_clock, commands_priority, "
        "outer_lease_binding, inner_lease_bounded_by_outer, duplicate prevention"
    )
    return specs, first_claim


def _scheduler_run_count(container: str, account_id: str, job_key: str) -> int:
    return int(
        scalar(
            container,
            "select count(*) from private.scheduler_job_runs where account_id="
            f"{sql_text(account_id)} and job_key={sql_text(job_key)};",
        )
    )


def _insert_expired_execution_fixture(
    container: str,
    outer_token: int,
    spec: dict[str, object],
    *,
    account_id: str,
    holder_id: str,
) -> str:
    if spec["job_key"] != "operations.execution" or spec["enabled"] is not True:
        raise VerificationError("expired execution fixture requires an enabled definition")
    run_id = str(uuid4())
    lease_token = str(uuid4())
    digest = definition_digest(spec)
    psql(
        container,
        "insert into private.scheduler_job_runs("
        "run_id,definition_id,account_id,job_key,definition_sha256,state,revision,"
        "attempt_count,lease_generation,replay_generation,scheduled_for,available_at,"
        "lease_token,lease_holder_id,lease_release_sha,outer_fencing_token,leased_at,"
        "lease_expires_at,created_at,updated_at) "
        f"select {sql_text(run_id)}::uuid,definition_id,{sql_text(account_id)},"
        f"'operations.execution',{sql_text(digest)},'leased',2,1,1,0,"
        "pg_catalog.clock_timestamp()-interval '3 seconds',"
        "pg_catalog.clock_timestamp()-interval '3 seconds',"
        f"{sql_text(lease_token)}::uuid,{sql_text(holder_id)},{sql_text(RELEASE_SHA)},"
        f"{outer_token},pg_catalog.clock_timestamp()-interval '2 seconds',"
        "pg_catalog.clock_timestamp()-interval '1 second',"
        "pg_catalog.clock_timestamp()-interval '3 seconds',"
        "pg_catalog.clock_timestamp()-interval '2 seconds' "
        "from private.scheduler_job_definitions "
        f"where account_id={sql_text(account_id)} and job_key='operations.execution' "
        f"and definition_sha256={sql_text(digest)};"
        "update private.scheduler_job_definitions "
        f"set latest_run_id={sql_text(run_id)}::uuid,revision=revision+1,"
        "updated_at=pg_catalog.clock_timestamp() "
        f"where account_id={sql_text(account_id)} and job_key='operations.execution';"
        "insert into private.scheduler_job_leases("
        "lease_token,run_id,definition_id,account_id,holder_id,release_sha,"
        "outer_fencing_token,attempt_number,lease_generation,run_revision,leased_at,"
        "lease_expires_at) "
        "select lease_token,run_id,definition_id,account_id,lease_holder_id,"
        "lease_release_sha,outer_fencing_token,attempt_count,lease_generation,revision,"
        "leased_at,lease_expires_at from private.scheduler_job_runs "
        f"where run_id={sql_text(run_id)}::uuid;",
    )
    if _scheduler_run_count(container, account_id, "operations.execution") != 1:
        raise VerificationError("expired execution fixture insertion failed")
    return run_id


def _require_convergence_status(
    receipt: dict[str, Any],
    expected_status: str,
) -> None:
    if receipt["status"] != expected_status:
        raise VerificationError(
            f"expected convergence status {expected_status}: {receipt}"
        )
    claim = receipt["claim"]
    active_run_id = receipt["active_run_id"]
    next_eligible_at = receipt["next_eligible_at"]
    reason_code = receipt["reason_code"]
    if expected_status == "converged":
        if (
            claim is not None
            or active_run_id is not None
            or next_eligible_at is not None
            or reason_code is not None
        ):
            raise VerificationError(f"converged receipt retained active work: {receipt}")
    elif expected_status == "claimed":
        if (
            type(claim) is not dict
            or active_run_id != claim["run"]["run_id"]
            or next_eligible_at is not None
            or reason_code is not None
        ):
            raise VerificationError(f"claimed convergence receipt mismatch: {receipt}")
    elif expected_status == "wait":
        if (
            claim is not None
            or active_run_id is None
            or next_eligible_at is None
            or not isinstance(reason_code, str)
        ):
            raise VerificationError(f"wait convergence receipt mismatch: {receipt}")
        if _timestamp(next_eligible_at) <= _timestamp(receipt["observed_at"]):
            raise VerificationError(f"wait deadline is not future DB time: {receipt}")
    elif expected_status == "manual_resolution" and (
        claim is not None
        or active_run_id is None
        or next_eligible_at is not None
        or not isinstance(reason_code, str)
    ):
        raise VerificationError(f"manual convergence receipt mismatch: {receipt}")


def verify_rolling_upgrade_definition_convergence(container: str) -> None:
    safe_account = "scheduler-recovery-safe"
    safe_holder = RECOVERY_HOLDER_ID
    safe_outer = acquire_outer_lease(
        container,
        account_id=safe_account,
        holder_id=safe_holder,
        release_sha=RELEASE_SHA,
    )
    safe_token = int(safe_outer["fencing_token"])

    disabled_command = definition_spec(
        "operations.commands",
        enabled=False,
        interval_seconds=300,
    )
    before_new = _scheduler_run_count(container, safe_account, "operations.commands")
    created = converge_definition(
        container,
        safe_token,
        disabled_command,
        account_id=safe_account,
        holder_id=safe_holder,
    )
    _require_convergence_status(created, "converged")
    if (
        created["definition"]["definition_sha256"]
        != definition_digest(disabled_command)
        or _scheduler_run_count(container, safe_account, "operations.commands")
        != before_new
    ):
        raise VerificationError(f"new convergence created cadence work: {created}")

    old_reconciliation = definition_spec(
        "operations.reconciliation",
        interval_seconds=1,
        lease_ttl_seconds=10,
        max_attempts=2,
        retry_base_seconds=1,
    )
    desired_reconciliation = dict(old_reconciliation)
    desired_reconciliation["interval_seconds"] = 2
    ensure_definition(
        container,
        safe_token,
        old_reconciliation,
        account_id=safe_account,
        holder_id=safe_holder,
    )
    initial_receipt = claim_due(
        container,
        safe_token,
        account_id=safe_account,
        holder_id=safe_holder,
    )
    initial_claim = validate_claim(initial_receipt, account_id=safe_account)
    if (
        initial_claim is None
        or initial_claim["run"]["job_key"] != "operations.reconciliation"
    ):
        raise VerificationError(f"rolling-upgrade fixture claim mismatch: {initial_receipt}")
    run_id = str(initial_claim["run"]["run_id"])
    run_count = _scheduler_run_count(container, safe_account, "operations.reconciliation")

    unexpired = converge_definition(
        container,
        safe_token,
        desired_reconciliation,
        account_id=safe_account,
        holder_id=safe_holder,
    )
    _require_convergence_status(unexpired, "wait")
    if (
        unexpired["active_run_id"] != run_id
        or _timestamp(unexpired["next_eligible_at"])
        != _timestamp(initial_claim["lease"]["lease_expires_at"])
        or _scheduler_run_count(container, safe_account, "operations.reconciliation")
        != run_count
    ):
        raise VerificationError(f"unexpired convergence wait mismatch: {unexpired}")

    wait_for_run_deadline(container, run_id, "lease_expires_at")
    expired = converge_definition(
        container,
        safe_token,
        desired_reconciliation,
        account_id=safe_account,
        holder_id=safe_holder,
    )
    _require_convergence_status(expired, "wait")
    retry_state = run_snapshot(container, run_id)
    if (
        retry_state["state"] != "retry_wait"
        or retry_state["failure_reason_code"] != "scheduler_lease_expired"
        or retry_state["failure_retryable"] is not True
        or _timestamp(expired["next_eligible_at"])
        != _timestamp(retry_state["available_at"])
    ):
        raise VerificationError(f"expired convergence retry wait mismatch: {expired}")

    wait_for_run_deadline(container, run_id, "available_at")
    drained = converge_definition(
        container,
        safe_token,
        desired_reconciliation,
        account_id=safe_account,
        holder_id=safe_holder,
    )
    _require_convergence_status(drained, "claimed")
    drain_claim = drained["claim"]
    if (
        drain_claim["run"]["run_id"] != run_id
        or drain_claim["run"]["attempt_count"] != 2
        or drain_claim["run"]["definition_sha256"]
        != definition_digest(old_reconciliation)
        or _scheduler_run_count(container, safe_account, "operations.reconciliation")
        != run_count
    ):
        raise VerificationError(f"old-digest drain claim mismatch: {drained}")
    complete_run(
        container,
        safe_token,
        drain_claim,
        "0" * 64,
        account_id=safe_account,
        holder_id=safe_holder,
    )
    updated = converge_definition(
        container,
        safe_token,
        desired_reconciliation,
        account_id=safe_account,
        holder_id=safe_holder,
    )
    _require_convergence_status(updated, "converged")
    if updated["definition"]["definition_sha256"] != definition_digest(
        desired_reconciliation
    ):
        raise VerificationError(f"desired definition did not converge: {updated}")
    repeated = converge_definition(
        container,
        safe_token,
        desired_reconciliation,
        account_id=safe_account,
        holder_id=safe_holder,
    )
    _require_convergence_status(repeated, "converged")
    if _scheduler_run_count(container, safe_account, "operations.reconciliation") != run_count:
        raise VerificationError("converged recovery RPC created a new cadence run")

    exhausted_account = "scheduler-recovery-exhausted"
    exhausted_holder = EXHAUSTED_RECOVERY_HOLDER_ID
    exhausted_outer = acquire_outer_lease(
        container,
        account_id=exhausted_account,
        holder_id=exhausted_holder,
        release_sha=RELEASE_SHA,
    )
    exhausted_token = int(exhausted_outer["fencing_token"])
    old_outbox = definition_spec(
        "operations.outbox",
        interval_seconds=1,
        lease_ttl_seconds=10,
        max_attempts=1,
    )
    desired_outbox = dict(old_outbox)
    desired_outbox["interval_seconds"] = 2
    ensure_definition(
        container,
        exhausted_token,
        old_outbox,
        account_id=exhausted_account,
        holder_id=exhausted_holder,
    )
    outbox_receipt = claim_due(
        container,
        exhausted_token,
        account_id=exhausted_account,
        holder_id=exhausted_holder,
    )
    outbox_claim = validate_claim(outbox_receipt, account_id=exhausted_account)
    if outbox_claim is None:
        raise VerificationError("exhausted convergence fixture was not claimed")
    outbox_run_id = str(outbox_claim["run"]["run_id"])
    wait_for_run_deadline(container, outbox_run_id, "lease_expires_at")
    exhausted = converge_definition(
        container,
        exhausted_token,
        desired_outbox,
        account_id=exhausted_account,
        holder_id=exhausted_holder,
    )
    _require_convergence_status(exhausted, "manual_resolution")
    outbox_state = run_snapshot(container, outbox_run_id)
    if (
        outbox_state["state"] != "dead_letter"
        or outbox_state["failure_retryable"] is not True
        or _scheduler_run_count(container, exhausted_account, "operations.outbox") != 1
    ):
        raise VerificationError(f"exhausted convergence boundary mismatch: {exhausted}")

    effectful_account = "scheduler-recovery-effectful"
    effectful_holder = EFFECTFUL_RECOVERY_HOLDER_ID
    effectful_outer = acquire_outer_lease(
        container,
        account_id=effectful_account,
        holder_id=effectful_holder,
        release_sha=RELEASE_SHA,
    )
    effectful_token = int(effectful_outer["fencing_token"])
    command_spec = definition_spec(
        "operations.commands",
        interval_seconds=300,
        lease_ttl_seconds=30,
    )
    ensure_definition(
        container,
        effectful_token,
        command_spec,
        account_id=effectful_account,
        holder_id=effectful_holder,
    )
    ensure_definition(
        container,
        effectful_token,
        definition_spec(
            "operations.reconciliation",
            interval_seconds=300,
            lease_ttl_seconds=30,
        ),
        account_id=effectful_account,
        holder_id=effectful_holder,
    )
    for job_key in ("operations.execution", "operations.settlement"):
        ensure_definition(
            container,
            effectful_token,
            definition_spec(job_key, interval_seconds=1, lease_ttl_seconds=10),
            account_id=effectful_account,
            holder_id=effectful_holder,
        )
    command_receipt = claim_due(
        container,
        effectful_token,
        account_id=effectful_account,
        holder_id=effectful_holder,
    )
    command_claim = validate_claim(command_receipt, account_id=effectful_account)
    if command_claim is None or command_claim["run"]["job_key"] != "operations.commands":
        raise VerificationError(f"effectful convergence command fixture mismatch: {command_receipt}")
    complete_run(
        container,
        effectful_token,
        command_claim,
        "1" * 64,
        account_id=effectful_account,
        holder_id=effectful_holder,
    )

    for index, job_key in enumerate(("operations.execution", "operations.settlement")):
        active_receipt = claim_due(
            container,
            effectful_token,
            account_id=effectful_account,
            holder_id=effectful_holder,
        )
        active_claim = validate_claim(active_receipt, account_id=effectful_account)
        if active_claim is None or active_claim["run"]["job_key"] != job_key:
            raise VerificationError(f"effectful recovery claim mismatch: {active_receipt}")
        active_run_id = str(active_claim["run"]["run_id"])
        desired = definition_spec(
            job_key,
            interval_seconds=2,
            lease_ttl_seconds=10,
        )
        waiting = converge_definition(
            container,
            effectful_token,
            desired,
            account_id=effectful_account,
            holder_id=effectful_holder,
        )
        _require_convergence_status(waiting, "wait")
        wait_for_run_deadline(container, active_run_id, "lease_expires_at")
        manual = converge_definition(
            container,
            effectful_token,
            desired,
            account_id=effectful_account,
            holder_id=effectful_holder,
        )
        _require_convergence_status(manual, "manual_resolution")
        inspection = inspect_dead_letter(
            container,
            effectful_token,
            active_run_id,
            account_id=effectful_account,
            holder_id=effectful_holder,
        )
        if (
            inspection["ineligibility_reason"]
            != "effectful_job_requires_resolution_evidence"
            or _scheduler_run_count(container, effectful_account, job_key) != 1
        ):
            raise VerificationError(f"effectful recovery auto-progressed: {inspection}")
        repeated_manual = converge_definition(
            container,
            effectful_token,
            desired,
            account_id=effectful_account,
            holder_id=effectful_holder,
        )
        _require_convergence_status(repeated_manual, "manual_resolution")
        if index == 0 and repeated_manual["active_run_id"] != active_run_id:
            raise VerificationError("execution manual-resolution identity changed")

    print(
        "PASS rolling_upgrade_definition_convergence, recovery_only_no_new_cadence, "
        "typed wait/claim/manual_resolution boundaries"
    )


def verify_concurrent_definition_idempotency(container: str) -> None:
    account_id = "scheduler-definition-race"
    holder_id = DEFINITION_RACE_HOLDER_ID
    outer = acquire_outer_lease(
        container,
        account_id=account_id,
        holder_id=holder_id,
    )
    outer_token = int(outer["fencing_token"])
    ensured_spec = definition_spec(
        "operations.commands",
        enabled=False,
        interval_seconds=300,
    )

    def ensure_target() -> dict[str, Any]:
        return ensure_definition(
            container,
            outer_token,
            ensured_spec,
            account_id=account_id,
            holder_id=holder_id,
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        ensured_receipts = list(pool.map(lambda _: ensure_target(), range(6)))
    ensured_identity = {
        (
            receipt["definition_id"],
            receipt["definition_sha256"],
            receipt["revision"],
            receipt["next_due_at"],
        )
        for receipt in ensured_receipts
    }
    if len(ensured_identity) != 1:
        raise VerificationError(
            f"concurrent ensure diverged on one target: {ensured_receipts}"
        )

    converged_spec = definition_spec(
        "operations.outbox",
        enabled=False,
        interval_seconds=300,
    )

    def converge_target() -> dict[str, Any]:
        receipt = converge_definition(
            container,
            outer_token,
            converged_spec,
            account_id=account_id,
            holder_id=holder_id,
        )
        _require_convergence_status(receipt, "converged")
        return receipt

    with ThreadPoolExecutor(max_workers=6) as pool:
        converged_receipts = list(pool.map(lambda _: converge_target(), range(6)))
    converged_identity = {
        (
            receipt["definition"]["definition_id"],
            receipt["definition"]["definition_sha256"],
            receipt["definition"]["revision"],
            receipt["definition"]["next_due_at"],
        )
        for receipt in converged_receipts
    }
    if len(converged_identity) != 1:
        raise VerificationError(
            f"concurrent convergence diverged on one target: {converged_receipts}"
        )
    durable = scalar(
        container,
        "select concat_ws('|',"
        "(select count(*) from private.scheduler_job_definitions "
        f"where account_id={sql_text(account_id)}),"
        "(select count(*) from private.scheduler_job_runs "
        f"where account_id={sql_text(account_id)}))",
    )
    if durable != "2|0":
        raise VerificationError(
            f"concurrent definition creation duplicated durable state: {durable}"
        )
    print("PASS concurrent_definition_idempotency for ensure and converge")


def verify_restart_expiry_and_stale_takeover(
    container: str,
    previous_outer: dict[str, Any],
    first_claim: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    run_id = str(first_claim["run"]["run_id"])
    before_restart = run_snapshot(container, run_id)
    run(["docker", "restart", container])
    wait_for_postgres(container)
    after_restart = run_snapshot(container, run_id)
    if after_restart != before_restart:
        raise VerificationError(
            f"database restart changed leased scheduler state: {after_restart}"
        )

    current_outer = acquire_outer_lease(container)
    current_token = int(current_outer["fencing_token"])
    previous_token = int(previous_outer["fencing_token"])
    if current_token <= previous_token:
        raise VerificationError(
            f"outer fencing generation did not advance: {previous_outer} -> {current_outer}"
        )
    _service_failure(
        container,
        HOLDER_ID,
        _claim_sql(previous_token),
        "scheduler_outer_lease_stale_or_missing",
    )
    _service_failure(
        container,
        OTHER_HOLDER_ID,
        "worker_api.claim_due_scheduler_job("
        f"{sql_text(ACCOUNT_ID)},{sql_text(OTHER_HOLDER_ID)},{current_token},"
        f"{sql_text(RELEASE_SHA)})",
        "scheduler_outer_lease_stale_or_missing",
    )
    _service_failure(
        container,
        HOLDER_ID,
        "worker_api.claim_due_scheduler_job("
        f"{sql_text(ACCOUNT_ID)},{sql_text(HOLDER_ID)},{current_token},"
        f"{sql_text(OTHER_RELEASE_SHA)})",
        "scheduler_outer_lease_stale_or_missing",
    )

    wait_for_run_deadline(container, run_id, "lease_expires_at")
    after_expiry = claim_due(container, current_token)
    if after_expiry["claimed"] is not False:
        raise VerificationError(f"expired attempt skipped retry wait: {after_expiry}")
    retry_wait = run_snapshot(container, run_id)
    if (
        retry_wait["state"] != "retry_wait"
        or retry_wait["attempt_count"] != 1
        or retry_wait["failure_reason_code"] != "scheduler_lease_expired"
        or retry_wait["failure_retryable"] is not True
    ):
        raise VerificationError(f"expired lease did not consume retry state: {retry_wait}")

    wait_for_run_deadline(container, run_id, "available_at")
    takeover_receipt = claim_due(container, current_token)
    takeover = validate_claim(takeover_receipt)
    if (
        takeover is None
        or takeover["run"]["run_id"] != run_id
        or takeover["run"]["attempt_count"] != 2
        or takeover["lease"]["lease_token"] == first_claim["lease"]["lease_token"]
        or takeover["lease"]["outer_fencing_token"] != current_token
    ):
        raise VerificationError(f"stale takeover binding mismatch: {takeover_receipt}")

    _service_failure(
        container,
        HOLDER_ID,
        _complete_sql(
            current_token,
            takeover,
            "2" * 64,
            lease_token=str(uuid4()),
        ),
        "scheduler_completion_compare_and_swap_failed",
    )
    _service_failure(
        container,
        HOLDER_ID,
        _complete_sql(current_token, first_claim, "2" * 64),
        "scheduler_completion_compare_and_swap_failed",
    )

    wait_for_run_deadline(container, run_id, "lease_expires_at")
    terminal_probe = claim_due(container, current_token)
    if terminal_probe["claimed"] is not False:
        raise VerificationError(f"exhausted crash budget issued another lease: {terminal_probe}")
    dead_letter_state = run_snapshot(container, run_id)
    if (
        dead_letter_state["state"] != "dead_letter"
        or dead_letter_state["attempt_count"] != 2
        or dead_letter_state["failure_reason_code"] != "scheduler_lease_expired"
    ):
        raise VerificationError(f"crash budget did not dead-letter: {dead_letter_state}")
    definition_state = scalar(
        container,
        "select concat_ws('|',scheduler_state,blocked_by_run_id::text,latest_run_id::text) "
        "from private.scheduler_job_definitions "
        "where job_key='operations.commands';",
    )
    if definition_state != f"blocked|{run_id}|{run_id}":
        raise VerificationError(f"dead letter did not block definition: {definition_state}")

    inspection = inspect_dead_letter(container, current_token, run_id)
    dead_letter = inspection.get("dead_letter")
    if (
        inspection["found"] is not True
        or inspection["eligible"] is not True
        or inspection["ineligibility_reason"] is not None
        or type(dead_letter) is not dict
    ):
        raise VerificationError(f"eligible dead-letter inspection mismatch: {inspection}")
    print(
        "PASS restart_persistence_and_stale_takeover, expired_lease_retry_budget, "
        "terminal definition block"
    )
    return current_outer, dead_letter


def verify_manual_replay_compare_and_swap(
    container: str,
    outer: dict[str, Any],
    dead_letter: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    outer_token = int(outer["fencing_token"])
    execution_enabled = definition_spec(
        "operations.execution",
        lease_ttl_seconds=10,
    )
    ensure_definition(container, outer_token, execution_enabled)
    barrier_probe = claim_due(container, outer_token)
    if barrier_probe["claimed"] is not False:
        raise VerificationError(
            f"execution bypassed a blocked command barrier: {barrier_probe}"
        )
    execution_disabled = dict(execution_enabled)
    execution_disabled["enabled"] = False
    ensure_definition(container, outer_token, execution_disabled)

    invalid_cases: tuple[tuple[dict[str, object], str], ...] = (
        (
            {"expected_revision": int(dead_letter["source_revision"]) + 1},
            "scheduler_replay_compare_and_swap_failed",
        ),
        (
            {"definition_sha256": "3" * 64},
            "scheduler_replay_compare_and_swap_failed",
        ),
        (
            {
                "failure_reason_code": "different_failure_reason",
                "confirmed_reason_code": "different_failure_reason",
            },
            "scheduler_replay_compare_and_swap_failed",
        ),
        (
            {"failure_sha256": "4" * 64},
            "scheduler_replay_compare_and_swap_failed",
        ),
        (
            {"replay_generation": int(dead_letter["replay_generation"]) + 1},
            "scheduler_replay_compare_and_swap_failed",
        ),
        (
            {"explicit_confirmation": False},
            "scheduler_replay_parameters_invalid",
        ),
        (
            {"confirmed_reason_code": "confirmation_reason_mismatch"},
            "scheduler_replay_parameters_invalid",
        ),
    )
    for overrides, expected_error in invalid_cases:
        request_id = str(uuid4())
        _service_failure(
            container,
            HOLDER_ID,
            _replay_sql(outer_token, dead_letter, request_id, **overrides),
            expected_error,
        )

    source_run_id = str(dead_letter["source_run_id"])
    immutable_source = run_snapshot(container, source_run_id)
    replay_request_id = str(uuid4())
    replay = replay_dead_letter(
        container,
        outer_token,
        dead_letter,
        replay_request_id,
    )
    if (
        replay["source_run_id"] != source_run_id
        or replay["replay_request_id"] != replay_request_id
        or replay["definition_sha256"] != dead_letter["definition_sha256"]
        or replay["source_revision"] != dead_letter["source_revision"]
        or replay["failure_reason_code"] != dead_letter["failure_reason_code"]
        or replay["replay_generation"] != 1
        or replay["state"] != "pending"
        or replay["idempotent"] is not False
        or replay["new_run_id"] == source_run_id
    ):
        raise VerificationError(f"manual replay creation receipt mismatch: {replay}")
    if run_snapshot(container, source_run_id) != immutable_source:
        raise VerificationError("manual replay mutated the source dead letter")
    child = run_snapshot(container, str(replay["new_run_id"]))
    if (
        child["state"] != "pending"
        or child["replay_of_run_id"] != source_run_id
        or child["replay_generation"] != 1
        or child["definition_sha256"] != dead_letter["definition_sha256"]
    ):
        raise VerificationError(f"manual replay child mismatch: {child}")

    _service_failure(
        container,
        HOLDER_ID,
        _replay_sql(outer_token, dead_letter, str(uuid4())),
        "scheduler_replay_compare_and_swap_failed",
    )

    # Model response loss: the creation committed, the original outer lease is
    # gone, and a new worker must recover the immutable receipt with the same
    # request/CAS payload rather than creating another child.
    psql(
        container,
        "update private.worker_leases set "
        "acquired_at=least(acquired_at,pg_catalog.clock_timestamp()-interval '3 seconds'),"
        "renewed_at=pg_catalog.clock_timestamp()-interval '2 seconds',"
        "expires_at=pg_catalog.clock_timestamp()-interval '1 second' "
        f"where account_id={sql_text(ACCOUNT_ID)};",
    )
    takeover_outer = acquire_outer_lease(
        container,
        account_id=ACCOUNT_ID,
        holder_id=OTHER_HOLDER_ID,
        release_sha=RELEASE_SHA,
    )
    takeover_token = int(takeover_outer["fencing_token"])
    if takeover_token <= outer_token:
        raise VerificationError("replay receipt takeover did not advance fencing")
    repeated = replay_dead_letter(
        container,
        takeover_token,
        dead_letter,
        replay_request_id,
        holder_id=OTHER_HOLDER_ID,
    )
    immutable_receipt_fields = REPLAY_FIELDS - {"observed_at", "idempotent"}
    if repeated["idempotent"] is not True or any(
        repeated[field] != replay[field] for field in immutable_receipt_fields
    ):
        raise VerificationError(f"takeover replay receipt mismatch: {repeated}")
    _service_failure(
        container,
        OTHER_HOLDER_ID,
        _replay_sql(
            takeover_token,
            dead_letter,
            replay_request_id,
            failure_reason_code="idempotency_conflict_reason",
            confirmed_reason_code="idempotency_conflict_reason",
            holder_id=OTHER_HOLDER_ID,
        ),
        "scheduler_replay_idempotency_conflict",
    )

    psql(
        container,
        "update private.worker_leases set "
        "acquired_at=least(acquired_at,pg_catalog.clock_timestamp()-interval '3 seconds'),"
        "renewed_at=pg_catalog.clock_timestamp()-interval '2 seconds',"
        "expires_at=pg_catalog.clock_timestamp()-interval '1 second' "
        f"where account_id={sql_text(ACCOUNT_ID)};",
    )
    resumed_outer = acquire_outer_lease(container)
    outer_token = int(resumed_outer["fencing_token"])
    source_after_replay = inspect_dead_letter(container, outer_token, source_run_id)
    if (
        source_after_replay["eligible"] is not False
        or source_after_replay["ineligibility_reason"] != "source_already_replayed"
    ):
        raise VerificationError(f"replayed source eligibility mismatch: {source_after_replay}")

    child_claim_receipt = claim_due(container, outer_token)
    child_claim = validate_claim(child_claim_receipt)
    if (
        child_claim is None
        or child_claim["run"]["run_id"] != replay["new_run_id"]
        or child_claim["run"]["replay_generation"] != 1
    ):
        raise VerificationError(f"manual replay child was not claimable: {child_claim_receipt}")
    complete_run(container, outer_token, child_claim, "5" * 64)
    expect_failure(
        container,
        "update private.scheduler_job_runs set updated_at=updated_at "
        f"where run_id={sql_text(source_run_id)}::uuid;",
        "scheduler_terminal_run_is_immutable",
    )
    expect_failure(
        container,
        "update private.scheduler_replay_requests set requested_at=requested_at "
        f"where replay_request_id={sql_text(replay_request_id)}::uuid;",
        "append_only",
    )
    print(
        "PASS manual_replay_compare_and_swap, immutable source/new child, "
        "takeover receipt recovery and reason-bound confirmation"
    )
    return replay, resumed_outer


def verify_startup_drain_and_execution_barrier(
    container: str,
    previous_outer: dict[str, Any],
    specs: dict[str, dict[str, object]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    previous_token = int(previous_outer["fencing_token"])
    execution_enabled = dict(specs["operations.execution"])
    execution_enabled["enabled"] = True
    ensure_definition(container, previous_token, execution_enabled)
    specs["operations.execution"] = execution_enabled
    settlement_enabled = dict(specs["operations.settlement"])
    settlement_enabled["enabled"] = True
    ensure_definition(container, previous_token, settlement_enabled)
    specs["operations.settlement"] = settlement_enabled
    reconciliation_enabled = dict(specs["operations.reconciliation"])
    reconciliation_enabled["enabled"] = True
    ensure_definition(container, previous_token, reconciliation_enabled)
    specs["operations.reconciliation"] = reconciliation_enabled

    outer = acquire_outer_lease(container)
    outer_token = int(outer["fencing_token"])
    if outer_token <= previous_token:
        raise VerificationError("startup outer fencing token did not advance")
    due_delta = float(
        scalar(
            container,
            "select extract(epoch from (next_due_at-"
            "pg_catalog.clock_timestamp())) from private.scheduler_job_definitions "
            "where account_id='paper-primary' and job_key='operations.commands';",
        )
    )
    if due_delta <= 0:
        raise VerificationError(
            "startup-drain fixture lost its future command cadence boundary"
        )

    forced_receipt = claim_due(container, outer_token)
    forced_command = validate_claim(forced_receipt)
    if (
        forced_command is None
        or forced_command["run"]["job_key"] != "operations.commands"
        or _timestamp(forced_command["run"]["scheduled_for"])
        > _timestamp(forced_receipt["observed_at"])
    ):
        raise VerificationError(f"startup did not force command drain: {forced_receipt}")
    complete_run(container, outer_token, forced_command, "6" * 64)

    command_barrier = scalar(
        container,
        "select concat_ws('|',run.state,run.definition_sha256=definition.definition_sha256,"
        "run.lease_holder_id,run.outer_fencing_token,run.lease_release_sha,"
        "run.completed_at>=outer_lease.acquired_at,"
        "definition.next_due_at>pg_catalog.clock_timestamp(),"
        "not exists (select 1 from private.scheduler_job_runs active "
        "where active.definition_id=definition.definition_id "
        "and active.state in ('pending','leased','retry_wait'))) "
        "from private.scheduler_job_definitions definition "
        "join private.scheduler_job_runs run on run.run_id=definition.latest_run_id "
        "join private.worker_leases outer_lease "
        "on outer_lease.account_id=definition.account_id "
        "where definition.account_id='paper-primary' "
        "and definition.job_key='operations.commands';",
    )
    expected = f"succeeded|t|{HOLDER_ID}|{outer_token}|{RELEASE_SHA}|t|t|t"
    if command_barrier != expected:
        raise VerificationError(f"current command barrier evidence mismatch: {command_barrier}")

    execution_receipt = claim_due(container, outer_token)
    execution = validate_claim(execution_receipt)
    if execution is None or execution["run"]["job_key"] != "operations.execution":
        raise VerificationError(
            f"execution was not released by the current command barrier: {execution_receipt}"
        )
    print(
        "PASS forced startup command drain and current outer/digest command barrier"
    )
    return outer, execution


def verify_settlement_gate_and_recovery_progress(container: str) -> None:
    account_id = "scheduler-settlement-blocked"
    holder_id = BLOCKED_SETTLEMENT_HOLDER_ID
    outer = acquire_outer_lease(
        container,
        account_id=account_id,
        holder_id=holder_id,
    )
    outer_token = int(outer["fencing_token"])
    specs = {
        "operations.commands": definition_spec(
            "operations.commands",
            interval_seconds=300,
            lease_ttl_seconds=30,
        ),
        "operations.execution": definition_spec(
            "operations.execution",
            interval_seconds=1,
            lease_ttl_seconds=30,
        ),
        "operations.settlement": definition_spec(
            "operations.settlement",
            interval_seconds=1,
            lease_ttl_seconds=10,
        ),
        "operations.reconciliation": definition_spec(
            "operations.reconciliation",
            enabled=False,
            interval_seconds=1,
            lease_ttl_seconds=30,
        ),
        "operations.outbox": definition_spec(
            "operations.outbox",
            enabled=False,
            interval_seconds=1,
            lease_ttl_seconds=30,
        ),
    }
    settlement_disabled = dict(specs["operations.settlement"])
    settlement_disabled["enabled"] = False
    specs["operations.settlement"] = settlement_disabled
    for job_key in JOB_KEYS:
        ensure_definition(
            container,
            outer_token,
            specs[job_key],
            account_id=account_id,
            holder_id=holder_id,
        )

    command_receipt = claim_due(
        container,
        outer_token,
        account_id=account_id,
        holder_id=holder_id,
    )
    command = validate_claim(command_receipt, account_id=account_id)
    if command is None or command["run"]["job_key"] != "operations.commands":
        raise VerificationError(
            f"disabled-settlement command fixture mismatch: {command_receipt}"
        )
    complete_run(
        container,
        outer_token,
        command,
        "a" * 64,
        account_id=account_id,
        holder_id=holder_id,
    )
    disabled_probe = claim_due(
        container,
        outer_token,
        account_id=account_id,
        holder_id=holder_id,
    )
    if disabled_probe["claimed"] is not False:
        raise VerificationError(
            f"disabled settlement released execution: {disabled_probe}"
        )
    if _scheduler_run_count(container, account_id, "operations.execution") != 0:
        raise VerificationError("disabled settlement created execution work")

    execution_disabled = dict(specs["operations.execution"])
    execution_disabled["enabled"] = False
    specs["operations.execution"] = execution_disabled
    ensure_definition(
        container,
        outer_token,
        execution_disabled,
        account_id=account_id,
        holder_id=holder_id,
    )
    settlement_enabled = dict(specs["operations.settlement"])
    settlement_enabled["enabled"] = True
    specs["operations.settlement"] = settlement_enabled
    ensure_definition(
        container,
        outer_token,
        settlement_enabled,
        account_id=account_id,
        holder_id=holder_id,
    )
    settlement_receipt = claim_due(
        container,
        outer_token,
        account_id=account_id,
        holder_id=holder_id,
    )
    settlement = validate_claim(settlement_receipt, account_id=account_id)
    if settlement is None or settlement["run"]["job_key"] != "operations.settlement":
        raise VerificationError(
            f"blocked-settlement fixture claim mismatch: {settlement_receipt}"
        )
    settlement_run_id = str(settlement["run"]["run_id"])
    execution_enabled = dict(execution_disabled)
    execution_enabled["enabled"] = True
    specs["operations.execution"] = execution_enabled
    ensure_definition(
        container,
        outer_token,
        execution_enabled,
        account_id=account_id,
        holder_id=holder_id,
    )
    wait_for_run_deadline(container, settlement_run_id, "lease_expires_at")
    expiry_probe = claim_due(
        container,
        outer_token,
        account_id=account_id,
        holder_id=holder_id,
    )
    settlement_state = run_snapshot(container, settlement_run_id)
    if (
        expiry_probe["claimed"] is not False
        or settlement_state["state"] != "dead_letter"
        or settlement_state["failure_retryable"] is not False
    ):
        raise VerificationError(
            f"settlement did not enter a fail-closed block: {settlement_state}"
        )
    if _scheduler_run_count(container, account_id, "operations.execution") != 0:
        raise VerificationError("expired settlement raced into execution work")
    blocked_probe = claim_due(
        container,
        outer_token,
        account_id=account_id,
        holder_id=holder_id,
    )
    if blocked_probe["claimed"] is not False:
        raise VerificationError(
            f"blocked settlement released execution: {blocked_probe}"
        )
    if _scheduler_run_count(container, account_id, "operations.execution") != 0:
        raise VerificationError("blocked settlement created execution work")

    expired_execution_run_id = _insert_expired_execution_fixture(
        container,
        outer_token,
        execution_enabled,
        account_id=account_id,
        holder_id=holder_id,
    )
    cleanup_probe = claim_due(
        container,
        outer_token,
        account_id=account_id,
        holder_id=holder_id,
    )
    expired_execution_state = run_snapshot(container, expired_execution_run_id)
    if (
        cleanup_probe["claimed"] is not False
        or expired_execution_state["state"] != "dead_letter"
        or expired_execution_state["failure_retryable"] is not False
        or _scheduler_run_count(container, account_id, "operations.execution") != 1
    ):
        raise VerificationError(
            "blocked settlement prevented expired execution cleanup: "
            f"{expired_execution_state}"
        )

    recovery_outer = acquire_outer_lease(
        container,
        account_id=account_id,
        holder_id=holder_id,
    )
    recovery_token = int(recovery_outer["fencing_token"])
    recovery_command_receipt = claim_due(
        container,
        recovery_token,
        account_id=account_id,
        holder_id=holder_id,
    )
    recovery_command = validate_claim(
        recovery_command_receipt,
        account_id=account_id,
    )
    if (
        recovery_command is None
        or recovery_command["run"]["job_key"] != "operations.commands"
    ):
        raise VerificationError(
            "settlement block prevented startup command recovery: "
            f"{recovery_command_receipt}"
        )
    complete_run(
        container,
        recovery_token,
        recovery_command,
        "b" * 64,
        account_id=account_id,
        holder_id=holder_id,
    )

    for index, job_key in enumerate(
        ("operations.reconciliation", "operations.outbox")
    ):
        enabled = dict(specs[job_key])
        enabled["enabled"] = True
        specs[job_key] = enabled
        ensure_definition(
            container,
            recovery_token,
            enabled,
            account_id=account_id,
            holder_id=holder_id,
        )
        receipt = claim_due(
            container,
            recovery_token,
            account_id=account_id,
            holder_id=holder_id,
        )
        recovery_claim = validate_claim(receipt, account_id=account_id)
        if recovery_claim is None or recovery_claim["run"]["job_key"] != job_key:
            raise VerificationError(
                f"settlement block prevented {job_key} recovery: {receipt}"
            )
        complete_run(
            container,
            recovery_token,
            recovery_claim,
            ("c" if index == 0 else "d") * 64,
            account_id=account_id,
            holder_id=holder_id,
        )

    final_state = scalar(
        container,
        "select concat_ws('|',"
        "(select scheduler_state from private.scheduler_job_definitions "
        f"where account_id={sql_text(account_id)} and job_key='operations.settlement'),"
        "(select count(*) from private.scheduler_job_runs "
        f"where account_id={sql_text(account_id)} and job_key='operations.execution'),"
        "(select count(*) from private.scheduler_job_runs "
        f"where account_id={sql_text(account_id)} and job_key in ("
        "'operations.commands','operations.reconciliation','operations.outbox') "
        "and state='succeeded'))",
    )
    if final_state != "blocked|1|4":
        raise VerificationError(
            f"effectful block recovery progress mismatch: {final_state}"
        )
    print(
        "PASS settlement_blocked_execution_gate, disabled settlement gate and "
        "effectful_block_recovery_progress, "
        "expired_execution_cleanup_without_barrier"
    )


def verify_missing_barrier_expired_execution_cleanup(container: str) -> None:
    account_id = "scheduler-expired-execution-missing"
    holder_id = MISSING_BARRIER_HOLDER_ID
    outer = acquire_outer_lease(
        container,
        account_id=account_id,
        holder_id=holder_id,
    )
    outer_token = int(outer["fencing_token"])
    execution_spec = definition_spec(
        "operations.execution",
        interval_seconds=300,
        lease_ttl_seconds=10,
    )
    ensure_definition(
        container,
        outer_token,
        execution_spec,
        account_id=account_id,
        holder_id=holder_id,
    )
    run_id = _insert_expired_execution_fixture(
        container,
        outer_token,
        execution_spec,
        account_id=account_id,
        holder_id=holder_id,
    )
    cleanup_probe = claim_due(
        container,
        outer_token,
        account_id=account_id,
        holder_id=holder_id,
    )
    state = run_snapshot(container, run_id)
    durable = scalar(
        container,
        "select concat_ws('|',"
        "(select count(*) from private.scheduler_job_definitions "
        f"where account_id={sql_text(account_id)} and job_key in ("
        "'operations.commands','operations.settlement')) ,"
        "(select scheduler_state from private.scheduler_job_definitions "
        f"where account_id={sql_text(account_id)} and job_key='operations.execution'),"
        "(select count(*) from private.scheduler_job_runs "
        f"where account_id={sql_text(account_id)} and job_key='operations.execution'))",
    )
    if (
        cleanup_probe["claimed"] is not False
        or state["state"] != "dead_letter"
        or state["failure_retryable"] is not False
        or durable != "0|blocked|1"
    ):
        raise VerificationError(
            f"missing barrier prevented expired execution cleanup: {state}|{durable}"
        )
    repeated = claim_due(
        container,
        outer_token,
        account_id=account_id,
        holder_id=holder_id,
    )
    if (
        repeated["claimed"] is not False
        or _scheduler_run_count(container, account_id, "operations.execution") != 1
    ):
        raise VerificationError(
            f"expired execution cleanup created replacement work: {repeated}"
        )
    print("PASS expired_execution_cleanup_without_barrier for missing prerequisites")


def verify_reconciliation_execution_gate(container: str) -> None:
    account_id = "scheduler-reconciliation-gate"
    holder_id = RECONCILIATION_GATE_HOLDER_ID
    outer = acquire_outer_lease(
        container,
        account_id=account_id,
        holder_id=holder_id,
    )
    outer_token = int(outer["fencing_token"])
    command_spec = definition_spec(
        "operations.commands",
        interval_seconds=300,
        lease_ttl_seconds=30,
    )
    execution_spec = definition_spec(
        "operations.execution",
        interval_seconds=1,
        lease_ttl_seconds=30,
    )
    settlement_spec = definition_spec(
        "operations.settlement",
        interval_seconds=300,
        lease_ttl_seconds=30,
    )
    for spec in (command_spec, execution_spec, settlement_spec):
        ensure_definition(
            container,
            outer_token,
            spec,
            account_id=account_id,
            holder_id=holder_id,
        )

    command_receipt = claim_due(
        container,
        outer_token,
        account_id=account_id,
        holder_id=holder_id,
    )
    command = validate_claim(command_receipt, account_id=account_id)
    if command is None or command["run"]["job_key"] != "operations.commands":
        raise VerificationError(
            f"reconciliation gate command fixture mismatch: {command_receipt}"
        )
    complete_run(
        container,
        outer_token,
        command,
        "4" * 64,
        account_id=account_id,
        holder_id=holder_id,
    )

    settlement_receipt = claim_due(
        container,
        outer_token,
        account_id=account_id,
        holder_id=holder_id,
    )
    settlement = validate_claim(settlement_receipt, account_id=account_id)
    if settlement is None or settlement["run"]["job_key"] != "operations.settlement":
        raise VerificationError(
            "missing reconciliation did not bypass execution in favor of settlement: "
            f"{settlement_receipt}"
        )
    complete_run(
        container,
        outer_token,
        settlement,
        "5" * 64,
        account_id=account_id,
        holder_id=holder_id,
    )
    missing_probe = claim_due(
        container,
        outer_token,
        account_id=account_id,
        holder_id=holder_id,
    )
    if (
        missing_probe["claimed"] is not False
        or _scheduler_run_count(container, account_id, "operations.execution") != 0
    ):
        raise VerificationError(
            f"missing reconciliation released execution: {missing_probe}"
        )

    reconciliation_disabled = definition_spec(
        "operations.reconciliation",
        enabled=False,
        interval_seconds=1,
        lease_ttl_seconds=10,
        max_attempts=1,
    )
    ensure_definition(
        container,
        outer_token,
        reconciliation_disabled,
        account_id=account_id,
        holder_id=holder_id,
    )
    disabled_probe = claim_due(
        container,
        outer_token,
        account_id=account_id,
        holder_id=holder_id,
    )
    if (
        disabled_probe["claimed"] is not False
        or _scheduler_run_count(container, account_id, "operations.execution") != 0
    ):
        raise VerificationError(
            f"disabled reconciliation released execution: {disabled_probe}"
        )

    execution_disabled = dict(execution_spec)
    execution_disabled["enabled"] = False
    ensure_definition(
        container,
        outer_token,
        execution_disabled,
        account_id=account_id,
        holder_id=holder_id,
    )
    reconciliation_enabled = dict(reconciliation_disabled)
    reconciliation_enabled["enabled"] = True
    ensure_definition(
        container,
        outer_token,
        reconciliation_enabled,
        account_id=account_id,
        holder_id=holder_id,
    )
    reconciliation_receipt = claim_due(
        container,
        outer_token,
        account_id=account_id,
        holder_id=holder_id,
    )
    reconciliation = validate_claim(reconciliation_receipt, account_id=account_id)
    if (
        reconciliation is None
        or reconciliation["run"]["job_key"] != "operations.reconciliation"
    ):
        raise VerificationError(
            f"expired reconciliation fixture mismatch: {reconciliation_receipt}"
        )
    execution_enabled = dict(execution_disabled)
    execution_enabled["enabled"] = True
    ensure_definition(
        container,
        outer_token,
        execution_enabled,
        account_id=account_id,
        holder_id=holder_id,
    )
    reconciliation_run_id = str(reconciliation["run"]["run_id"])
    wait_for_run_deadline(container, reconciliation_run_id, "lease_expires_at")
    expiry_probe = claim_due(
        container,
        outer_token,
        account_id=account_id,
        holder_id=holder_id,
    )
    reconciliation_state = run_snapshot(container, reconciliation_run_id)
    blocked_state = scalar(
        container,
        "select scheduler_state from private.scheduler_job_definitions "
        f"where account_id={sql_text(account_id)} "
        "and job_key='operations.reconciliation';",
    )
    if (
        expiry_probe["claimed"] is not False
        or reconciliation_state["state"] != "dead_letter"
        or reconciliation_state["failure_retryable"] is not True
        or blocked_state != "blocked"
        or _scheduler_run_count(container, account_id, "operations.execution") != 0
    ):
        raise VerificationError(
            "expired reconciliation was not cleaned before execution: "
            f"{reconciliation_state}|{blocked_state}|{expiry_probe}"
        )
    blocked_probe = claim_due(
        container,
        outer_token,
        account_id=account_id,
        holder_id=holder_id,
    )
    if (
        blocked_probe["claimed"] is not False
        or _scheduler_run_count(container, account_id, "operations.execution") != 0
    ):
        raise VerificationError(
            f"blocked reconciliation released execution: {blocked_probe}"
        )
    print(
        "PASS reconciliation_execution_gate for missing/disabled/blocked states and "
        "expired_reconciliation_cleanup_priority"
    )


def verify_job_specific_retry_matrix_and_completion(
    container: str,
    outer: dict[str, Any],
    specs: dict[str, dict[str, object]],
    execution: dict[str, Any],
) -> None:
    outer_token = int(outer["fencing_token"])
    _service_failure(
        container,
        HOLDER_ID,
        _complete_sql(
            outer_token,
            execution,
            "8" * 64,
            lease_token=str(uuid4()),
        ),
        "scheduler_completion_compare_and_swap_failed",
    )
    effectful_expiry_isolation = (
        "operations.settlement",
        "operations.reconciliation",
        "operations.outbox",
    )
    for job_key in effectful_expiry_isolation:
        disabled = dict(specs[job_key])
        disabled["enabled"] = False
        ensure_definition(container, outer_token, disabled)
        specs[job_key] = disabled
    settlement_disabled = specs["operations.settlement"]
    wait_for_run_deadline(
        container,
        str(execution["run"]["run_id"]),
        "lease_expires_at",
    )
    execution_expiry_probe = claim_due(container, outer_token)
    if execution_expiry_probe["claimed"] is not False:
        raise VerificationError(
            f"execution lease expiry issued replacement work: {execution_expiry_probe}"
        )
    execution_inspection = inspect_dead_letter(
        container,
        outer_token,
        str(execution["run"]["run_id"]),
    )
    if (
        execution_inspection["eligible"] is not False
        or execution_inspection["ineligibility_reason"]
        != "effectful_job_requires_resolution_evidence"
        or execution_inspection["dead_letter"]["failure_reason_code"]
        != "scheduler_lease_expired"
    ):
        raise VerificationError(
            f"execution expiry resolution boundary mismatch: {execution_inspection}"
        )

    settlement_enabled = dict(settlement_disabled)
    settlement_enabled["enabled"] = True
    ensure_definition(container, outer_token, settlement_enabled)
    specs["operations.settlement"] = settlement_enabled
    settlement_receipt = claim_due(container, outer_token)
    settlement = validate_claim(settlement_receipt)
    if settlement is None or settlement["run"]["job_key"] != "operations.settlement":
        raise VerificationError(f"settlement priority mismatch: {settlement_receipt}")
    wait_for_run_deadline(
        container,
        str(settlement["run"]["run_id"]),
        "lease_expires_at",
    )
    settlement_expiry_probe = claim_due(container, outer_token)
    if settlement_expiry_probe["claimed"] is not False:
        raise VerificationError(
            f"settlement lease expiry issued replacement work: {settlement_expiry_probe}"
        )
    settlement_inspection = inspect_dead_letter(
        container,
        outer_token,
        str(settlement["run"]["run_id"]),
    )
    if (
        settlement_inspection["eligible"] is not False
        or settlement_inspection["ineligibility_reason"]
        != "effectful_job_requires_resolution_evidence"
        or settlement_inspection["dead_letter"]["failure_reason_code"]
        != "scheduler_lease_expired"
    ):
        raise VerificationError(
            f"settlement expiry resolution boundary mismatch: {settlement_inspection}"
        )

    for job_key in ("operations.reconciliation", "operations.outbox"):
        enabled = dict(specs[job_key])
        enabled["enabled"] = True
        ensure_definition(container, outer_token, enabled)
        specs[job_key] = enabled

    reconciliation_receipt = claim_due(container, outer_token)
    reconciliation = validate_claim(reconciliation_receipt)
    if (
        reconciliation is None
        or reconciliation["run"]["job_key"] != "operations.reconciliation"
    ):
        raise VerificationError(
            f"reconciliation priority mismatch: {reconciliation_receipt}"
        )
    reconciliation_failure = fail_run(
        container,
        outer_token,
        reconciliation,
        "reconciliation_poll_retryable",
        "b" * 64,
        True,
    )
    if (
        reconciliation_failure["state"] != "retry_wait"
        or reconciliation_failure["next_attempt_at"] is None
    ):
        raise VerificationError(
            f"reconciliation retry matrix mismatch: {reconciliation_failure}"
        )

    # Claim outbox before running the extra reconciliation idempotency probes;
    # its ten-second retry delay ensures the scheduler cannot race back to the
    # higher-priority reconciliation definition.
    outbox_receipt = claim_due(container, outer_token)
    outbox = validate_claim(outbox_receipt)
    if outbox is None or outbox["run"]["job_key"] != "operations.outbox":
        raise VerificationError(f"outbox priority mismatch: {outbox_receipt}")

    repeated_reconciliation_failure = fail_run(
        container,
        outer_token,
        reconciliation,
        "reconciliation_poll_retryable",
        "b" * 64,
        True,
    )
    if any(
        repeated_reconciliation_failure[field] != reconciliation_failure[field]
        for field in SETTLEMENT_FIELDS - {"observed_at"}
    ):
        raise VerificationError(
            "reconciliation failure receipt was not idempotent: "
            f"{repeated_reconciliation_failure}"
        )
    _service_failure(
        container,
        HOLDER_ID,
        _fail_sql(
            outer_token,
            reconciliation,
            "reconciliation_poll_retryable",
            "c" * 64,
            True,
        ),
        "scheduler_failure_compare_and_swap_failed",
    )

    outbox_failure = fail_run(
        container,
        outer_token,
        outbox,
        "outbox_poll_retryable",
        "d" * 64,
        True,
    )
    if outbox_failure["state"] != "retry_wait" or outbox_failure["next_attempt_at"] is None:
        raise VerificationError(f"outbox retry matrix mismatch: {outbox_failure}")

    wait_for_run_deadline(
        container,
        str(reconciliation["run"]["run_id"]),
        "available_at",
    )
    reconciliation_retry_receipt = claim_due(container, outer_token)
    reconciliation_retry = validate_claim(reconciliation_retry_receipt)
    if (
        reconciliation_retry is None
        or reconciliation_retry["run"]["run_id"]
        != reconciliation["run"]["run_id"]
        or reconciliation_retry["run"]["attempt_count"] != 2
    ):
        raise VerificationError(
            f"reconciliation retry claim mismatch: {reconciliation_retry_receipt}"
        )
    reconciliation_result = "e" * 64
    completed = complete_run(
        container,
        outer_token,
        reconciliation_retry,
        reconciliation_result,
    )
    repeated_completion = complete_run(
        container,
        outer_token,
        reconciliation_retry,
        reconciliation_result,
    )
    if any(
        repeated_completion[field] != completed[field]
        for field in SETTLEMENT_FIELDS - {"observed_at"}
    ):
        raise VerificationError(f"completion receipt was not idempotent: {repeated_completion}")
    _service_failure(
        container,
        HOLDER_ID,
        _complete_sql(outer_token, reconciliation_retry, "f" * 64),
        "scheduler_completion_compare_and_swap_failed",
    )
    _service_failure(
        container,
        HOLDER_ID,
        _complete_sql(
            outer_token,
            reconciliation_retry,
            reconciliation_result,
            expected_revision=int(completed["run_revision"]),
        ),
        "scheduler_completion_compare_and_swap_failed",
    )

    reconciliation_disabled = dict(specs["operations.reconciliation"])
    reconciliation_disabled["enabled"] = False
    ensure_definition(container, outer_token, reconciliation_disabled)
    specs["operations.reconciliation"] = reconciliation_disabled
    wait_for_run_deadline(
        container,
        str(outbox["run"]["run_id"]),
        "available_at",
    )
    outbox_retry_receipt = claim_due(container, outer_token)
    outbox_retry = validate_claim(outbox_retry_receipt)
    if (
        outbox_retry is None
        or outbox_retry["run"]["run_id"] != outbox["run"]["run_id"]
        or outbox_retry["run"]["attempt_count"] != 2
    ):
        raise VerificationError(f"outbox retry claim mismatch: {outbox_retry_receipt}")
    complete_run(container, outer_token, outbox_retry, "1" * 64)

    retry_flags = scalar(
        container,
        "select concat_ws('|',"
        "(select failure_retryable::text from private.scheduler_job_runs "
        f"where run_id={sql_text(str(execution['run']['run_id']))}::uuid),"
        "(select failure_retryable::text from private.scheduler_job_runs "
        f"where run_id={sql_text(str(settlement['run']['run_id']))}::uuid),"
        "(select count(*) from private.scheduler_job_leases where run_id in ("
        f"{sql_text(str(reconciliation['run']['run_id']))}::uuid,"
        f"{sql_text(str(outbox['run']['run_id']))}::uuid)))",
    )
    if retry_flags != "false|false|4":
        raise VerificationError(f"job-specific retry evidence mismatch: {retry_flags}")
    print(
        "PASS effectful lease expiry resolution boundary, job_specific_retry_matrix and "
        "completion_exact_revision_and_idempotency"
    )


def verify_concurrent_single_claim(
    container: str,
) -> None:
    account_id = "scheduler-concurrent-claim"
    holder_id = CONCURRENT_CLAIM_HOLDER_ID
    outer = acquire_outer_lease(
        container,
        account_id=account_id,
        holder_id=holder_id,
    )
    outer_token = int(outer["fencing_token"])
    ensure_definition(
        container,
        outer_token,
        definition_spec(
            "operations.outbox",
            interval_seconds=300,
            lease_ttl_seconds=30,
        ),
        account_id=account_id,
        holder_id=holder_id,
    )

    def concurrent_claim() -> dict[str, Any]:
        value = _service_rpc(
            container,
            holder_id,
            _claim_sql(
                outer_token,
                account_id=account_id,
                holder_id=holder_id,
            ),
        )
        validate_claim(value, account_id=account_id)
        return value

    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(lambda _: concurrent_claim(), range(4)))
    winners = [
        validate_claim(receipt, account_id=account_id)
        for receipt in receipts
        if receipt["claimed"]
    ]
    if len(winners) != 1 or winners[0] is None:
        raise VerificationError(f"concurrent due claim winner mismatch: {receipts}")
    winner = winners[0]
    if winner["run"]["job_key"] != "operations.outbox":
        raise VerificationError(f"unexpected concurrent claim winner: {winner}")
    if len({receipt["claim"]["run"]["run_id"] for receipt in receipts if receipt["claimed"]}) != 1:
        raise VerificationError(f"concurrent calls created duplicate runs: {receipts}")
    complete_run(
        container,
        outer_token,
        winner,
        "2" * 64,
        account_id=account_id,
        holder_id=holder_id,
    )
    invariant = scalar(
        container,
        "select concat_ws('|',"
        "(select coalesce(max(active_count),0) from ("
        "select definition_id,count(*) as active_count "
        "from private.scheduler_job_runs "
        f"where account_id={sql_text(account_id)} "
        "and state in ('pending','leased','retry_wait') group by definition_id) active),"
        "(select count(*)-count(distinct run_id) from private.scheduler_job_runs "
        f"where account_id={sql_text(account_id)}),"
        "(select count(*) from private.scheduler_job_definitions "
        f"where account_id={sql_text(account_id)}))",
    )
    if invariant != "0|0|1":
        raise VerificationError(f"concurrent single-active invariant mismatch: {invariant}")
    print("PASS concurrent_single_claim and one-active-run uniqueness")


def verify_effectful_explicit_failure_is_not_retryable(container: str) -> None:
    account_id = "contract-test-primary"
    holder_id = OTHER_HOLDER_ID
    release_sha = OTHER_RELEASE_SHA
    outer = acquire_outer_lease(
        container,
        account_id=account_id,
        holder_id=holder_id,
        release_sha=release_sha,
    )
    outer_token = int(outer["fencing_token"])
    specs = (
        definition_spec(
            "operations.commands",
            interval_seconds=300,
            lease_ttl_seconds=30,
        ),
        definition_spec("operations.execution", lease_ttl_seconds=30),
        definition_spec("operations.settlement", lease_ttl_seconds=30),
        definition_spec(
            "operations.reconciliation",
            interval_seconds=300,
            lease_ttl_seconds=30,
        ),
    )
    for spec in specs:
        ensure_definition(
            container,
            outer_token,
            spec,
            account_id=account_id,
            holder_id=holder_id,
            release_sha=release_sha,
        )

    command_receipt = claim_due(
        container,
        outer_token,
        account_id=account_id,
        holder_id=holder_id,
        release_sha=release_sha,
    )
    command = validate_claim(command_receipt, account_id=account_id)
    if command is None or command["run"]["job_key"] != "operations.commands":
        raise VerificationError(f"secondary command barrier fixture mismatch: {command_receipt}")
    complete_run(
        container,
        outer_token,
        command,
        "7" * 64,
        account_id=account_id,
        holder_id=holder_id,
        release_sha=release_sha,
    )

    failed_runs: list[str] = []
    for job_key, reason_code, failure_sha in (
        ("operations.execution", "execution_failure_explicit", "8" * 64),
        ("operations.settlement", "settlement_failure_explicit", "9" * 64),
    ):
        receipt = claim_due(
            container,
            outer_token,
            account_id=account_id,
            holder_id=holder_id,
            release_sha=release_sha,
        )
        claim = validate_claim(receipt, account_id=account_id)
        if claim is None or claim["run"]["job_key"] != job_key:
            raise VerificationError(f"secondary effectful claim mismatch: {receipt}")
        failure = fail_run(
            container,
            outer_token,
            claim,
            reason_code,
            failure_sha,
            True,
            account_id=account_id,
            holder_id=holder_id,
            release_sha=release_sha,
        )
        if failure["state"] != "dead_letter" or failure["next_attempt_at"] is not None:
            raise VerificationError(f"effectful explicit failure retried: {failure}")
        inspection = inspect_dead_letter(
            container,
            outer_token,
            str(claim["run"]["run_id"]),
            account_id=account_id,
            holder_id=holder_id,
            release_sha=release_sha,
        )
        if (
            inspection["eligible"] is not False
            or inspection["ineligibility_reason"]
            != "effectful_job_requires_resolution_evidence"
        ):
            raise VerificationError(f"effectful explicit failure became replayable: {inspection}")
        failed_runs.append(str(claim["run"]["run_id"]))

    flags = scalar(
        container,
        "select pg_catalog.string_agg(failure_retryable::text,'|' order by job_key) "
        "from private.scheduler_job_runs where run_id in ("
        + ",".join(f"{sql_text(run_id)}::uuid" for run_id in failed_runs)
        + ");",
    )
    if flags != "false|false":
        raise VerificationError(f"effectful explicit retry flags mismatch: {flags}")
    print("PASS execution/settlement explicit failures never auto-retry")


def verify_command_retry_and_manual_replay_budget(
    container: str,
    previous_outer: dict[str, Any],
) -> dict[str, Any]:
    outer = acquire_outer_lease(container)
    outer_token = int(outer["fencing_token"])
    if outer_token <= int(previous_outer["fencing_token"]):
        raise VerificationError("command retry startup fencing token did not advance")
    forced_receipt = claim_due(container, outer_token)
    command = validate_claim(forced_receipt)
    if command is None or command["run"]["job_key"] != "operations.commands":
        raise VerificationError(f"command startup retry fixture mismatch: {forced_receipt}")

    first_failure = fail_run(
        container,
        outer_token,
        command,
        "command_poll_retryable",
        "a" * 64,
        True,
    )
    if first_failure["state"] != "retry_wait":
        raise VerificationError(f"command retry reason was not bounded: {first_failure}")
    repeated = fail_run(
        container,
        outer_token,
        command,
        "command_poll_retryable",
        "a" * 64,
        True,
    )
    if any(
        repeated[field] != first_failure[field]
        for field in SETTLEMENT_FIELDS - {"observed_at"}
    ):
        raise VerificationError(f"command failure receipt was not idempotent: {repeated}")
    _service_failure(
        container,
        HOLDER_ID,
        _fail_sql(
            outer_token,
            command,
            "command_poll_retryable",
            "b" * 64,
            True,
        ),
        "scheduler_failure_compare_and_swap_failed",
    )

    run_id = str(command["run"]["run_id"])
    wait_for_run_deadline(container, run_id, "available_at")
    retry_receipt = claim_due(container, outer_token)
    retry_claim = validate_claim(retry_receipt)
    if (
        retry_claim is None
        or retry_claim["run"]["run_id"] != run_id
        or retry_claim["run"]["attempt_count"] != 2
    ):
        raise VerificationError(f"command retry claim mismatch: {retry_receipt}")
    terminal = fail_run(
        container,
        outer_token,
        retry_claim,
        "command_poll_retryable",
        "c" * 64,
        True,
    )
    if terminal["state"] != "dead_letter":
        raise VerificationError(f"command retry budget did not terminate: {terminal}")
    inspection = inspect_dead_letter(container, outer_token, run_id)
    dead_letter = inspection["dead_letter"]
    if inspection["eligible"] is not True or type(dead_letter) is not dict:
        raise VerificationError(f"command dead letter was not replayable: {inspection}")

    replay_request_id = str(uuid4())
    replay = replay_dead_letter(
        container,
        outer_token,
        dead_letter,
        replay_request_id,
    )
    if replay["state"] != "pending" or replay["replay_generation"] != 1:
        raise VerificationError(f"command replay generation mismatch: {replay}")
    _service_failure(
        container,
        HOLDER_ID,
        _replay_sql(outer_token, dead_letter, str(uuid4())),
        "scheduler_replay_compare_and_swap_failed",
    )

    child_receipt = claim_due(container, outer_token)
    child = validate_claim(child_receipt)
    if child is None or child["run"]["run_id"] != replay["new_run_id"]:
        raise VerificationError(f"command replay child claim mismatch: {child_receipt}")
    child_first = fail_run(
        container,
        outer_token,
        child,
        "command_poll_retryable",
        "d" * 64,
        True,
    )
    if child_first["state"] != "retry_wait":
        raise VerificationError(f"replay child retry mismatch: {child_first}")
    wait_for_run_deadline(container, str(replay["new_run_id"]), "available_at")
    child_retry_receipt = claim_due(container, outer_token)
    child_retry = validate_claim(child_retry_receipt)
    if (
        child_retry is None
        or child_retry["run"]["run_id"] != replay["new_run_id"]
        or child_retry["run"]["attempt_count"] != 2
    ):
        raise VerificationError(f"replay child retry claim mismatch: {child_retry_receipt}")
    child_terminal = fail_run(
        container,
        outer_token,
        child_retry,
        "command_poll_retryable",
        "e" * 64,
        True,
    )
    if child_terminal["state"] != "dead_letter":
        raise VerificationError(f"replay child budget did not terminate: {child_terminal}")
    child_inspection = inspect_dead_letter(
        container,
        outer_token,
        str(replay["new_run_id"]),
    )
    if (
        child_inspection["eligible"] is not False
        or child_inspection["ineligibility_reason"] != "manual_replay_budget_exhausted"
    ):
        raise VerificationError(f"manual replay budget was not exhausted: {child_inspection}")

    rebudgeted = definition_spec(
        "operations.commands",
        interval_seconds=300,
        lease_ttl_seconds=10,
        retry_base_seconds=1,
        max_manual_replays=2,
    )
    _service_failure(
        container,
        HOLDER_ID,
        _ensure_sql(rebudgeted).format(outer_token=outer_token),
        "scheduler_definition_change_requires_quiescence",
    )
    child_dead_letter = child_inspection["dead_letter"]
    if type(child_dead_letter) is not dict:
        raise VerificationError("budget-exhausted child omitted dead-letter evidence")
    _service_failure(
        container,
        HOLDER_ID,
        _replay_sql(outer_token, child_dead_letter, str(uuid4())),
        "scheduler_replay_compare_and_swap_failed",
    )
    expect_failure(
        container,
        "update private.scheduler_job_runs set updated_at=updated_at "
        f"where run_id={sql_text(str(replay['new_run_id']))}::uuid;",
        "scheduler_terminal_run_is_immutable",
    )
    print("PASS commands bounded retry, second replay and rebudget refusal")
    return outer


def verify_outer_lease_time_and_budget_boundaries(container: str) -> None:
    budget_account = "scheduler-lease-budget"
    budget_holder = LEASE_BUDGET_HOLDER_ID
    long_outer = acquire_outer_lease(
        container,
        account_id=budget_account,
        holder_id=budget_holder,
        ttl_seconds=60,
    )
    long_token = int(long_outer["fencing_token"])
    invalid_ttl_spec = definition_spec(
        "operations.settlement",
        lease_ttl_seconds=9,
    )
    _service_failure(
        container,
        budget_holder,
        _ensure_sql(
            invalid_ttl_spec,
            account_id=budget_account,
            holder_id=budget_holder,
        ).format(outer_token=long_token),
        "scheduler_definition_parameters_invalid",
    )
    invalid_rows = scalar(
        container,
        "select count(*) from private.scheduler_job_definitions "
        f"where account_id={sql_text(budget_account)} "
        "and job_key='operations.settlement';",
    )
    if invalid_rows != "0":
        raise VerificationError("lease TTL 9 created a scheduler definition")

    retry_spec = definition_spec(
        "operations.outbox",
        interval_seconds=300,
        lease_ttl_seconds=10,
        max_attempts=2,
        retry_base_seconds=1,
        retry_max_seconds=1,
    )
    ensure_definition(
        container,
        long_token,
        retry_spec,
        account_id=budget_account,
        holder_id=budget_holder,
    )
    first_receipt = claim_due(
        container,
        long_token,
        account_id=budget_account,
        holder_id=budget_holder,
    )
    first_claim = validate_claim(first_receipt, account_id=budget_account)
    if first_claim is None or first_claim["run"]["job_key"] != "operations.outbox":
        raise VerificationError(f"outer budget fixture claim mismatch: {first_receipt}")
    retry_receipt = fail_run(
        container,
        long_token,
        first_claim,
        "outbox_poll_retryable",
        "6" * 64,
        True,
        account_id=budget_account,
        holder_id=budget_holder,
    )
    if retry_receipt["state"] != "retry_wait":
        raise VerificationError(f"outer budget retry fixture mismatch: {retry_receipt}")
    run_id = str(first_claim["run"]["run_id"])
    wait_for_run_deadline(container, run_id, "available_at")

    short_outer = acquire_outer_lease(
        container,
        account_id=budget_account,
        holder_id=budget_holder,
        ttl_seconds=9,
    )
    short_token = int(short_outer["fencing_token"])
    state_before = run_snapshot(container, run_id)
    lease_count_before = scalar(
        container,
        "select count(*) from private.scheduler_job_leases "
        f"where run_id={sql_text(run_id)}::uuid;",
    )
    idle = claim_due(
        container,
        short_token,
        account_id=budget_account,
        holder_id=budget_holder,
    )
    if idle["claimed"] is not False:
        raise VerificationError(f"nine-second outer lease issued work: {idle}")
    waiting = converge_definition(
        container,
        short_token,
        retry_spec,
        account_id=budget_account,
        holder_id=budget_holder,
    )
    _require_convergence_status(waiting, "wait")
    lease_count_after = scalar(
        container,
        "select count(*) from private.scheduler_job_leases "
        f"where run_id={sql_text(run_id)}::uuid;",
    )
    if (
        waiting["active_run_id"] != run_id
        or waiting["reason_code"] != "scheduler_outer_lease_renewal_required"
        or _timestamp(waiting["next_eligible_at"])
        != _timestamp(short_outer["expires_at"])
        or run_snapshot(container, run_id) != state_before
        or lease_count_after != lease_count_before
    ):
        raise VerificationError(
            f"outer renewal wait mutated durable work: {waiting}"
        )
    renewed_outer = acquire_outer_lease(
        container,
        account_id=budget_account,
        holder_id=budget_holder,
        ttl_seconds=60,
    )
    renewed_token = int(renewed_outer["fencing_token"])
    resumed = converge_definition(
        container,
        renewed_token,
        retry_spec,
        account_id=budget_account,
        holder_id=budget_holder,
    )
    _require_convergence_status(resumed, "claimed")
    resumed_claim = resumed["claim"]
    if (
        resumed_claim["run"]["run_id"] != run_id
        or resumed_claim["run"]["attempt_count"] != 2
    ):
        raise VerificationError(f"renewed outer did not resume work: {resumed}")
    complete_run(
        container,
        renewed_token,
        resumed_claim,
        "7" * 64,
        account_id=budget_account,
        holder_id=budget_holder,
    )

    future_account = "scheduler-future-outer"
    future_holder = FUTURE_OUTER_HOLDER_ID
    future_outer = _service_rpc(
        container,
        future_holder,
        "worker_api.acquire_worker_lease("
        f"{sql_text(future_account)},{sql_text(future_holder)},"
        "pg_catalog.clock_timestamp()+interval '12 seconds',30,"
        f"{sql_text(RELEASE_SHA)})",
    )
    required = {"account_id", "holder_id", "fencing_token", "acquired_at", "expires_at"}
    if (
        set(future_outer) != required
        or future_outer["account_id"] != future_account
        or future_outer["holder_id"] != future_holder
        or _timestamp(future_outer["acquired_at"]) <= database_now(container)
    ):
        raise VerificationError(f"future outer fixture mismatch: {future_outer}")
    future_token = int(future_outer["fencing_token"])
    command_spec = definition_spec(
        "operations.commands",
        interval_seconds=300,
        lease_ttl_seconds=10,
    )
    disabled_outbox = definition_spec(
        "operations.outbox",
        enabled=False,
        interval_seconds=300,
        lease_ttl_seconds=10,
    )
    _service_failure(
        container,
        future_holder,
        _ensure_sql(
            command_spec,
            account_id=future_account,
            holder_id=future_holder,
        ).format(outer_token=future_token),
        "scheduler_outer_lease_stale_or_missing",
    )
    _service_failure(
        container,
        future_holder,
        _converge_sql(
            disabled_outbox,
            future_token,
            account_id=future_account,
            holder_id=future_holder,
        ),
        "scheduler_outer_lease_stale_or_missing",
    )
    _service_failure(
        container,
        future_holder,
        _claim_sql(
            future_token,
            account_id=future_account,
            holder_id=future_holder,
        ),
        "scheduler_outer_lease_stale_or_missing",
    )
    future_mutations = scalar(
        container,
        "select concat_ws('|',"
        "(select count(*) from private.scheduler_job_definitions "
        f"where account_id={sql_text(future_account)}),"
        "(select count(*) from private.scheduler_job_runs "
        f"where account_id={sql_text(future_account)}))",
    )
    if future_mutations != "0|0":
        raise VerificationError(
            f"future outer lease mutated scheduler state: {future_mutations}"
        )
    psql(
        container,
        "select pg_catalog.pg_sleep(least(greatest(extract(epoch from ("
        "acquired_at-pg_catalog.clock_timestamp())),0)+0.25,15)) "
        "from private.worker_leases "
        f"where account_id={sql_text(future_account)};",
    )
    ensure_definition(
        container,
        future_token,
        command_spec,
        account_id=future_account,
        holder_id=future_holder,
    )
    converged = converge_definition(
        container,
        future_token,
        disabled_outbox,
        account_id=future_account,
        holder_id=future_holder,
    )
    _require_convergence_status(converged, "converged")
    command_receipt = claim_due(
        container,
        future_token,
        account_id=future_account,
        holder_id=future_holder,
    )
    command = validate_claim(command_receipt, account_id=future_account)
    if command is None or command["run"]["job_key"] != "operations.commands":
        raise VerificationError(
            f"future outer lease did not become valid: {command_receipt}"
        )
    complete_run(
        container,
        future_token,
        command,
        "8" * 64,
        account_id=future_account,
        holder_id=future_holder,
    )
    print(
        "PASS minimum_scheduler_lease_ttl, outer_lease_remaining_budget and "
        "future_outer_lease_not_yet_valid"
    )


def _wait_for_lock_fixture(container: str, marker: str) -> None:
    for _ in range(LOCK_FIXTURE_START_POLL_ATTEMPTS):
        active = scalar(
            container,
            "select count(*) from pg_catalog.pg_stat_activity "
            "where pid<>pg_catalog.pg_backend_pid() and state='active' "
            "and wait_event='PgSleep' and query like "
            f"{sql_text('%' + marker + '%')};",
        )
        if active == "1":
            return
        time.sleep(LOCK_FIXTURE_START_POLL_SECONDS)
    raise VerificationError(f"lock fixture did not become active: {marker}")


def verify_lock_wait_clock_revalidation(
    container: str,
    previous_outer: dict[str, Any],
    outbox_spec: dict[str, object],
) -> dict[str, Any]:
    short_outer = acquire_outer_lease(container, ttl_seconds=5)
    if int(short_outer["fencing_token"]) <= int(previous_outer["fencing_token"]):
        raise VerificationError("short lock-wait outer lease did not advance")
    short_token = int(short_outer["fencing_token"])
    definition_before = scalar(
        container,
        "select concat_ws('|',revision,definition_sha256,interval_seconds) "
        "from private.scheduler_job_definitions where account_id='paper-primary' "
        "and job_key='operations.outbox';",
    )
    changed = dict(outbox_spec)
    changed["interval_seconds"] = int(outbox_spec["interval_seconds"]) + 1

    definition_marker = "scheduler_definition_lock_fixture"
    with ThreadPoolExecutor(max_workers=1) as pool:
        lock_future = pool.submit(
            psql,
            container,
            "begin; select definition_id from private.scheduler_job_definitions "
            "where account_id='paper-primary' and job_key='operations.outbox' "
            "for update; select pg_catalog.pg_sleep(6) "
            f"/* {definition_marker} */; commit;",
        )
        _wait_for_lock_fixture(container, definition_marker)
        _service_failure(
            container,
            HOLDER_ID,
            _ensure_sql(changed).format(outer_token=short_token),
            "scheduler_outer_lease_stale_or_missing",
        )
        lock_future.result()
    definition_after = scalar(
        container,
        "select concat_ws('|',revision,definition_sha256,interval_seconds) "
        "from private.scheduler_job_definitions where account_id='paper-primary' "
        "and job_key='operations.outbox';",
    )
    if definition_after != definition_before:
        raise VerificationError(
            "lock-wait definition mutation committed with an expired outer lease"
        )

    inner_outer = acquire_outer_lease(container, ttl_seconds=12)
    inner_token = int(inner_outer["fencing_token"])
    claim_receipt = claim_due(container, inner_token)
    claim = validate_claim(claim_receipt)
    if claim is None or claim["run"]["job_key"] != "operations.outbox":
        raise VerificationError(f"inner lock-wait fixture claim mismatch: {claim_receipt}")
    if _timestamp(claim["lease"]["lease_expires_at"]) > _timestamp(
        inner_outer["expires_at"]
    ):
        raise VerificationError("lock-wait fixture inner lease exceeded outer expiry")
    run_id = str(claim["run"]["run_id"])
    run_before = run_snapshot(container, run_id)
    run_marker = "scheduler_run_lock_fixture"
    with ThreadPoolExecutor(max_workers=1) as pool:
        lock_future = pool.submit(
            psql,
            container,
            "begin; select run_id from private.scheduler_job_runs "
            f"where run_id={sql_text(run_id)}::uuid for update; "
            "select pg_catalog.pg_sleep(13) "
            f"/* {run_marker} */; commit;",
        )
        _wait_for_lock_fixture(container, run_marker)
        _service_failure(
            container,
            HOLDER_ID,
            _complete_sql(inner_token, claim, "f" * 64),
            "scheduler_outer_lease_stale_or_missing",
        )
        lock_future.result()
    if run_snapshot(container, run_id) != run_before:
        raise VerificationError("lock-wait completion mutated an expired inner lease")

    current_outer = acquire_outer_lease(container)
    current_token = int(current_outer["fencing_token"])
    expiry_probe = claim_due(container, current_token)
    if expiry_probe["claimed"] is not False:
        raise VerificationError(f"expired outbox skipped retry wait: {expiry_probe}")
    retry_state = run_snapshot(container, run_id)
    if retry_state["state"] != "retry_wait" or retry_state["failure_retryable"] is not True:
        raise VerificationError(f"lock-wait expired run retry mismatch: {retry_state}")
    wait_for_run_deadline(container, run_id, "available_at")
    retry_receipt = claim_due(container, current_token)
    retry_claim = validate_claim(retry_receipt)
    if retry_claim is None or retry_claim["run"]["run_id"] != run_id:
        raise VerificationError(f"lock-wait retry claim mismatch: {retry_receipt}")
    complete_run(container, current_token, retry_claim, "f" * 64)
    print("PASS lock_wait_clock_revalidation and outer/inner lease CAS")
    return current_outer


def verify_security_contract(container: str) -> None:
    catalog = scalar(
        container,
        "select concat_ws('|',"
        "(select count(*) from pg_catalog.pg_class relation where relation.oid in ("
        "'private.scheduler_job_definitions'::regclass,"
        "'private.scheduler_job_runs'::regclass,"
        "'private.scheduler_job_leases'::regclass,"
        "'private.scheduler_replay_requests'::regclass) "
        "and relation.relrowsecurity and relation.relforcerowsecurity),"
        "(select count(*) from pg_catalog.pg_policy policy where policy.polrelid in ("
        "'private.scheduler_job_definitions'::regclass,"
        "'private.scheduler_job_runs'::regclass,"
        "'private.scheduler_job_leases'::regclass,"
        "'private.scheduler_replay_requests'::regclass)),"
        "(select count(*) from pg_catalog.pg_proc procedure "
        "join pg_catalog.pg_namespace namespace on namespace.oid=procedure.pronamespace "
        "where namespace.nspname='worker_api' and procedure.proname like '%scheduler%'),"
        "(select count(*) from pg_catalog.pg_proc procedure "
        "join pg_catalog.pg_namespace namespace on namespace.oid=procedure.pronamespace "
        "where namespace.nspname='worker_api' and procedure.proname like '%scheduler%' "
        "and not procedure.prosecdef and procedure.provolatile='v' "
        "and procedure.proconfig=array['search_path=\"\"']::text[]),"
        "(select count(*) from pg_catalog.pg_proc procedure "
        "join pg_catalog.pg_namespace namespace on namespace.oid=procedure.pronamespace "
        "where namespace.nspname='private' "
        "and procedure.proname in ("
        "'ensure_scheduler_job_definition_impl',"
        "'converge_scheduler_job_definition_impl','claim_due_scheduler_job_impl',"
        "'complete_scheduler_job_run_impl','fail_scheduler_job_run_impl',"
        "'inspect_scheduler_dead_letter_impl','replay_scheduler_dead_letter_impl') "
        "and procedure.prosecdef and procedure.provolatile='v' "
        "and procedure.proconfig=array['search_path=\"\"']::text[]),"
        "(select count(*) from pg_catalog.pg_proc procedure "
        "join pg_catalog.pg_namespace namespace on namespace.oid=procedure.pronamespace "
        "where namespace.nspname='api' and procedure.proname like '%scheduler%'),"
        "(select count(*) from pg_catalog.pg_class relation "
        "cross join lateral pg_catalog.aclexplode(coalesce(relation.relacl,"
        "pg_catalog.acldefault('r',relation.relowner))) acl "
        "left join pg_catalog.pg_roles grantee on grantee.oid=acl.grantee "
        "where relation.oid in ('private.scheduler_job_definitions'::regclass,"
        "'private.scheduler_job_runs'::regclass,"
        "'private.scheduler_job_leases'::regclass,"
        "'private.scheduler_replay_requests'::regclass) "
        "and (acl.grantee=0 or grantee.rolname in "
        "('anon','authenticated','authenticator','service_role'))),"
        "(select count(*) from pg_catalog.pg_proc procedure "
        "join pg_catalog.pg_namespace namespace on namespace.oid=procedure.pronamespace "
        "where namespace.nspname='private' and procedure.proname in ("
        "'scheduler_definition_sha256_v1','require_scheduler_outer_lease_v1',"
        "'scheduler_command_barrier_satisfied_v1','scheduler_definition_document_v1',"
        "'scheduler_run_document_v1','guard_scheduler_job_definition_v1',"
        "'guard_scheduler_job_run_v1') and ("
        "pg_catalog.has_function_privilege('service_role',procedure.oid,'EXECUTE') or "
        "pg_catalog.has_function_privilege('authenticated',procedure.oid,'EXECUTE') or "
        "pg_catalog.has_function_privilege('anon',procedure.oid,'EXECUTE') or exists ("
        "select 1 from pg_catalog.aclexplode(coalesce(procedure.proacl,"
        "pg_catalog.acldefault('f',procedure.proowner))) acl "
        "where acl.grantee=0 and acl.privilege_type='EXECUTE'))))",
    )
    if catalog != "4|0|7|7|7|0|0|0":
        raise VerificationError(f"scheduler security catalog mismatch: {catalog}")

    execute_matrix = scalar(
        container,
        "select concat_ws('|',"
        "(select count(*) from pg_catalog.pg_proc procedure "
        "join pg_catalog.pg_namespace namespace on namespace.oid=procedure.pronamespace "
        "where namespace.nspname='worker_api' and procedure.proname like '%scheduler%' "
        "and pg_catalog.has_function_privilege('service_role',procedure.oid,'EXECUTE')),"
        "(select count(*) from pg_catalog.pg_proc procedure "
        "join pg_catalog.pg_namespace namespace on namespace.oid=procedure.pronamespace "
        "where namespace.nspname='worker_api' and procedure.proname like '%scheduler%' "
        "and pg_catalog.has_function_privilege('authenticated',procedure.oid,'EXECUTE')),"
        "(select count(*) from pg_catalog.pg_proc procedure "
        "join pg_catalog.pg_namespace namespace on namespace.oid=procedure.pronamespace "
        "where namespace.nspname='worker_api' and procedure.proname like '%scheduler%' "
        "and pg_catalog.has_function_privilege('anon',procedure.oid,'EXECUTE')))",
    )
    if execute_matrix != "7|0|0":
        raise VerificationError(f"scheduler RPC ACL mismatch: {execute_matrix}")

    owner_contract = scalar(
        container,
        "with contract_object(owner_oid) as ("
        "select relation.relowner from pg_catalog.pg_class relation "
        "where relation.oid in ('private.scheduler_job_definitions'::regclass,"
        "'private.scheduler_job_runs'::regclass,"
        "'private.scheduler_job_leases'::regclass,"
        "'private.scheduler_replay_requests'::regclass) union all "
        "select procedure.proowner from pg_catalog.pg_proc procedure "
        "join pg_catalog.pg_namespace namespace on namespace.oid=procedure.pronamespace "
        "where (namespace.nspname='private' and procedure.proname in ("
        "'scheduler_definition_sha256_v1','require_scheduler_outer_lease_v1',"
        "'scheduler_command_barrier_satisfied_v1','scheduler_definition_document_v1',"
        "'scheduler_run_document_v1','guard_scheduler_job_definition_v1',"
        "'guard_scheduler_job_run_v1','ensure_scheduler_job_definition_impl',"
        "'converge_scheduler_job_definition_impl','claim_due_scheduler_job_impl',"
        "'complete_scheduler_job_run_impl','fail_scheduler_job_run_impl',"
        "'inspect_scheduler_dead_letter_impl','replay_scheduler_dead_letter_impl')) "
        "or (namespace.nspname='worker_api' and procedure.proname in ("
        "'ensure_scheduler_job_definition','converge_scheduler_job_definition',"
        "'claim_due_scheduler_job','complete_scheduler_job_run',"
        "'fail_scheduler_job_run','inspect_scheduler_dead_letter',"
        "'replay_scheduler_dead_letter'))) "
        "select concat_ws('|',count(*),count(distinct contract_object.owner_oid),"
        "count(*) filter (where owner.rolname in ("
        "'anon','authenticated','authenticator','service_role')),"
        "pg_catalog.bool_and(owner.rolsuper or owner.rolbypassrls)) "
        "from contract_object join pg_catalog.pg_roles owner "
        "on owner.oid=contract_object.owner_oid;",
    )
    if owner_contract != "25|1|0|t":
        raise VerificationError(
            f"scheduler trusted owner catalog mismatch: {owner_contract}"
        )

    expect_failure(
        container,
        jwt_claim_sql(HOLDER_ID, role="service_role")
        + "\nselect count(*) from private.scheduler_job_runs;",
        "permission denied",
    )
    expect_failure(
        container,
        jwt_claim_sql(HOLDER_ID, role="authenticated")
        + "\nselect * from worker_api.claim_due_scheduler_job("
        f"{sql_text(ACCOUNT_ID)},{sql_text(HOLDER_ID)},1,{sql_text(RELEASE_SHA)});",
        "permission denied",
    )
    for role in ("service_role", "authenticated", "anon"):
        expect_failure(
            container,
            jwt_claim_sql(HOLDER_ID, role=role)
            + "\nselect private.scheduler_definition_sha256_v1("
            "'operations.commands',1,1,1,1,1,0,true);",
            "permission denied",
        )
    lease_id = scalar(
        container,
        "select lease_token::text from private.scheduler_job_leases order by leased_at limit 1;",
    )
    expect_failure(
        container,
        "update private.scheduler_job_leases set leased_at=leased_at "
        f"where lease_token={sql_text(lease_id)}::uuid;",
        "append_only",
    )
    print(
        "PASS rls_acl_search_path_catalog, single_trusted_owner_catalog and "
        "append-only lease/replay ledgers"
    )


def verify_zero_side_effects(container: str, before: str) -> None:
    after = domain_snapshot(container)
    if after != before:
        raise VerificationError(
            f"scheduler changed trading/order domain rows: before={before}, after={after}"
        )
    print("PASS zero_trading_order_side_effects")


def scheduler_conflict_patch_snapshot(container: str) -> dict[str, Any]:
    function_values = ",".join(
        f"({sql_text(signature)},{sql_text(signature)}::regprocedure)"
        for signature in CONFLICT_PATCH_FUNCTIONS
    )
    value = json.loads(
        scalar(
            container,
            f"""
with target_function(expected_identity, function_oid) as (
  values {function_values}
)
select pg_catalog.jsonb_build_object(
  'functions', (
    select pg_catalog.jsonb_agg(
      pg_catalog.jsonb_build_object(
        'identity', target_function.expected_identity,
        'catalog_identity', procedure.oid::regprocedure::text,
        'oid', procedure.oid,
        'owner', procedure.proowner,
        'acl', procedure.proacl,
        'security_definer', procedure.prosecdef,
        'volatility', procedure.provolatile,
        'config', procedure.proconfig,
        'source_sha256', pg_catalog.encode(
          extensions.digest(
            pg_catalog.convert_to(
              pg_catalog.pg_get_functiondef(procedure.oid),
              'UTF8'
            ),
            'sha256'
          ),
          'hex'
        ),
        'legacy_occurrences', (
          pg_catalog.length(pg_catalog.pg_get_functiondef(procedure.oid))
          - pg_catalog.length(
              pg_catalog.replace(
                pg_catalog.pg_get_functiondef(procedure.oid),
                {sql_text(LEGACY_CONFLICT_FRAGMENT)},
                ''
              )
            )
        ) / pg_catalog.length({sql_text(LEGACY_CONFLICT_FRAGMENT)}),
        'constraint_occurrences', (
          pg_catalog.length(pg_catalog.pg_get_functiondef(procedure.oid))
          - pg_catalog.length(
              pg_catalog.replace(
                pg_catalog.pg_get_functiondef(procedure.oid),
                {sql_text(CONSTRAINT_CONFLICT_FRAGMENT)},
                ''
              )
            )
        ) / pg_catalog.length({sql_text(CONSTRAINT_CONFLICT_FRAGMENT)})
      )
      order by target_function.expected_identity
    )
    from target_function
    join pg_catalog.pg_proc as procedure
      on procedure.oid = target_function.function_oid
  ),
  'constraint', (
    select pg_catalog.jsonb_build_object(
      'oid', constraint_record.oid,
      'relation_oid', constraint_record.conrelid,
      'name', constraint_record.conname,
      'type', constraint_record.contype,
      'definition', pg_catalog.pg_get_constraintdef(
        constraint_record.oid,
        false
      )
    )
    from pg_catalog.pg_constraint as constraint_record
    where constraint_record.conrelid =
          'private.scheduler_job_definitions'::regclass
      and constraint_record.conname =
          'scheduler_job_definitions_account_id_job_key_key'
  )
)::text;
""",
        )
    )
    if type(value) is not dict:
        raise VerificationError(f"conflict patch snapshot must be an object: {value!r}")
    functions = value.get("functions")
    constraint = value.get("constraint")
    if (
        not isinstance(functions, list)
        or len(functions) != 2
        or any(type(function) is not dict for function in functions)
    ):
        raise VerificationError(f"conflict patch functions are incomplete: {value!r}")
    if not isinstance(constraint, dict):
        raise VerificationError(f"conflict patch constraint is incomplete: {value!r}")
    return value


def conflict_patch_metadata(snapshot: dict[str, Any]) -> dict[str, Any]:
    functions = snapshot["functions"]
    if not isinstance(functions, list):
        raise VerificationError("conflict patch metadata functions must be a list")
    return {
        "functions": [
            {
                key: value
                for key, value in function.items()
                if key
                not in {
                    "source_sha256",
                    "legacy_occurrences",
                    "constraint_occurrences",
                }
            }
            for function in functions
        ],
        "constraint": snapshot["constraint"],
    }


def verify_conflict_patch_transition(
    before: dict[str, Any],
    after: dict[str, Any],
) -> None:
    if conflict_patch_metadata(before) != conflict_patch_metadata(after):
        raise VerificationError("conflict patch changed function or constraint metadata")
    before_functions = before["functions"]
    after_functions = after["functions"]
    if not isinstance(before_functions, list) or not isinstance(after_functions, list):
        raise VerificationError("conflict patch transition functions must be lists")
    for before_function, after_function in zip(
        before_functions,
        after_functions,
        strict=True,
    ):
        if (
            before_function["legacy_occurrences"] != 1
            or before_function["constraint_occurrences"] != 0
            or after_function["legacy_occurrences"] != 0
            or after_function["constraint_occurrences"] != 1
            or before_function["source_sha256"] == after_function["source_sha256"]
        ):
            raise VerificationError(
                "conflict patch source transition mismatch: "
                f"before={before_function!r}, after={after_function!r}"
            )
    print("PASS conflict target catalog transition and metadata preservation")


def verify_conflict_target_drift_rollback(container: str) -> None:
    baseline = scheduler_conflict_patch_snapshot(container)
    drift_targets = ",".join(
        f"{sql_text(signature)}::regprocedure"
        for signature in CONFLICT_PATCH_FUNCTIONS
    )
    duplicate_target = sql_text(CONFLICT_PATCH_FUNCTIONS[1])
    psql(
        container,
        f"""
do $drift$
declare
  target_functions constant regprocedure[] := array[{drift_targets}];
  duplicate_target constant regprocedure := {duplicate_target}::regprocedure;
  target_function regprocedure;
  function_definition text;
  replacement_fragment text;
  legacy_fragment constant text := {sql_text(LEGACY_CONFLICT_FRAGMENT)};
  constraint_fragment constant text := {sql_text(CONSTRAINT_CONFLICT_FRAGMENT)};
begin
  foreach target_function in array target_functions loop
    function_definition := pg_catalog.pg_get_functiondef(target_function);
    if (
      pg_catalog.length(function_definition)
      - pg_catalog.length(
          pg_catalog.replace(function_definition, constraint_fragment, '')
        )
    ) / pg_catalog.length(constraint_fragment) <> 1
       or pg_catalog.strpos(function_definition, legacy_fragment) > 0 then
      raise exception 'durable_scheduler_conflict_drift_fixture_invalid';
    end if;
    replacement_fragment := legacy_fragment;
    if target_function = duplicate_target then
      replacement_fragment := replacement_fragment
        || E'\\n  /* '
        || legacy_fragment
        || ' */';
    end if;
    execute pg_catalog.replace(
      function_definition,
      constraint_fragment,
      replacement_fragment
    );
  end loop;
end;
$drift$;
""",
    )
    drifted = scheduler_conflict_patch_snapshot(container)
    if conflict_patch_metadata(drifted) != conflict_patch_metadata(baseline):
        raise VerificationError("drift fixture changed protected catalog metadata")
    drifted_functions = drifted["functions"]
    if not isinstance(drifted_functions, list):
        raise VerificationError("conflict patch drift functions must be a list")
    drift_receipts = {
        function["identity"]: (
            function["legacy_occurrences"],
            function["constraint_occurrences"],
        )
        for function in drifted_functions
    }
    target_identity = CONFLICT_PATCH_FUNCTIONS[1]
    expected_receipts = {
        CONFLICT_PATCH_FUNCTIONS[0]: (1, 0),
        target_identity: (2, 0),
    }
    if drift_receipts != expected_receipts:
        raise VerificationError(
            f"duplicate conflict drift fixture mismatch: {drift_receipts!r}"
        )

    fix_sql = (MIGRATIONS / CONFLICT_FIX_MIGRATION_NAME).read_text(encoding="utf-8")
    expect_failure(
        container,
        "\\set VERBOSITY verbose\n" + fix_sql,
        "23514",
        "durable_scheduler_conflict_patch_target_invalid",
    )
    after_failure = scheduler_conflict_patch_snapshot(container)
    if after_failure != drifted:
        raise VerificationError(
            "failed conflict patch did not roll back atomically: "
            f"before={drifted!r}, after={after_failure!r}"
        )
    print("PASS conflict_target_drift_rollback")


def verify_populated_upgrade(container: str) -> None:
    psql(container, bootstrap_sql())
    target = MIGRATIONS / MIGRATION_NAME
    conflict_fix = MIGRATIONS / CONFLICT_FIX_MIGRATION_NAME
    migrations = sorted(MIGRATIONS.glob("*.sql"))
    try:
        target_index = migrations.index(target)
        conflict_fix_index = migrations.index(conflict_fix)
    except ValueError as error:
        raise VerificationError("durable scheduler migration boundary is missing") from error
    if conflict_fix_index <= target_index:
        raise VerificationError("durable scheduler conflict fix boundary is invalid")
    for migration in migrations[:target_index]:
        psql(container, migration.read_text(encoding="utf-8"))
    psql(container, SEED.read_text(encoding="utf-8"))
    open_scheduler_account_fixtures(container)

    outer = acquire_outer_lease(container)
    before_domain = domain_snapshot(container)
    populated_before = scalar(
        container,
        "select concat_ws('|',"
        "(select count(*) from private.trading_accounts),"
        "(select count(*) from public.bot_settings),"
        "(select count(*) from private.worker_leases),"
        "(select holder_id from private.worker_leases "
        f"where account_id={sql_text(ACCOUNT_ID)}),"
        "(select fencing_token from private.worker_leases "
        f"where account_id={sql_text(ACCOUNT_ID)}),"
        "(select count(*) from public.orders),"
        "(select count(*) from private.order_intents))",
    )

    psql(container, target.read_text(encoding="utf-8"))
    for migration in migrations[target_index + 1 : conflict_fix_index]:
        psql(container, migration.read_text(encoding="utf-8"))
    before_conflict_fix = scheduler_conflict_patch_snapshot(container)
    psql(container, conflict_fix.read_text(encoding="utf-8"))
    after_conflict_fix = scheduler_conflict_patch_snapshot(container)
    verify_conflict_patch_transition(before_conflict_fix, after_conflict_fix)
    for migration in migrations[conflict_fix_index + 1 :]:
        psql(container, migration.read_text(encoding="utf-8"))
    spec = definition_spec(
        "operations.outbox",
        interval_seconds=60,
        lease_ttl_seconds=30,
        max_attempts=2,
        retry_base_seconds=5,
        retry_max_seconds=5,
    )
    ensure_definition(container, int(outer["fencing_token"]), spec)
    claim_receipt = claim_due(container, int(outer["fencing_token"]))
    claim = validate_claim(claim_receipt)
    if claim is None or claim["run"]["job_key"] != "operations.outbox":
        raise VerificationError(f"populated upgrade scheduler claim mismatch: {claim_receipt}")
    complete_run(container, int(outer["fencing_token"]), claim, "3" * 64)

    populated_after = scalar(
        container,
        "select concat_ws('|',"
        "(select count(*) from private.trading_accounts),"
        "(select count(*) from public.bot_settings),"
        "(select count(*) from private.worker_leases),"
        "(select holder_id from private.worker_leases "
        f"where account_id={sql_text(ACCOUNT_ID)}),"
        "(select fencing_token from private.worker_leases "
        f"where account_id={sql_text(ACCOUNT_ID)}),"
        "(select count(*) from public.orders),"
        "(select count(*) from private.order_intents))",
    )
    if populated_after != populated_before or domain_snapshot(container) != before_domain:
        raise VerificationError(
            "populated upgrade changed retained trading state: "
            f"before={populated_before}, after={populated_after}"
        )

    run(["docker", "restart", container])
    wait_for_postgres(container)
    durable = scalar(
        container,
        "select concat_ws('|',"
        "(select count(*) from private.scheduler_job_definitions),"
        "(select count(*) from private.scheduler_job_runs where state='succeeded'),"
        "(select count(*) from private.scheduler_job_leases))",
    )
    if durable != "1|1|1":
        raise VerificationError(f"populated upgrade was not restart durable: {durable}")
    print("PASS populated_upgrade and reconnect durability")


def cleanup_disposable_resources(containers: tuple[str, ...]) -> str | None:
    try:
        for container in containers:
            run(["docker", "rm", "-f", "-v", container], check=False)
        listed = run(
            ["docker", "ps", "-a", "--format", "{{.Names}}"],
            check=False,
        )
    except OSError:
        return "disposable_container_cleanup_command_unavailable"
    if listed.returncode != 0:
        return "disposable_container_cleanup_verification_failed"
    remaining = set(listed.stdout.splitlines()).intersection(containers)
    if remaining:
        return "disposable_container_cleanup_incomplete:" + ",".join(sorted(remaining))
    return None


def _start_postgres(container: str) -> None:
    run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            container,
            "--volume",
            "/var/lib/postgresql/data",
            "-e",
            f"POSTGRES_PASSWORD={DB_PASSWORD}",
            POSTGRES_IMAGE,
        ]
    )
    wait_for_postgres(container)


def main() -> int:
    suffix = uuid4().hex[:10]
    fresh = f"msp-durable-scheduler-fresh-{suffix}"
    upgrade = f"msp-durable-scheduler-upgrade-{suffix}"
    failure: str | None = None
    try:
        verify_checksum_wiring()
        run(["docker", "info"])
        for container in (fresh, upgrade):
            _start_postgres(container)

        apply_repository(fresh)
        open_scheduler_account_fixtures(fresh)
        before_domain = domain_snapshot(fresh)
        first_outer = acquire_outer_lease(fresh)
        specs, first_claim = verify_definition_digest_and_db_clock(
            fresh,
            first_outer,
        )
        current_outer, dead_letter = verify_restart_expiry_and_stale_takeover(
            fresh,
            first_outer,
            first_claim,
        )
        _, resumed_outer = verify_manual_replay_compare_and_swap(
            fresh,
            current_outer,
            dead_letter,
        )
        barrier_outer, execution = verify_startup_drain_and_execution_barrier(
            fresh,
            resumed_outer,
            specs,
        )
        verify_job_specific_retry_matrix_and_completion(
            fresh,
            barrier_outer,
            specs,
            execution,
        )
        verify_settlement_gate_and_recovery_progress(fresh)
        verify_missing_barrier_expired_execution_cleanup(fresh)
        verify_reconciliation_execution_gate(fresh)
        verify_concurrent_definition_idempotency(fresh)
        verify_concurrent_single_claim(fresh)
        verify_effectful_explicit_failure_is_not_retryable(fresh)
        verify_rolling_upgrade_definition_convergence(fresh)
        command_outer = verify_command_retry_and_manual_replay_budget(
            fresh,
            barrier_outer,
        )
        verify_outer_lease_time_and_budget_boundaries(fresh)
        verify_lock_wait_clock_revalidation(
            fresh,
            command_outer,
            specs["operations.outbox"],
        )
        verify_security_contract(fresh)
        verify_zero_side_effects(fresh, before_domain)
        verify_populated_upgrade(upgrade)
        verify_conflict_target_drift_rollback(upgrade)
    except (
        VerificationError,
        OSError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as error:
        failure = str(error)
    finally:
        cleanup_failure = cleanup_disposable_resources((upgrade, fresh))
    if cleanup_failure is not None:
        failure = cleanup_failure if failure is None else f"{failure}; {cleanup_failure}"
    if failure is not None:
        print(f"FINAL=FAIL {failure}", file=sys.stderr)
        return 1
    print("PASS disposable_container_cleanup")
    print("FINAL=PASS durable_operations_scheduler_verifier")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
