from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from uuid import uuid4

import pytest

import app.application.use_cases.apply_operation_commands as commands_module
from app.application.services.scheduler_invocation_deadline import (
    SchedulerInvocationEffectAuthorization,
    SchedulerInvocationPermitRevoked,
)
from app.application.use_cases.apply_operation_commands import (
    ApplyOperationCommands,
    OperationCommandRunResult,
)
from app.domain.common.json import JsonObject
from app.domain.execution_v2.models import ExecutionInvariantError, WorkerLease
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
    holder_id = str(uuid4())
    lease = _lease(now, holder_id=holder_id)

    result = await ApplyOperationCommands(
        port,
        account_id="paper-primary",
        holder_id=holder_id,
        current_release_sha="a" * 40,
        lease_provider=lambda: lease,
        clock=lambda: now,
    ).run_once()

    assert result == OperationCommandRunResult(1, 1)
    assert port.ack_phase == "applied"
    assert port.claim_gate == ("paper-primary", holder_id, "a" * 40, 7)
    assert port.ack_gate == ("paper-primary", holder_id, "a" * 40, 7, 4)
    assert port.result_summary == {
        "schema_version": 1,
        "command_type": "pause_paper",
        "claimed_revision": 4,
    }


async def test_scheduled_command_propagates_authorization_to_every_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    port = FakeOperationCommandPort(_command(now, command_type="pause_paper"))
    holder_id = str(uuid4())
    authorization = _authorization()
    gate = FakeSchedulerAuthorizationGate(
        authorization,
        expected_job_key="operations.commands",
    )
    monkeypatch.setattr(
        commands_module,
        "require_scheduler_invocation_effect_authorization",
        gate,
    )

    result = await ApplyOperationCommands(
        port,
        account_id="paper-primary",
        holder_id=holder_id,
        current_release_sha="a" * 40,
        lease_provider=lambda: _lease(now, holder_id=holder_id),
        clock=lambda: now,
    ).run_scheduled(authorization)

    assert result == OperationCommandRunResult(1, 1)
    assert gate.calls == 3
    assert port.scheduler_authorizations == [authorization, authorization]


async def test_scheduled_command_rejects_missing_authorization_before_claim() -> None:
    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    port = FakeOperationCommandPort(_command(now, command_type="pause_paper"))
    holder_id = str(uuid4())

    with pytest.raises(SchedulerInvocationPermitRevoked, match="permit_not_issued"):
        await ApplyOperationCommands(
            port,
            account_id="paper-primary",
            holder_id=holder_id,
            current_release_sha="a" * 40,
            lease_provider=lambda: _lease(now, holder_id=holder_id),
            clock=lambda: now,
        ).run_scheduled(cast(SchedulerInvocationEffectAuthorization, None))

    assert port.claim_gate is None
    assert port.scheduler_authorizations == []


async def test_scheduled_command_revocation_before_claim_blocks_first_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    port = FakeOperationCommandPort(_command(now, command_type="pause_paper"))
    holder_id = str(uuid4())
    authorization = _authorization()
    gate = FakeSchedulerAuthorizationGate(
        authorization,
        expected_job_key="operations.commands",
        revoke_at=1,
    )
    monkeypatch.setattr(
        commands_module,
        "require_scheduler_invocation_effect_authorization",
        gate,
    )

    with pytest.raises(SchedulerInvocationPermitRevoked, match="deadline"):
        await ApplyOperationCommands(
            port,
            account_id="paper-primary",
            holder_id=holder_id,
            current_release_sha="a" * 40,
            lease_provider=lambda: _lease(now, holder_id=holder_id),
            clock=lambda: now,
        ).run_scheduled(authorization)

    assert port.claim_gate is None
    assert port.scheduler_authorizations == []


async def test_scheduled_command_mid_run_revocation_escapes_broad_exception_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    port = FakeOperationCommandPort(_command(now, command_type="pause_paper"))
    holder_id = str(uuid4())
    authorization = _authorization()
    gate = FakeSchedulerAuthorizationGate(
        authorization,
        expected_job_key="operations.commands",
        revoke_at=3,
    )
    monkeypatch.setattr(
        commands_module,
        "require_scheduler_invocation_effect_authorization",
        gate,
    )

    with pytest.raises(SchedulerInvocationPermitRevoked, match="deadline"):
        await ApplyOperationCommands(
            port,
            account_id="paper-primary",
            holder_id=holder_id,
            current_release_sha="a" * 40,
            lease_provider=lambda: _lease(now, holder_id=holder_id),
            clock=lambda: now,
        ).run_scheduled(authorization)

    assert port.claim_gate is not None
    assert port.ack_phase is None
    assert port.scheduler_authorizations == [authorization]


async def test_poison_command_is_failed_without_starving_following_command() -> None:
    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    first = _command(now, command_type="pause_paper")
    second = _command(now, command_type="emergency_stop")
    port = PoisonAwareOperationCommandPort((first, second), poison_id=first.command_id)
    holder_id = str(uuid4())
    lease = _lease(now, holder_id=holder_id)

    result = await ApplyOperationCommands(
        port,
        account_id="paper-primary",
        holder_id=holder_id,
        current_release_sha="a" * 40,
        lease_provider=lambda: lease,
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
    holder_id = str(uuid4())
    lease = _lease(now, holder_id=holder_id)

    result = await ApplyOperationCommands(
        port,
        account_id="paper-primary",
        holder_id=holder_id,
        current_release_sha="a" * 40,
        lease_provider=lambda: lease,
        clock=lambda: now,
    ).run_once()

    assert result == OperationCommandRunResult(2, 1, 1, 1)
    assert port.calls[-1][:2] == (second.command_id, "applied")


async def test_command_claim_requires_current_account_lease() -> None:
    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    holder_id = str(uuid4())
    expired_lease = _lease(now - timedelta(minutes=1), holder_id=holder_id)
    port = FakeOperationCommandPort(_command(now, command_type="pause_paper"))

    with pytest.raises(
        ExecutionInvariantError,
        match="operation_worker_lease_is_not_current",
    ):
        await ApplyOperationCommands(
            port,
            account_id="paper-primary",
            holder_id=holder_id,
            current_release_sha="a" * 40,
            lease_provider=lambda: expired_lease,
            clock=lambda: now,
        ).run_once()

    assert port.claim_gate is None


async def test_command_claim_rejects_cross_account_result_before_ack() -> None:
    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    holder_id = str(uuid4())
    port = FakeOperationCommandPort(
        ClaimedOperationCommand(
            command_id=str(uuid4()),
            command_type="pause_paper",
            environment="contract_test",
            account_id="contract-test-primary",
            requested_change={"expected_state_version": 3},
            requested_at=now - timedelta(minutes=1),
            expires_at=now + timedelta(minutes=5),
            revision=4,
            claimed_at=now,
            claim_expires_at=now + timedelta(seconds=30),
        )
    )

    with pytest.raises(
        ExecutionInvariantError,
        match="operation_command_account_scope_mismatch",
    ):
        await ApplyOperationCommands(
            port,
            account_id="paper-primary",
            holder_id=holder_id,
            current_release_sha="a" * 40,
            lease_provider=lambda: _lease(now, holder_id=holder_id),
            clock=lambda: now,
        ).run_once()

    assert port.ack_phase is None


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
        self.ack_phase: Literal["applied", "failed"] | None = None
        self.result_summary: JsonObject | None = None
        self.claim_gate: tuple[str, str, str, int] | None = None
        self.ack_gate: tuple[str, str, str, int, int] | None = None
        self.scheduler_authorizations: list[
            SchedulerInvocationEffectAuthorization | None
        ] = []

    async def claim_operation_command_batch(
        self,
        *,
        account_id: str,
        holder_id: str,
        release_sha: str,
        fencing_token: int,
        now: datetime,
        limit: int,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> tuple[ClaimedOperationCommand, ...]:
        del now, limit
        self.scheduler_authorizations.append(scheduler_authorization)
        self.claim_gate = (account_id, holder_id, release_sha, fencing_token)
        return (self.command,)

    async def acknowledge_operation_command(
        self,
        *,
        command_id: str,
        phase: Literal["applied", "failed"],
        account_id: str,
        holder_id: str,
        release_sha: str,
        fencing_token: int,
        expected_revision: int,
        now: datetime,
        result_summary: JsonObject,
        failure_code: str | None = None,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> OperationCommandAcknowledgement:
        del failure_code
        self.scheduler_authorizations.append(scheduler_authorization)
        self.ack_phase = phase
        self.ack_gate = (
            account_id,
            holder_id,
            release_sha,
            fencing_token,
            expected_revision,
        )
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
            tuple[str, Literal["applied", "failed"], str | None]
        ] = []

    async def claim_operation_command_batch(
        self,
        *,
        account_id: str,
        holder_id: str,
        release_sha: str,
        fencing_token: int,
        now: datetime,
        limit: int,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> tuple[ClaimedOperationCommand, ...]:
        del account_id, holder_id, release_sha, fencing_token, now, limit
        del scheduler_authorization
        return self.commands

    async def acknowledge_operation_command(
        self,
        *,
        command_id: str,
        phase: Literal["applied", "failed"],
        account_id: str,
        holder_id: str,
        release_sha: str,
        fencing_token: int,
        expected_revision: int,
        now: datetime,
        result_summary: JsonObject,
        failure_code: str | None = None,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> OperationCommandAcknowledgement:
        del account_id, holder_id, release_sha, fencing_token, expected_revision
        del result_summary, scheduler_authorization
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


def _lease(now: datetime, *, holder_id: str) -> WorkerLease:
    return WorkerLease(
        account_id="paper-primary",
        holder_id=holder_id,
        fencing_token=7,
        acquired_at=now - timedelta(seconds=5),
        expires_at=now + timedelta(seconds=30),
    )


class FakeSchedulerAuthorizationGate:
    def __init__(
        self,
        authorization: SchedulerInvocationEffectAuthorization,
        *,
        expected_job_key: str,
        revoke_at: int | None = None,
    ) -> None:
        self.authorization = authorization
        self.expected_job_key = expected_job_key
        self.revoke_at = revoke_at
        self.calls = 0

    def __call__(
        self,
        value: object,
        *,
        expected_job_key: str,
    ) -> SchedulerInvocationEffectAuthorization:
        assert value is self.authorization
        assert expected_job_key == self.expected_job_key
        self.calls += 1
        if self.calls == self.revoke_at:
            raise SchedulerInvocationPermitRevoked("deadline")
        return self.authorization


def _authorization() -> SchedulerInvocationEffectAuthorization:
    return cast(SchedulerInvocationEffectAuthorization, object())
