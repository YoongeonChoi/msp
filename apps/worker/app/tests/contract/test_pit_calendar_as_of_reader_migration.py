from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
MIGRATION = (
    ROOT
    / "supabase"
    / "migrations"
    / "20260719050000_pit_calendar_as_of_reader.sql"
)
VERIFIER = ROOT / "supabase" / "verify_pit_calendar_as_of_reader.py"
WORKFLOW = ROOT / ".github" / "workflows" / "migration-check.yml"
REPOSITORY_SAFETY = ROOT / ".github" / "scripts" / "repository_safety.py"


def _sql() -> str:
    return " ".join(MIGRATION.read_text(encoding="utf-8").lower().split())


def _verifier() -> str:
    return VERIFIER.read_text(encoding="utf-8")


def _candidate_helper(sql: str) -> str:
    start = sql.index(
        "create or replace function private.pit_kr_calendar_as_of_candidates_v1"
    )
    end = sql.index(
        "create or replace function private.list_pit_kr_daily_sessions_as_of_v1_impl"
    )
    return sql[start:end]


def _implementation(sql: str) -> str:
    start = sql.index(
        "create or replace function private.list_pit_kr_daily_sessions_as_of_v1_impl"
    )
    end = sql.index(
        "create or replace function worker_api.list_pit_kr_daily_sessions_as_of_v1"
    )
    return sql[start:end]


def _wrapper(sql: str) -> str:
    start = sql.index(
        "create or replace function worker_api.list_pit_kr_daily_sessions_as_of_v1"
    )
    end = sql.index("revoke all on function", start)
    return sql[start:end]


def test_reader_migration_is_forward_only_bounded_and_additive() -> None:
    sql = _sql()

    assert sql.startswith("begin;")
    assert sql.endswith("commit;")
    assert "set local lock_timeout = '5s'" in sql
    assert "set local statement_timeout = '90s'" in sql
    for dependency in (
        "private.pit_calendar_content_revisions",
        "private.pit_calendar_observation_occurrences",
        "private.pit_calendar_observation_quarantine",
    ):
        assert dependency in sql
    assert "pit_calendar_as_of_reader_dependency_missing" in sql
    assert " drop table " not in f" {sql} "
    assert " truncate " not in f" {sql} "
    assert "grant all" not in sql


def test_reader_has_exact_versioned_rpc_and_private_helper_signatures() -> None:
    sql = _sql()
    helper = (
        "private.pit_kr_calendar_as_of_candidates_v1( "
        "p_provider text, p_market text, p_start_session_date date, "
        "p_end_session_date date, p_as_of timestamptz, "
        "p_snapshot pg_catalog.pg_snapshot )"
    )
    implementation = (
        "private.list_pit_kr_daily_sessions_as_of_v1_impl( "
        "p_provider text, p_market text, p_start_session_date date, "
        "p_end_session_date date, p_as_of timestamptz, p_limit integer, "
        "p_cursor jsonb )"
    )
    wrapper = (
        "worker_api.list_pit_kr_daily_sessions_as_of_v1( "
        "p_provider text, p_market text, p_start_session_date date, "
        "p_end_session_date date, p_as_of timestamptz, p_limit integer, "
        "p_cursor jsonb default null )"
    )

    assert helper in sql
    assert implementation in sql
    assert wrapper in sql
    assert "contract_version constant text := 'pit_calendar_as_of_reader.v1'" in sql
    assert "cursor_version constant text := 'pit_calendar_as_of_cursor.v1'" in sql


def test_reader_uses_stable_definers_behind_stable_invoker_wrapper() -> None:
    sql = _sql()
    helper_start = sql.index(
        "create or replace function private.pit_kr_calendar_as_of_candidates_v1"
    )
    impl_start = sql.index(
        "create or replace function private.list_pit_kr_daily_sessions_as_of_v1_impl"
    )
    wrapper_start = sql.index(
        "create or replace function worker_api.list_pit_kr_daily_sessions_as_of_v1"
    )
    revoke_start = sql.index("revoke all on function", wrapper_start)

    helper_contract = sql[helper_start:impl_start]
    impl_contract = sql[impl_start:wrapper_start]
    wrapper_contract = sql[wrapper_start:revoke_start]
    for contract in (helper_contract, impl_contract, wrapper_contract):
        assert "stable" in contract
        assert "set search_path = ''" in contract
    assert "security definer" in helper_contract
    assert "security definer" in impl_contract
    assert "perform private.require_service_role()" in impl_contract
    assert "security invoker" in wrapper_contract
    assert "security definer" not in wrapper_contract


def test_reader_grants_only_guarded_entry_points_to_service_role() -> None:
    sql = _sql()

    assert "from public, anon, authenticated, authenticator, service_role" in sql
    assert (
        "grant execute on function "
        "private.list_pit_kr_daily_sessions_as_of_v1_impl" in sql
    )
    assert "worker_api.list_pit_kr_daily_sessions_as_of_v1" in sql
    assert "to service_role" in sql
    assert "pit_calendar_as_of_reader_security_contract_failed" in sql
    assert "private.pit_kr_calendar_as_of_candidates_v1" in sql
    assert "has_function_privilege( 'service_role'" in sql
    assert "owner_role.rolname not in" in sql
    assert "procedure.proowner =" in sql
    assert "canonical_helper_count <> 2" in sql
    assert "calendar_rls_count <> 4" in sql
    assert "calendar_policy_count <> 0" in sql
    assert "forbidden_table_acl_count <> 0" in sql


def test_reader_is_bounded_to_one_provider_kr_range() -> None:
    sql = _sql()
    helper = _candidate_helper(sql)

    assert "private.pit_calendar_identity_sha256_v1(" in helper
    assert "p_provider, p_market" in helper
    assert "calendar_occurrence.observed_at <= p_as_of" in helper
    assert "calendar_revision.calendar_payload->>'provider' = p_provider" not in helper
    assert "calendar_revision.calendar_payload->>'market' = p_market" not in helper
    assert "p_market is distinct from 'kr'" in sql
    assert "p_start_session_date > p_end_session_date" in sql
    assert "p_end_session_date - p_start_session_date > 365" in sql
    assert "p_limit < 25" in sql
    assert "p_limit > 100" in sql
    assert "candidate_count_value > 1000" in sql
    assert "pit_calendar_as_of_reader_candidate_limit_exceeded" in sql
    assert "received_at <= p_as_of" not in helper
    assert "p_start_session_date::text" not in sql
    assert "p_end_session_date::text" not in sql
    assert "pg_catalog.to_char(p_start_session_date, 'yyyy-mm-dd')" in sql
    assert "pg_catalog.to_char(p_end_session_date, 'yyyy-mm-dd')" in sql


def test_reader_uses_open_and_closed_immutable_lineage_not_mutable_heads() -> None:
    sql = _sql()
    helper = _candidate_helper(sql)

    assert "private.pit_calendar_content_revisions" in helper
    assert "private.pit_calendar_observation_occurrences" in helper
    assert (
        "calendar_occurrence.content_revision_id = calendar_revision.id" in helper
    )
    runtime_reader = helper + _implementation(sql) + _wrapper(sql)
    assert "pit_calendar_stream_heads" not in runtime_reader
    assert "calendar_payload->>'is_open' = 'true'" not in helper
    for field in ("is_open", "regular_start_at", "regular_end_at"):
        assert field in sql
    assert helper.count("pg_catalog.pg_visible_in_snapshot") >= 2


def test_reader_revalidates_payload_hash_and_occurrence_lineage_fail_closed() -> None:
    sql = _sql()

    for helper in (
        "private.jsonb_exact_keys_v1",
        "private.pit_calendar_identity_sha256_v1",
        "private.pit_calendar_canonical_evidence_sha256_v1",
    ):
        assert helper in sql
    for field in (
        "calendar_idempotency_key",
        "calendar_canonical_evidence_sha256",
        "calendar_revision_id",
        "calendar_occurrence_id",
        "calendar_payload",
        "candidate_lineage_sha256",
    ):
        assert field in sql
    assert (
        "jsonb_typeof( candidate.calendar_content_payload->'observed_at' ) "
        "is distinct from 'string'" in sql
    )
    assert (
        "candidate.calendar_content_payload->>'observed_at' is distinct from"
        in sql
    )
    assert "pit_calendar_as_of_reader_integrity_violation" in sql


def test_reader_cursor_binds_query_snapshot_manifest_and_total_order() -> None:
    sql = _sql()

    assert "pg_catalog.pg_current_snapshot()" in sql
    assert "cursor_ttl constant interval := interval '15 minutes'" in sql
    assert "pit_calendar_as_of_reader_cursor_invalid" in sql
    assert "pit_calendar_as_of_reader_snapshot_mismatch" in sql
    for field in (
        "schema_version",
        "query_sha256",
        "snapshot_token",
        "snapshot_issued_at",
        "snapshot_manifest_sha256",
        "last_session_date",
        "last_occurrence_observed_at",
        "last_calendar_revision",
        "last_calendar_revision_id",
        "last_calendar_occurrence_id",
    ):
        assert f"'{field}'" in sql
    assert "pg_catalog.string_agg(" in sql
    assert "candidate_lineage_sha256" in sql


def test_reader_returns_exact_envelope_and_immutable_item_lineage() -> None:
    sql = _sql()
    for key in (
        "schema_version",
        "query_sha256",
        "snapshot_token",
        "snapshot_issued_at",
        "snapshot_manifest_sha256",
        "candidate_count",
        "items",
        "next_cursor",
    ):
        assert f"'{key}'" in sql
    for key in (
        "session_date",
        "calendar_idempotency_key",
        "calendar_revision_id",
        "calendar_revision",
        "calendar_canonical_evidence_sha256",
        "calendar_revision_observed_at",
        "calendar_revision_received_at",
        "calendar_content_payload",
        "calendar_occurrence_id",
        "calendar_occurrence_observed_at",
        "calendar_occurrence_received_at",
        "calendar_occurrence_origin",
        "calendar_payload",
        "candidate_lineage_sha256",
    ):
        assert f"'{key}'" in sql


def test_full_eligible_timeline_ambiguity_blocks_the_entire_read() -> None:
    sql = _sql()
    for reason in (
        "pit_calendar_observation_time_regressed",
        "pit_calendar_historical_hash_recurrence_ambiguous",
        "pit_calendar_revision_time_not_increasing",
    ):
        assert reason in sql
    assert "private.pit_calendar_observation_quarantine" in sql
    assert "quarantine.candidate_observed_at <= p_as_of" in sql
    assert "pit_calendar_as_of_reader_timeline_ambiguous" in sql


def test_runtime_reader_functions_are_zero_write_and_return_an_enveloped_page() -> None:
    sql = _sql()
    runtime_contract = _candidate_helper(sql) + _implementation(sql) + _wrapper(sql)

    for mutation in (" insert into ", " update ", " delete from ", " truncate "):
        assert mutation not in f" {runtime_contract} "
    assert "return pg_catalog.jsonb_build_object(" in _implementation(sql)
    assert "'items'" in _implementation(sql)


def test_dedicated_verifier_covers_behavior_upgrade_security_and_zero_write() -> None:
    verifier = _verifier()

    assert f'MIGRATION_NAME = "{MIGRATION.name}"' in verifier
    assert "migrations.index(target)" in verifier
    assert "migrations[:target_index]" in verifier
    assert "migrations[target_index + 1 :]" in verifier
    for evidence in (
        "open",
        "closed",
        "cursor",
        "snapshot",
        "quarantine",
        "zero_write",
        "upgrade",
        "future quarantine",
        "json-null content observation clock",
        "fixed snapshot was polluted by later quarantine",
        "zero-write continuation",
    ):
        assert evidence in verifier.lower()


def test_ci_and_repository_safety_cover_the_new_reader() -> None:
    verifier = _verifier()
    workflow = WORKFLOW.read_text(encoding="utf-8")
    safety = REPOSITORY_SAFETY.read_text(encoding="utf-8")

    assert "FINAL=PASS pit_calendar_as_of_reader_verifier" in verifier
    assert workflow.count('"supabase/verify_pit_calendar_as_of_reader.py"') == 2
    assert "python supabase/verify_pit_calendar_as_of_reader.py" in workflow
    assert workflow.count(
        '"apps/worker/app/application/ports/calendar_as_of_reader_port.py"'
    ) == 2
    assert workflow.count(
        '"apps/worker/app/adapters/persistence/supabase_calendar_as_of_reader.py"'
    ) == 2
    assert '"list_pit_kr_daily_sessions_as_of_v1"' in safety
