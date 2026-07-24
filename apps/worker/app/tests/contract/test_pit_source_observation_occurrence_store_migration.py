from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
MIGRATION = (
    ROOT
    / "supabase"
    / "migrations"
    / "20260719020000_pit_source_observation_occurrence_store.sql"
)
VERIFIER = (
    ROOT / "supabase" / "verify_pit_source_observation_occurrence_store.py"
)
WORKFLOW = ROOT / ".github" / "workflows" / "migration-check.yml"


def _sql() -> str:
    return " ".join(MIGRATION.read_text(encoding="utf-8").lower().split())


def _verifier() -> str:
    return VERIFIER.read_text(encoding="utf-8")


def test_migration_is_forward_only_bounded_and_additive() -> None:
    sql = _sql()

    assert sql.startswith("begin;")
    assert sql.endswith("commit;")
    assert "set local lock_timeout = '5s'" in sql
    assert "set local statement_timeout = '90s'" in sql
    assert "private.pit_candle_observation_revisions" in sql
    assert "private.pit_calendar_content_revisions" in sql
    assert "private.pit_daily_candle_timing_revisions" in sql
    assert " drop table " not in f" {sql} "
    assert " truncate " not in f" {sql} "
    assert "grant all" not in sql


def test_occurrence_ledgers_are_private_rls_and_append_only() -> None:
    sql = _sql()
    tables = (
        "private.pit_candle_observation_occurrences",
        "private.pit_calendar_observation_occurrences",
    )

    for table in tables:
        assert f"create table {table}" in sql
        assert f"alter table {table} enable row level security" in sql
        assert table in sql
    assert sql.count("execute function private.reject_append_only_mutation()") >= 2
    assert "from public, anon, authenticated, service_role" in sql
    assert "record_origin" in sql
    assert "observation_payload" in sql
    assert "received_at" in sql


def test_occurrences_have_exact_content_and_observation_uniqueness() -> None:
    sql = _sql()

    for column in (
        "content_revision_id",
        "observed_at",
        "observation_payload",
        "record_origin",
    ):
        assert column in sql
    assert "canonical_observation_sha256" in sql
    assert "canonical_evidence_sha256" in sql
    assert "pit_candle_observation_occurrences" in sql
    assert "pit_calendar_observation_occurrences" in sql
    assert "unique" in sql
    assert "foreign key" in sql or "references private." in sql


def test_timing_revisions_bind_both_exact_occurrences() -> None:
    sql = _sql()

    assert "candle_occurrence_id" in sql
    assert "calendar_occurrence_id" in sql
    assert (
        "references private.pit_candle_observation_occurrences" in sql
    )
    assert (
        "references private.pit_calendar_observation_occurrences" in sql
    )
    assert "alter column candle_occurrence_id set not null" in sql
    assert "alter column calendar_occurrence_id set not null" in sql
    assert "unique (candle_occurrence_id, calendar_occurrence_id)" in sql
    assert (
        "on private.pit_daily_candle_timing_revisions "
        "( candle_occurrence_id, candle_revision_id )" in sql
    )
    assert (
        "on private.pit_daily_candle_timing_revisions "
        "( calendar_occurrence_id, calendar_revision_id )" in sql
    )


def test_upgrade_backfills_content_head_and_timing_links_before_not_null() -> None:
    sql = _sql()

    candle_backfill = sql.index(
        "insert into private.pit_candle_observation_occurrences"
    )
    calendar_backfill = sql.index(
        "insert into private.pit_calendar_observation_occurrences"
    )
    timing_backfill = sql.index(
        "update private.pit_daily_candle_timing_revisions"
    )
    candle_not_null = sql.index(
        "alter column candle_occurrence_id set not null"
    )
    calendar_not_null = sql.index(
        "alter column calendar_occurrence_id set not null"
    )

    assert candle_backfill < timing_backfill < candle_not_null
    assert calendar_backfill < timing_backfill < calendar_not_null
    assert "pit_candle_stream_heads" in sql[candle_backfill:timing_backfill]
    assert "pit_calendar_stream_heads" in sql[calendar_backfill:timing_backfill]
    assert "last_seen_observed_at" in sql


def test_private_v1_implementations_are_replaced_without_api_shape_change() -> None:
    sql = _sql()

    assert (
        "create or replace function private.append_pit_candle_observation_v1_impl"
        in sql
    )
    assert (
        "create or replace function "
        "private.append_pit_daily_candle_timing_evidence_v1_impl" in sql
    )
    assert (
        "create or replace function worker_api.append_pit_candle_observation_v1"
        not in sql
    )
    assert (
        "create or replace function "
        "worker_api.append_pit_daily_candle_timing_evidence_v1" not in sql
    )
    for reason in (
        "candle_observation_store_observation_time_regressed",
        "candle_observation_store_historical_hash_recurrence_ambiguous",
        "pit_calendar_observation_time_regressed",
        "pit_calendar_historical_hash_recurrence_ambiguous",
        "pit_timing_observation_time_regressed",
        "pit_timing_historical_hash_recurrence_ambiguous",
    ):
        assert reason in sql


def test_rpc_serializes_before_occurrence_and_head_reads() -> None:
    sql = _sql()

    candle_impl = sql.index(
        "create or replace function private.append_pit_candle_observation_v1_impl"
    )
    timing_impl = sql.index(
        "create or replace function "
        "private.append_pit_daily_candle_timing_evidence_v1_impl"
    )
    candle_contract = sql[candle_impl:timing_impl]
    timing_contract = sql[timing_impl:]

    candle_lock = candle_contract.index("pg_catalog.pg_advisory_xact_lock")
    candle_head_read = candle_contract.index(
        "select stream.* into head", candle_lock
    )
    request_lock = timing_contract.index("pg_catalog.pg_advisory_xact_lock")
    request_read = timing_contract.index(
        "select ledger.original_request_sha256", request_lock
    )
    calendar_lock = timing_contract.index(
        "pg_catalog.pg_advisory_xact_lock", request_read
    )
    calendar_head_read = timing_contract.index(
        "select stream.* into calendar_head", calendar_lock
    )
    timing_lock = timing_contract.index(
        "pg_catalog.pg_advisory_xact_lock", calendar_head_read
    )
    timing_head_read = timing_contract.index(
        "select stream.* into timing_head", timing_lock
    )

    assert candle_lock < candle_head_read
    assert request_lock < request_read < calendar_lock < calendar_head_read
    assert calendar_head_read < timing_lock < timing_head_read
    assert timing_contract.count("pg_catalog.pg_advisory_xact_lock") >= 3
    assert "for update" in candle_contract
    assert "for update" in timing_contract


def test_dedicated_verifier_covers_upgrade_and_adversarial_paths() -> None:
    verifier = _verifier()

    assert f'MIGRATION_NAME = "{MIGRATION.name}"' in verifier
    assert "migrations.index(target)" in verifier
    assert "migrations[:target_index]" in verifier
    assert "migrations[target_index + 1 :]" in verifier
    for check in (
        "verify_catalog_acl_and_append_only",
        "verify_exact_occurrence_binding",
        "verify_same_content_later_observations",
        "verify_same_availability_component_monotonicity",
        "verify_regression_and_aba_guards",
        "verify_concurrent_delivery",
        "verify_request_replay",
    ):
        assert f"{check}(fresh)" in verifier
    assert "verify_populated_upgrade(upgrade)" in verifier


def test_migration_workflow_requires_the_dedicated_verifier() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert workflow.count(
        '"supabase/verify_pit_source_observation_occurrence_store.py"'
    ) == 2
    assert (
        "python supabase/verify_pit_source_observation_occurrence_store.py"
        in workflow
    )
