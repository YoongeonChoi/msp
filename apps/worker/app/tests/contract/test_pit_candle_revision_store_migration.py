from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
MIGRATION = (
    ROOT
    / "supabase"
    / "migrations"
    / "20260719001947_pit_candle_revision_store.sql"
)
WORKFLOW = ROOT / ".github" / "workflows" / "migration-check.yml"


def _sql() -> str:
    return " ".join(MIGRATION.read_text(encoding="utf-8").lower().split())


def test_migration_is_bounded_and_additive() -> None:
    sql = _sql()

    assert sql.startswith("begin;")
    assert sql.endswith("commit;")
    assert "set local lock_timeout = '5s'" in sql
    assert "set local statement_timeout = '90s'" in sql
    assert " drop table " not in f" {sql} "
    assert " truncate " not in f" {sql} "


def test_private_store_is_revisioned_append_only_and_not_directly_granted() -> None:
    sql = _sql()

    for table in (
        "private.pit_candle_stream_heads",
        "private.pit_candle_observation_revisions",
        "private.pit_candle_observation_quarantine",
    ):
        assert f"create table {table}" in sql
        assert f"alter table {table} enable row level security" in sql
    assert sql.count("execute function private.reject_append_only_mutation()") == 2
    assert "unique (idempotency_key, revision)" in sql
    assert "unique (idempotency_key, canonical_observation_sha256)" in sql
    assert "from public, anon, authenticated, service_role" in sql
    assert "grant all" not in sql


def test_rpc_recomputes_hashes_and_serializes_before_reading_head() -> None:
    sql = _sql()

    assert "private.pit_candle_identity_sha256_v1" in sql
    assert "private.pit_candle_observation_sha256_v1" in sql
    assert "extensions.digest" in sql
    lock_position = sql.index("pg_catalog.pg_advisory_xact_lock")
    head_read_position = sql.index("select stream.* into head")
    assert lock_position < head_read_position
    assert "for update" in sql[head_read_position:]
    assert "candidate_sha256_value <> expected_candidate_sha256" in sql


def test_quarantine_returns_receipt_without_rolling_back_evidence() -> None:
    sql = _sql()
    quarantine_call = sql.index(
        "quarantine_id_value := private.quarantine_pit_candle_observation_v1"
    )
    quarantine_receipt = sql.index(
        "'quarantined'::text", quarantine_call
    )
    next_revision = sql.index(
        "next_revision := head.latest_revision + 1", quarantine_receipt
    )

    assert "return query select" in sql[quarantine_call:quarantine_receipt]
    assert "return;" in sql[quarantine_receipt:next_revision]
    assert "raise exception" not in sql[quarantine_call:next_revision]


def test_exposed_function_is_invoker_and_service_role_only() -> None:
    sql = _sql()

    wrapper = sql.index(
        "create or replace function worker_api.append_pit_candle_observation_v1"
    )
    revoke = sql.index("revoke all on function", wrapper)
    wrapper_contract = sql[wrapper:revoke]
    assert "security invoker" in wrapper_contract
    assert "set search_path = ''" in wrapper_contract
    assert "security definer" not in wrapper_contract
    assert (
        "worker_api.append_pit_candle_observation_v1(jsonb) to service_role"
        in sql
    )


def test_dedicated_behavior_verifier_is_a_required_migration_check() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert workflow.count(
        '"apps/worker/app/domain/market_data/point_in_time.py"'
    ) == 2
    assert "supabase/verify_pit_candle_revision_store.py" in workflow
    assert "python supabase/verify_pit_candle_revision_store.py" in workflow
