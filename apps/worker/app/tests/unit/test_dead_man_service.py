from datetime import UTC, datetime, timedelta

from app.application.services.dead_man_service import (
    DeadManEvaluator,
    DeadManSnapshot,
)


def test_dead_man_evaluator_accepts_fresh_independent_operational_state() -> None:
    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)

    result = DeadManEvaluator().evaluate(
        DeadManSnapshot(
            observed_at=now,
            latest_heartbeat_at=now - timedelta(seconds=10),
            latest_heartbeat_status="ok",
            latest_heartbeat_release_sha="a" * 40,
            commands_last_completed_at=now - timedelta(seconds=1),
            execution_last_completed_at=now - timedelta(seconds=1),
            settlement_last_completed_at=now - timedelta(seconds=1),
            reconciliation_last_completed_at=now - timedelta(seconds=1),
            outbox_last_completed_at=now - timedelta(seconds=1),
            lease_holder_id="worker-a",
            lease_expires_at=now + timedelta(minutes=1),
            lease_release_sha="a" * 40,
            oldest_pending_outbox_at=None,
            dead_letter_count=0,
            active_incident_opened_at=None,
            active_incident_acknowledged_at=None,
        )
    )

    assert result.healthy
    assert result.reason_codes == ()


def test_dead_man_evaluator_reports_all_independent_fail_closed_signals() -> None:
    now = datetime(2026, 7, 14, 9, 10, tzinfo=UTC)

    result = DeadManEvaluator().evaluate(
        DeadManSnapshot(
            observed_at=now,
            latest_heartbeat_at=now - timedelta(minutes=6),
            latest_heartbeat_status="error",
            latest_heartbeat_release_sha="a" * 40,
            commands_last_completed_at=None,
            execution_last_completed_at=None,
            settlement_last_completed_at=None,
            reconciliation_last_completed_at=None,
            outbox_last_completed_at=None,
            lease_holder_id="worker-a",
            lease_expires_at=now - timedelta(seconds=1),
            lease_release_sha="a" * 40,
            oldest_pending_outbox_at=now - timedelta(minutes=6),
            dead_letter_count=2,
            active_incident_opened_at=now - timedelta(minutes=6),
            active_incident_acknowledged_at=None,
        )
    )

    assert not result.healthy
    assert result.reason_codes == (
        "worker_heartbeat_stale",
        "worker_heartbeat_error",
        "worker_lease_expired",
        "delivery_outbox_stale",
        "delivery_outbox_dead_lettered",
        "incident_ack_overdue",
    )


def test_dead_man_evaluator_rejects_invalid_incident_ack_timeline() -> None:
    now = datetime(2026, 7, 14, 9, 10, tzinfo=UTC)

    result = DeadManEvaluator().evaluate(
        DeadManSnapshot(
            observed_at=now,
            latest_heartbeat_at=now,
            latest_heartbeat_status="ok",
            latest_heartbeat_release_sha="a" * 40,
            commands_last_completed_at=now,
            execution_last_completed_at=now,
            settlement_last_completed_at=now,
            reconciliation_last_completed_at=now,
            outbox_last_completed_at=now,
            lease_holder_id="worker-a",
            lease_expires_at=now + timedelta(minutes=1),
            lease_release_sha="a" * 40,
            oldest_pending_outbox_at=None,
            dead_letter_count=0,
            active_incident_opened_at=now - timedelta(minutes=1),
            active_incident_acknowledged_at=now + timedelta(seconds=1),
        )
    )

    assert result.reason_codes == ("incident_ack_timestamp_invalid",)


def test_dead_man_evaluator_reports_heartbeat_status_and_release_mismatch() -> None:
    now = datetime(2026, 7, 14, 9, 10, tzinfo=UTC)

    result = DeadManEvaluator().evaluate(
        DeadManSnapshot(
            observed_at=now,
            latest_heartbeat_at=now - timedelta(seconds=1),
            latest_heartbeat_status="warning",
            latest_heartbeat_release_sha="a" * 40,
            commands_last_completed_at=None,
            execution_last_completed_at=None,
            settlement_last_completed_at=None,
            reconciliation_last_completed_at=None,
            outbox_last_completed_at=None,
            lease_holder_id="worker-a",
            lease_expires_at=now + timedelta(minutes=1),
            lease_release_sha="b" * 40,
            oldest_pending_outbox_at=None,
            dead_letter_count=0,
            active_incident_opened_at=None,
            active_incident_acknowledged_at=None,
        )
    )

    assert result.reason_codes == (
        "worker_heartbeat_warning",
        "worker_release_mismatch",
    )


def test_dead_man_evaluator_detects_partial_scheduler_stall() -> None:
    now = datetime(2026, 7, 14, 9, 10, tzinfo=UTC)

    result = DeadManEvaluator().evaluate(
        DeadManSnapshot(
            observed_at=now,
            latest_heartbeat_at=now - timedelta(seconds=1),
            latest_heartbeat_status="ok",
            latest_heartbeat_release_sha="a" * 40,
            commands_last_completed_at=now - timedelta(seconds=16),
            execution_last_completed_at=now - timedelta(seconds=31),
            settlement_last_completed_at=now - timedelta(seconds=91),
            reconciliation_last_completed_at=now - timedelta(seconds=91),
            outbox_last_completed_at=None,
            lease_holder_id="worker-a",
            lease_expires_at=now + timedelta(minutes=1),
            lease_release_sha="a" * 40,
            oldest_pending_outbox_at=None,
            dead_letter_count=0,
            active_incident_opened_at=None,
            active_incident_acknowledged_at=None,
        )
    )

    assert result.reason_codes == (
        "worker_commands_stage_stale",
        "worker_execution_stage_stale",
        "worker_settlement_stage_stale",
        "worker_reconciliation_stage_stale",
        "worker_outbox_stage_missing",
    )


def test_dead_man_default_allows_normally_ageing_thirty_second_lease() -> None:
    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)

    result = DeadManEvaluator().evaluate(
        DeadManSnapshot(
            observed_at=now,
            latest_heartbeat_at=now - timedelta(seconds=1),
            latest_heartbeat_status="ok",
            latest_heartbeat_release_sha="a" * 40,
            commands_last_completed_at=now,
            execution_last_completed_at=now,
            settlement_last_completed_at=now,
            reconciliation_last_completed_at=now,
            outbox_last_completed_at=now,
            lease_holder_id="worker-a",
            lease_expires_at=now + timedelta(seconds=20),
            lease_release_sha="a" * 40,
            oldest_pending_outbox_at=None,
            dead_letter_count=0,
            active_incident_opened_at=None,
            active_incident_acknowledged_at=None,
        )
    )

    assert result.healthy
    assert "worker_lease_expiring" not in result.reason_codes
