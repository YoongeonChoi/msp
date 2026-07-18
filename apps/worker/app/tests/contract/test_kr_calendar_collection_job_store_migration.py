from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
MIGRATION = (
    ROOT
    / "supabase"
    / "migrations"
    / "20260719060000_kr_calendar_collection_job_store.sql"
)
VERIFIER = ROOT / "supabase" / "verify_kr_calendar_collection_job_store.py"


def _sql() -> str:
    return " ".join(MIGRATION.read_text(encoding="utf-8").lower().split())


def test_migration_is_forward_only_and_defines_private_durable_state() -> None:
    sql = _sql()

    assert sql.startswith("begin;")
    assert sql.endswith("commit;")
    assert "create table private.kr_calendar_collection_jobs" in sql
    assert "create table private.kr_calendar_collection_attempt_ledger" in sql
    assert " drop table " not in f" {sql} "
    assert " truncate " not in f" {sql} "
    assert " ttl" not in sql
    assert "expires_at" not in sql
    assert "takeover" in sql  # Explicitly documented as absent.
    assert "automatic_retry_allowed', false" in sql


def test_state_tables_are_rls_forced_policy_free_and_ledger_is_append_only() -> None:
    sql = _sql()

    for table in (
        "private.kr_calendar_collection_jobs",
        "private.kr_calendar_collection_attempt_ledger",
    ):
        assert f"alter table {table} enable row level security" in sql
        assert f"alter table {table} force row level security" in sql
    assert "reject_kr_calendar_collection_attempt_mutation" in sql
    assert "private.reject_append_only_mutation()" in sql
    assert "policy_count <> 0" in sql
    assert "forbidden_table_acl_count <> 0" in sql
    assert "from public, anon, authenticated, authenticator, service_role" in sql


def test_exact_five_guarded_rpc_pairs_are_present() -> None:
    sql = _sql()
    names = (
        "load_or_create_kr_calendar_collection_job_v1",
        "begin_kr_calendar_collection_date_attempt_v1",
        "pause_kr_calendar_collection_date_attempt_v1",
        "block_kr_calendar_collection_date_attempt_v1",
        "confirm_kr_calendar_collection_date_v1",
    )
    for name in names:
        assert f"create or replace function private.{name}_impl" in sql
        assert f"create or replace function worker_api.{name}" in sql
    assert sql.count("perform private.require_service_role()") == 5
    assert "impl_contract_count <> 5" in sql
    assert "wrapper_contract_count <> 5" in sql
    assert "security definer" in sql
    assert "security invoker" in sql
    assert "set search_path = ''" in sql


def test_state_machine_uses_exact_cas_fencing_and_contiguous_dates() -> None:
    sql = _sql()

    for marker in (
        "job.revision <> p_expected_revision",
        "job.active_fencing_revision <> p_expected_revision",
        "job.active_attempt_id <> attempt_id_value",
        "job.active_holder_id <> holder_id_value",
        "job.active_target_date <> p_target_date",
        "p_target_date <> next_date_value",
        "p_target_date <> job.start_date + confirmed_count",
        "kr_calendar_collection_job_revision_conflict",
        "kr_calendar_collection_job_attempt_fence_mismatch",
        "kr_calendar_collection_job_attempt_reused",
    ):
        assert marker in sql
    assert "for update" in sql
    assert "end_date_value - start_date_value > 365" in sql


def test_spec_and_manifest_hashes_match_sorted_compact_json_contract() -> None:
    sql = _sql()

    assert "kr_calendar_collection_canonical_json_v1" in sql
    assert "order by item.key collate \"c\"" in sql
    assert "'maximum_inclusive_days', 366" in sql
    assert "'automatic_retry_allowed', false" in sql
    assert "'kr_calendar_collection_job_manifest.v1'" in sql
    assert "kr_calendar_collection_manifest_sha256_v1" in sql
    assert "private.pit_sha256_text_v1" in sql
    assert "terminal_manifest_sha256 = manifest_value" in sql


def test_confirm_binds_checkpoint_to_immutable_calendar_occurrence() -> None:
    sql = _sql()

    assert "private.pit_calendar_observation_occurrences" in sql
    assert "private.pit_calendar_content_revisions" in sql
    assert "occurrence.observation_payload = p_session" in sql
    assert "revision.revision = (p_receipt->>'revision')::bigint" in sql
    assert "observed_at_value < job.active_begun_at" in sql
    assert "observed_at_value > p_now" in sql
    assert "event.event_kind = 'confirmed'" in sql


def test_verifier_is_wired_for_fresh_upgrade_concurrency_and_security() -> None:
    verifier = VERIFIER.read_text(encoding="utf-8")

    for marker in (
        "verify_fresh",
        "verify_populated_upgrade",
        "verify_concurrent_begin",
        "verify_pause_rebegin_block",
        "verify_completion_manifest",
        "verify_security_contract",
        "FINAL=PASS kr_calendar_collection_job_store_verifier",
    ):
        assert marker in verifier
