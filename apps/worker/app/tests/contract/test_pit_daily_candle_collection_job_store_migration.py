from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
MIGRATION = (
    ROOT / "supabase" / "migrations" / "20260719090000_pit_daily_candle_collection_job_store.sql"
)
VERIFIER = ROOT / "supabase" / "verify_pit_daily_candle_collection_job_store.py"
WORKFLOW = ROOT / ".github" / "workflows" / "migration-check.yml"
SAFETY = ROOT / ".github" / "scripts" / "repository_safety.py"
CHECKSUMS = ROOT / "supabase" / "migration-checksums.v1.json"

RPC_NAMES = {
    "load_or_create_pit_daily_candle_collection_job_v1",
    "inspect_pit_daily_candle_collection_job_v1",
    "begin_pit_daily_candle_collection_attempt_v1",
    "fence_pit_daily_candle_collection_candidate_v1",
    "pause_pit_daily_candle_collection_attempt_v1",
    "block_pit_daily_candle_collection_attempt_v1",
    "confirm_pit_daily_candle_collection_attempt_v1",
}
PRE_CANDIDATE_BLOCK_REASONS = {
    "provider_read_outcome_unknown_before_candidate",
    "unexpected_failure_before_candidate",
    "cancelled_before_candidate",
}
POST_CANDIDATE_BLOCK_REASONS = {
    "append_outcome_unknown",
    "confirm_outcome_unknown",
    "unexpected_failure_after_candidate",
    "cancelled_after_candidate",
}


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _function_body(sql: str, qualified_name: str) -> str:
    marker = f"create or replace function {qualified_name}("
    start = sql.index(marker)
    body_start = sql.index("as $$", start) + len("as $$")
    return sql[body_start : sql.index("$$;", body_start)]


def test_migration_is_one_forward_only_additive_boundary() -> None:
    sql = _sql()

    assert sql.startswith("begin;\n")
    assert sql.rstrip().endswith("commit;")
    assert "create table private.pit_daily_candle_collection_jobs" in sql
    assert "create table private.pit_daily_candle_collection_attempt_ledger" in sql
    assert "drop table" not in sql.lower()
    assert "truncate " not in sql.lower()
    assert "delete from" not in sql.lower()
    for existing_table in (
        "private.pit_candle_stream_heads",
        "private.pit_candle_observation_revisions",
        "private.pit_candle_observation_occurrences",
    ):
        assert f"alter table {existing_table}" not in sql.lower()


def test_exact_single_candle_spec_and_state_machine_are_pinned() -> None:
    sql = _sql()

    for field in (
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
    ):
        assert f"'{field}'" in sql
    assert "'daily_candle_collection_job.v1'" in sql
    assert "'daily_candle_collection_job_snapshot.v1'" in sql
    assert "'pit_daily_candle_collection_job.v1'" not in sql
    assert "maximum_candles" not in sql
    assert "p_spec->>'count' <> '1'" in sql
    assert "p_spec->>'pagination_allowed' <> 'false'" in sql
    assert "p_spec->>'automatic_retry_allowed' <> 'false'" in sql
    for state in (
        "ready",
        "collecting",
        "candidate_fenced",
        "paused_retryable",
        "blocked_unknown",
        "completed",
    ):
        assert f"'{state}'" in sql
    assert "'append_started'" not in sql
    load_or_create = _function_body(
        sql, "private.load_or_create_pit_daily_candle_collection_job_v1_impl"
    )
    assert "pg_catalog.jsonb_typeof(p_spec->'schema_version') <> 'string'" in load_or_create
    for function_name in (
        "private.begin_pit_daily_candle_collection_attempt_v1_impl",
        "private.fence_pit_daily_candle_collection_candidate_v1_impl",
        "private.pause_pit_daily_candle_collection_attempt_v1_impl",
        "private.block_pit_daily_candle_collection_attempt_v1_impl",
        "private.confirm_pit_daily_candle_collection_attempt_v1_impl",
    ):
        assert "p_spec_sha256 is null" in _function_body(sql, function_name)


def test_candidate_fence_preserves_scope_and_canonical_attempt() -> None:
    sql = _sql()
    body = _function_body(sql, "private.fence_pit_daily_candle_collection_candidate_v1_impl")

    for parameter in (
        "p_spec_sha256",
        "p_expected_revision",
        "p_attempt_id",
        "p_holder_id",
        "p_fencing_revision",
        "p_candidate",
    ):
        assert parameter in body
    for comparison in (
        "p_candidate->>'provider' <> job.provider",
        "p_candidate->>'symbol' <> job.symbol",
        "p_candidate->>'market' <> job.market",
        "p_candidate->>'interval' <> job.interval",
        "provider_event_at_value > job.before_at",
        "job.provider_contract_sha256",
        "private.pit_candle_identity_sha256_v1",
        "private.pit_candle_observation_sha256_v1",
    ):
        assert comparison in body
    for field in (
        "market",
        "interval",
        "currency",
        "provider_contract_sha256",
        "canonical_observation_sha256",
    ):
        assert re.search(
            rf"pg_catalog\.jsonb_typeof\(\s*p_candidate->'{field}'\s*\)\s*<>\s*'string'",
            body,
        )
    assert "state = 'candidate_fenced'" in body
    assert "candidate_begun_at = job.active_begun_at" in body
    snapshot = _function_body(sql, "private.pit_daily_candle_collection_snapshot_v1")
    assert "'begun_at'" in snapshot
    assert "job.candidate_begun_at" in snapshot


def test_pause_and_block_reason_sets_are_state_scoped() -> None:
    sql = _sql()
    pause = _function_body(sql, "private.pause_pit_daily_candle_collection_attempt_v1_impl")
    block = _function_body(sql, "private.block_pit_daily_candle_collection_attempt_v1_impl")

    assert "provider_read_failed_before_candidate" in pause
    assert "p_reason_code is distinct from" in pause
    assert "job.state not in ('collecting', 'candidate_fenced')" in block
    assert "pit_daily_candle_collection_job_reason_scope_invalid" in block
    for reason in PRE_CANDIDATE_BLOCK_REASONS | POST_CANDIDATE_BLOCK_REASONS:
        assert reason in block
    verifier = VERIFIER.read_text(encoding="utf-8")
    assert "'append_outcome_unknown'" in verifier
    assert "pit_daily_candle_collection_job_reason_scope_invalid" in verifier
    assert "'arbitrary_retry_reason'" in verifier


def test_confirm_derives_durable_ids_from_exact_occurrence_revision_join() -> None:
    sql = _sql()
    body = _function_body(sql, "private.confirm_pit_daily_candle_collection_attempt_v1_impl")

    assert "from private.pit_candle_observation_occurrences as occurrence" in body
    assert "join private.pit_candle_observation_revisions as content" in body
    assert "occurrence.observed_at = job.candidate_observed_at" in body
    assert "occurrence.observation_payload = job.candidate_payload" in body
    assert "content.revision = receipt_revision_value" in body
    assert "content.observed_at = receipt_stored_observed_at_value" in body
    assert "p_receipt->>'inserted'" not in body[body.index("select occurrence.id") :]
    assert "confirmed_occurrence_id = occurrence_id_value" in body
    assert "confirmed_content_revision_id = content_revision_id_value" in body
    snapshot = _function_body(sql, "private.pit_daily_candle_collection_snapshot_v1")
    for field in (
        "persistence_kind",
        "occurrence_id",
        "content_revision_id",
        "occurrence_observed_at",
        "content_revision",
        "content_revision_observed_at",
        "idempotency_key",
        "canonical_observation_sha256",
    ):
        assert f"'{field}'" in snapshot
    assert "'persistence_kind', 'durable'" in snapshot


def test_attempt_ledger_and_cas_reject_reuse_aba_overflow_and_takeover() -> None:
    sql = _sql()

    assert "generated always as identity" in sql
    assert "unique (job_id, job_revision)" in sql
    assert "unique (attempt_id, event_kind)" in sql
    assert "where event_kind = 'begun'" in sql
    assert "private.reject_append_only_mutation()" in sql
    assert "pit_daily_candle_collection_job_attempt_reused" in sql
    for function_name, maximum in (
        ("private.begin_pit_daily_candle_collection_attempt_v1_impl", 9223372036854775803),
        ("private.fence_pit_daily_candle_collection_candidate_v1_impl", 9223372036854775804),
        ("private.pause_pit_daily_candle_collection_attempt_v1_impl", 9223372036854775802),
        ("private.block_pit_daily_candle_collection_attempt_v1_impl", 9223372036854775805),
        ("private.confirm_pit_daily_candle_collection_attempt_v1_impl", 9223372036854775805),
    ):
        body = _function_body(sql, function_name)
        assert "p_expected_revision >= 9223372036854775807" in body
        assert f"p_expected_revision > {maximum}" in body
        assert "pit_daily_candle_collection_job_revision_exhausted" in body
    assert "pit_daily_candle_collection_revision_headroom_check" in sql
    assert re.search(
        r"state <> 'paused_retryable'\s+or \(revision % 2 = 1 and revision <= ",
        sql,
    )
    confirm_body = _function_body(
        sql,
        "private.confirm_pit_daily_candle_collection_attempt_v1_impl",
    )
    argument_error = confirm_body.index(
        "raise exception 'pit_daily_candle_collection_job_argument_invalid'"
    )
    receipt_error = confirm_body.index(
        "raise exception 'pit_daily_candle_collection_job_receipt_invalid'"
    )
    assert argument_error < receipt_error
    for function_name in (
        "private.fence_pit_daily_candle_collection_candidate_v1_impl",
        "private.pause_pit_daily_candle_collection_attempt_v1_impl",
        "private.block_pit_daily_candle_collection_attempt_v1_impl",
        "private.confirm_pit_daily_candle_collection_attempt_v1_impl",
    ):
        body = _function_body(sql, function_name)
        assert "p_fencing_revision % 2 <> 0" in body
        assert "p_fencing_revision > 9223372036854775804" in body
    assert "pit_daily_candle_collection_job_revision_conflict" in sql
    assert "pit_daily_candle_collection_job_attempt_fence_mismatch" in sql
    assert re.search(r"create or replace function worker_api\..*takeover", sql) is None
    assert re.search(r"create or replace function worker_api\..*expire", sql) is None
    assert re.search(r"create or replace function worker_api\..*reset", sql) is None
    verifier = VERIFIER.read_text(encoding="utf-8")
    for marker in (
        "verify_concurrent_begin_and_global_uuid_reuse",
        "verify_revision_overflow",
        "begin headroom rejection changed durable state",
        "fence headroom rejection changed durable state",
        "pause headroom rejection changed durable state",
        "terminal headroom rejection changed durable state",
        "maximum begin/fence/confirm path did not complete",
        "maximum pause retry path did not complete",
        "paused revision parity rejection changed durable state",
        "maximum block path did not complete",
        "timedelta(days=365)",
        "pit_daily_candle_collection_job_revision_conflict",
    ):
        assert marker in verifier


def test_private_rls_acl_and_exact_worker_rpc_surface() -> None:
    sql = _sql()
    normalized = re.sub(r"\s+", " ", sql)
    worker_rpcs = set(re.findall(r"create or replace function worker_api\.([a-z0-9_]+)\(", sql))
    impls = set(re.findall(r"create or replace function private\.([a-z0-9_]+)_impl\(", sql))

    assert worker_rpcs == RPC_NAMES
    assert impls == RPC_NAMES
    for table in (
        "private.pit_daily_candle_collection_jobs",
        "private.pit_daily_candle_collection_attempt_ledger",
    ):
        assert f"alter table {table} enable row level security" in normalized
        assert f"alter table {table} force row level security" in normalized
    assert "create policy" not in sql.lower()
    assert "from public, anon, authenticated, authenticator, service_role" in sql
    assert "to service_role;" in sql
    assert sql.count("set search_path = ''") >= 20
    safety = SAFETY.read_text(encoding="utf-8")
    for rpc in RPC_NAMES:
        assert f'"{rpc}"' in safety


def test_verifier_workflow_and_checksum_are_wired() -> None:
    verifier = VERIFIER.read_text(encoding="utf-8")
    workflow = WORKFLOW.read_text(encoding="utf-8")
    for marker in (
        "verify_completion_exact_join",
        "verify_pause_rebegin_and_aba",
        "verify_block_reason_boundaries",
        "verify_scope_receipt_and_fence_fail_closed",
        "verify_concurrent_begin_and_global_uuid_reuse",
        "verify_revision_overflow",
        "verify_security_contract",
        "verify_populated_upgrade",
        "FINAL=PASS pit_daily_candle_collection_job_store_verifier",
    ):
        assert marker in verifier
    assert '"supabase/verify_pit_daily_candle_collection_job_store.py"' in workflow
    assert "python supabase/verify_pit_daily_candle_collection_job_store.py" in workflow
    for path in (
        "apps/worker/app/application/ports/daily_candle_collection_job_store_port.py",
        "apps/worker/app/adapters/persistence/supabase_daily_candle_collection_job_store.py",
    ):
        assert workflow.count(f'"{path}"') == 2
    for field in (
        "market",
        "interval",
        "currency",
        "provider_contract_sha256",
        "canonical_observation_sha256",
    ):
        assert f'"{field}",' in verifier
    assert "candidate_payload[field] = None" in verifier
    assert 'malformed["schema_version"] = None' in verifier
    assert "null spec SHA changed durable state" in verifier

    manifest = json.loads(CHECKSUMS.read_text(encoding="utf-8"))["migrations"]
    expected = hashlib.sha256(MIGRATION.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    assert manifest[MIGRATION.name] == expected
