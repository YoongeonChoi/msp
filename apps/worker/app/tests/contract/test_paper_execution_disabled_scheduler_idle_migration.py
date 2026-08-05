from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
MIGRATION = (
    ROOT
    / "supabase"
    / "migrations"
    / "20260726150000_paper_execution_disabled_scheduler_idle.sql"
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8").lower()


def test_disabled_paper_execution_returns_idle_only_with_current_worker_lease() -> None:
    sql = _sql()

    assert "private.paper_execution_claim_is_disabled_v1" in sql
    assert "perform private.require_service_role()" in sql
    assert "control.execution_enabled" in sql
    assert "lease.holder_id = p_worker_id::text" in sql
    assert "lease.release_sha = p_release_sha" in sql
    assert "lease.expires_at > authorization_time" in sql
    assert "paper_source_gate_lease_or_qualification_stale" in sql


def test_enabled_paper_execution_still_uses_full_qualification_gate() -> None:
    sql = _sql()

    assert "if execution_enabled_value then" in sql
    assert "return false" in sql
    assert "private.claim_paper_execution_v1_impl" in sql
    assert "if private.paper_execution_claim_is_disabled_v1" in sql
    assert "return query" in sql


def test_claim_gate_remains_service_role_only() -> None:
    sql = _sql()

    assert "from public, anon, authenticated" in sql
    assert "to service_role" in sql
