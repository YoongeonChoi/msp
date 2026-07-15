from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

import pytest

from app.application.use_cases.apply_operation_commands import (
    ApplyOperationCommands,
    OperationCommandRunResult,
)
from app.domain.common.json import JsonObject
from app.domain.operations.models import (
    ClaimedOperationCommand,
    OperationCommandAcknowledgement,
    OperationsInvariantError,
)


async def test_claimed_command_is_applied_through_atomic_postcondition_ack() -> None:
    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    command = ClaimedOperationCommand(
        command_id=str(uuid4()),
        command_type="pause_paper",
        environment="paper",
        account_id="paper-primary",
        requested_change={"expected_state_version": 3},
        requested_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(minutes=5),
        revision=4,
        claimed_at=now,
        claim_expires_at=now + timedelta(seconds=30),
    )
    port = FakeOperationCommandPort(command)

    result = await ApplyOperationCommands(
        port,
        holder_id=str(uuid4()),
        clock=lambda: now,
    ).run_once()

    assert result == OperationCommandRunResult(1, 1)
    assert port.ack_phase == "applied"
    assert port.result_summary == {
        "schema_version": 1,
        "command_type": "pause_paper",
        "claimed_revision": 4,
    }


async def test_poison_command_is_failed_without_starving_following_command() -> None:
    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    first = _command(now, command_type="pause_paper")
    second = _command(now, command_type="emergency_stop")
    port = PoisonAwareOperationCommandPort((first, second), poison_id=first.command_id)

    result = await ApplyOperationCommands(
        port,
        holder_id=str(uuid4()),
        clock=lambda: now,
    ).run_once()

    assert result == OperationCommandRunResult(2, 1, 1, 0)
    assert [(command_id, phase) for command_id, phase, _code in port.calls] == [
        (first.command_id, "applied"),
        (first.command_id, "failed"),
        (second.command_id, "applied"),
    ]
    assert port.calls[1][2] == "worker_apply_runtimeerror"


async def test_failed_terminal_ack_is_counted_and_next_command_still_runs() -> None:
    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    first = _command(now, command_type="pause_paper")
    second = _command(now, command_type="emergency_stop")
    port = PoisonAwareOperationCommandPort(
        (first, second),
        poison_id=first.command_id,
        fail_terminal_ack=True,
    )

    result = await ApplyOperationCommands(
        port,
        holder_id=str(uuid4()),
        clock=lambda: now,
    ).run_once()

    assert result == OperationCommandRunResult(2, 1, 1, 1)
    assert port.calls[-1][:2] == (second.command_id, "applied")


def test_worker_rejects_command_without_atomic_apply_branch() -> None:
    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    with pytest.raises(OperationsInvariantError, match="operation_command_type_is_invalid"):
        ClaimedOperationCommand(
            command_id=str(uuid4()),
            command_type="release_promotion",  # type: ignore[arg-type]
            environment="paper",
            account_id="paper-primary",
            requested_change={},
            requested_at=now - timedelta(minutes=1),
            expires_at=now + timedelta(minutes=5),
            revision=1,
            claimed_at=now,
            claim_expires_at=now + timedelta(seconds=30),
        )


class FakeOperationCommandPort:
    def __init__(self, command: ClaimedOperationCommand) -> None:
        self.command = command
        self.ack_phase: Literal["claimed", "applied", "failed"] | None = None
        self.result_summary: JsonObject | None = None

    async def claim_operation_command_batch(
        self,
        *,
        holder_id: str,
        now: datetime,
        limit: int,
    ) -> tuple[ClaimedOperationCommand, ...]:
        del holder_id, now, limit
        return (self.command,)

    async def acknowledge_operation_command(
        self,
        *,
        command_id: str,
        phase: Literal["claimed", "applied", "failed"],
        holder_id: str,
        now: datetime,
        result_summary: JsonObject,
        failure_code: str | None = None,
    ) -> OperationCommandAcknowledgement:
        del holder_id, failure_code
        self.ack_phase = phase
        self.result_summary = result_summary
        return OperationCommandAcknowledgement(
            command_id=command_id,
            state="applied",
            claimed_at=self.command.claimed_at,
            applied_at=now,
            post_control_epoch=4,
            failure_code=None,
        )


class PoisonAwareOperationCommandPort:
    def __init__(
        self,
        commands: tuple[ClaimedOperationCommand, ...],
        *,
        poison_id: str,
        fail_terminal_ack: bool = False,
    ) -> None:
        self.commands = commands
        self.poison_id = poison_id
        self.fail_terminal_ack = fail_terminal_ack
        self.calls: list[
            tuple[str, Literal["claimed", "applied", "failed"], str | None]
        ] = []

    async def claim_operation_command_batch(
        self,
        *,
        holder_id: str,
        now: datetime,
        limit: int,
    ) -> tuple[ClaimedOperationCommand, ...]:
        del holder_id, now, limit
        return self.commands

    async def acknowledge_operation_command(
        self,
        *,
        command_id: str,
        phase: Literal["claimed", "applied", "failed"],
        holder_id: str,
        now: datetime,
        result_summary: JsonObject,
        failure_code: str | None = None,
    ) -> OperationCommandAcknowledgement:
        del holder_id, result_summary
        self.calls.append((command_id, phase, failure_code))
        if command_id == self.poison_id and phase == "applied":
            raise RuntimeError("poison_command")
        if command_id == self.poison_id and phase == "failed" and self.fail_terminal_ack:
            raise RuntimeError("failed_ack_unavailable")
        return OperationCommandAcknowledgement(
            command_id=command_id,
            state="failed" if phase == "failed" else "applied",
            claimed_at=now,
            applied_at=now,
            post_control_epoch=None,
            failure_code=failure_code,
        )


def _command(
    now: datetime,
    *,
    command_type: Literal["pause_paper", "emergency_stop"],
) -> ClaimedOperationCommand:
    return ClaimedOperationCommand(
        command_id=str(uuid4()),
        command_type=command_type,
        environment="paper",
        account_id="paper-primary",
        requested_change={"expected_state_version": 3},
        requested_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(minutes=5),
        revision=4,
        claimed_at=now,
        claim_expires_at=now + timedelta(seconds=30),
    )
