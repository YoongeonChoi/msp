from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
MIGRATION = ROOT / "supabase" / "migrations" / "0013_worker_deployment_lock.sql"


def _sql() -> str:
    return " ".join(MIGRATION.read_text(encoding="utf-8").lower().split())


def test_deployment_lock_disables_live_before_deploy() -> None:
    sql = _sql()

    assert "deployment_lock boolean not null default false" in sql
    assert "set enabled = false, live_order_allowed = false, deployment_lock = true" in sql
    assert "deployment_lock_requires_disabled_live_execution" in sql
    assert "live_execution_blocked_by_deployment_lock" in sql
    assert "deployment_completed_at" in sql
    assert "deployment_triggered_at" in sql
    assert "reviewed_at > coalesce( new.deployment_completed_at" in sql
    assert "deployment_target_sha is not null and deployment_target_sha ~" in sql


def test_deployment_lock_requires_service_role() -> None:
    sql = _sql()

    assert "begin_worker_deployment_requires_service_role" in sql
    assert "complete_worker_deployment_requires_service_role" in sql
    assert "mark_worker_deployment_triggered_requires_service_role" in sql
    assert "abort_worker_deployment_requires_service_role" in sql
    assert "deployment_lock_requires_service_role" in sql
    assert sql.count("if normalized_sha is null or normalized_sha !~") == 4
    assert "grant execute on function public.begin_worker_deployment(text) to service_role" in sql
    assert (
        "grant execute on function public.complete_worker_deployment(text, integer) to service_role"
        in sql
    )
    assert (
        "grant execute on function public.mark_worker_deployment_triggered(text) to service_role"
        in sql
    )
    assert "grant execute on function public.abort_worker_deployment(text) to service_role" in sql


def test_deployment_lock_only_releases_for_fresh_matching_healthy_heartbeat() -> None:
    sql = _sql()

    assert "from public.worker_heartbeats order by created_at desc limit 1" in sql
    assert "deployment_heartbeat_not_ok" in sql
    assert "deployment_heartbeat_release_mismatch" in sql
    assert "deployment_heartbeat_lock_mismatch" in sql
    assert "deployment_heartbeat_precedes_trigger" in sql
    assert "details->>'deployment_target_sha'" in sql
    assert "deployment_heartbeat_not_fresh" in sql
    assert "if max_age_seconds is null or max_age_seconds < 1" in sql
    assert "set deployment_lock = false, deployment_target_sha = null" in sql


def test_deployment_lock_rejects_parallel_start_and_supports_safe_abort() -> None:
    sql = _sql()

    assert "deployment_already_locked" in sql
    assert "create or replace function public.abort_worker_deployment" in sql
    assert "set enabled = false, live_order_allowed = false, deployment_lock = false" in sql
