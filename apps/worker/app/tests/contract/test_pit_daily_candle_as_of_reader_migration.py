from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
MIGRATION = (
    ROOT
    / "supabase"
    / "migrations"
    / "20260719030000_pit_daily_candle_as_of_reader.sql"
)
VERIFIER = ROOT / "supabase" / "verify_pit_daily_candle_as_of_reader.py"
WORKFLOW = ROOT / ".github" / "workflows" / "migration-check.yml"


def _sql() -> str:
    return " ".join(MIGRATION.read_text(encoding="utf-8").lower().split())


def _verifier() -> str:
    return VERIFIER.read_text(encoding="utf-8")


def test_reader_migration_is_forward_only_bounded_and_additive() -> None:
    sql = _sql()

    assert sql.startswith("begin;")
    assert sql.endswith("commit;")
    assert "set local lock_timeout = '5s'" in sql
    assert "set local statement_timeout = '90s'" in sql
    for dependency in (
        "private.pit_candle_observation_occurrences",
        "private.pit_calendar_observation_occurrences",
        "private.pit_daily_candle_timing_revisions",
        "private.pit_candle_observation_quarantine",
        "private.pit_calendar_observation_quarantine",
        "private.pit_daily_candle_timing_quarantine",
    ):
        assert dependency in sql
    assert "pit_daily_candle_as_of_reader_dependency_missing" in sql
    assert " drop table " not in f" {sql} "
    assert " truncate " not in f" {sql} "
    assert " delete from " not in f" {sql} "
    assert " update private. " not in f" {sql} "
    assert "grant all" not in sql


def test_reader_has_exact_versioned_rpc_and_private_helper_signatures() -> None:
    sql = _sql()
    helper = (
        "private.pit_daily_candle_as_of_candidates_v1( "
        "p_provider text, p_market text, p_symbol text, p_interval text, "
        "p_adjusted boolean, p_start_session_date date, "
        "p_end_session_date date, p_as_of timestamptz, "
        "p_snapshot pg_catalog.pg_snapshot )"
    )
    implementation = (
        "private.list_pit_daily_candles_as_of_v1_impl( "
        "p_provider text, p_market text, p_symbol text, p_interval text, "
        "p_adjusted boolean, p_start_session_date date, "
        "p_end_session_date date, p_as_of timestamptz, p_limit integer, "
        "p_cursor jsonb )"
    )
    wrapper = (
        "worker_api.list_pit_daily_candles_as_of_v1( "
        "p_provider text, p_market text, p_symbol text, p_interval text, "
        "p_adjusted boolean, p_start_session_date date, "
        "p_end_session_date date, p_as_of timestamptz, p_limit integer, "
        "p_cursor jsonb default null )"
    )

    assert helper in sql
    assert implementation in sql
    assert wrapper in sql
    assert "contract_version constant text := 'pit_daily_candle_as_of_reader.v1'" in sql
    assert "cursor_version constant text := 'pit_daily_candle_as_of_cursor.v1'" in sql


def test_reader_uses_stable_definers_behind_stable_invoker_wrapper() -> None:
    sql = _sql()
    helper_start = sql.index(
        "create or replace function private.pit_daily_candle_as_of_candidates_v1"
    )
    impl_start = sql.index(
        "create or replace function private.list_pit_daily_candles_as_of_v1_impl"
    )
    wrapper_start = sql.index(
        "create or replace function worker_api.list_pit_daily_candles_as_of_v1"
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


def test_reader_grants_only_wrapper_and_guarded_impl_to_service_role() -> None:
    sql = _sql()

    assert (
        "from public, anon, authenticated, authenticator, service_role" in sql
    )
    assert (
        "grant execute on function "
        "private.list_pit_daily_candles_as_of_v1_impl" in sql
    )
    assert "worker_api.list_pit_daily_candles_as_of_v1" in sql
    assert "to service_role" in sql
    assert "pit_daily_candle_as_of_reader_security_contract_failed" in sql
    assert "private.pit_daily_candle_as_of_candidates_v1" in sql
    assert "has_function_privilege( 'service_role'" in sql


def test_reader_is_bounded_to_one_exact_daily_series() -> None:
    sql = _sql()

    for predicate in (
        "timing.timing_payload->>'provider' = p_provider",
        "timing.timing_payload->>'market' = p_market",
        "timing.timing_payload->>'symbol' = p_symbol",
        "timing.timing_payload->>'interval' = p_interval",
        "timing.timing_payload->>'adjusted' = p_adjusted::text",
        "timing.evidence_available_at <= p_as_of",
    ):
        assert predicate in sql
    assert "p_market is distinct from 'kr'" in sql
    assert "p_interval is distinct from '1d'" in sql
    assert "p_end_session_date - p_start_session_date > 365" in sql
    assert "p_limit < 25" in sql
    assert "p_limit > 100" in sql
    assert "candidate_count_value > 1000" in sql
    assert "pit_daily_candle_as_of_reader_candidate_limit_exceeded" in sql
    assert "p_start_session_date::text" not in sql
    assert "p_end_session_date::text" not in sql
    assert "pg_catalog.to_char(p_start_session_date, 'yyyy-mm-dd')" in sql
    assert "pg_catalog.to_char(p_end_session_date, 'yyyy-mm-dd')" in sql


def test_reader_uses_exact_composite_occurrence_lineage_not_mutable_heads() -> None:
    sql = _sql()
    helper_start = sql.index(
        "create or replace function private.pit_daily_candle_as_of_candidates_v1"
    )
    impl_start = sql.index(
        "create or replace function private.list_pit_daily_candles_as_of_v1_impl"
    )
    helper = sql[helper_start:impl_start]

    assert (
        "candle_occurrence.id = timing.candle_occurrence_id" in helper
    )
    assert (
        "candle_occurrence.content_revision_id = timing.candle_revision_id"
        in helper
    )
    assert (
        "calendar_occurrence.id = timing.calendar_occurrence_id" in helper
    )
    assert (
        "calendar_occurrence.content_revision_id = timing.calendar_revision_id"
        in helper
    )
    assert "pit_candle_stream_heads" not in helper
    assert "pit_calendar_stream_heads" not in helper
    assert "pit_daily_candle_timing_heads" not in helper
    assert helper.count("pg_catalog.pg_visible_in_snapshot") >= 5
    assert "left join private.pit_candle_observation_occurrences" in helper
    assert "left join private.pit_calendar_observation_occurrences" in helper


def test_reader_revalidates_full_payload_and_hash_lineage_fail_closed() -> None:
    sql = _sql()

    for helper in (
        "private.jsonb_exact_keys_v1",
        "private.pit_candle_identity_sha256_v1",
        "private.pit_candle_observation_sha256_v1",
        "private.pit_calendar_identity_sha256_v1",
        "private.pit_calendar_canonical_evidence_sha256_v1",
        "private.pit_daily_candle_timing_identity_sha256_v1",
        "private.pit_daily_candle_timing_evidence_sha256_v1",
    ):
        assert helper in sql
    assert "pit_daily_candle_as_of_reader_integrity_violation" in sql
    assert "candidate_lineage_sha256" in sql
    assert "candle_occurrence_id" in sql
    assert "calendar_occurrence_id" in sql
    assert "candle_payload" in sql
    assert "calendar_payload" in sql
    assert "timing_payload" in sql


def test_reader_cursor_binds_query_snapshot_manifest_and_total_order() -> None:
    sql = _sql()

    assert "pg_catalog.pg_current_snapshot()" in sql
    assert "cursor_ttl constant interval := interval '15 minutes'" in sql
    assert "pit_daily_candle_as_of_reader_cursor_invalid" in sql
    assert "pit_daily_candle_as_of_reader_snapshot_mismatch" in sql
    for field in (
        "schema_version",
        "query_sha256",
        "snapshot_token",
        "snapshot_issued_at",
        "snapshot_manifest_sha256",
        "last_session_date",
        "last_candle_observed_at",
        "last_evidence_available_at",
        "last_timing_revision",
        "last_timing_revision_id",
    ):
        assert f"'{field}'" in sql
    order = (
        "candidates.session_date, candidates.candle_occurrence_observed_at, "
        "candidates.timing_evidence_available_at, "
        "candidates.timing_revision, candidates.timing_revision_id"
    )
    assert sql.count(order) >= 3
    assert "pg_catalog.string_agg( candidates.candidate_lineage_sha256" in sql


def test_reader_returns_exact_envelope_and_item_contract() -> None:
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
        "timing_revision_id",
        "timing_idempotency_key",
        "timing_revision",
        "timing_canonical_evidence_sha256",
        "timing_evidence_available_at",
        "timing_received_at",
        "timing_payload",
        "candle_revision_id",
        "candle_revision",
        "candle_canonical_observation_sha256",
        "candle_occurrence_id",
        "candle_occurrence_observed_at",
        "candle_occurrence_received_at",
        "candle_occurrence_origin",
        "candle_payload",
        "calendar_revision_id",
        "calendar_revision",
        "calendar_canonical_evidence_sha256",
        "calendar_occurrence_id",
        "calendar_occurrence_observed_at",
        "calendar_occurrence_received_at",
        "calendar_occurrence_origin",
        "calendar_payload",
        "candidate_lineage_sha256",
    ):
        assert f"'{key}'" in sql


def test_only_timeline_ambiguity_quarantine_blocks_the_reader() -> None:
    sql = _sql()
    allowed = (
        "candle_observation_store_observation_time_regressed",
        "candle_observation_store_historical_hash_recurrence_ambiguous",
        "candle_observation_store_revision_time_not_increasing",
        "pit_calendar_observation_time_regressed",
        "pit_calendar_historical_hash_recurrence_ambiguous",
        "pit_calendar_revision_time_not_increasing",
        "pit_timing_observation_time_regressed",
        "pit_timing_historical_hash_recurrence_ambiguous",
        "pit_timing_revision_time_not_increasing",
    )

    for reason in allowed:
        assert reason in sql
    assert "pit_daily_candle_as_of_reader_timeline_ambiguous" in sql
    assert "pit_timing_request_idempotency_conflict" not in sql
    assert "pit_timing_source_binding_mismatch" not in sql


def test_dedicated_verifier_covers_behavior_upgrade_and_future_convergence() -> None:
    verifier = _verifier()

    assert f'MIGRATION_NAME = "{MIGRATION.name}"' in verifier
    assert "migrations.index(target)" in verifier
    assert "migrations[:target_index]" in verifier
    assert "migrations[target_index + 1 :]" in verifier
    for check in (
        "verify_empty_envelope_and_acl",
        "verify_cutoff_and_timezone_equivalence",
        "verify_revision_history_and_oracle",
        "verify_same_availability_component_advance",
        "verify_pagination_cursor_and_snapshot",
        "verify_exact_occurrence_lineage",
        "verify_zero_write_contract",
    ):
        assert f"{check}(fresh)" in verifier
    assert "verify_populated_upgrade(upgrade)" in verifier


def test_verifier_is_available_for_migration_ci() -> None:
    verifier = _verifier()
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "FINAL=PASS pit_daily_candle_as_of_reader_verifier" in verifier
    if "verify_pit_daily_candle_as_of_reader.py" in workflow:
        assert workflow.count(
            '"supabase/verify_pit_daily_candle_as_of_reader.py"'
        ) == 2
        assert "python supabase/verify_pit_daily_candle_as_of_reader.py" in workflow
