from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

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
    ) -> None:
        if not account_id.strip():
            raise OperationsInvariantError("dead_man_account_id_is_required")
        self.source = source
        self.destination = destination
        self.evaluator = evaluator
        self.account_id = account_id
        self.clock = clock
        self._last_unhealthy_reasons: tuple[str, ...] | None = None

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
        if not evaluation.healthy:
            await self.destination.deliver_dead_man_alert(
                account_id=self.account_id,
                event="unhealthy",
                reason_codes=evaluation.reason_codes,
                observed_at=evaluation.evaluated_at,
            )
            self._last_unhealthy_reasons = evaluation.reason_codes
            delivered = True
        elif self._last_unhealthy_reasons is not None:
            await self.destination.deliver_dead_man_alert(
                account_id=self.account_id,
                event="recovered",
                reason_codes=self._last_unhealthy_reasons,
                observed_at=evaluation.evaluated_at,
            )
            self._last_unhealthy_reasons = None
            delivered = True

        return DeadManMonitorRunResult(
            source_available=source_available,
            evaluation=evaluation,
            alert_delivered=delivered,
        )

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise OperationsInvariantError("dead_man_clock_must_be_timezone_aware")
        return value
