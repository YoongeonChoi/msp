import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
MIGRATIONS = ROOT / "supabase" / "migrations"
MIGRATION = MIGRATIONS / "0011_data_api_grants.sql"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8").lower()


def _created_public_tables() -> set[str]:
    tables: set[str] = set()
    pattern = re.compile(r"create table(?: if not exists)? public\.([a-z_]+)\s*\(")
    for path in sorted(MIGRATIONS.glob("*.sql")):
        tables.update(pattern.findall(path.read_text(encoding="utf-8").lower()))
    return tables


def _grant_table_targets(sql: str, privilege: str, role: str) -> set[str]:
    targets: set[str] = set()
    pattern = re.compile(
        rf"grant {privilege} on table\s+(?P<tables>.*?)\s+to {role}\s*;",
        re.DOTALL,
    )
    for match in pattern.finditer(sql):
        for item in match.group("tables").split(","):
            normalized = item.strip()
            if normalized.startswith("public."):
                targets.add(normalized.removeprefix("public."))
    return targets


def test_data_api_grants_revoke_implicit_exposure_defaults() -> None:
    sql = _sql()

    required_fragments = [
        "revoke all on schema public from public, anon, authenticated, service_role",
        "revoke all on all tables in schema public from public, anon, authenticated, service_role",
        (
            "revoke all on all sequences in schema public "
            "from public, anon, authenticated, service_role"
        ),
        (
            "revoke execute on all functions in schema public "
            "from public, anon, authenticated, service_role"
        ),
        (
            "alter default privileges in schema public\n"
            "  revoke select, insert, update, delete on tables "
            "from public, anon, authenticated, service_role"
        ),
        (
            "alter default privileges in schema public\n"
            "  revoke execute on functions "
            "from public, anon, authenticated, service_role"
        ),
    ]

    for fragment in required_fragments:
        assert fragment in sql


def test_authenticated_select_grants_cover_every_public_table() -> None:
    sql = _sql()

    assert _grant_table_targets(sql, "select", "authenticated") == _created_public_tables()
    assert "to anon" not in sql
    assert "grant create on schema public" not in sql
    assert "grant usage on schema public to authenticated, service_role" in sql


def test_authenticated_write_grants_match_existing_rls_write_policies() -> None:
    sql = _sql()

    assert _grant_table_targets(sql, "insert", "authenticated") == {"manual_commands"}
    assert _grant_table_targets(sql, "update", "authenticated") == {
        "bot_settings",
        "manual_commands",
        "strategy_versions",
        "ai_upgrade_candidates",
    }
    assert _grant_table_targets(sql, "insert, update, delete", "authenticated") == {
        "sector_rules",
        "watchlist",
    }
    assert "grant delete on table" not in sql


def test_service_role_and_security_definer_execute_grants_are_explicit() -> None:
    sql = _sql()

    assert (
        "grant select, insert, update, delete on all tables in schema public to service_role"
        in sql
    )
    assert "grant usage, select, update on all sequences in schema public to service_role" in sql
    assert "grant execute on function public.is_admin() to authenticated, service_role" in sql
    assert "grant execute on function public.database_size_bytes() to service_role" in sql
    assert "grant execute on function public.run_retention_cleanup(boolean) to service_role" in sql
