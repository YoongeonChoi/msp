from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.application.services.dead_man_service import DeadManEvaluator, DeadManSnapshot
from app.application.use_cases.run_dead_man_monitor import RunDeadManMonitor

NOW = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)


async def test_unhealthy_state_alerts_and_later_recovery_uses_prior_reasons() -> None:
    source = SequenceSource((_snapshot(heartbeat_age=timedelta(minutes=6)), _snapshot()))
    destination = RecordingDestination()
    runner = RunDeadManMonitor(
        source,
        destination,
        DeadManEvaluator(),
        account_id="paper-primary",
        clock=lambda: NOW,
    )

    unhealthy = await runner.run_once()
    recovered = await runner.run_once()

    assert not unhealthy.evaluation.healthy
    assert unhealthy.alert_delivered
    assert recovered.evaluation.healthy
    assert recovered.alert_delivered
    assert destination.events == [
        ("unhealthy", ("worker_heartbeat_stale",)),
        ("recovered", ("worker_heartbeat_stale",)),
    ]


async def test_snapshot_failure_uses_direct_monitor_source_alert() -> None:
    destination = RecordingDestination()
    runner = RunDeadManMonitor(
        FailingSource(),
        destination,
        DeadManEvaluator(),
        account_id="paper-primary",
        clock=lambda: NOW,
    )

    result = await runner.run_once()

    assert not result.source_available
    assert result.evaluation.reason_codes == ("monitor_source_unreachable",)
    assert destination.events == [
        ("unhealthy", ("monitor_source_unreachable",))
    ]


class SequenceSource:
    def __init__(self, snapshots: tuple[DeadManSnapshot, ...]) -> None:
        self.snapshots = list(snapshots)

    async def get_dead_man_snapshot(
        self,
        *,
        account_id: str,
        observed_at: datetime,
    ) -> DeadManSnapshot:
        assert account_id == "paper-primary"
        assert observed_at == NOW
        return self.snapshots.pop(0)


class FailingSource:
    async def get_dead_man_snapshot(
        self,
        *,
        account_id: str,
        observed_at: datetime,
    ) -> DeadManSnapshot:
        del account_id, observed_at
        raise RuntimeError("database unavailable")


class RecordingDestination:
    def __init__(self) -> None:
        self.events: list[tuple[str, tuple[str, ...]]] = []

    async def deliver_dead_man_alert(
        self,
        *,
        account_id: str,
        event: str,
        reason_codes: tuple[str, ...],
        observed_at: datetime,
    ) -> None:
        assert account_id == "paper-primary"
        assert observed_at == NOW
        self.events.append((event, reason_codes))


def _snapshot(*, heartbeat_age: timedelta = timedelta(seconds=1)) -> DeadManSnapshot:
    return DeadManSnapshot(
        observed_at=NOW,
        latest_heartbeat_at=NOW - heartbeat_age,
        latest_heartbeat_status="ok",
        latest_heartbeat_release_sha="a" * 40,
        commands_last_completed_at=NOW - timedelta(seconds=1),
        execution_last_completed_at=NOW - timedelta(seconds=1),
        settlement_last_completed_at=NOW - timedelta(seconds=1),
        reconciliation_last_completed_at=NOW - timedelta(seconds=1),
        outbox_last_completed_at=NOW - timedelta(seconds=1),
        lease_holder_id="worker-a",
        lease_expires_at=NOW + timedelta(minutes=1),
        lease_release_sha="a" * 40,
        oldest_pending_outbox_at=None,
        dead_letter_count=0,
        active_incident_opened_at=None,
        active_incident_acknowledged_at=None,
    )
