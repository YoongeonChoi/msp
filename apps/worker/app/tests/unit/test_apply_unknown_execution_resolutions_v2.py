from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from uuid import uuid4

import pytest

import app.application.use_cases.apply_unknown_execution_resolutions_v2 as unknown_module
from app.application.services.scheduler_invocation_deadline import (
    SchedulerInvocationEffectAuthorization,
    SchedulerInvocationPermitRevoked,
)
from app.application.use_cases.apply_unknown_execution_resolutions_v2 import (
    ApplyUnknownExecutionResolutionsV2,
    RunExecutionReconciliationStageV2,
    UnknownResolutionRunResult,
)
from app.application.use_cases.reconcile_execution_v2 import (
    ExecutionReconciliationRunResult,
)
from app.domain.execution_v2.models import ExecutionInvariantError, WorkerLease
from app.domain.execution_v2.unknown_resolution import (
    UnknownResolutionApplicationReceipt,
    UnknownResolutionApplyAmbiguousError,
    UnknownResolutionCandidate,
    UnknownResolutionClaim,
)

NOW = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)
HOLDER_ID = "00000000-0000-4000-8000-000000000001"
RELEASE_SHA = "a" * 40


async def test_unknown_resolution_claims_and_applies_a_bounded_candidate() -> None:
    candidate = _candidate()
    port = StubUnknownPort((candidate,))
    runner = _runner(port)

    result = await runner.run_once()

    assert result == UnknownResolutionRunResult(1, 1, 0, 1, 0, 0)
    assert port.calls == ["list:3", "claim", "apply:False"]


async def test_scheduled_unknown_resolution_propagates_authorization_to_every_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _candidate()
    port = StubUnknownPort((candidate,))
    authorization = cast(SchedulerInvocationEffectAuthorization, object())
    gate = FakeSchedulerAuthorizationGate(authorization)
    monkeypatch.setattr(
        unknown_module,
        "require_scheduler_invocation_effect_authorization",
        gate,
    )

    result = await _runner(port).run_scheduled(authorization)

    assert result == UnknownResolutionRunResult(1, 1, 0, 1, 0, 0)
    assert gate.calls == 4
    assert port.scheduler_authorizations == [
        authorization,
        authorization,
        authorization,
    ]


async def test_scheduled_unknown_resolution_rejects_missing_authorization() -> None:
    port = StubUnknownPort((_candidate(),))

    with pytest.raises(SchedulerInvocationPermitRevoked, match="permit_not_issued"):
        await _runner(port).run_scheduled(
            cast(SchedulerInvocationEffectAuthorization, None)
        )

    assert port.calls == []
    assert port.scheduler_authorizations == []


async def test_scheduled_unknown_resolution_revocation_blocks_ambiguous_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = StubUnknownPort((_candidate(),), ambiguous_apply_count=1)
    authorization = cast(SchedulerInvocationEffectAuthorization, object())
    gate = FakeSchedulerAuthorizationGate(authorization, revoke_at=5)
    monkeypatch.setattr(
        unknown_module,
        "require_scheduler_invocation_effect_authorization",
        gate,
    )

    with pytest.raises(SchedulerInvocationPermitRevoked, match="deadline"):
        await _runner(port).run_scheduled(authorization)

    assert port.calls == ["list:3", "claim", "apply:False"]
    assert port.scheduler_authorizations == [
        authorization,
        authorization,
        authorization,
    ]


async def test_unknown_resolution_resumes_owned_claim_without_reclaiming() -> None:
    candidate = _candidate(
        work_state="claimed",
        claim_token=str(uuid4()),
        claim_expires_at=NOW + timedelta(seconds=20),
    )
    port = StubUnknownPort((candidate,))

    result = await _runner(port).run_once()

    assert result == UnknownResolutionRunResult(1, 0, 1, 1, 0, 0)
    assert port.calls == ["list:3", "apply:False"]


async def test_unknown_resolution_checks_exact_post_state_once_after_lost_response() -> None:
    candidate = _candidate()
    port = StubUnknownPort((candidate,), ambiguous_apply_count=1)

    result = await _runner(port).run_once()

    assert result == UnknownResolutionRunResult(1, 1, 0, 1, 1, 0)
    assert port.calls == ["list:3", "claim", "apply:False", "apply:True"]


async def test_unknown_resolution_does_not_retry_after_second_ambiguous_response() -> None:
    candidate = _candidate()
    port = StubUnknownPort((candidate,), ambiguous_apply_count=2)

    result = await _runner(port).run_once()

    assert result == UnknownResolutionRunResult(1, 1, 0, 0, 0, 1)
    assert port.calls == ["list:3", "claim", "apply:False", "apply:True"]


async def test_unknown_resolution_rejects_stale_fence_without_claim_or_apply() -> None:
    candidate = _candidate(fencing_token=8)
    port = StubUnknownPort((candidate,))

    result = await _runner(port).run_once()

    assert result == UnknownResolutionRunResult(1, 0, 0, 0, 0, 1)
    assert port.calls == ["list:3"]


async def test_unknown_resolution_rejects_claim_identity_mismatch() -> None:
    port = StubUnknownPort((_candidate(),), mismatched_claim=True)

    result = await _runner(port).run_once()

    assert result == UnknownResolutionRunResult(1, 0, 0, 0, 0, 1)
    assert port.calls == ["list:3", "claim"]


async def test_unknown_resolution_rejects_receipt_claim_token_mismatch() -> None:
    port = StubUnknownPort((_candidate(),), mismatched_receipt=True)

    result = await _runner(port).run_once()

    assert result == UnknownResolutionRunResult(1, 1, 0, 0, 0, 1)
    assert port.calls == ["list:3", "claim", "apply:False"]


async def test_unknown_resolution_rejects_response_over_the_fixed_run_bound() -> None:
    port = StubUnknownPort(tuple(_candidate() for _ in range(4)))

    with pytest.raises(ExecutionInvariantError, match="list_exceeds_run_bound"):
        await _runner(port).run_once()


async def test_reconciliation_stage_applies_unknown_before_generic_scan() -> None:
    calls: list[str] = []
    stage = RunExecutionReconciliationStageV2(
        StubUnknownRunner(calls, UnknownResolutionRunResult(1, 1, 0, 1, 0, 0)),
        StubGenericRunner(calls, ExecutionReconciliationRunResult(2, 1, 1, 0)),
    )

    result = await stage.run_once()

    assert calls == ["unknown", "generic"]
    assert result == ExecutionReconciliationRunResult(
        claimed=3,
        completed=2,
        rescheduled=1,
        manual=0,
        failed=0,
        unknown_listed=1,
        unknown_claimed=1,
        unknown_resumed=0,
        unknown_applied=1,
        unknown_replayed=0,
        unknown_failed=0,
    )


async def test_reconciliation_stage_attempts_generic_after_unknown_boundary_failure() -> None:
    calls: list[str] = []
    stage = RunExecutionReconciliationStageV2(
        FailingUnknownRunner(calls),
        StubGenericRunner(calls, ExecutionReconciliationRunResult(0, 0, 0, 0)),
    )

    with pytest.raises(ExecutionInvariantError, match="unknown:RuntimeError"):
        await stage.run_once()

    assert calls == ["unknown", "generic"]


async def test_scheduled_reconciliation_stage_rejects_missing_authorization() -> None:
    calls: list[str] = []
    stage = RunExecutionReconciliationStageV2(
        StubUnknownRunner(calls, UnknownResolutionRunResult(0, 0, 0, 0, 0, 0)),
        StubGenericRunner(calls, ExecutionReconciliationRunResult(0, 0, 0, 0)),
    )

    with pytest.raises(SchedulerInvocationPermitRevoked, match="permit_not_issued"):
        await stage.run_scheduled(
            cast(SchedulerInvocationEffectAuthorization, None)
        )

    assert calls == []


def _runner(port: StubUnknownPort) -> ApplyUnknownExecutionResolutionsV2:
    lease = WorkerLease(
        account_id="paper-primary",
        holder_id=HOLDER_ID,
        fencing_token=7,
        acquired_at=NOW - timedelta(seconds=1),
        expires_at=NOW + timedelta(minutes=1),
    )
    return ApplyUnknownExecutionResolutionsV2(
        port,
        account_id="paper-primary",
        environment="paper",
        holder_id=HOLDER_ID,
        release_sha=RELEASE_SHA,
        lease_provider=lambda: lease,
        clock=lambda: NOW,
    )


def _candidate(
    *,
    work_state: Literal["approved", "claimed"] = "approved",
    claim_token: str | None = None,
    claim_expires_at: datetime | None = None,
    fencing_token: int = 7,
) -> UnknownResolutionCandidate:
    command_id = str(uuid4())
    return UnknownResolutionCandidate(
        command_id=command_id,
        request_id=command_id,
        review_id=str(uuid4()),
        break_id=str(uuid4()),
        intent_id=str(uuid4()),
        terminal_status="filled",
        request_payload_sha256="b" * 64,
        review_payload_sha256="c" * 64,
        command_revision=2,
        work_revision=0,
        work_state=work_state,
        claim_token=claim_token,
        claim_expires_at=claim_expires_at,
        expected_control_epoch=4,
        account_id="paper-primary",
        environment="paper",
        holder_id=HOLDER_ID,
        release_sha=RELEASE_SHA,
        lease_fencing_token=fencing_token,
    )


class StubUnknownPort:
    def __init__(
        self,
        candidates: tuple[UnknownResolutionCandidate, ...],
        *,
        ambiguous_apply_count: int = 0,
        mismatched_claim: bool = False,
        mismatched_receipt: bool = False,
    ) -> None:
        self.candidates = candidates
        self.ambiguous_apply_count = ambiguous_apply_count
        self.mismatched_claim = mismatched_claim
        self.mismatched_receipt = mismatched_receipt
        self.calls: list[str] = []
        self.scheduler_authorizations: list[
            SchedulerInvocationEffectAuthorization | None
        ] = []

    async def list_unknown_resolution_candidates(
        self,
        *,
        account_id: str,
        environment: Literal["paper", "contract_test"],
        holder_id: str,
        release_sha: str,
        fencing_token: int,
        now: datetime,
        limit: int,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> tuple[UnknownResolutionCandidate, ...]:
        del account_id, environment, holder_id, release_sha, fencing_token, now
        self.calls.append(f"list:{limit}")
        self.scheduler_authorizations.append(scheduler_authorization)
        return self.candidates

    async def claim_unknown_resolution(
        self,
        candidate: UnknownResolutionCandidate,
        *,
        now: datetime,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> UnknownResolutionClaim:
        self.calls.append("claim")
        self.scheduler_authorizations.append(scheduler_authorization)
        claim = UnknownResolutionClaim(
            command_id=candidate.command_id,
            request_id=candidate.request_id,
            review_id=candidate.review_id,
            break_id=candidate.break_id,
            intent_id=candidate.intent_id,
            terminal_status=candidate.terminal_status,
            request_payload_sha256=candidate.request_payload_sha256,
            review_payload_sha256=candidate.review_payload_sha256,
            command_revision=candidate.command_revision + 1,
            work_revision=candidate.work_revision + 1,
            claim_token=str(uuid4()),
            claim_expires_at=now + timedelta(seconds=30),
            expected_control_epoch=candidate.expected_control_epoch,
            account_id=candidate.account_id,
            environment=candidate.environment,
            holder_id=candidate.holder_id,
            release_sha=candidate.release_sha,
            lease_fencing_token=candidate.lease_fencing_token,
        )
        if self.mismatched_claim:
            other_command_id = str(uuid4())
            return replace(
                claim,
                command_id=other_command_id,
                request_id=other_command_id,
            )
        return claim

    async def apply_unknown_resolution(
        self,
        claim: UnknownResolutionClaim,
        *,
        now: datetime,
        replay: bool,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> UnknownResolutionApplicationReceipt:
        del now
        self.calls.append(f"apply:{replay}")
        self.scheduler_authorizations.append(scheduler_authorization)
        if self.ambiguous_apply_count:
            self.ambiguous_apply_count -= 1
            raise UnknownResolutionApplyAmbiguousError
        receipt = _receipt(claim, inserted=not replay)
        if self.mismatched_receipt:
            return replace(receipt, claim_token=str(uuid4()))
        return receipt


def _receipt(
    claim: UnknownResolutionClaim,
    *,
    inserted: bool,
) -> UnknownResolutionApplicationReceipt:
    return UnknownResolutionApplicationReceipt(
        schema_version=2,
        command_id=claim.command_id,
        break_id=claim.break_id,
        intent_id=claim.intent_id,
        state="applied",
        receipt_revision=claim.command_revision + 1,
        break_revision=5,
        request_digest_sha256=claim.request_payload_sha256,
        review_digest_sha256=claim.review_payload_sha256,
        terminal_status=claim.terminal_status,
        claim_token=claim.claim_token,
        work_revision=claim.work_revision + 1,
        application_id=str(uuid4()),
        application_sha256="d" * 64,
        accounting_mutation_allowed=True,
        resolution_complete=True,
        inserted=inserted,
        account_id=claim.account_id,
        environment=claim.environment,
        holder_id=claim.holder_id,
        release_sha=claim.release_sha,
        lease_fencing_token=claim.lease_fencing_token,
        control_epoch=claim.expected_control_epoch,
    )


class StubUnknownRunner:
    def __init__(self, calls: list[str], result: UnknownResolutionRunResult) -> None:
        self.calls = calls
        self.result = result

    async def run_once(self) -> UnknownResolutionRunResult:
        self.calls.append("unknown")
        return self.result

    async def run_scheduled(
        self,
        authorization: SchedulerInvocationEffectAuthorization,
    ) -> UnknownResolutionRunResult:
        del authorization
        self.calls.append("unknown")
        return self.result


class FailingUnknownRunner(StubUnknownRunner):
    def __init__(self, calls: list[str]) -> None:
        super().__init__(calls, UnknownResolutionRunResult(0, 0, 0, 0, 0, 0))

    async def run_once(self) -> UnknownResolutionRunResult:
        self.calls.append("unknown")
        raise RuntimeError("unknown_boundary_failure")

    async def run_scheduled(
        self,
        authorization: SchedulerInvocationEffectAuthorization,
    ) -> UnknownResolutionRunResult:
        del authorization
        self.calls.append("unknown")
        raise RuntimeError("unknown_boundary_failure")


class StubGenericRunner:
    def __init__(
        self,
        calls: list[str],
        result: ExecutionReconciliationRunResult,
    ) -> None:
        self.calls = calls
        self.result = result

    async def run_once(self) -> ExecutionReconciliationRunResult:
        self.calls.append("generic")
        return self.result

    async def run_scheduled(
        self,
        authorization: SchedulerInvocationEffectAuthorization,
    ) -> ExecutionReconciliationRunResult:
        del authorization
        self.calls.append("generic")
        return self.result


class FakeSchedulerAuthorizationGate:
    def __init__(
        self,
        authorization: SchedulerInvocationEffectAuthorization,
        *,
        revoke_at: int | None = None,
    ) -> None:
        self.authorization = authorization
        self.revoke_at = revoke_at
        self.calls = 0

    def __call__(
        self,
        value: object,
        *,
        expected_job_key: str,
    ) -> SchedulerInvocationEffectAuthorization:
        assert value is self.authorization
        assert expected_job_key == "operations.reconciliation"
        self.calls += 1
        if self.calls == self.revoke_at:
            raise SchedulerInvocationPermitRevoked("deadline")
        return self.authorization
