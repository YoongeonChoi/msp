from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
MIGRATION = (
    ROOT
    / "supabase"
    / "migrations"
    / "20260715041915_paper_evidence_and_sell_reservation_guards.sql"
)


def test_paper_fill_guard_pins_one_exact_bar_evidence_identity() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    assert "paper_bar_participation_committed_evidence_conflict" in sql
    assert "paper_bar_participation_committed_evidence_missing" in sql
    assert "paper_bar_participation_evidence_conflict" in sql
    assert "candidate.fixture_series_id" in sql
    assert "series.volume_source" in sql
    assert "series.volume_evidence_sha256" in sql
    assert "bar.source_sha256" in sql
    assert "bar.volume" in sql
    assert "pg_advisory_xact_lock" in sql
    assert "private.utc_iso8601(bar_completed_at)" in sql
    assert "lock table private.fills in share row exclusive mode" in sql


def test_sell_reservation_guard_is_serialized_and_release_aware() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    assert "create trigger enforce_single_active_sell_reservation_v1" in sql
    assert "drop trigger" not in sql
    assert "active_sell_reservation_exists" in sql
    assert "multiple_active_sell_reservations_present" in sql
    assert "order by event.event_sequence desc" in sql
    assert "coalesce(latest.remaining_quantity, reservation.reserved_quantity) > 0" in sql
    assert "new.account_id || '|' || intent_symbol" in sql
    assert "lock table private.order_reservations in share row exclusive mode" in sql


def test_guard_rollout_is_atomic_and_uses_one_way_lock_order() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    index_offset = sql.index(
        "create index if not exists "
        "order_intents_active_sell_reservation_lookup_idx"
    )
    fill_lock_offset = sql.index(
        "lock table private.fills in share row exclusive mode"
    )
    reservation_lock_offset = sql.index(
        "lock table private.order_reservations in share row exclusive mode"
    )
    fill_preflight_offset = sql.index(
        "paper_bar_participation_committed_evidence_conflict"
    )
    fill_function_offset = sql.index(
        "create or replace function "
        "private.enforce_paper_bar_participation_guard()"
    )
    sell_trigger_offset = sql.index(
        "create trigger enforce_single_active_sell_reservation_v1"
    )

    assert sql.splitlines()[4].strip() == "begin;"
    assert (
        index_offset
        < fill_lock_offset
        < reservation_lock_offset
        < fill_preflight_offset
        < fill_function_offset
        < sell_trigger_offset
        < sql.rindex("commit;")
    )
    assert sql.rstrip().endswith("commit;")


def test_execution_conflict_guard_functions_are_not_publicly_executable() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    assert sql.count("security invoker") == 2
    assert sql.count("set search_path = ''") == 2
    assert (
        "revoke all on function private.enforce_paper_bar_participation_guard()"
        in sql
    )
    assert (
        "revoke all on function private.enforce_single_active_sell_reservation_v1()"
        in sql
    )
