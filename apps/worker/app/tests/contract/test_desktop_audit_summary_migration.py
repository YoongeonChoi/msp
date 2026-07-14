from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
MIGRATION = ROOT / "supabase" / "migrations" / "0014_desktop_audit_summary.sql"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8").lower()


def test_desktop_audit_summary_returns_only_safe_columns() -> None:
    sql = _sql()

    assert "create or replace function public.get_audit_log_summaries" in sql
    returns = sql.split("returns table", maxsplit=1)[1].split(")", maxsplit=1)[0]
    assert "changed_fields text[]" in returns
    assert "actor_user_id" not in returns
    assert "before_snapshot" not in returns
    assert "after_snapshot" not in returns


def test_desktop_audit_summary_requires_admin_and_hides_raw_table() -> None:
    sql = _sql()

    assert "security definer" in sql
    assert "set search_path = ''" in sql
    assert "if not (select public.is_admin())" in sql
    assert "revoke select on table public.audit_logs from authenticated" in sql


def test_desktop_audit_summary_has_explicit_execute_grants() -> None:
    sql = _sql()

    assert (
        "revoke execute on function public.get_audit_log_summaries(integer)\n"
        "  from public, anon, authenticated, service_role"
    ) in sql
    assert (
        "grant execute on function public.get_audit_log_summaries(integer)\n"
        "  to authenticated"
    ) in sql
    assert "grant execute on function public.get_audit_log_summaries(integer) to anon" not in sql
