from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
MIGRATION = (
    ROOT
    / "supabase"
    / "migrations"
    / "20260719070000_kr_calendar_collection_job_conflict_boundary.sql"
)
VERIFIER = ROOT / "supabase" / "verify_kr_calendar_collection_job_store.py"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8").lower()


def test_migration_replaces_only_the_four_mutating_worker_wrappers() -> None:
    sql = _sql()

    expected = (
        "begin_kr_calendar_collection_date_attempt_v1",
        "pause_kr_calendar_collection_date_attempt_v1",
        "block_kr_calendar_collection_date_attempt_v1",
        "confirm_kr_calendar_collection_date_v1",
    )
    for function_name in expected:
        assert f"create or replace function worker_api.{function_name}(" in sql
        assert f"private.{function_name}_impl(" in sql

    assert "load_or_create_kr_calendar_collection_job_v1" not in sql
    assert sql.count("create or replace function worker_api.") == 4
    assert "drop function" not in sql
    assert "drop table" not in sql
    assert "truncate " not in sql
    assert "delete from" not in sql


def test_each_wrapper_remaps_only_owned_conflicts_to_bounded_response() -> None:
    sql = _sql()

    assert sql.count("language plpgsql") == 4
    assert sql.count("security invoker") == 4
    assert sql.count("set search_path = ''") == 4
    assert sql.count("when sqlstate '40001' then") == 4
    assert sql.count("raise sqlstate 'pt409' using message = sqlerrm") == 4
    for message in (
        "kr_calendar_collection_job_spec_hash_mismatch",
        "kr_calendar_collection_job_revision_conflict",
        "kr_calendar_collection_job_clock_regressed",
        "kr_calendar_collection_job_attempt_fence_mismatch",
    ):
        assert sql.count(f"'{message}'") == 4
    assert sql.count("    raise;\n") == 4
    assert "automatic retry" not in sql
    assert "require reload" in sql


def test_migration_is_atomic_bounded_and_checks_final_security_state() -> None:
    sql = _sql()

    assert sql.startswith("begin;\n")
    assert sql.rstrip().endswith("commit;")
    assert "set local lock_timeout = '5s'" in sql
    assert "set local statement_timeout = '90s'" in sql
    assert "foreach function_oid in array" in sql
    assert "procedure.prosecdef" in sql
    assert "has_function_privilege('public'" in sql
    assert "has_function_privilege('anon'" in sql
    assert "has_function_privilege('authenticated'" in sql
    assert "has_function_privilege('authenticator'" in sql
    assert "has_function_privilege(\n         'service_role'" in sql


def test_conflict_wrappers_remain_service_role_only() -> None:
    sql = _sql()

    assert "from public, anon, authenticated, authenticator, service_role" in sql
    assert "to service_role" in sql
    assert "to anon" not in sql
    assert "to authenticated" not in sql
    assert "to authenticator" not in sql


def test_postgrest_verifier_requires_exact_pt409_without_state_change() -> None:
    verifier = VERIFIER.read_text(encoding="utf-8")

    for marker in (
        "stale_before = job_state_fingerprint",
        "expected_status=409",
        'expected_code="PT409"',
        'expected_message="kr_calendar_collection_job_revision_conflict"',
        "stale PostgREST CAS changed durable job state",
    ):
        assert marker in verifier

    assert 'expected_code="40001"' not in verifier
