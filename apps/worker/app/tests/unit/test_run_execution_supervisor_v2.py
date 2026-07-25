from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal, cast
from uuid import UUID, uuid4

import pytest

import app.application.use_cases.run_execution_supervisor_v2 as supervisor_module
from app.adapters.persistence.unavailable_paper_execution_source import (
    UnavailablePaperExecutionCommandSource,
)
from app.application.ports.paper_execution_command_source_port import (
    ClaimedPaperExecutionCommand,
    PaperExecutionCommandBundle,
    PaperExecutionCommandKind,
    PaperExecutionSourceCompletion,
    PaperExecutionSourceOutcome,
    PaperExecutionSourceState,
)
from app.application.services.risk_service import RiskService
from app.application.services.scheduler_invocation_deadline import (
    SchedulerInvocationEffectAuthorization,
    SchedulerInvocationPermitRevoked,
)
from app.application.use_cases.run_execution_supervisor_v2 import (
    ExecutionSupervisorV2RunResult,
    RunExecutionSupervisorV2,
)
from app.application.use_cases.run_execution_v2 import (
    ExecutionV2RunOutcome,
    PaperExecutionV2Command,
)
from app.domain.execution_v2.models import (
    ExecutionCostSchedule,
    ExecutionIntent,
    ExecutionInvariantError,
    MinuteBar,
    PaperExecutionEvidence,
)
from app.domain.risk.entities import RiskResult
from app.domain.risk.value_objects import RiskInput
from app.domain.trading.entities import AccountState, BotSettings, Quote, Signal

WORKER_ID = "00000000-0000-4000-8000-000000000001"
RELEASE_SHA = "a" * 40


async def test_new_candidate_rechecks_risk_and_calls_v2_execute_path() -> None:
    command, risk_input, _resume = _commands()
    now = command.evaluated_at
    source = FakePaperExecutionSource(
        command,
        risk_input,
        resume_command=None,
        available_at=now,
    )
    execution = FakeExecutionRunner([ExecutionV2RunOutcome("completed", None)])
    risk_service = RecordingRiskService()

    result = await RunExecutionSupervisorV2(
        source,
        execution,
        risk_service,
        worker_id=WORKER_ID,
        current_release_sha=RELEASE_SHA,
        clock=lambda: now,
    ).run_once()

    assert result == ExecutionSupervisorV2RunResult(1, 1, 0, 1, 0, 0, 0)
    assert risk_service.paper_calls == 1
    assert execution.execute_calls == [command]
    assert execution.resume_calls == []
    assert source.settlements[0]["claim_token"] == source.claim_tokens[0]
    assert source.settlements[0]["expected_revision"] == 1


async def test_partial_resumes_on_next_bar_after_restart_and_stays_idempotent() -> None:
    command, risk_input, resume_command = _commands()
    first_cycle_at = command.evaluated_at
    next_bar_at = resume_command.evaluated_at
    source = FakePaperExecutionSource(
        command,
        risk_input,
        resume_command=resume_command,
        available_at=first_cycle_at,
    )
    first_execution = FakeExecutionRunner([ExecutionV2RunOutcome("pending", None)])

    first = await RunExecutionSupervisorV2(
        source,
        first_execution,
        RecordingRiskService(),
        worker_id=WORKER_ID,
        current_release_sha=RELEASE_SHA,
        clock=lambda: first_cycle_at,
    ).run_once()

    assert first == ExecutionSupervisorV2RunResult(1, 1, 0, 0, 1, 0, 0)
    assert source.available_at == next_bar_at
    assert source.kind == "resume_existing"
    assert source.source_revision == 2

    before_next_bar = await RunExecutionSupervisorV2(
        source,
        FakeExecutionRunner([]),
        RecordingRiskService(),
        worker_id=WORKER_ID,
        current_release_sha=RELEASE_SHA,
        clock=lambda: next_bar_at - timedelta(seconds=1),
    ).run_once()
    assert before_next_bar.claimed == 0

    restarted_execution = FakeExecutionRunner(
        [ExecutionV2RunOutcome("completed", None)]
    )
    restarted = await RunExecutionSupervisorV2(
        source,
        restarted_execution,
        RecordingRiskService(),
        worker_id=WORKER_ID,
        current_release_sha=RELEASE_SHA,
        clock=lambda: next_bar_at,
    ).run_once()

    assert restarted == ExecutionSupervisorV2RunResult(1, 0, 1, 1, 0, 0, 0)
    assert restarted_execution.execute_calls == []
    assert restarted_execution.resume_calls == [resume_command]
    assert source.settlements[-1]["claim_token"] == source.claim_tokens[-1]
    assert source.settlements[-1]["expected_revision"] == 2
    assert source.source_revision == 3

    no_replay = await RunExecutionSupervisorV2(
        source,
        FakeExecutionRunner([]),
        RecordingRiskService(),
        worker_id=WORKER_ID,
        current_release_sha=RELEASE_SHA,
        clock=lambda: next_bar_at + timedelta(minutes=1),
    ).run_once()
    assert no_replay.claimed == 0


async def test_new_candidate_with_expired_risk_evidence_goes_manual() -> None:
    command, risk_input, _resume = _commands()
    source = FakePaperExecutionSource(
        command,
        risk_input,
        resume_command=None,
        available_at=command.evaluated_at,
    )
    execution = FakeExecutionRunner([])
    risk_service = RecordingRiskService()

    result = await RunExecutionSupervisorV2(
        source,
        execution,
        risk_service,
        worker_id=WORKER_ID,
        current_release_sha=RELEASE_SHA,
        clock=lambda: command.intent.risk_expires_at,
    ).run_once()

    assert result == ExecutionSupervisorV2RunResult(1, 0, 0, 0, 0, 0, 1)
    assert source.state == "manual"
    assert source.settlements[0]["reason_code"] == "paper_execution_risk_evidence_expired"
    assert risk_service.paper_calls == 0
    assert execution.execute_calls == []


async def test_missing_durable_source_is_an_explicit_fail_closed_error() -> None:
    now = datetime(2026, 7, 14, 9, 2, tzinfo=UTC)
    with pytest.raises(
        ExecutionInvariantError,
        match="paper_execution_source_unavailable",
    ):
        await RunExecutionSupervisorV2(
            UnavailablePaperExecutionCommandSource(),
            FakeExecutionRunner([]),
            RecordingRiskService(),
            worker_id=WORKER_ID,
            current_release_sha=RELEASE_SHA,
            clock=lambda: now,
        ).run_once()

    with pytest.raises(
        ExecutionInvariantError,
        match="paper_execution_source_unavailable",
    ):
        await UnavailablePaperExecutionCommandSource().claim_available_paper_execution(
            worker_id=WORKER_ID,
            release_sha=RELEASE_SHA,
            now=now,
            lease_ttl=timedelta(seconds=30),
            scheduler_authorization=cast(
                SchedulerInvocationEffectAuthorization,
                object(),
            ),
        )


async def test_scheduled_run_revalidates_and_propagates_execution_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command, risk_input, _resume = _commands()
    now = command.evaluated_at
    source = FakePaperExecutionSource(
        command,
        risk_input,
        resume_command=None,
        available_at=now,
    )
    execution = FakeExecutionRunner([ExecutionV2RunOutcome("completed", None)])
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
        supervisor_module,
        "require_scheduler_invocation_effect_authorization",
        require_authorization,
    )

    result = await RunExecutionSupervisorV2(
        source,
        execution,
        RecordingRiskService(),
        worker_id=WORKER_ID,
        current_release_sha=RELEASE_SHA,
        clock=lambda: now,
    ).run_scheduled(authorization)

    assert result == ExecutionSupervisorV2RunResult(1, 1, 0, 1, 0, 0, 0)
    assert len(validations) == 6
    assert source.scheduler_authorizations == [
        authorization,
        authorization,
        authorization,
        authorization,
    ]
    assert execution.scheduled_authorizations == [authorization]
    assert execution.execute_calls == [command]


async def test_scheduled_run_blocks_runner_after_authority_is_revoked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command, risk_input, _resume = _commands()
    now = command.evaluated_at
    source = FakePaperExecutionSource(
        command,
        risk_input,
        resume_command=None,
        available_at=now,
    )
    execution = FakeExecutionRunner([ExecutionV2RunOutcome("completed", None)])
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
        supervisor_module,
        "require_scheduler_invocation_effect_authorization",
        require_authorization,
    )

    with pytest.raises(SchedulerInvocationPermitRevoked, match="deadline"):
        await RunExecutionSupervisorV2(
            source,
            execution,
            RecordingRiskService(),
            worker_id=WORKER_ID,
            current_release_sha=RELEASE_SHA,
            clock=lambda: now,
        ).run_scheduled(authorization)

    assert execution.execute_calls == []
    assert execution.scheduled_authorizations == []
    assert source.settlements == []


class RecordingRiskService(RiskService):
    def __init__(self) -> None:
        super().__init__()
        self.paper_calls = 0

    def evaluate_paper_order(self, risk_input: RiskInput) -> RiskResult:
        self.paper_calls += 1
        return super().evaluate_paper_order(risk_input)


class FakeExecutionRunner:
    def __init__(self, outcomes: list[ExecutionV2RunOutcome]) -> None:
        self.outcomes = outcomes
        self.execute_calls: list[PaperExecutionV2Command] = []
        self.resume_calls: list[PaperExecutionV2Command] = []
        self.scheduled_authorizations: list[object] = []

    async def execute_paper(
        self,
        command: PaperExecutionV2Command,
    ) -> ExecutionV2RunOutcome:
        self.execute_calls.append(command)
        return self.outcomes.pop(0)

    async def resume_existing_paper(
        self,
        command: PaperExecutionV2Command,
    ) -> ExecutionV2RunOutcome:
        self.resume_calls.append(command)
        return self.outcomes.pop(0)

    async def execute_scheduled_paper(
        self,
        command: PaperExecutionV2Command,
        scheduler_authorization: SchedulerInvocationEffectAuthorization,
    ) -> ExecutionV2RunOutcome:
        self.scheduled_authorizations.append(scheduler_authorization)
        return await self.execute_paper(command)

    async def resume_existing_scheduled_paper(
        self,
        command: PaperExecutionV2Command,
        scheduler_authorization: SchedulerInvocationEffectAuthorization,
    ) -> ExecutionV2RunOutcome:
        self.scheduled_authorizations.append(scheduler_authorization)
        return await self.resume_existing_paper(command)


@dataclass
class FakePaperExecutionSource:
    command: PaperExecutionV2Command
    risk_input: RiskInput
    resume_command: PaperExecutionV2Command | None
    available_at: datetime
    command_id: str = field(init=False, default_factory=lambda: str(uuid4()))
    kind: PaperExecutionCommandKind = field(init=False, default="new_candidate")
    state: Literal["pending", "claimed", "complete", "manual"] = field(
        init=False,
        default="pending",
    )
    source_revision: int = field(init=False, default=1)
    active_claim: ClaimedPaperExecutionCommand | None = field(init=False, default=None)
    claim_tokens: list[str] = field(init=False, default_factory=list)
    settlements: list[dict[str, object]] = field(init=False, default_factory=list)
    scheduler_authorizations: list[object | None] = field(
        init=False,
        default_factory=list,
    )

    async def claim_available_paper_execution(
        self,
        *,
        worker_id: str,
        release_sha: str,
        now: datetime,
        lease_ttl: timedelta,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> ClaimedPaperExecutionCommand | None:
        self.scheduler_authorizations.append(scheduler_authorization)
        if self.state != "pending" or self.available_at > now:
            return None
        token = str(uuid4())
        claim = ClaimedPaperExecutionCommand(
            command_id=self.command_id,
            intent_id=self.command.intent.id,
            kind=self.kind,
            claim_token=token,
            source_revision=self.source_revision,
            worker_id=worker_id,
            release_sha=release_sha,
            available_at=self.available_at,
            claimed_at=now,
            claim_expires_at=now + lease_ttl,
        )
        self.state = "claimed"
        self.active_claim = claim
        self.claim_tokens.append(token)
        return claim

    async def load_claimed_paper_execution_bundle(
        self,
        claim: ClaimedPaperExecutionCommand,
        *,
        now: datetime,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> PaperExecutionCommandBundle:
        del now
        self.scheduler_authorizations.append(scheduler_authorization)
        assert claim == self.active_claim
        return PaperExecutionCommandBundle(
            command=self.command,
            risk_input=self.risk_input if self.kind == "new_candidate" else None,
        )

    async def complete_or_reschedule_paper_execution(
        self,
        *,
        command_id: str,
        claim_token: str,
        expected_revision: int,
        worker_id: str,
        release_sha: str,
        now: datetime,
        outcome: PaperExecutionSourceOutcome,
        next_available_at: datetime | None,
        reason_code: str,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> PaperExecutionSourceCompletion:
        del now
        self.scheduler_authorizations.append(scheduler_authorization)
        assert self.active_claim is not None
        assert command_id == self.command_id
        assert claim_token == self.active_claim.claim_token
        assert expected_revision == self.source_revision
        assert worker_id == WORKER_ID
        assert release_sha == RELEASE_SHA
        self.settlements.append(
            {
                "claim_token": claim_token,
                "expected_revision": expected_revision,
                "outcome": outcome,
                "reason_code": reason_code,
            }
        )
        self.source_revision += 1
        self.active_claim = None
        if outcome == "reschedule":
            assert next_available_at is not None
            assert self.resume_command is not None
            self.state = "pending"
            self.kind = "resume_existing"
            self.command = self.resume_command
            self.available_at = next_available_at
            completion_state: PaperExecutionSourceState = "pending"
        else:
            assert next_available_at is None
            self.state = "manual" if outcome == "manual" else "complete"
            completion_state = "manual" if outcome == "manual" else "complete"
        return PaperExecutionSourceCompletion(
            command_id=self.command_id,
            state=completion_state,
            source_revision=self.source_revision,
            next_available_at=next_available_at,
        )


def _commands() -> tuple[PaperExecutionV2Command, RiskInput, PaperExecutionV2Command]:
    decision_at = datetime(2026, 7, 14, 9, 0, 30, tzinfo=UTC)
    expires_at = datetime(2026, 7, 14, 9, 5, tzinfo=UTC)
    strategy_id = UUID("00000000-0000-4000-8000-000000000010")
    schedule = ExecutionCostSchedule(
        version="fees-v1",
        effective_from=decision_at - timedelta(days=1),
        effective_until=decision_at + timedelta(days=1),
        evidence_sha256="b" * 64,
        settlement_days=2,
        settlement_evidence_sha256="c" * 64,
        buy_commission_rate=Decimal("0.00015"),
        sell_commission_rate=Decimal("0.00015"),
        sell_tax_rate=Decimal("0.0018"),
    )
    intent = ExecutionIntent.create(
        account_id=str(UUID(int=100)),
        environment="paper",
        decision_id=str(uuid4()),
        risk_result_id=str(uuid4()),
        decision_feature_sha256="d" * 64,
        risk_allowed=True,
        risk_reason_codes=(),
        risk_evaluated_at=decision_at,
        risk_expires_at=expires_at + timedelta(minutes=1),
        strategy_version_id=str(strategy_id),
        symbol="005930",
        side="buy",
        quantity=2,
        limit_price_krw=10_000,
        decision_at=decision_at,
        signal_valid_from=decision_at - timedelta(minutes=1),
        signal_valid_until=expires_at,
        execution_policy_version="paper-minute-v1",
        cost_schedule=schedule,
        expires_at=expires_at,
        gate_epoch=1,
        lease_holder_id=WORKER_ID,
        lease_fencing_token=1,
    )
    evidence = PaperExecutionEvidence(
        version="verified-v1",
        execution_policy_version=intent.execution_policy_version,
        effective_from=decision_at - timedelta(days=1),
        effective_until=decision_at + timedelta(days=1),
        tick_rule_version="krx-tick-v1",
        tick_size_krw=1,
        tick_rule_evidence_sha256="e" * 64,
        volume_source="verified_fixture",
        volume_unit="shares",
        volume_evidence_sha256="f" * 64,
        corporate_action_status="not_required",
        corporate_action_evidence_sha256="1" * 64,
        market_calendar_version="krx-calendar-v1",
        market_calendar_status="open_sessions_verified",
        market_calendar_evidence_sha256="2" * 64,
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
            source_sha256="3" * 64,
            is_complete=True,
            open_krw=9_000,
            high_krw=9_100,
            low_krw=8_900,
            close_krw=9_000,
            volume=100,
        )
        for offset in range(2)
    )
    first = PaperExecutionV2Command.create(
        intent=intent,
        bars=(bars[0],),
        cost_schedule=schedule,
        execution_evidence=evidence,
        dispatch_at=intent.eligible_at,
        evaluated_at=bars[0].completed_at,
    )
    resume = PaperExecutionV2Command.create(
        intent=intent,
        bars=bars,
        cost_schedule=schedule,
        execution_evidence=evidence,
        dispatch_at=bars[1].completed_at,
        evaluated_at=bars[1].completed_at,
    )
    risk_input = RiskInput(
        settings=BotSettings(enabled=True, mode="paper"),
        signal=Signal(
            symbol=intent.symbol,
            action="buy",
            final_score=0.9,
            confidence=0.9,
            order_amount_krw=100_000,
            sector="technology",
            reason_json={"source": "test"},
        ),
        account_state=AccountState(
            synced=True,
            cash_krw=10_000_000,
            equity_krw=10_000_000,
            daily_loss_pct=0.0,
            daily_order_count=0,
            synced_at=decision_at,
        ),
        quote=Quote(
            symbol=intent.symbol,
            price_krw=intent.limit_price_krw,
            as_of=decision_at,
        ),
        now=decision_at,
        provider_health={"supabase": True, "market_data": True},
        market_open=True,
        existing_position_pct=0.0,
        sector_position_pct=0.0,
        available_position_quantity=10,
        critical_news_risk=False,
        liquidity_ok=True,
        volatility_ok=True,
        cooldown_active=False,
        duplicate_order=False,
        strategy_version_id=strategy_id,
        strategy_status="active",
        strategy_approved=True,
    )
    return first, risk_input, resume
