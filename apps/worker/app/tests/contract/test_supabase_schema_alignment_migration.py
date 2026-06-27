from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
MIGRATION = ROOT / "supabase" / "migrations" / "0005_schema_alignment.sql"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8").lower()


def test_schema_alignment_migration_adds_required_columns() -> None:
    sql = _sql()

    required_fragments = [
        "alter table public.worker_heartbeats",
        "add column if not exists memory_mb numeric",
        "add column if not exists last_loop_ms integer",
        "add column if not exists message text",
        "alter table public.api_health",
        "add column if not exists error_code text",
        "add column if not exists checked_at timestamptz default now()",
        "alter column checked_at set default now()",
    ]

    for fragment in required_fragments:
        assert fragment in sql

    assert sql.count("add column if not exists message text") >= 2


def test_schema_alignment_migration_deduplicates_watchlist_before_unique_index() -> None:
    sql = _sql()

    assert "with ranked_watchlist as" in sql
    assert "partition by symbol, market" in sql
    assert "duplicate_rank > 1" in sql
    assert sql.index("with ranked_watchlist as") < sql.index(
        "idx_watchlist_symbol_market_unique"
    )


def test_schema_alignment_migration_creates_expected_indexes() -> None:
    sql = _sql()

    expected_indexes = [
        "idx_worker_heartbeats_created_at",
        "idx_api_health_provider_checked_at",
        "idx_watchlist_enabled",
        "idx_orders_created_at",
        "idx_orders_symbol_created_at",
        "idx_decision_snapshots_symbol_time",
        "idx_watchlist_symbol_market_unique",
        "idx_strategy_versions_version_unique",
    ]

    for index_name in expected_indexes:
        assert index_name in sql


def test_schema_alignment_migration_does_not_weaken_rls_or_public_writes() -> None:
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
