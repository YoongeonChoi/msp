from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from app.application.ports.operation_command_port import OperationCommandPort
from app.domain.common.time import now_utc
from app.domain.execution_v2.models import ExecutionInvariantError, WorkerLease

_RELEASE_SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")


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
        account_id: str,
        holder_id: str,
        current_release_sha: str,
        lease_provider: Callable[[], WorkerLease | None],
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        if not account_id.strip() or not holder_id.strip():
            raise ExecutionInvariantError("operation_lease_identity_is_required")
        if _RELEASE_SHA_RE.fullmatch(current_release_sha) is None:
            raise ExecutionInvariantError("operation_release_sha_is_invalid")
        self.port = port
        self.account_id = account_id
        self.holder_id = holder_id
        self.current_release_sha = current_release_sha
        self.lease_provider = lease_provider
        self.clock = clock

    async def run_once(self, *, limit: int = 25) -> OperationCommandRunResult:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
            raise ExecutionInvariantError("operation_command_limit_is_invalid")
        claimed_at = self._now()
        lease = self._current_lease(claimed_at)
        commands = await self.port.claim_operation_command_batch(
            account_id=self.account_id,
            holder_id=self.holder_id,
            release_sha=self.current_release_sha,
            fencing_token=lease.fencing_token,
            now=claimed_at,
            limit=limit,
        )
        if any(command.account_id != self.account_id for command in commands):
            raise ExecutionInvariantError("operation_command_account_scope_mismatch")
        applied = 0
        failed = 0
        unacknowledged = 0
        for command in commands:
            try:
                applied_at = self._now()
                lease = self._current_lease(applied_at)
                acknowledgement = await self.port.acknowledge_operation_command(
                    command_id=command.command_id,
                    phase="applied",
                    account_id=self.account_id,
                    holder_id=self.holder_id,
                    release_sha=self.current_release_sha,
                    fencing_token=lease.fencing_token,
                    expected_revision=command.revision,
                    now=applied_at,
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
                    failed_at = self._now()
                    lease = self._current_lease(failed_at)
                    failure_acknowledgement = (
                        await self.port.acknowledge_operation_command(
                            command_id=command.command_id,
                            phase="failed",
                            account_id=self.account_id,
                            holder_id=self.holder_id,
                            release_sha=self.current_release_sha,
                            fencing_token=lease.fencing_token,
                            expected_revision=command.revision,
                            now=failed_at,
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

    def _current_lease(self, now: datetime) -> WorkerLease:
        lease = self.lease_provider()
        if (
            lease is None
            or lease.account_id != self.account_id
            or lease.holder_id != self.holder_id
            or not lease.is_active(now)
        ):
            raise ExecutionInvariantError("operation_worker_lease_is_not_current")
        return lease


def _safe_command_failure_code(exc: Exception) -> str:
    return f"worker_apply_{type(exc).__name__.lower()}"[:120]
