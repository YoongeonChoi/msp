from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from app.application.ports.operation_command_port import OperationCommandPort
from app.domain.common.time import now_utc
from app.domain.execution_v2.models import ExecutionInvariantError


@dataclass(frozen=True, slots=True)
class OperationCommandRunResult:
    claimed: int
    applied: int
    failed: int = 0
    unacknowledged: int = 0


class ApplyOperationCommands:
    """Discover approved commands and atomically apply their DB postconditions."""

    def __init__(
        self,
        port: OperationCommandPort,
        *,
        holder_id: str,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        if not holder_id.strip():
            raise ExecutionInvariantError("operation_holder_id_is_required")
        self.port = port
        self.holder_id = holder_id
        self.clock = clock

    async def run_once(self, *, limit: int = 25) -> OperationCommandRunResult:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
            raise ExecutionInvariantError("operation_command_limit_is_invalid")
        commands = await self.port.claim_operation_command_batch(
            holder_id=self.holder_id,
            now=self._now(),
            limit=limit,
        )
        applied = 0
        failed = 0
        unacknowledged = 0
        for command in commands:
            try:
                acknowledgement = await self.port.acknowledge_operation_command(
                    command_id=command.command_id,
                    phase="applied",
                    holder_id=self.holder_id,
                    now=self._now(),
                    result_summary={
                        "schema_version": 1,
                        "command_type": command.command_type,
                        "claimed_revision": command.revision,
                    },
                )
                if acknowledgement.command_id != command.command_id:
                    raise ExecutionInvariantError("operation_ack_identity_mismatch")
                if acknowledgement.state != "applied":
                    raise ExecutionInvariantError("operation_ack_postcondition_not_applied")
            except Exception as exc:
                failed += 1
                try:
                    failure_acknowledgement = (
                        await self.port.acknowledge_operation_command(
                            command_id=command.command_id,
                            phase="failed",
                            holder_id=self.holder_id,
                            now=self._now(),
                            result_summary={
                                "schema_version": 1,
                                "command_type": command.command_type,
                                "claimed_revision": command.revision,
                                "failure_stage": "atomic_apply_ack",
                                "error_type": type(exc).__name__,
                            },
                            failure_code=_safe_command_failure_code(exc),
                        )
                    )
                    if failure_acknowledgement.command_id != command.command_id:
                        raise ExecutionInvariantError("operation_failure_ack_identity_mismatch")
                    if failure_acknowledgement.state != "failed":
                        raise ExecutionInvariantError(
                            "operation_failure_ack_postcondition_not_failed"
                        )
                except Exception:
                    unacknowledged += 1
                continue
            applied += 1
        return OperationCommandRunResult(
            claimed=len(commands),
            applied=applied,
            failed=failed,
            unacknowledged=unacknowledged,
        )

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ExecutionInvariantError("operation_clock_must_be_timezone_aware")
        return value


def _safe_command_failure_code(exc: Exception) -> str:
    return f"worker_apply_{type(exc).__name__.lower()}"[:120]
