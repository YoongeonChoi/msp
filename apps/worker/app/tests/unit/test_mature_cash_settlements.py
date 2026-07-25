from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import cast

import pytest

import app.application.use_cases.mature_cash_settlements as settlement_module
from app.application.services.scheduler_invocation_deadline import (
    SchedulerInvocationEffectAuthorization,
    SchedulerInvocationPermitRevoked,
)
from app.application.use_cases.mature_cash_settlements import MatureCashSettlements
from app.domain.execution_v2.cash_settlement import (
    CashSettlementClaim,
    CashSettlementCompletionAmbiguousError,
    CashSettlementCompletionRetryableError,
    CashSettlementFailureReceipt,
    CashSettlementReceipt,
)
from app.domain.execution_v2.models import ExecutionInvariantError, WorkerLease

NOW = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)
WORKER_ID = "00000000-0000-4000-8000-000000000001"


async def test_maturity_completes_one_due_claim() -> None:
    port = FakeSettlementPort(claims=(_claim(1),))
    lease = _lease()
    runner = MatureCashSettlements(
        port,
        account_id="paper-primary",
        environment="paper",
        holder_id=WORKER_ID,
        release_sha="a" * 40,
        lease_provider=lambda: lease,
        clock=lambda: NOW,
    )

    result = await runner.run_once()

    assert result.claimed == 1
    assert result.completed == 1
    assert result.failed == 0
    assert result.retried == 0
    assert result.dead_lettered == 0
    assert result.replayed == 0
    assert port.completed == [port.claims[0].obligation_id]
    assert port.failed == []


async def test_scheduled_maturity_propagates_authorization_to_every_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = FakeSettlementPort(claims=(_claim(1),))
    authorization = _authorization()
    gate = FakeSchedulerAuthorizationGate(
        authorization,
        expected_job_key="operations.settlement",
    )
    monkeypatch.setattr(
        settlement_module,
        "require_scheduler_invocation_effect_authorization",
        gate,
    )

    result = await MatureCashSettlements(
        port,
        account_id="paper-primary",
        environment="paper",
        holder_id=WORKER_ID,
        release_sha="a" * 40,
        lease_provider=_lease,
        clock=lambda: NOW,
    ).run_scheduled(authorization)

    assert result.completed == 1
    assert gate.calls == 4
    assert port.scheduler_authorizations == [
        authorization,
        authorization,
        authorization,
    ]


async def test_scheduled_maturity_rejects_missing_authorization_before_claim() -> None:
    port = FakeSettlementPort(claims=(_claim(1),))

    with pytest.raises(SchedulerInvocationPermitRevoked, match="permit_not_issued"):
        await MatureCashSettlements(
            port,
            account_id="paper-primary",
            environment="paper",
            holder_id=WORKER_ID,
            release_sha="a" * 40,
            lease_provider=_lease,
            clock=lambda: NOW,
        ).run_scheduled(cast(SchedulerInvocationEffectAuthorization, None))

    assert not port.claim_called
    assert port.scheduler_authorizations == []


async def test_scheduled_maturity_revocation_before_claim_blocks_first_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = FakeSettlementPort(claims=(_claim(1),))
    authorization = _authorization()
    gate = FakeSchedulerAuthorizationGate(
        authorization,
        expected_job_key="operations.settlement",
        revoke_at=1,
    )
    monkeypatch.setattr(
        settlement_module,
        "require_scheduler_invocation_effect_authorization",
        gate,
    )

    with pytest.raises(SchedulerInvocationPermitRevoked, match="deadline"):
        await MatureCashSettlements(
            port,
            account_id="paper-primary",
            environment="paper",
            holder_id=WORKER_ID,
            release_sha="a" * 40,
            lease_provider=_lease,
            clock=lambda: NOW,
        ).run_scheduled(authorization)

    assert not port.claim_called
    assert port.scheduler_authorizations == []


async def test_scheduled_maturity_mid_run_revocation_blocks_following_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = FakeSettlementPort(claims=(_claim(1),))
    authorization = _authorization()
    gate = FakeSchedulerAuthorizationGate(
        authorization,
        expected_job_key="operations.settlement",
        revoke_at=3,
    )
    monkeypatch.setattr(
        settlement_module,
        "require_scheduler_invocation_effect_authorization",
        gate,
    )

    with pytest.raises(SchedulerInvocationPermitRevoked, match="deadline"):
        await MatureCashSettlements(
            port,
            account_id="paper-primary",
            environment="paper",
            holder_id=WORKER_ID,
            release_sha="a" * 40,
            lease_provider=_lease,
            clock=lambda: NOW,
        ).run_scheduled(authorization)

    assert port.claim_called
    assert port.completed == []
    assert port.failed == []
    assert port.scheduler_authorizations == [authorization]


async def test_maturity_schedules_only_proven_retryable_completion_failure() -> None:
    port = FakeSettlementPort(claims=(_claim(1),), fail_completion=True)
    runner = MatureCashSettlements(
        port,
        account_id="paper-primary",
        environment="paper",
        holder_id=WORKER_ID,
        release_sha="a" * 40,
        lease_provider=_lease,
        clock=lambda: NOW,
    )

    result = await runner.run_once()

    assert result.claimed == 1
    assert result.completed == 0
    assert result.failed == 1
    assert result.retried == 1
    assert port.failed == [port.claims[0].obligation_id]


async def test_maturity_rejects_missing_or_expired_scheduler_lease() -> None:
    port = FakeSettlementPort(claims=())
    runner = MatureCashSettlements(
        port,
        account_id="paper-primary",
        environment="paper",
        holder_id=WORKER_ID,
        release_sha="a" * 40,
        lease_provider=lambda: None,
        clock=lambda: NOW,
    )

    with pytest.raises(ExecutionInvariantError, match="lease_is_missing"):
        await runner.run_once()

    assert not port.claim_called


async def test_maturity_replays_ambiguous_complete_without_failure_transition() -> None:
    claim = _claim(1)
    port = AmbiguousOnceSettlementPort(claims=(claim,))
    runner = MatureCashSettlements(
        port,
        account_id="paper-primary",
        environment="paper",
        holder_id=WORKER_ID,
        release_sha="a" * 40,
        lease_provider=_lease,
        clock=lambda: NOW,
    )

    result = await runner.run_once()

    assert result.completed == 1
    assert result.replayed == 1
    assert result.failed == 0
    assert port.complete_attempts == 2
    assert port.failed == []


async def test_maturity_aborts_after_second_ambiguous_result_without_failure() -> None:
    port = AlwaysAmbiguousSettlementPort(claims=(_claim(1),))
    runner = MatureCashSettlements(
        port,
        account_id="paper-primary",
        environment="paper",
        holder_id=WORKER_ID,
        release_sha="a" * 40,
        lease_provider=_lease,
        clock=lambda: NOW,
    )

    with pytest.raises(CashSettlementCompletionAmbiguousError):
        await runner.run_once()

    assert port.complete_attempts == 2
    assert port.failed == []


@dataclass
class FakeSettlementPort:
    claims: tuple[CashSettlementClaim, ...]
    fail_completion: bool = False
    completed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    claim_called: bool = False
    scheduler_authorizations: list[
        SchedulerInvocationEffectAuthorization | None
    ] = field(default_factory=list)

    async def claim_cash_settlement_batch(
        self, **kwargs: object
    ) -> tuple[CashSettlementClaim, ...]:
        assert kwargs["fencing_token"] == 7
        assert kwargs["limit"] == 1
        self.scheduler_authorizations.append(
            cast(
                SchedulerInvocationEffectAuthorization | None,
                kwargs.get("scheduler_authorization"),
            )
        )
        if self.claim_called:
            return ()
        self.claim_called = True
        return self.claims

    async def complete_cash_settlement(
        self,
        claim: CashSettlementClaim,
        **kwargs: object,
    ) -> CashSettlementReceipt:
        self.scheduler_authorizations.append(
            cast(
                SchedulerInvocationEffectAuthorization | None,
                kwargs.get("scheduler_authorization"),
            )
        )
        if self.fail_completion:
            raise CashSettlementCompletionRetryableError(
                "settlement_projection_conflict"
            )
        self.completed.append(claim.obligation_id)
        return CashSettlementReceipt(
            obligation_id=claim.obligation_id,
            settlement_transaction_id="00000000-0000-4000-8000-000000000099",
            claim_revision=claim.revision,
            settled_revision=claim.revision + 1,
            obligation_type=claim.obligation_type,
            amount_krw=claim.amount_krw,
            settlement_date=claim.settlement_date,
            settled_at=NOW,
            replayed=False,
        )

    async def fail_cash_settlement_attempt(
        self,
        claim: CashSettlementClaim,
        **kwargs: object,
    ) -> CashSettlementFailureReceipt:
        assert kwargs["error_code"] == "settlement_projection_conflict"
        self.scheduler_authorizations.append(
            cast(
                SchedulerInvocationEffectAuthorization | None,
                kwargs.get("scheduler_authorization"),
            )
        )
        self.failed.append(claim.obligation_id)
        return CashSettlementFailureReceipt(
            obligation_id=claim.obligation_id,
            claim_revision=claim.revision,
            revision=claim.revision + 1,
            state="pending",
            attempt_count=1,
            available_at=NOW + timedelta(seconds=5),
            error_code="settlement_projection_conflict",
            replayed=False,
        )


@dataclass
class AmbiguousOnceSettlementPort(FakeSettlementPort):
    complete_attempts: int = 0

    async def complete_cash_settlement(
        self,
        claim: CashSettlementClaim,
        **kwargs: object,
    ) -> CashSettlementReceipt:
        del kwargs
        self.complete_attempts += 1
        if self.complete_attempts == 1:
            raise CashSettlementCompletionAmbiguousError()
        return CashSettlementReceipt(
            obligation_id=claim.obligation_id,
            settlement_transaction_id="00000000-0000-4000-8000-000000000099",
            claim_revision=claim.revision,
            settled_revision=claim.revision + 1,
            obligation_type=claim.obligation_type,
            amount_krw=claim.amount_krw,
            settlement_date=claim.settlement_date,
            settled_at=NOW,
            replayed=True,
        )


@dataclass
class AlwaysAmbiguousSettlementPort(FakeSettlementPort):
    complete_attempts: int = 0

    async def complete_cash_settlement(
        self,
        claim: CashSettlementClaim,
        **kwargs: object,
    ) -> CashSettlementReceipt:
        del claim, kwargs
        self.complete_attempts += 1
        raise CashSettlementCompletionAmbiguousError()


def _claim(sequence: int) -> CashSettlementClaim:
    return CashSettlementClaim(
        obligation_id=f"00000000-0000-4000-8000-{sequence:012d}",
        fill_id=f"00000000-0000-4000-8001-{sequence:012d}",
        intent_id=f"00000000-0000-4000-8002-{sequence:012d}",
        account_id="paper-primary",
        environment="paper",
        obligation_type="cash_payable",
        amount_krw=10_010,
        settlement_date=date(2026, 7, 15),
        obligation_sha256="b" * 64,
        revision=1,
        claim_token=f"00000000-0000-4000-8003-{sequence:012d}",
        claim_expires_at=NOW + timedelta(seconds=30),
    )


def _lease() -> WorkerLease:
    return WorkerLease(
        account_id="paper-primary",
        holder_id=WORKER_ID,
        fencing_token=7,
        acquired_at=NOW - timedelta(seconds=1),
        expires_at=NOW + timedelta(seconds=30),
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
