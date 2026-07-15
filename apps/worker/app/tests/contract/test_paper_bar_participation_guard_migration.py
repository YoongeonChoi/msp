from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
MIGRATION = (
    ROOT
    / "supabase"
    / "migrations"
    / "20260715041903_paper_bar_participation_guard.sql"
)


def test_paper_bar_participation_guard_is_atomic_and_source_visible() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    assert "pg_advisory_xact_lock" in sql
    assert "paper_bar_participation_capacity_exceeded" in sql
    assert "paper_bar_participation_evidence_missing" in sql
    assert "other_intent_filled_quantity" in sql
    assert "create trigger enforce_paper_bar_participation" in sql
    assert "create or replace function worker_api.load_claimed_paper_execution_bundle_v1" in sql
    assert "contract_qualification_ledger_invariants_not_proven" in sql
    assert "'ledger_invariants', 'production_order_network_zero'" in sql
    assert "function private.validate_qualification_run_v2" in sql
    assert "function worker_api.register_qualification_run_v2" in sql
    assert "contract-test-qualification-v2" in sql
    assert "function private.validate_qualification_run_v1" not in sql
