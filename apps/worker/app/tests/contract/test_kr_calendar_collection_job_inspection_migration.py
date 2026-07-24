from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
MIGRATION = (
    ROOT
    / "supabase"
    / "migrations"
    / "20260719080000_kr_calendar_collection_job_inspection.sql"
)
REPOSITORY_SAFETY = ROOT / ".github" / "scripts" / "repository_safety.py"
VERIFIER = ROOT / "supabase" / "verify_kr_calendar_collection_job_store.py"
MIGRATION_WORKFLOW = ROOT / ".github" / "workflows" / "migration-check.yml"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8").lower()


def _private_body(sql: str) -> str:
    start = sql.index(
        "create or replace function "
        "private.inspect_kr_calendar_collection_job_v1_impl("
    )
    end = sql.index(
        "create or replace function "
        "worker_api.inspect_kr_calendar_collection_job_v1(",
        start,
    )
    return sql[start:end]


def test_inspection_migration_is_atomic_bounded_and_dependency_guarded() -> None:
    sql = _sql()

    assert sql.startswith("begin;\n")
    assert sql.rstrip().endswith("commit;")
    assert "set local lock_timeout = '5s'" in sql
    assert "set local statement_timeout = '90s'" in sql
    for dependency in (
        "private.kr_calendar_collection_jobs",
        "private.require_service_role()",
        "private.kr_calendar_collection_uuid4_v1(text)",
        "private.kr_calendar_collection_snapshot_v1(uuid)",
    ):
        assert dependency in sql
    assert "kr_calendar_collection_job_inspection_dependency_missing" in sql


def test_private_implementation_is_stable_definer_and_read_only() -> None:
    sql = _sql()
    body = _private_body(sql)

    assert (
        "create or replace function "
        "private.inspect_kr_calendar_collection_job_v1_impl(" in body
    )
    assert "returns table(job_found boolean, snapshot jsonb)" in body
    assert "language plpgsql\nstable\nsecurity definer" in body
    assert "set search_path = ''" in body
    assert "perform private.require_service_role()" in body
    for forbidden in (
        "insert into",
        "update ",
        "delete from",
        "truncate ",
        "for update",
        "pg_advisory",
        "clock_timestamp",
    ):
        assert forbidden not in body


def test_inspection_requires_exact_uuid4_before_casting() -> None:
    body = _private_body(_sql())

    validation = "private.kr_calendar_collection_uuid4_v1(p_job_id)"
    cast = "job_id_value := p_job_id::uuid"
    assert validation in body
    assert "when invalid_text_representation then" in body
    assert "kr_calendar_collection_job_argument_invalid" in body
    assert "using errcode = '22023'" in body
    assert body.index(validation) < body.index(cast)


def test_missing_and_present_jobs_have_one_canonical_response_shape() -> None:
    body = _private_body(_sql())

    assert "if not exists (" in body
    assert "from private.kr_calendar_collection_jobs as stored" in body
    assert "select false as job_found, null::jsonb as snapshot" in body
    assert "private.kr_calendar_collection_snapshot_v1(job_id_value) as snapshot" in body
    assert body.count("return query") == 2
    assert "load_or_create" not in body


def test_worker_wrapper_is_stable_invoker_and_only_delegates() -> None:
    sql = _sql()
    start = sql.index(
        "create or replace function "
        "worker_api.inspect_kr_calendar_collection_job_v1("
    )
    end = sql.index("revoke all on function", start)
    wrapper = sql[start:end]

    assert "returns table(job_found boolean, snapshot jsonb)" in wrapper
    assert "language sql\nstable\nsecurity invoker" in wrapper
    assert "set search_path = ''" in wrapper
    assert (
        "from private.inspect_kr_calendar_collection_job_v1_impl(p_job_id)"
        in wrapper
    )


def test_both_functions_are_service_role_only_with_catalog_postcondition() -> None:
    sql = _sql()

    assert "from public, anon, authenticated, authenticator, service_role" in sql
    assert "to service_role" in sql
    assert "to anon" not in sql
    assert "to authenticated" not in sql
    assert "to authenticator" not in sql
    for marker in (
        "impl_contract_count",
        "wrapper_contract_count",
        "function_owner_count",
        "service_function_acl_count",
        "forbidden_function_acl_count",
        "forbidden_effective_privilege_count",
        "procedure.prosecdef",
        "procedure.provolatile = 's'",
        "procedure.proconfig = array['search_path=\"\"']::text[]",
        "pg_catalog.aclexplode(",
        "pg_catalog.has_function_privilege(",
        "acl.privilege_type = 'execute'",
        "kr_calendar_collection_job_inspection_security_contract_failed",
    ):
        assert marker in sql


def test_repository_safety_verifier_and_ci_cover_the_inspection_rpc() -> None:
    rpc = "inspect_kr_calendar_collection_job_v1"
    sql = _sql()
    repository_safety = REPOSITORY_SAFETY.read_text(encoding="utf-8")
    verifier = VERIFIER.read_text(encoding="utf-8")
    workflow = MIGRATION_WORKFLOW.read_text(encoding="utf-8")

    assert f'"{rpc}"' in repository_safety
    assert f'"{rpc}"' in verifier
    assert "verify_read_only_inspection" in verifier
    assert "postgrest_inspection" in verifier
    assert '"supabase/migrations/**"' in workflow
    assert '"supabase/verify_kr_calendar_collection_job_store.py"' in workflow
    assert sql.count("does not authorize mutation, retry, or recovery") == 2
