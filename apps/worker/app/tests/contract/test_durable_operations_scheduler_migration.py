from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
MIGRATION_NAME = "20260724210000_durable_operations_scheduler.sql"
MIGRATION = ROOT / "supabase" / "migrations" / MIGRATION_NAME
VERIFIER = ROOT / "supabase" / "verify_durable_operations_scheduler.py"
G1_G2_VERIFIER = ROOT / "supabase" / "verify_g1_g2_migration.py"
MANIFEST = ROOT / "supabase" / "migration-checksums.v1.json"
WORKFLOW = ROOT / ".github" / "workflows" / "migration-check.yml"
PREFLIGHT = ROOT / "supabase" / "preflight" / "pgcrypto_replay_preflight.sql"

EXPECTED_JOBS = {
    "operations.commands",
    "operations.execution",
    "operations.settlement",
    "operations.reconciliation",
    "operations.outbox",
}
EXPECTED_RPCS = {
    "ensure_scheduler_job_definition",
    "converge_scheduler_job_definition",
    "claim_due_scheduler_job",
    "complete_scheduler_job_run",
    "fail_scheduler_job_run",
    "inspect_scheduler_dead_letter",
    "replay_scheduler_dead_letter",
}
EXPECTED_VERIFICATION_MARKERS = {
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
    "disposable_container_cleanup",
}


def _regular_file(path: Path) -> str:
    assert path.is_file()
    assert not path.is_symlink()
    return path.read_text(encoding="utf-8")


def _function_block(sql: str, qualified_name: str) -> str:
    match = re.search(
        rf"(?ms)^create or replace function {re.escape(qualified_name)}\(.*?^\$\$;",
        sql,
    )
    assert match is not None
    return match.group(0)


def test_scheduler_migration_pins_fixed_jobs_and_rpc_surface() -> None:
    sql = _regular_file(MIGRATION)
    job_values = set(re.findall(r"'operations\.[a-z_]+'", sql))
    assert {value.strip("'") for value in job_values} == EXPECTED_JOBS

    exposed = set(
        re.findall(
            r"(?m)^create or replace function worker_api\.([a-z0-9_]*scheduler[a-z0-9_]*)\(",
            sql,
        )
    )
    assert exposed == EXPECTED_RPCS
    assert "create unique index scheduler_job_runs_one_active_per_definition_idx" in sql
    assert "where state in ('pending', 'leased', 'retry_wait');" in sql
    assert "for update skip locked;" in sql
    assert "private.scheduler_job_definitions enable row level security" in sql
    assert "private.scheduler_job_definitions force row level security" in sql


def test_scheduler_migration_uses_database_clock_and_revalidates_after_locks() -> None:
    sql = _regular_file(MIGRATION)
    outer_guard = _function_block(sql, "private.require_scheduler_outer_lease_v1")
    assert "outer_lease.acquired_at <= p_observed_at" in outer_guard
    assert "lease_ttl_seconds between 10 and 3600" in sql
    assert "p_now" not in "\n".join(
        _function_block(sql, f"worker_api.{rpc}").partition("returns")[0]
        for rpc in EXPECTED_RPCS
    )
    assert sql.count("authorization_time := pg_catalog.clock_timestamp();") >= 6
    for implementation in (
        "ensure_scheduler_job_definition_impl",
        "converge_scheduler_job_definition_impl",
        "claim_due_scheduler_job_impl",
        "complete_scheduler_job_run_impl",
        "fail_scheduler_job_run_impl",
        "inspect_scheduler_dead_letter_impl",
        "replay_scheduler_dead_letter_impl",
    ):
        block = _function_block(sql, f"private.{implementation}")
        assert "pg_catalog.clock_timestamp()" in block
        assert "private.require_scheduler_outer_lease_v1" in block
        assert "set search_path = ''" in block

    claim = _function_block(sql, "private.claim_due_scheduler_job_impl")
    convergence = _function_block(
        sql,
        "private.converge_scheduler_job_definition_impl",
    )
    completion = _function_block(sql, "private.complete_scheduler_job_run_impl")
    failure = _function_block(sql, "private.fail_scheduler_job_run_impl")
    assert "expired_run.revision = run_row.revision" in claim
    assert "current_outer_lease.fencing_token = p_outer_fencing_token" in claim
    assert "completed_run.revision = p_expected_run_revision" in completion
    assert "failed_run.revision = p_expected_run_revision" in failure
    assert "pg_catalog.make_interval(secs => 10)" in claim
    assert "return query select false, null::jsonb, authorization_time" in claim
    assert "scheduler_outer_lease_renewal_required" in convergence


def test_scheduler_commands_gate_execution_and_force_startup_drain() -> None:
    sql = _regular_file(MIGRATION)
    barrier = _function_block(sql, "private.scheduler_command_barrier_satisfied_v1")
    claim = _function_block(sql, "private.claim_due_scheduler_job_impl")
    for binding in (
        "completed_command.definition_sha256 = command_definition.definition_sha256",
        "completed_command.state = 'succeeded'",
        "completed_command.lease_holder_id = p_holder_id",
        "completed_command.outer_fencing_token = p_outer_fencing_token",
        "completed_command.lease_release_sha = p_release_sha",
        "completed_command.completed_at >= p_outer_acquired_at",
        "command_definition.next_due_at > p_observed_at",
        "settlement_definition.enabled",
        "settlement_definition.scheduler_state = 'ready'",
        "reconciliation_definition.enabled",
        "reconciliation_definition.scheduler_state = 'ready'",
    ):
        assert binding in barrier
    assert "definition.job_key = 'operations.commands'" in claim
    assert "startup_command.completed_at >= outer_lease_acquired_at" in claim
    assert "definition.job_key <> 'operations.execution'" in claim
    assert "uncertain_settlement.state = 'leased'" in barrier
    assert "uncertain_settlement.lease_expires_at <= p_observed_at" in barrier
    assert "uncertain_reconciliation.state = 'leased'" in barrier
    assert "uncertain_reconciliation.lease_expires_at <= p_observed_at" in barrier
    assert "from private.scheduler_job_runs as expired_execution" in claim
    assert "expired_execution.lease_expires_at <= authorization_time" in claim
    assert "active_run_found" in claim
    assert "run_row.lease_expires_at <= authorization_time" in claim
    assert "from private.scheduler_job_definitions as reconciliation_definition" in claim
    assert claim.count("private.scheduler_command_barrier_satisfied_v1") >= 2


def test_scheduler_retry_and_manual_replay_boundaries_are_reason_bound() -> None:
    sql = _regular_file(MIGRATION)
    claim = _function_block(sql, "private.claim_due_scheduler_job_impl")
    failure = _function_block(sql, "private.fail_scheduler_job_run_impl")
    inspection = _function_block(sql, "private.inspect_scheduler_dead_letter_impl")
    replay = _function_block(sql, "private.replay_scheduler_dead_letter_impl")
    for reason in (
        "command_poll_retryable",
        "reconciliation_poll_retryable",
        "outbox_poll_retryable",
    ):
        assert reason in failure
    assert "lease_expiry_retryable := definition_row.job_key in (" in claim
    assert "'operations.commands'" in claim
    assert "'operations.reconciliation'" in claim
    assert "'operations.outbox'" in claim
    assert "effectful_job_requires_resolution_evidence" in inspection
    assert "source_row.job_key in ('operations.execution', 'operations.settlement')" in replay
    assert "p_confirmed_reason_code <> p_expected_failure_reason_code" in replay
    assert "p_explicit_confirmation is distinct from true" in replay
    assert "request_row.holder_id <> p_holder_id" not in replay
    assert "request_row.outer_fencing_token <> p_outer_fencing_token" not in replay
    assert "scheduler_replay_idempotency_conflict" in replay


def test_scheduler_recovery_rpc_is_typed_and_never_creates_cadence_work() -> None:
    sql = _regular_file(MIGRATION)
    convergence = _function_block(
        sql,
        "private.converge_scheduler_job_definition_impl",
    )
    wrapper = _function_block(sql, "worker_api.converge_scheduler_job_definition")
    for status in ("converged", "claimed", "wait", "manual_resolution"):
        assert f"'{status}'" in convergence
    for column in (
        "status text",
        "definition jsonb",
        "claim jsonb",
        "active_run_id uuid",
        "next_eligible_at timestamptz",
        "reason_code text",
        "observed_at timestamptz",
    ):
        assert column in convergence
        assert column in wrapper
    assert "insert into private.scheduler_job_runs" not in convergence
    assert "scheduler_definition_change_requires_quiescence" not in convergence
    assert "effectful_job_requires_resolution_evidence" in convergence
    assert "for update" in convergence
    assert "pg_catalog.clock_timestamp()" in convergence
    assert "private.require_scheduler_outer_lease_v1" in convergence
    assert "'definition_id', definition_row.definition_id" in convergence
    assert "'account_id', definition_row.account_id" in convergence
    assert "'revision', definition_row.revision" in convergence
    assert "'next_due_at', definition_row.next_due_at" in convergence
    assert "'scheduler_state', definition_row.scheduler_state" in convergence


def test_scheduler_creation_races_and_catalog_owner_are_fail_closed() -> None:
    sql = _regular_file(MIGRATION)
    ensure = _function_block(sql, "private.ensure_scheduler_job_definition_impl")
    convergence = _function_block(
        sql,
        "private.converge_scheduler_job_definition_impl",
    )
    for block in (ensure, convergence):
        assert "on conflict (account_id, job_key) do nothing" in block
        assert block.count("private.require_scheduler_outer_lease_v1") >= 3
        assert block.count("pg_catalog.clock_timestamp()") >= 3
    ensure_after_insert = ensure.partition(
        "on conflict (account_id, job_key) do nothing"
    )[2].partition("if definition_row.definition_sha256")[0]
    assert "authorization_time := pg_catalog.clock_timestamp();" in ensure_after_insert
    assert "private.require_scheduler_outer_lease_v1" in ensure_after_insert
    convergence_after_insert = convergence.partition(
        "on conflict (account_id, job_key) do nothing"
    )[2].partition("if definition_inserted then")[0]
    assert "definition_inserted := found;" in convergence_after_insert
    assert "authorization_time := pg_catalog.clock_timestamp();" in convergence_after_insert
    assert "private.require_scheduler_outer_lease_v1" in convergence_after_insert
    assert "outer_lease.expires_at > authorization_time" in convergence_after_insert
    assert "contract_owner_count <> 1" in sql
    assert "contract_object_count <> 25" in sql
    assert "contract_owner_trusted is distinct from true" in sql
    assert "role.rolname not in ('anon','authenticated','authenticator','service_role')" in sql
    assert "role.rolsuper or role.rolbypassrls" in sql


def test_scheduler_verifier_pins_behavior_upgrade_and_cleanup_evidence() -> None:
    source = _regular_file(VERIFIER)
    assert 'POSTGRES_IMAGE = "postgres:17.6-alpine"' in source
    assert f'MIGRATION_NAME = "{MIGRATION_NAME}"' in source
    for marker in EXPECTED_VERIFICATION_MARKERS:
        assert f'"{marker}"' in source
    assert "apply_repository(fresh)" in source
    assert "verify_populated_upgrade(upgrade)" in source
    assert '["docker", "restart", container]' in source
    assert '["docker", "rm", "-f", "-v", container]' in source
    assert "domain_snapshot(fresh)" in source
    assert "scheduler_definition_lock_fixture" in source
    assert "scheduler_run_lock_fixture" in source
    assert "OTHER_HOLDER_ID" in source
    assert "takeover replay receipt mismatch" in source


def test_populated_upgrade_fixture_models_and_revokes_trusted_owner_capability() -> None:
    source = _regular_file(G1_G2_VERIFIER)

    assert "create role supabase_admin nologin nosuperuser bypassrls;" in source
    assert "create role migration_operator login nosuperuser inherit bypassrls;" in source
    assert 'identity != "migration_operator|f|t"' in source
    assert "alter role migration_operator nobypassrls;" in source
    assert 'scheduler_owner_receipt != "25|1|supabase_admin|t|t"' in source
    assert 'cleanup_receipt != "0|postgres|f|f|f|f|f|f"' in source


def test_scheduler_verifier_is_triggered_for_pull_requests_and_pushes() -> None:
    workflow = _regular_file(WORKFLOW)
    verifier_path = "supabase/verify_durable_operations_scheduler.py"
    contract_path = (
        "apps/worker/app/tests/contract/"
        "test_durable_operations_scheduler_migration.py"
    )
    assert workflow.count(f'      - "{verifier_path}"') == 2
    assert workflow.count(f'      - "{contract_path}"') == 2
    assert workflow.count("run: python supabase/verify_durable_operations_scheduler.py") == 1


def test_pgcrypto_preflight_accepts_the_exact_scheduler_repository_tail() -> None:
    preflight = _regular_file(PREFLIGHT)
    version_match = re.search(
        r"expected_versions constant text\[\] := array\[(.*?)\];",
        preflight,
        re.DOTALL,
    )
    name_match = re.search(
        r"expected_names constant text\[\] := array\[(.*?)\];",
        preflight,
        re.DOTALL,
    )
    assert version_match is not None
    assert name_match is not None

    migration_parts = [
        path.stem.split("_", maxsplit=1)
        for path in sorted(MIGRATION.parent.glob("*.sql"))
    ]
    assert all(len(parts) == 2 for parts in migration_parts)
    expected_versions = [parts[0] for parts in migration_parts]
    expected_names = [parts[1] for parts in migration_parts]
    assert re.findall(r"'([^']+)'", version_match.group(1)) == expected_versions
    assert re.findall(r"'([^']+)'", name_match.group(1)) == expected_names


def test_scheduler_migration_checksum_is_wired_after_final_freeze() -> None:
    migration = _regular_file(MIGRATION)
    manifest = json.loads(_regular_file(MANIFEST))
    assert manifest["algorithm"] == "sha256"
    assert manifest["canonicalization"] == "utf-8-lf"
    expected = hashlib.sha256(migration.encode("utf-8")).hexdigest()
    assert manifest["migrations"][MIGRATION_NAME] == expected
