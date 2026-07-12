from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
MIGRATION = ROOT / "supabase" / "migrations" / "0012_runtime_safety_invariants.sql"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8").lower()


def test_live_execution_requires_a_fresh_approval_for_every_complete_transition() -> None:
    sql = _sql()

    required = [
        "old_executable := old.enabled is true",
        "new_executable := new.enabled is true",
        "and old.mode = 'live'",
        "and new.mode = 'live'",
        "and old.live_order_allowed is true",
        "and new.live_order_allowed is true",
        "if new_executable and not old_executable then",
        "live_execution_requires_fresh_accepted_manual_command",
        "for update skip locked",
        "set status = 'applied'",
    ]
    for fragment in required:
        assert fragment in sql


def test_incomplete_live_permission_state_is_impossible() -> None:
    sql = _sql()

    assert "bot_settings_live_execution_state_check" in sql
    assert "live_order_allowed is false" in sql
    assert "or (enabled is true and mode = 'live')" in sql
    assert "live_order_allowed_requires_live_mode" in sql
    assert "live_order_allowed_requires_enabled_bot" in sql


def test_numeric_storage_and_daily_order_limit_are_bounded() -> None:
    sql = _sql()

    assert "bot_settings_max_daily_order_count_check" in sql
    assert "max_daily_order_count between 1 and 1000" in sql
    assert "max_order_amount_krw between 1 and 100000000" in sql
    assert "alter column market_value_krw type bigint" in sql
    assert "alter column unrealized_pnl_krw type bigint" in sql


def test_strategy_configuration_and_promotion_are_database_enforced() -> None:
    sql = _sql()

    required = [
        "guard_strategy_version_safety",
        "before insert or update on public.strategy_versions",
        "strategy_weights_invalid",
        "strategy_thresholds_invalid",
        "promoted_strategy_is_immutable",
        "strategy_status_transition_invalid",
        "strategy_activation_requires_authenticated_approver",
        "strategy_self_approval_forbidden",
        "new.approved_by := actor",
        "new.approved_at := now()",
        "active_strategy_requires_approval",
    ]
    for fragment in required:
        assert fragment in sql


def test_migration_preserves_rls_and_public_write_boundaries() -> None:
    sql = _sql()

    for forbidden in (
        "disable row level security",
        " to anon",
        " to public",
        "grant insert",
        "grant update",
    ):
        assert forbidden not in sql
