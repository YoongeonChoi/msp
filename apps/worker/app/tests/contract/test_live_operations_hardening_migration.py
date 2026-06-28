from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
MIGRATION = ROOT / "supabase" / "migrations" / "0009_live_operations_hardening.sql"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8").lower()


def test_live_operations_migration_adds_manual_live_enable_controls() -> None:
    sql = _sql()

    required_fragments = [
        "add column if not exists expires_at timestamptz",
        "add column if not exists reviewed_by uuid references auth.users(id)",
        "add column if not exists reviewed_at timestamptz",
        "request_live_enable",
        "manual_commands_live_enable_acceptance_check",
        "manual_commands_live_enable_applied_check",
        "requested_by is not null",
        "reviewed_by <> requested_by",
        "expires_at > reviewed_at",
        "and applied_at is null",
        "applied_at is not null",
        "expires_at > applied_at",
        "applied_at >= reviewed_at",
        "manual_commands_live_enable_request_evidence_check",
        "nullif(btrim(payload->>'provider_contract_version'), '') is not null",
        "nullif(btrim(payload->>'risk_report_id'), '') is not null",
        "nullif(btrim(payload->>'release_version'), '') is not null",
        "expires_at <= created_at + interval '4 hours'",
    ]

    for fragment in required_fragments:
        assert fragment in sql


def test_live_operations_migration_extends_order_status_for_reconciliation() -> None:
    sql = _sql()

    required_fragments = [
        "drop constraint orders_status_check",
        "add constraint orders_status_check",
        "'partial_filled'",
        "'canceled'",
        "'unknown_requires_manual_check'",
    ]

    for fragment in required_fragments:
        assert fragment in sql


def test_live_operations_migration_blocks_live_flag_without_fresh_approval() -> None:
    sql = _sql()

    required_fragments = [
        "guard_bot_settings_live_enable",
        "before update on public.bot_settings",
        "new.live_order_allowed is true",
        "new.mode <> 'live'",
        "new.enabled is not true",
        "live_order_allowed_requires_fresh_accepted_manual_command",
        "live_enable_command_id uuid",
        "into live_enable_command_id",
        "expires_at > now()",
        "status = 'accepted'",
        "applied_at is null",
        "reviewed_by <> requested_by",
        "for update skip locked",
        "set status = 'applied'",
        "applied_at = now()",
    ]

    for fragment in required_fragments:
        assert fragment in sql


def test_live_operations_migration_blocks_self_reviewed_live_enable() -> None:
    sql = _sql()

    required_fragments = [
        "guard_manual_command_live_enable_review",
        "before insert or update on public.manual_commands",
        "live_enable_request_requires_authenticated_actor",
        "live_enable_request_must_start_pending",
        "new.requested_by := actor",
        "live_enable_request_expiry_out_of_range",
        "live_enable_request_requires_evidence_payload",
        "manual_command_review_requires_authenticated_actor",
        "live_enable_review_requires_requester",
        "live_enable_command_type_immutable",
        "live_enable_review_is_immutable",
        "live_enable_request_evidence_immutable",
        "live_enable_review_fields_require_status_transition",
        "live_enable_invalid_status_transition",
        "or new.applied_at is distinct from old.applied_at",
        "new.applied_at := null",
        "new.reviewed_by := actor",
        "new.reviewed_at := now()",
        "live_enable_self_review_forbidden",
        "live_enable_review_requires_future_expiry",
        "live_enable_rejection_requires_reason",
        "old.status = 'accepted' and new.status = 'applied'",
        "live_enable_accepted_command_apply_fields_immutable",
        "live_enable_apply_requires_applied_at_after_review",
        "live_enable_apply_requires_fresh_expiry",
    ]

    for fragment in required_fragments:
        assert fragment in sql


def test_live_operations_migration_adds_audit_triggers() -> None:
    sql = _sql()

    expected_triggers = [
        "audit_bot_settings_changes",
        "audit_watchlist_changes",
        "audit_strategy_versions_changes",
        "audit_ai_upgrade_candidates_changes",
        "audit_manual_commands_changes",
    ]

    assert "create or replace function public.audit_table_change()" in sql
    assert "insert into public.audit_logs" in sql
    for trigger_name in expected_triggers:
        assert trigger_name in sql


def test_live_operations_migration_uses_targeted_indexes() -> None:
    sql = _sql()

    assert "idx_manual_commands_live_enable_ready" in sql
    assert "where command_type = 'request_live_enable'" in sql
    assert "idx_orders_live_reconciliation" in sql
    assert "status in ('sent', 'partial_filled', 'unknown_requires_manual_check')" in sql
    assert "idx_orders_live_daily_count" in sql
    assert "status <> 'blocked'" in sql
    assert "idx_audit_logs_target_created_at" in sql


def test_live_operations_migration_rewrites_live_control_rls_for_cached_admin_check() -> None:
    sql = _sql()

    expected_fragments = [
        "drop policy if exists bot_settings_admin_read",
        "drop policy if exists bot_settings_admin_update",
        "drop policy if exists manual_commands_admin_read",
        "drop policy if exists manual_commands_admin_insert",
        "drop policy if exists manual_commands_admin_update",
        "drop policy if exists audit_logs_admin_read",
        "using ((select public.is_admin()))",
        "with check ((select public.is_admin()))",
    ]

    for fragment in expected_fragments:
        assert fragment in sql


def test_live_operations_migration_uses_hardened_security_definer_search_path() -> None:
    sql = _sql()

    assert sql.count("set search_path = ''") >= 3
    assert "set search_path = public" not in sql


def test_live_operations_migration_does_not_weaken_rls_or_public_writes() -> None:
    sql = _sql()

    forbidden_fragments = [
        "disable row level security",
        " to anon",
        " to public",
        "grant insert",
        "grant update",
    ]

    for fragment in forbidden_fragments:
        assert fragment not in sql
