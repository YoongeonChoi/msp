from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal, Never, cast
from uuid import UUID, uuid4

import pytest

import app.application.use_cases.run_execution_v2 as execution_module
from app.adapters.persistence.execution_kernel_v2 import InMemoryExecutionKernelV2
from app.application.services.paper_execution_v2 import DeterministicPaperExecutionSimulator
from app.application.services.scheduler_invocation_deadline import (
    SchedulerInvocationEffectAuthorization,
    SchedulerInvocationPermitRevoked,
)
from app.application.use_cases.run_execution_v2 import (
    PaperExecutionV2Command,
    RunExecutionV2,
)
from app.domain.execution_v2.models import (
    AccountingTransaction,
    ExecutionCostSchedule,
    ExecutionGate,
    ExecutionIntent,
    ExecutionInvariantError,
    ExecutionObservation,
    MinuteBar,
    ObservationRecordResult,
    OrderIntentReservationResult,
    PaperExecutionCheckpoint,
    PaperExecutionEvidence,
    PaperPositionCostBasis,
    build_observation_history_sha256,
    build_semantic_key,
)
from app.tools.resume_paper_execution_v2_once import load_resume_input


@pytest.mark.parametrize(
    "crash_point",
    ["reserve", "dispatch", "observation-1", "observation-2"],
)
async def test_durable_command_replays_after_every_commit_boundary(
    crash_point: str,
) -> None:
    command = _command()
    durable = CrashInjectingDurablePort(crash_point)
    runner = RunExecutionV2(
        InMemoryExecutionKernelV2("paper"),
        durable_port=durable,
    )

    with pytest.raises(RuntimeError, match=f"crash_after_{crash_point}"):
        await runner.execute_paper(command)

    outcome = await runner.execute_paper(command)

    assert outcome.status == "completed"
    assert durable.reservation_states == ["created", "existing_replay"]
    assert durable.dispatch_marker_writes >= 1
    assert sorted(durable.observations) == [1, 2]
    assert [item.status for item in durable.observations.values()] == [
        "partial_filled",
        "filled",
    ]
    assert durable.accounting_sequences == {1, 2}


async def test_semantic_duplicate_never_dispatches_the_existing_intent() -> None:
    command = _command()
    durable = SemanticDuplicateDurablePort()
    outcome = await RunExecutionV2(
        InMemoryExecutionKernelV2("paper"),
        durable_port=durable,
    ).execute_paper(command)

    assert outcome.status == "duplicate_semantic_intent"
    assert outcome.result is None
    assert outcome.reason_code == "duplicate_semantic_intent"
    assert durable.dispatch_marker_writes == 0


async def test_existing_replay_rejects_a_different_intent_before_dispatch() -> None:
    durable = MismatchedExistingReplayDurablePort()

    with pytest.raises(ExecutionInvariantError, match="durable_replay_intent_mismatch"):
        await RunExecutionV2(
            InMemoryExecutionKernelV2("paper"),
            durable_port=durable,
        ).execute_paper(_command())

    assert durable.dispatch_marker_writes == 0


async def test_durable_sell_uses_explicit_immutable_position_cost_basis() -> None:
    command = _command(side="sell")
    outcome = await RunExecutionV2(
        InMemoryExecutionKernelV2("paper"),
        durable_port=CrashInjectingDurablePort("never"),
    ).execute_paper(command)

    assert outcome.status == "completed"
    assert outcome.result is not None
    assert outcome.result.fills
    assert outcome.result.fills[0].position_cost_relief_krw > 0


async def test_durable_partial_result_remains_pending_before_expiry() -> None:
    command = _command()
    first_bar = command.bars[0]
    partial_command = PaperExecutionV2Command.create(
        intent=command.intent,
        bars=(first_bar,),
        cost_schedule=command.cost_schedule,
        execution_evidence=command.execution_evidence,
        dispatch_at=command.dispatch_at,
        evaluated_at=first_bar.completed_at,
    )

    outcome = await RunExecutionV2(
        InMemoryExecutionKernelV2("paper"),
        durable_port=CrashInjectingDurablePort("never"),
    ).execute_paper(partial_command)

    assert outcome.status == "pending"
    assert outcome.result is not None
    assert [item.status for item in outcome.result.observations] == ["partial_filled"]


async def test_in_memory_bar_participation_is_shared_across_semantic_intents() -> None:
    first = _command()
    kernel = InMemoryExecutionKernelV2("paper")
    await kernel.configure_account(first.intent.account_id, cash_krw=1_000_000)
    await kernel.replace_gate(
        ExecutionGate(
            account_id=first.intent.account_id,
            environment="paper",
            enabled=True,
            control_epoch=first.intent.gate_epoch,
            effective_at=first.intent.decision_at,
            expires_at=first.intent.expires_at + timedelta(hours=1),
        )
    )
    lease = await kernel.acquire_lease(
        account_id=first.intent.account_id,
        holder_id=first.intent.lease_holder_id,
        now=first.intent.decision_at,
        ttl=timedelta(hours=1),
    )
    assert lease.fencing_token == first.intent.lease_fencing_token
    second_intent = ExecutionIntent.create(
        account_id=first.intent.account_id,
        environment="paper",
        decision_id=str(uuid4()),
        risk_result_id=str(uuid4()),
        decision_feature_sha256=first.intent.decision_feature_sha256,
        risk_allowed=True,
        risk_reason_codes=(),
        risk_evaluated_at=first.intent.risk_evaluated_at,
        risk_expires_at=first.intent.risk_expires_at,
        strategy_version_id="strategy-v2",
        symbol=first.intent.symbol,
        side="buy",
        quantity=first.intent.quantity,
        limit_price_krw=first.intent.limit_price_krw,
        decision_at=first.intent.decision_at,
        signal_valid_from=first.intent.signal_valid_from,
        signal_valid_until=first.intent.signal_valid_until,
        execution_policy_version=first.intent.execution_policy_version,
        cost_schedule=first.cost_schedule,
        expires_at=first.intent.expires_at,
        gate_epoch=first.intent.gate_epoch,
        lease_holder_id=first.intent.lease_holder_id,
        lease_fencing_token=first.intent.lease_fencing_token,
    )
    second = replace(first, intent=second_intent)
    runner = RunExecutionV2(kernel)

    first_outcome = await runner.execute_paper(first)
    first_replay = await runner.execute_paper(first)
    second_outcome = await runner.execute_paper(second)

    assert first_outcome.result is not None
    assert first_replay.result == first_outcome.result
    assert second_outcome.result is not None
    assert sum(fill.quantity for fill in first_outcome.result.fills) == 2
    assert sum(fill.quantity for fill in second_outcome.result.fills) == 0
    assert [item.status for item in second_outcome.result.observations] == ["expired"]
    snapshot = await kernel.account_snapshot(first.intent.account_id)
    assert snapshot.quantity_for(first.intent.symbol) == 2


async def test_durable_partial_resumes_next_bar_after_process_and_lease_restart() -> None:
    command = _command()
    first_bar = command.bars[0]
    durable = CrashInjectingDurablePort("never")
    partial_command = PaperExecutionV2Command.create(
        intent=command.intent,
        bars=(first_bar,),
        cost_schedule=command.cost_schedule,
        execution_evidence=command.execution_evidence,
        dispatch_at=command.dispatch_at,
        evaluated_at=first_bar.completed_at,
    )

    first_outcome = await RunExecutionV2(
        InMemoryExecutionKernelV2("paper"),
        durable_port=durable,
    ).execute_paper(partial_command)

    assert first_outcome.status == "pending"
    assert durable.record_calls == [1]
    original_intent_id = command.intent.id

    # Model a process restart after the old lease expired and fencing token 2
    # was acquired. A different semantic duplicate cannot take ownership of the
    # original intent, while the original id with its stale fence still fails.
    durable.reacquire(fencing_token=2)
    duplicate_command = replace(
        command,
        intent=replace(command.intent, id=str(uuid4())),
    )
    duplicate = await RunExecutionV2(
        InMemoryExecutionKernelV2("paper"),
        durable_port=durable,
    ).execute_paper(duplicate_command)
    assert duplicate.status == "duplicate_semantic_intent"

    with pytest.raises(ExecutionInvariantError, match="worker_fencing_token_stale"):
        await RunExecutionV2(
            InMemoryExecutionKernelV2("paper"),
            durable_port=durable,
        ).execute_paper(command)

    resumed_command = replace(
        command,
        intent=replace(
            command.intent,
            lease_fencing_token=2,
        ),
        evaluated_at=command.bars[1].completed_at,
    )
    restarted_runner = RunExecutionV2(
        InMemoryExecutionKernelV2("paper"),
        durable_port=durable,
    )

    resumed = await restarted_runner.execute_paper(resumed_command)

    assert resumed.status == "completed"
    assert resumed.result is not None
    assert [item.status for item in resumed.result.observations] == [
        "partial_filled",
        "filled",
    ]
    assert {item.intent_id for item in resumed.result.observations} == {
        original_intent_id
    }
    assert durable.record_calls == [1, 2]
    assert durable.accounting_sequences == {1, 2}
    assert durable.dispatch_times[-1] == resumed_command.evaluated_at


async def test_bounded_partial_resume_never_reserves_a_new_intent() -> None:
    command = _command()
    first_bar = command.bars[0]
    durable = CrashInjectingDurablePort("never")
    partial_command = replace(
        command,
        bars=(first_bar,),
        evaluated_at=first_bar.completed_at,
    )
    await RunExecutionV2(
        InMemoryExecutionKernelV2("paper"),
        durable_port=durable,
    ).execute_paper(partial_command)
    assert durable.reservation_states == ["created"]

    durable.reacquire(fencing_token=2)
    resumed_command = replace(
        command,
        intent=replace(command.intent, lease_fencing_token=2),
        evaluated_at=command.bars[1].completed_at,
    )
    outcome = await RunExecutionV2(durable_port=durable).resume_existing_paper(
        resumed_command
    )

    assert outcome.status == "completed"
    assert durable.reservation_states == ["created"]
    assert durable.record_calls == [1, 2]


async def test_scheduled_execute_revalidates_before_every_durable_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = _command()
    durable = CrashInjectingDurablePort("never")
    authorization = cast(SchedulerInvocationEffectAuthorization, object())
    validations: list[object] = []

    def require_authorization(
        value: object,
        *,
        expected_job_key: str,
    ) -> SchedulerInvocationEffectAuthorization:
        assert value is authorization
        assert expected_job_key == "operations.execution"
        validations.append(value)
        return authorization

    monkeypatch.setattr(
        execution_module,
        "require_scheduler_invocation_effect_authorization",
        require_authorization,
    )

    outcome = await RunExecutionV2(
        durable_port=durable,
    ).execute_scheduled_paper(command, authorization)

    assert outcome.status == "completed"
    assert len(validations) == 5
    assert durable.scheduler_authorizations == [authorization] * 4
    assert durable.record_calls == [1, 2]


async def test_scheduled_resume_revalidates_before_every_durable_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = _command()
    durable = CrashInjectingDurablePort("never")
    durable.reserved = True
    durable.persisted_intent_id = command.intent.id
    durable.active_fencing_token = command.intent.lease_fencing_token
    durable.observations[1] = ExecutionObservation.create(
        intent_id=command.intent.id,
        sequence=1,
        status="open",
        observed_at=command.intent.eligible_at,
        provider_order_id=f"paper:{command.intent.id}",
        provider_execution_id=None,
        cumulative_quantity=0,
        cumulative_gross_krw=0,
        cumulative_commission_krw=0,
        cumulative_tax_krw=0,
    )
    resumed_command = replace(
        command,
        dispatch_at=command.bars[1].completed_at,
        evaluated_at=command.bars[1].completed_at,
    )
    authorization = cast(SchedulerInvocationEffectAuthorization, object())
    validations: list[object] = []

    def require_authorization(
        value: object,
        *,
        expected_job_key: str,
    ) -> SchedulerInvocationEffectAuthorization:
        assert value is authorization
        assert expected_job_key == "operations.execution"
        validations.append(value)
        return authorization

    monkeypatch.setattr(
        execution_module,
        "require_scheduler_invocation_effect_authorization",
        require_authorization,
    )

    outcome = await RunExecutionV2(
        durable_port=durable,
    ).resume_existing_scheduled_paper(resumed_command, authorization)

    assert outcome.status == "completed"
    assert len(validations) == 5
    assert durable.scheduler_authorizations == [authorization] * 4
    assert durable.record_calls == [2, 3]


async def test_scheduled_execute_blocks_next_effect_after_revocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = _command()
    durable = CrashInjectingDurablePort("never")
    authorization = cast(SchedulerInvocationEffectAuthorization, object())
    validation_count = 0

    def require_authorization(
        value: object,
        *,
        expected_job_key: str,
    ) -> SchedulerInvocationEffectAuthorization:
        nonlocal validation_count
        assert value is authorization
        assert expected_job_key == "operations.execution"
        validation_count += 1
        if validation_count == 4:
            raise SchedulerInvocationPermitRevoked("deadline")
        return authorization

    monkeypatch.setattr(
        execution_module,
        "require_scheduler_invocation_effect_authorization",
        require_authorization,
    )

    with pytest.raises(SchedulerInvocationPermitRevoked, match="deadline"):
        await RunExecutionV2(
            durable_port=durable,
        ).execute_scheduled_paper(command, authorization)

    assert durable.reservation_states == ["created"]
    assert durable.dispatch_marker_writes == 1
    assert durable.record_calls == []
    assert durable.scheduler_authorizations == [authorization, authorization]


async def test_bounded_open_resume_resequences_fills_without_new_reservation() -> None:
    command = _command()
    durable = CrashInjectingDurablePort("never")
    durable.reserved = True
    durable.persisted_intent_id = command.intent.id
    durable.active_fencing_token = command.intent.lease_fencing_token
    durable.observations[1] = ExecutionObservation.create(
        intent_id=command.intent.id,
        sequence=1,
        status="open",
        observed_at=command.intent.eligible_at,
        provider_order_id=f"paper:{command.intent.id}",
        provider_execution_id=None,
        cumulative_quantity=0,
        cumulative_gross_krw=0,
        cumulative_commission_krw=0,
        cumulative_tax_krw=0,
    )
    resumed_command = replace(
        command,
        dispatch_at=command.bars[1].completed_at,
        evaluated_at=command.bars[1].completed_at,
    )

    outcome = await RunExecutionV2(durable_port=durable).resume_existing_paper(
        resumed_command
    )

    assert outcome.status == "completed"
    assert outcome.result is not None
    assert [item.sequence for item in outcome.result.observations] == [2, 3]
    assert [item.status for item in durable.observations.values()] == [
        "open",
        "partial_filled",
        "filled",
    ]
    assert durable.reservation_states == []
    assert durable.record_calls == [2, 3]
    assert durable.accounting_sequences == {2, 3}


async def test_open_then_partial_resumes_after_a_second_process_restart() -> None:
    command = _command()
    durable = CrashInjectingDurablePort("never")
    durable.reserved = True
    durable.persisted_intent_id = command.intent.id
    durable.active_fencing_token = command.intent.lease_fencing_token
    durable.observations[1] = ExecutionObservation.create(
        intent_id=command.intent.id,
        sequence=1,
        status="open",
        observed_at=command.intent.eligible_at,
        provider_order_id=f"paper:{command.intent.id}",
        provider_execution_id=None,
        cumulative_quantity=0,
        cumulative_gross_krw=0,
        cumulative_commission_krw=0,
        cumulative_tax_krw=0,
    )
    first_bar = command.bars[0]
    first_resume = replace(
        command,
        bars=(first_bar,),
        dispatch_at=first_bar.completed_at,
        evaluated_at=first_bar.completed_at,
    )

    pending = await RunExecutionV2(durable_port=durable).resume_existing_paper(
        first_resume
    )

    assert pending.status == "pending"
    assert durable.record_calls == [2]
    assert [item.status for item in durable.observations.values()] == [
        "open",
        "partial_filled",
    ]

    second_bar = command.bars[1]
    second_resume = replace(
        command,
        dispatch_at=second_bar.completed_at,
        evaluated_at=second_bar.completed_at,
    )
    completed = await RunExecutionV2(durable_port=durable).resume_existing_paper(
        second_resume
    )

    assert completed.status == "completed"
    assert durable.reservation_states == []
    assert durable.record_calls == [2, 3]
    assert durable.accounting_sequences == {2, 3}
    assert [item.status for item in durable.observations.values()] == [
        "open",
        "partial_filled",
        "filled",
    ]


async def test_bounded_resume_rejects_an_empty_persisted_prefix_without_dispatch() -> None:
    durable = CrashInjectingDurablePort("never")
    command = _command()
    durable.active_fencing_token = command.intent.lease_fencing_token

    with pytest.raises(
        ExecutionInvariantError,
        match="paper_resume_requires_persisted_open_or_partial",
    ):
        await RunExecutionV2(durable_port=durable).resume_existing_paper(command)

    assert durable.reservation_states == []
    assert durable.dispatch_marker_writes == 0
    assert durable.record_calls == []


def test_resume_input_requires_an_exact_hash_and_strict_schema(tmp_path: Path) -> None:
    command = _command()
    extended_expiry = command.intent.eligible_at + timedelta(minutes=20)
    extended_intent = replace(
        command.intent,
        signal_valid_until=extended_expiry,
        expires_at=extended_expiry,
        semantic_key=build_semantic_key(
            account_id=command.intent.account_id,
            environment=command.intent.environment,
            strategy_version_id=command.intent.strategy_version_id,
            symbol=command.intent.symbol,
            side=command.intent.side,
            signal_valid_from=command.intent.signal_valid_from,
            signal_valid_until=extended_expiry,
            execution_policy_version=command.intent.execution_policy_version,
        ),
    )
    command = replace(command, intent=extended_intent)
    payload = {
        "schema_version": 1,
        "intent": asdict(command.intent),
        "bars": [asdict(bar) for bar in command.bars],
        "cost_schedule": asdict(command.cost_schedule),
        "execution_evidence": asdict(command.execution_evidence),
        "position_cost_basis": None,
    }
    raw = json.dumps(
        payload,
        default=_resume_json_default,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    path = tmp_path / "resume.json"
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()

    loaded = load_resume_input(path, expected_sha256=digest)

    assert loaded.intent.id == command.intent.id
    assert len(loaded.bars) == 2
    resumed_at = command.intent.eligible_at + timedelta(minutes=10)
    assert loaded.to_command(evaluated_at=resumed_at).dispatch_at == resumed_at
    with pytest.raises(ExecutionInvariantError, match="sha256_mismatch"):
        load_resume_input(path, expected_sha256="0" * 64)

    payload["unexpected"] = True
    invalid_raw = json.dumps(
        payload,
        default=_resume_json_default,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    path.write_bytes(invalid_raw)
    with pytest.raises(ExecutionInvariantError, match="schema_is_invalid"):
        load_resume_input(
            path,
            expected_sha256=hashlib.sha256(invalid_raw).hexdigest(),
        )


async def test_emergency_stop_blocks_partial_resume_before_simulation() -> None:
    command = _command()
    first_bar = command.bars[0]
    durable = CrashInjectingDurablePort("never")
    partial_command = replace(
        command,
        bars=(first_bar,),
        evaluated_at=first_bar.completed_at,
    )
    await RunExecutionV2(
        InMemoryExecutionKernelV2("paper"),
        durable_port=durable,
    ).execute_paper(partial_command)
    assert durable.record_calls == [1]
    assert durable.accounting_sequences == {1}

    durable.emergency_stop()
    resume = replace(command, intent=replace(command.intent, id=str(uuid4())))
    with pytest.raises(
        ExecutionInvariantError,
        match="execution_control_stale_or_disabled",
    ):
        await RunExecutionV2(
            durable_port=durable,
            simulator=NeverCalledPaperSimulator(),
        ).resume_existing_paper(resume)

    assert durable.record_calls == [1]
    assert durable.accounting_sequences == {1}


async def test_durable_resume_quarantines_mismatched_observation_history() -> None:
    command = _command()
    first_bar = command.bars[0]
    durable = CrashInjectingDurablePort("never")
    partial_command = replace(
        command,
        bars=(first_bar,),
        evaluated_at=first_bar.completed_at,
    )
    await RunExecutionV2(
        InMemoryExecutionKernelV2("paper"),
        durable_port=durable,
    ).execute_paper(partial_command)
    durable.corrupt_checkpoint_history = True

    outcome = await RunExecutionV2(
        InMemoryExecutionKernelV2("paper"),
        durable_port=durable,
    ).execute_paper(command)

    assert outcome.status == "quarantined"
    assert outcome.reason_code == "paper_execution_resume_history_mismatch"
    assert durable.record_calls == [1]
    assert durable.accounting_sequences == {1}

class SemanticDuplicateDurablePort:
    def __init__(self) -> None:
        self.dispatch_marker_writes = 0

    @property
    def current_release_sha(self) -> str:
        return "a" * 40

    async def reserve_order_intent(
        self,
        intent: ExecutionIntent,
        *,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> OrderIntentReservationResult:
        del intent, scheduler_authorization
        return OrderIntentReservationResult(
            state="semantic_duplicate",
            intent_id=str(UUID(int=99)),
            reservation_id=str(UUID(int=100)),
            reason_code="duplicate_semantic_intent",
        )

    async def mark_dispatch_started(
        self,
        intent: ExecutionIntent,
        *,
        now: datetime,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> None:
        del intent, now, scheduler_authorization
        self.dispatch_marker_writes += 1

    async def load_paper_execution_checkpoint(
        self,
        intent: ExecutionIntent,
        *,
        now: datetime,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> PaperExecutionCheckpoint:
        del now, scheduler_authorization
        return PaperExecutionCheckpoint(
            intent_id=intent.id,
            attempt_id=str(UUID(int=101)),
            provider_order_id=f"paper:{intent.id}",
            latest_sequence=1,
            latest_status="filled",
            latest_observed_at=intent.expires_at,
            cumulative_quantity=intent.quantity,
            cumulative_gross_krw=intent.quantity * intent.limit_price_krw,
            cumulative_commission_krw=0,
            cumulative_tax_krw=0,
            observation_history_sha256="f" * 64,
            expires_at=intent.expires_at,
            intent_release_sha="a" * 40,
            lease_release_sha="a" * 40,
            position_cost_basis_method=None,
            position_quantity_snapshot=None,
            position_total_cost_krw=None,
            position_cost_basis_sha256=None,
        )

    async def record_execution_observation(
        self,
        intent: ExecutionIntent,
        observation: ExecutionObservation,
        *,
        accounting_transaction: AccountingTransaction | None = None,
        intent_release_sha: str,
        now: datetime,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> ObservationRecordResult:
        del (
            intent,
            observation,
            accounting_transaction,
            intent_release_sha,
            now,
            scheduler_authorization,
        )
        raise AssertionError("semantic duplicate must not record observations")


class MismatchedExistingReplayDurablePort(SemanticDuplicateDurablePort):
    async def reserve_order_intent(
        self,
        intent: ExecutionIntent,
        *,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> OrderIntentReservationResult:
        del intent, scheduler_authorization
        return OrderIntentReservationResult(
            state="existing_replay",
            intent_id=str(UUID(int=99)),
            reservation_id=str(UUID(int=100)),
            reason_code="reservation_replay",
        )


class CrashInjectingDurablePort:
    def __init__(self, crash_point: str) -> None:
        self.crash_point = crash_point
        self.crashed = False
        self.reserved = False
        self.persisted_intent_id: str | None = None
        self.active_fencing_token: int | None = None
        self.reservation_states: list[str] = []
        self.dispatch_started = False
        self.dispatch_marker_writes = 0
        self.dispatch_times: list[datetime] = []
        self.observations: dict[int, ExecutionObservation] = {}
        self.record_calls: list[int] = []
        self.accounting_sequences: set[int] = set()
        self.control_epoch = 1
        self.execution_enabled = True
        self.corrupt_checkpoint_history = False
        self.scheduler_authorizations: list[object | None] = []

    @property
    def current_release_sha(self) -> str:
        return "a" * 40

    async def reserve_order_intent(
        self,
        intent: ExecutionIntent,
        *,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> OrderIntentReservationResult:
        self.scheduler_authorizations.append(scheduler_authorization)
        if self.persisted_intent_id is None:
            self.persisted_intent_id = intent.id
            self.active_fencing_token = intent.lease_fencing_token
        state: Literal["created", "existing_replay", "semantic_duplicate"] = (
            "created"
            if not self.reserved
            else (
                "existing_replay"
                if self.persisted_intent_id == intent.id
                else "semantic_duplicate"
            )
        )
        self.reserved = True
        self.reservation_states.append(state)
        self._crash_once("reserve")
        return OrderIntentReservationResult(
            state=state,
            intent_id=self.persisted_intent_id,
            reservation_id=str(UUID(int=100)),
            reason_code="reserved" if state == "created" else "duplicate_semantic_intent",
        )

    async def mark_dispatch_started(
        self,
        intent: ExecutionIntent,
        *,
        now: datetime,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> None:
        self.scheduler_authorizations.append(scheduler_authorization)
        if not self.execution_enabled or intent.gate_epoch != self.control_epoch:
            raise ExecutionInvariantError("execution_control_stale_or_disabled")
        self.dispatch_times.append(now)
        self.dispatch_started = True
        self.dispatch_marker_writes += 1
        self._crash_once("dispatch")

    async def load_paper_execution_checkpoint(
        self,
        intent: ExecutionIntent,
        *,
        now: datetime,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> PaperExecutionCheckpoint:
        self.scheduler_authorizations.append(scheduler_authorization)
        del now
        if not self.execution_enabled or intent.gate_epoch != self.control_epoch:
            raise ExecutionInvariantError("execution_control_stale_or_disabled")
        if intent.lease_fencing_token != self.active_fencing_token:
            raise ExecutionInvariantError("worker_fencing_token_stale")
        observations = tuple(self.observations[index] for index in sorted(self.observations))
        latest = observations[-1] if observations else None
        return PaperExecutionCheckpoint(
            intent_id=intent.id,
            attempt_id=str(UUID(int=101)),
            provider_order_id=(latest.provider_order_id if latest is not None else None),
            latest_sequence=(latest.sequence if latest is not None else None),
            latest_status=(latest.status if latest is not None else None),
            latest_observed_at=(latest.observed_at if latest is not None else None),
            cumulative_quantity=(
                latest.cumulative_quantity if latest is not None else 0
            ),
            cumulative_gross_krw=(
                latest.cumulative_gross_krw if latest is not None else 0
            ),
            cumulative_commission_krw=(
                latest.cumulative_commission_krw if latest is not None else 0
            ),
            cumulative_tax_krw=(
                latest.cumulative_tax_krw if latest is not None else 0
            ),
            observation_history_sha256=(
                (
                    "0" * 64
                    if self.corrupt_checkpoint_history
                    else build_observation_history_sha256(observations)
                )
                if observations
                else None
            ),
            expires_at=intent.expires_at,
            intent_release_sha="a" * 40,
            lease_release_sha="a" * 40,
            position_cost_basis_method=None,
            position_quantity_snapshot=None,
            position_total_cost_krw=None,
            position_cost_basis_sha256=None,
        )

    async def record_execution_observation(
        self,
        intent: ExecutionIntent,
        observation: ExecutionObservation,
        *,
        accounting_transaction: AccountingTransaction | None = None,
        intent_release_sha: str,
        now: datetime,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> ObservationRecordResult:
        self.scheduler_authorizations.append(scheduler_authorization)
        del now
        if intent_release_sha != self.current_release_sha:
            raise ExecutionInvariantError("worker_api_observation_origin_release_mismatch")
        if intent.lease_fencing_token != self.active_fencing_token:
            raise ExecutionInvariantError("worker_fencing_token_stale")
        self.record_calls.append(observation.sequence)
        existing = self.observations.get(observation.sequence)
        if existing is not None and existing != observation:
            raise AssertionError("observation idempotency payload conflict")
        inserted = existing is None
        self.observations[observation.sequence] = observation
        if accounting_transaction is not None:
            self.accounting_sequences.add(accounting_transaction.observation_sequence)
        self._crash_once(f"observation-{observation.sequence}")
        return ObservationRecordResult(
            observation_id=str(UUID(int=200 + observation.sequence)),
            inserted=inserted,
            quarantined=False,
            reason_code="recorded" if inserted else "duplicate_observation",
        )

    def reacquire(self, *, fencing_token: int) -> None:
        self.active_fencing_token = fencing_token

    def emergency_stop(self) -> None:
        self.control_epoch += 1
        self.execution_enabled = False

    def _crash_once(self, point: str) -> None:
        if self.crash_point == point and not self.crashed:
            self.crashed = True
            raise RuntimeError(f"crash_after_{point}")


class NeverCalledPaperSimulator(DeterministicPaperExecutionSimulator):
    def simulate(self, *args: object, **kwargs: object) -> Never:
        del args, kwargs
        raise AssertionError("paper simulator must not run after emergency stop")


def _command(*, side: Literal["buy", "sell"] = "buy") -> PaperExecutionV2Command:
    now = datetime(2026, 7, 14, 9, 0, 30, tzinfo=UTC)
    expires_at = now.replace(hour=9, minute=5, second=0)
    schedule = ExecutionCostSchedule(
        version="fees-v1",
        effective_from=now - timedelta(days=1),
        effective_until=now + timedelta(days=1),
        evidence_sha256="a" * 64,
        settlement_days=2,
        settlement_evidence_sha256="f" * 64,
        buy_commission_rate=Decimal("0.00015"),
        sell_commission_rate=Decimal("0.00015"),
        sell_tax_rate=Decimal("0.0018"),
    )
    intent = ExecutionIntent.create(
        account_id=str(UUID(int=1)),
        environment="paper",
        decision_id=str(uuid4()),
        risk_result_id=str(uuid4()),
        decision_feature_sha256="e" * 64,
        risk_allowed=True,
        risk_reason_codes=(),
        risk_evaluated_at=now,
        risk_expires_at=expires_at + timedelta(hours=1),
        strategy_version_id="strategy-v1",
        symbol="005930",
        side=side,
        quantity=2,
        limit_price_krw=9_000 if side == "sell" else 10_000,
        decision_at=now,
        signal_valid_from=now - timedelta(minutes=1),
        signal_valid_until=expires_at,
        execution_policy_version="paper-minute-v1",
        cost_schedule=schedule,
        expires_at=expires_at,
        gate_epoch=1,
        lease_holder_id=str(UUID(int=2)),
        lease_fencing_token=1,
    )
    evidence = PaperExecutionEvidence(
        version="verified-fixture-v1",
        execution_policy_version=intent.execution_policy_version,
        effective_from=now - timedelta(days=1),
        effective_until=now + timedelta(days=1),
        tick_rule_version="krx-tick-v1",
        tick_size_krw=1,
        tick_rule_evidence_sha256="b" * 64,
        volume_source="verified_fixture",
        volume_unit="shares",
        volume_evidence_sha256="c" * 64,
        corporate_action_status="not_required",
        corporate_action_evidence_sha256="d" * 64,
        market_calendar_version="krx-calendar-v1",
        market_calendar_status="open_sessions_verified",
        market_calendar_evidence_sha256="f" * 64,
        open_session_dates=tuple(
            intent.eligible_at.date() + timedelta(days=offset) for offset in range(5)
        ),
    )
    bars = tuple(
        MinuteBar(
            symbol=intent.symbol,
            minute=intent.eligible_at + timedelta(minutes=offset),
            completed_at=intent.eligible_at + timedelta(minutes=offset + 1),
            as_of=intent.eligible_at + timedelta(minutes=offset + 1),
            source_sha256="c" * 64,
            is_complete=True,
            open_krw=9_000,
            high_krw=9_100,
            low_krw=8_900,
            close_krw=9_000,
            volume=100,
        )
        for offset in range(2)
    )
    return PaperExecutionV2Command.create(
        intent=intent,
        bars=bars,
        cost_schedule=schedule,
        execution_evidence=evidence,
        position_cost_basis=(
            PaperPositionCostBasis(
                symbol=intent.symbol,
                quantity=10,
                total_cost_krw=80_000,
            )
            if side == "sell"
            else None
        ),
        dispatch_at=intent.eligible_at,
        evaluated_at=expires_at,
    )


def _resume_json_default(value: object) -> str:
    if isinstance(value, (date, datetime, Decimal)):
        return str(value) if isinstance(value, Decimal) else value.isoformat()
    raise TypeError("resume_fixture_contains_unsupported_value")
