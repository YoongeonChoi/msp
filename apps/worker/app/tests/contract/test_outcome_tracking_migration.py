from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
MIGRATION = ROOT / "supabase" / "migrations" / "0006_outcome_tracking.sql"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8").lower()


def test_outcome_tracking_migration_adds_required_columns() -> None:
    sql = _sql()

    required_fragments = [
        "add column if not exists return_1d numeric(12,6)",
        "add column if not exists return_5d numeric(12,6)",
        "add column if not exists return_20d numeric(12,6)",
        "add column if not exists max_drawdown_20d numeric(12,6)",
        "add column if not exists hit_target boolean",
        "add column if not exists hit_stop boolean",
        "add column if not exists realized_pnl_krw integer",
        "add column if not exists price_at_decision numeric(20,6)",
        "add column if not exists outcome_status text not null default 'pending'",
        "add column if not exists updated_at timestamptz not null default now()",
    ]

    for fragment in required_fragments:
        assert fragment in sql


def test_outcome_tracking_migration_rolls_up_legacy_horizon_rows_before_unique_index() -> None:
    sql = _sql()

    assert "outcome_rollup as" in sql
    assert "filter (where horizon_days = 1)" in sql
    assert "filter (where horizon_days = 5)" in sql
    assert "filter (where horizon_days = 20)" in sql
    assert "duplicate_rank > 1" in sql
    assert sql.index("outcome_rollup as") < sql.index("idx_outcomes_decision_id_unique")


def test_outcome_tracking_migration_creates_idempotency_indexes() -> None:
    sql = _sql()

    assert "create unique index if not exists idx_outcomes_decision_id_unique" in sql
    assert "on public.outcomes(decision_id)" in sql
    assert "create index if not exists idx_outcomes_updated_at" in sql
    assert "on public.outcomes(updated_at desc)" in sql


def test_outcome_tracking_migration_does_not_weaken_rls_or_public_writes() -> None:
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
