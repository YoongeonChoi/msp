from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from app.application.ports.dead_man_monitor_port import (
    DeadManAlertDestinationPort,
    DeadManSnapshotSourcePort,
)
from app.application.services.dead_man_service import (
    DeadManEvaluation,
    DeadManEvaluator,
)
from app.domain.common.time import now_utc
from app.domain.operations.models import OperationsInvariantError


@dataclass(frozen=True, slots=True)
class DeadManMonitorRunResult:
    source_available: bool
    evaluation: DeadManEvaluation
    alert_delivered: bool


class DeadManAlertDeliveryError(RuntimeError):
    """Retryable direct-channel failure with a fixed, non-sensitive message."""


@dataclass(frozen=True, slots=True)
class _PendingDeadManAlert:
    event: Literal["unhealthy", "recovered"]
    reason_codes: tuple[str, ...]
    observed_at: datetime


class RunDeadManMonitor:
    """Evaluate DB-independent liveness and use a direct alert channel."""

    def __init__(
        self,
        source: DeadManSnapshotSourcePort,
        destination: DeadManAlertDestinationPort,
        evaluator: DeadManEvaluator,
        *,
        account_id: str,
        clock: Callable[[], datetime] = now_utc,
        episode_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not account_id.strip():
            raise OperationsInvariantError("dead_man_account_id_is_required")
        self.source = source
        self.destination = destination
        self.evaluator = evaluator
        self.account_id = account_id
        self.clock = clock
        self.episode_id_factory = episode_id_factory or _new_episode_id
        # This state intentionally completes process-lifetime semantics only.
        # Hosted G2 remains blocked until an independent durable episode store
        # preserves it across monitor restarts and failover.
        self._active_episode_id: str | None = None
        self._last_unhealthy_reasons: tuple[str, ...] | None = None
        self._pending_alert: _PendingDeadManAlert | None = None

    async def run_once(self) -> DeadManMonitorRunResult:
        requested_at = self._now()
        try:
            snapshot = await self.source.get_dead_man_snapshot(
                account_id=self.account_id,
                observed_at=requested_at,
            )
            evaluation = self.evaluator.evaluate(snapshot)
            source_available = True
        except Exception:
            evaluation = DeadManEvaluation(
                healthy=False,
                reason_codes=("monitor_source_unreachable",),
                evaluated_at=requested_at,
            )
            source_available = False

        delivered = False
        if self._pending_alert is not None:
            await self._deliver_pending_alert()
            delivered = True
        if not evaluation.healthy:
            if self._active_episode_id is None:
                self._active_episode_id = _validated_episode_id(self.episode_id_factory())
            if evaluation.reason_codes != self._last_unhealthy_reasons:
                self._pending_alert = _PendingDeadManAlert(
                    event="unhealthy",
                    reason_codes=evaluation.reason_codes,
                    observed_at=evaluation.evaluated_at,
                )
                await self._deliver_pending_alert()
                delivered = True
        elif self._last_unhealthy_reasons is not None:
            if self._active_episode_id is None:
                raise OperationsInvariantError("dead_man_episode_state_is_invalid")
            self._pending_alert = _PendingDeadManAlert(
                event="recovered",
                reason_codes=self._last_unhealthy_reasons,
                observed_at=evaluation.evaluated_at,
            )
            await self._deliver_pending_alert()
            delivered = True

        return DeadManMonitorRunResult(
            source_available=source_available,
            evaluation=evaluation,
            alert_delivered=delivered,
        )

    async def _deliver_pending_alert(self) -> None:
        pending = self._pending_alert
        episode_id = self._active_episode_id
        if pending is None or episode_id is None:
            raise OperationsInvariantError("dead_man_episode_state_is_invalid")
        try:
            await self.destination.deliver_dead_man_alert(
                account_id=self.account_id,
                episode_id=episode_id,
                event=pending.event,
                reason_codes=pending.reason_codes,
                observed_at=pending.observed_at,
            )
        except Exception:
            raise DeadManAlertDeliveryError("dead_man_alert_delivery_failed") from None
        if pending.event == "unhealthy":
            self._last_unhealthy_reasons = pending.reason_codes
        else:
            self._active_episode_id = None
            self._last_unhealthy_reasons = None
        self._pending_alert = None

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise OperationsInvariantError("dead_man_clock_must_be_timezone_aware")
        return value


def _new_episode_id() -> str:
    return str(uuid4())


def _validated_episode_id(value: object) -> str:
    if not isinstance(value, str):
        raise OperationsInvariantError("dead_man_episode_id_is_invalid")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise OperationsInvariantError("dead_man_episode_id_is_invalid") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise OperationsInvariantError("dead_man_episode_id_is_invalid")
    return value
