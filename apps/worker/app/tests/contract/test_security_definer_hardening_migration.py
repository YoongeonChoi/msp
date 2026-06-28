from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
MIGRATION = ROOT / "supabase" / "migrations" / "0010_security_definer_hardening.sql"


def test_security_definer_functions_use_empty_search_path() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    assert sql.count("security definer") == 3
    assert sql.count("set search_path = ''") == 3
    assert "set search_path = public" not in sql


def test_destructive_retention_rpc_is_not_publicly_executable() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    assert (
        "revoke execute on function public.run_retention_cleanup(boolean) "
        "from public, anon, authenticated"
    ) in sql
    assert "grant execute on function public.run_retention_cleanup(boolean) to service_role" in sql


def test_readonly_admin_helper_remains_available_to_authenticated_policies() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    assert "revoke execute on function public.is_admin() from public, anon" in sql
    assert "grant execute on function public.is_admin() to authenticated, service_role" in sql
    assert "where user_id = (select auth.uid())" in sql
