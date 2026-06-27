from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
MIGRATION = ROOT / "supabase" / "migrations" / "0007_backtest_runs.sql"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8").lower()


def test_backtest_runs_migration_creates_required_table_and_columns() -> None:
    sql = _sql()

    required_fragments = [
        "create table if not exists public.backtest_runs",
        "strategy text not null",
        "period_start date not null",
        "period_end date not null",
        "total_return numeric(14,8)",
        "max_drawdown numeric(14,8)",
        "sharpe_like numeric(14,8)",
        "win_rate numeric(14,8)",
        "number_of_trades integer",
        "blocked_reason_counts jsonb",
        "assumptions jsonb",
    ]

    for fragment in required_fragments:
        assert fragment in sql


def test_backtest_runs_migration_enables_rls_and_indexes() -> None:
    sql = _sql()

    assert "alter table public.backtest_runs enable row level security" in sql
    assert "idx_backtest_runs_created_at" in sql
    assert "idx_backtest_runs_strategy_period" in sql


def test_backtest_runs_migration_does_not_allow_public_writes() -> None:
    sql = _sql()

    forbidden_fragments = [
        "disable row level security",
        "create policy",
        " to anon",
        " to public",
        "grant insert",
        "grant update",
    ]

    for fragment in forbidden_fragments:
        assert fragment not in sql
