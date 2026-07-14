from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
MIGRATION = ROOT / "supabase" / "migrations" / "0015_paper_order_execution_details.sql"


def _sql() -> str:
    return " ".join(MIGRATION.read_text(encoding="utf-8").lower().split())


def test_order_execution_details_are_nullable_for_existing_rows() -> None:
    sql = _sql()

    assert "add column if not exists quantity integer" in sql
    assert "add column if not exists price_krw integer" in sql
    assert "not null" not in sql


def test_order_execution_details_require_positive_values_when_present() -> None:
    sql = _sql()

    assert "orders_quantity_positive" in sql
    assert "check (quantity is null or quantity > 0)" in sql
    assert "orders_price_krw_positive" in sql
    assert "check (price_krw is null or price_krw > 0)" in sql


def test_order_execution_details_migration_does_not_change_rls_or_grants() -> None:
    sql = _sql()

    assert "disable row level security" not in sql
    assert "create policy" not in sql
    assert " grant " not in f" {sql} "
