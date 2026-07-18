from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
MIGRATION = (
    ROOT
    / "supabase"
    / "migrations"
    / "20260719010000_pit_daily_candle_timing_store.sql"
)
VERIFIER = ROOT / "supabase" / "verify_pit_daily_candle_timing_store.py"
CONFIG = ROOT / "supabase" / "config.toml"
WORKFLOW = ROOT / ".github" / "workflows" / "migration-check.yml"
REPOSITORY_SAFETY = ROOT / ".github" / "scripts" / "repository_safety.py"


def _sql() -> str:
    return " ".join(MIGRATION.read_text(encoding="utf-8").lower().split())


def _verifier() -> str:
    return VERIFIER.read_text(encoding="utf-8")


def test_migration_is_bounded_additive_and_has_exact_dependencies() -> None:
    sql = _sql()

    assert sql.startswith("begin;")
    assert sql.endswith("commit;")
    assert "set local lock_timeout = '5s'" in sql
    assert "set local statement_timeout = '90s'" in sql
    assert "private.pit_candle_observation_revisions" in sql
    assert " drop table " not in f" {sql} "
    assert " truncate " not in f" {sql} "
    assert "grant all" not in sql


def test_private_tables_are_rls_protected_and_evidence_is_append_only() -> None:
    sql = _sql()
    tables = (
        "private.pit_calendar_stream_heads",
        "private.pit_calendar_content_revisions",
        "private.pit_calendar_observation_quarantine",
        "private.pit_daily_candle_timing_heads",
        "private.pit_daily_candle_timing_revisions",
        "private.pit_daily_candle_timing_quarantine",
        "private.pit_daily_candle_timing_request_ledger",
        "private.pit_daily_candle_timing_request_receipts",
    )

    for table in tables:
        assert f"create table {table}" in sql
        assert f"alter table {table} enable row level security" in sql
    assert sql.count("execute function private.reject_append_only_mutation()") == 6
    assert "from public, anon, authenticated, service_role" in sql


def test_rpc_uses_request_and_stream_locks_before_mutable_head_reads() -> None:
    sql = _sql()

    assert sql.count("pg_catalog.pg_advisory_xact_lock") >= 3
    request_lock = sql.index("pg_catalog.pg_advisory_xact_lock")
    request_read = sql.index("pit_daily_candle_timing_request_ledger", request_lock)
    calendar_lock = sql.index("pg_catalog.pg_advisory_xact_lock", request_read)
    calendar_read = sql.index("pit_calendar_stream_heads", calendar_lock)
    timing_lock = sql.index("pg_catalog.pg_advisory_xact_lock", calendar_read)
    timing_read = sql.index("pit_daily_candle_timing_heads", timing_lock)

    assert request_lock < request_read < calendar_lock < calendar_read
    assert calendar_read < timing_lock < timing_read
    assert "for update" in sql[calendar_read:timing_read]


def test_rpc_recomputes_hashes_and_exactly_binds_immutable_sources() -> None:
    sql = _sql()

    for helper in (
        "private.pit_calendar_identity_sha256_v1",
        "private.pit_calendar_canonical_evidence_sha256_v1",
        "private.pit_daily_candle_timing_identity_sha256_v1",
        "private.pit_daily_candle_timing_evidence_sha256_v1",
    ):
        assert helper in sql
    assert "private.pit_sha256_text_v1" in sql
    assert "private.pit_candle_observation_revisions" in sql
    assert "pit_timing_candle_revision_missing" in sql
    assert "pit_timing_calendar_revision_missing" in sql
    assert "pit_timing_source_binding_mismatch" in sql
    assert "greatest(" in sql


def test_quarantine_and_request_retry_return_durable_receipts() -> None:
    sql = _sql()

    assert "pit_timing_request_idempotency_conflict" in sql
    assert "private.pit_daily_candle_timing_request_receipts" in sql
    assert "'quarantined'," in sql
    assert "return query select" in sql
    for reason in (
        "pit_calendar_observation_time_regressed",
        "pit_calendar_historical_hash_recurrence_ambiguous",
        "pit_calendar_revision_time_not_increasing",
        "pit_timing_observation_time_regressed",
        "pit_timing_historical_hash_recurrence_ambiguous",
        "pit_timing_revision_time_not_increasing",
    ):
        assert reason in sql


def test_exposed_rpc_is_invoker_with_exact_service_role_grant() -> None:
    sql = _sql()
    signature = "worker_api.append_pit_daily_candle_timing_evidence_v1(text,jsonb,jsonb)"
    wrapper = sql.index(
        "create or replace function worker_api.append_pit_daily_candle_timing_evidence_v1"
    )
    revoke = sql.index("revoke all on function", wrapper)
    wrapper_contract = sql[wrapper:revoke]

    assert "security invoker" in wrapper_contract
    assert "set search_path = ''" in wrapper_contract
    assert "security definer" not in wrapper_contract
    assert f"{signature} to service_role" in sql
    config = CONFIG.read_text(encoding="utf-8")
    assert 'schemas = ["api", "worker_api"]' in config
    assert 'schemas = ["api", "worker_api", "private"]' not in config


def test_dedicated_verifier_covers_target_and_future_upgrade_paths() -> None:
    verifier = _verifier()

    assert f'MIGRATION_NAME = "{MIGRATION.name}"' in verifier
    assert "migrations.index(target)" in verifier
    assert "migrations[:target_index]" in verifier
    assert "migrations[target_index + 1 :]" in verifier
    for check in (
        "verify_python_sql_golden_vectors",
        "verify_concurrent_exact_delivery",
        "verify_replay_and_corrections",
        "verify_exact_reobservation_has_no_clock_poisoning",
        "verify_calendar_revision_guards",
        "verify_timing_revision_guards",
        "verify_request_idempotency_conflict",
        "verify_missing_and_forged_sources",
        "verify_append_only_evidence",
    ):
        assert f"{check}(fresh)" in verifier


def test_ci_triggers_and_repository_safety_allowlist_cover_the_new_rpc() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    safety = REPOSITORY_SAFETY.read_text(encoding="utf-8")

    assert workflow.count(
        '"apps/worker/app/domain/market_data/point_in_time_calendar.py"'
    ) == 2
    assert workflow.count(
        '"apps/worker/app/domain/market_data/daily_candle_timing.py"'
    ) == 2
    assert workflow.count(
        '"supabase/verify_pit_daily_candle_timing_store.py"'
    ) == 2
    assert "python supabase/verify_pit_daily_candle_timing_store.py" in workflow
    assert (
        '"append_pit_daily_candle_timing_evidence_v1"'
        in safety
    )
