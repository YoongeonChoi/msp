from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from app.domain.operations.models import OperationsInvariantError

_RELEASE_SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
DeadManHeartbeatStatus = Literal["ok", "warning", "error", "shutting_down"]


@dataclass(frozen=True, slots=True)
class DeadManPolicy:
    heartbeat_max_age: timedelta = timedelta(minutes=5)
    # The scheduler defaults to a 30-second lease renewed every 10 seconds.
    # Alert only after a renewal window is actually at risk.
    lease_min_remaining: timedelta = timedelta(seconds=10)
    delivery_outbox_max_age: timedelta = timedelta(minutes=5)
    incident_ack_deadline: timedelta = timedelta(minutes=5)
    commands_stage_max_age: timedelta = timedelta(seconds=15)
    execution_stage_max_age: timedelta = timedelta(seconds=30)
    settlement_stage_max_age: timedelta = timedelta(seconds=90)
    reconciliation_stage_max_age: timedelta = timedelta(seconds=90)
    outbox_stage_max_age: timedelta = timedelta(seconds=15)

    def __post_init__(self) -> None:
        for value in (
            self.heartbeat_max_age,
            self.lease_min_remaining,
            self.delivery_outbox_max_age,
            self.incident_ack_deadline,
            self.commands_stage_max_age,
            self.execution_stage_max_age,
            self.settlement_stage_max_age,
            self.reconciliation_stage_max_age,
            self.outbox_stage_max_age,
        ):
            if not isinstance(value, timedelta) or value <= timedelta(0):
                raise OperationsInvariantError("dead_man_policy_duration_is_invalid")


@dataclass(frozen=True, slots=True)
class DeadManSnapshot:
    observed_at: datetime
    latest_heartbeat_at: datetime | None
    latest_heartbeat_status: DeadManHeartbeatStatus | None
    latest_heartbeat_release_sha: str | None
    commands_last_completed_at: datetime | None
    execution_last_completed_at: datetime | None
    settlement_last_completed_at: datetime | None
    reconciliation_last_completed_at: datetime | None
    outbox_last_completed_at: datetime | None
    lease_holder_id: str | None
    lease_expires_at: datetime | None
    lease_release_sha: str | None
    oldest_pending_outbox_at: datetime | None
    dead_letter_count: int
    active_incident_opened_at: datetime | None
    active_incident_acknowledged_at: datetime | None

    def __post_init__(self) -> None:
        _require_aware(self.observed_at, "dead_man_observed_at")
        for value, field in (
            (self.latest_heartbeat_at, "latest_heartbeat_at"),
            (self.commands_last_completed_at, "commands_last_completed_at"),
            (self.execution_last_completed_at, "execution_last_completed_at"),
            (self.settlement_last_completed_at, "settlement_last_completed_at"),
            (
                self.reconciliation_last_completed_at,
                "reconciliation_last_completed_at",
            ),
            (self.outbox_last_completed_at, "outbox_last_completed_at"),
            (self.lease_expires_at, "lease_expires_at"),
            (self.oldest_pending_outbox_at, "oldest_pending_outbox_at"),
            (self.active_incident_opened_at, "active_incident_opened_at"),
            (self.active_incident_acknowledged_at, "active_incident_acknowledged_at"),
        ):
            if value is not None:
                _require_aware(value, field)
        if (
            isinstance(self.dead_letter_count, bool)
            or not isinstance(self.dead_letter_count, int)
            or self.dead_letter_count < 0
        ):
            raise OperationsInvariantError("dead_letter_count_is_invalid")
        if self.latest_heartbeat_at is None:
            if (
                self.latest_heartbeat_status is not None
                or self.latest_heartbeat_release_sha is not None
            ):
                raise OperationsInvariantError("dead_man_heartbeat_shape_is_invalid")
        elif (
            self.latest_heartbeat_status
            not in {"ok", "warning", "error", "shutting_down"}
            or self.latest_heartbeat_release_sha is None
            or _RELEASE_SHA_RE.fullmatch(self.latest_heartbeat_release_sha) is None
        ):
            raise OperationsInvariantError("dead_man_heartbeat_shape_is_invalid")
        if self.lease_expires_at is None:
            if self.lease_holder_id is not None or self.lease_release_sha is not None:
                raise OperationsInvariantError("dead_man_lease_shape_is_invalid")
        elif (
            not isinstance(self.lease_holder_id, str)
            or not self.lease_holder_id.strip()
            or self.lease_release_sha is None
            or _RELEASE_SHA_RE.fullmatch(self.lease_release_sha) is None
        ):
            raise OperationsInvariantError("dead_man_lease_shape_is_invalid")


@dataclass(frozen=True, slots=True)
class DeadManEvaluation:
    healthy: bool
    reason_codes: tuple[str, ...]
    evaluated_at: datetime


class DeadManEvaluator:
    """Pure independent evaluator; it has no broker or order execution dependency."""

    def __init__(self, policy: DeadManPolicy | None = None) -> None:
        self.policy = policy or DeadManPolicy()

    def evaluate(self, snapshot: DeadManSnapshot) -> DeadManEvaluation:
        now = snapshot.observed_at
        reasons: list[str] = []

        heartbeat = snapshot.latest_heartbeat_at
        if heartbeat is None:
            reasons.append("worker_heartbeat_missing")
        elif heartbeat > now:
            reasons.append("worker_heartbeat_from_future")
        elif now - heartbeat > self.policy.heartbeat_max_age:
            reasons.append("worker_heartbeat_stale")
        if heartbeat is not None and snapshot.latest_heartbeat_status != "ok":
            reasons.append(f"worker_heartbeat_{snapshot.latest_heartbeat_status}")
        if snapshot.latest_heartbeat_status == "ok":
            for stage, completed_at, max_age in (
                (
                    "commands",
                    snapshot.commands_last_completed_at,
                    self.policy.commands_stage_max_age,
                ),
                (
                    "execution",
                    snapshot.execution_last_completed_at,
                    self.policy.execution_stage_max_age,
                ),
                (
                    "settlement",
                    snapshot.settlement_last_completed_at,
                    self.policy.settlement_stage_max_age,
                ),
                (
                    "reconciliation",
                    snapshot.reconciliation_last_completed_at,
                    self.policy.reconciliation_stage_max_age,
                ),
                (
                    "outbox",
                    snapshot.outbox_last_completed_at,
                    self.policy.outbox_stage_max_age,
                ),
            ):
                if completed_at is None:
                    reasons.append(f"worker_{stage}_stage_missing")
                elif completed_at > now:
                    reasons.append(f"worker_{stage}_stage_from_future")
                elif now - completed_at > max_age:
                    reasons.append(f"worker_{stage}_stage_stale")

        lease_expires_at = snapshot.lease_expires_at
        if lease_expires_at is None:
            reasons.append("worker_lease_missing")
        elif lease_expires_at <= now:
            reasons.append("worker_lease_expired")
        elif lease_expires_at - now < self.policy.lease_min_remaining:
            reasons.append("worker_lease_expiring")
        if (
            snapshot.latest_heartbeat_release_sha is not None
            and snapshot.lease_release_sha is not None
            and snapshot.latest_heartbeat_release_sha != snapshot.lease_release_sha
        ):
            reasons.append("worker_release_mismatch")

        oldest_pending = snapshot.oldest_pending_outbox_at
        if oldest_pending is not None:
            if oldest_pending > now:
                reasons.append("delivery_outbox_timestamp_from_future")
            elif now - oldest_pending > self.policy.delivery_outbox_max_age:
                reasons.append("delivery_outbox_stale")
        if snapshot.dead_letter_count > 0:
            reasons.append("delivery_outbox_dead_lettered")

        opened_at = snapshot.active_incident_opened_at
        acknowledged_at = snapshot.active_incident_acknowledged_at
        if opened_at is None:
            if acknowledged_at is not None:
                reasons.append("incident_ack_without_active_incident")
        elif opened_at > now:
            reasons.append("incident_opened_at_from_future")
        elif acknowledged_at is None:
            if now - opened_at > self.policy.incident_ack_deadline:
                reasons.append("incident_ack_overdue")
            else:
                reasons.append("incident_ack_pending")
        elif acknowledged_at < opened_at or acknowledged_at > now:
            reasons.append("incident_ack_timestamp_invalid")

        return DeadManEvaluation(
            healthy=not reasons,
            reason_codes=tuple(reasons),
            evaluated_at=now,
        )


def _require_aware(value: object, field: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise OperationsInvariantError(f"{field}_must_be_timezone_aware")
