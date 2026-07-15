from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from app.application.use_cases.reconcile_execution_v2 import (
    ExecutionReconciliationRunResult,
    FailClosedExecutionReconciliationHandler,
    ReconcileExecutionV2,
)
from app.domain.execution_v2.models import ExecutionInvariantError, WorkerLease
from app.domain.execution_v2.reconciliation import (
    ExecutionReconciliationClaim,
    ExecutionReconciliationCompletion,
    ExecutionReconciliationDecision,
    ExpiredPaperIntentResult,
    PreDispatchFailureResult,
)

WORKER_A = "00000000-0000-4000-8000-000000000001"
WORKER_B = "00000000-0000-4000-8000-000000000002"


def test_sell_claim_requires_complete_integer_cost_basis_snapshot() -> None:
    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    with pytest.raises(
        ExecutionInvariantError,
        match="sell_position_cost_basis_is_required",
    ):
        replace(_claim(0, now), side="sell")

    claim = replace(
        _claim(0, now),
        side="sell",
        position_cost_basis_method="moving_weighted_average_v1",
        position_quantity_snapshot=10,
        position_average_cost_krw="8000.0000",
        position_total_cost_krw=80_000,
        position_projection_version=4,
        position_cost_basis_sha256="b" * 64,
    )

    assert claim.position_total_cost_krw == 80_000


def test_claim_rejects_stale_current_lease_or_control_evidence() -> None:
    claim = _claim(0, datetime(2026, 7, 14, 9, 0, tzinfo=UTC))

    with pytest.raises(ExecutionInvariantError, match="current_fencing_token_is_stale"):
        replace(claim, lease_fencing_token=1, reservation_fencing_token=2)
    with pytest.raises(ExecutionInvariantError, match="current_control_epoch_is_stale"):
        replace(claim, control_epoch=1, reservation_control_epoch=2)


async def test_priority_keyset_pages_past_fifty_without_starvation() -> None:
    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    claims = [_claim(index, now) for index in range(60)]
    port = FakeReconciliationPort(claims)
    handler = IdempotentHandler()
    lease = _lease(now)

    result = await ReconcileExecutionV2(
        port,
        handler,
        account_id=lease.account_id,
        worker_id=WORKER_A,
        current_release_sha="a" * 40,
        lease_provider=lambda: lease,
        clock=lambda: now,
    ).run_once()

    assert result == ExecutionReconciliationRunResult(60, 60, 0, 0)
    assert port.claim_cursors == [(None, None), (0, claims[49].intent_id)]
    assert set(port.claim_gates) == {
        (lease.account_id, WORKER_A, "a" * 40, lease.fencing_token)
    }
    assert len(handler.effects) == 60
    assert claims[-1].intent_id in handler.effects
    assert {
        (release_sha, fencing_token)
        for _, release_sha, fencing_token in port.completion_claims
    } == {("a" * 40, 7)}


async def test_reconciliation_claim_requires_current_configured_account_lease() -> None:
    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    port = FakeReconciliationPort([_claim(0, now)])
    expired_lease = _lease(now - timedelta(minutes=1))

    with pytest.raises(
        ExecutionInvariantError,
        match="reconciliation_worker_lease_is_not_current",
    ):
        await ReconcileExecutionV2(
            port,
            IdempotentHandler(),
            account_id=expired_lease.account_id,
            worker_id=WORKER_A,
            current_release_sha="a" * 40,
            lease_provider=lambda: expired_lease,
            clock=lambda: now,
        ).run_once()

    assert port.claim_gates == []


async def test_reconciliation_rejects_cross_account_claim_before_handler() -> None:
    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    lease = _lease(now)
    cross_account = replace(
        _claim(0, now),
        account_id=str(UUID(int=10_002)),
    )
    port = FakeReconciliationPort([cross_account])
    handler = IdempotentHandler()

    with pytest.raises(
        ExecutionInvariantError,
        match="reconciliation_claim_gate_mismatch",
    ):
        await ReconcileExecutionV2(
            port,
            handler,
            account_id=lease.account_id,
            worker_id=WORKER_A,
            current_release_sha="a" * 40,
            lease_provider=lambda: lease,
            clock=lambda: now,
        ).run_once()

    assert handler.calls == []


async def test_poison_handler_is_manualized_without_starving_next_claim() -> None:
    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    first = _claim(0, now)
    second = _claim(1, now)
    port = FakeReconciliationPort([first, second])
    crashing_handler = IdempotentHandler(crash_once=True)

    result = await ReconcileExecutionV2(
        port,
        crashing_handler,
        account_id=str(UUID(int=10_001)),
        worker_id=WORKER_A,
        current_release_sha="a" * 40,
        lease_provider=lambda: _lease(now),
        clock=lambda: now,
    ).run_once()

    assert result == ExecutionReconciliationRunResult(2, 1, 0, 1, 0)
    assert port.completions == [first.intent_id, second.intent_id]
    assert port.completion_reasons[0] == (
        first.intent_id,
        "manual",
        "handler_runtimeerror",
    )
    assert crashing_handler.effects == {second.intent_id}


async def test_completion_crash_replays_idempotent_effect_after_restart() -> None:
    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    first = _claim(0, now)
    second = _claim(1, now)
    port = FakeReconciliationPort([first, second], completion_crash_once=True)
    handler = IdempotentHandler()

    first_result = await ReconcileExecutionV2(
        port,
        handler,
        account_id=str(UUID(int=10_001)),
        worker_id=WORKER_A,
        current_release_sha="a" * 40,
        lease_provider=lambda: _lease(now),
        clock=lambda: now,
    ).run_once()

    assert first_result == ExecutionReconciliationRunResult(2, 1, 0, 0, 1)
    assert port.completions == [second.intent_id]

    restarted_at = now + timedelta(minutes=1)
    result = await ReconcileExecutionV2(
        port,
        handler,
        account_id=str(UUID(int=10_001)),
        worker_id=WORKER_B,
        current_release_sha="a" * 40,
        lease_provider=lambda: _lease(restarted_at, holder_id=WORKER_B),
        clock=lambda: restarted_at,
    ).run_once()

    assert result.completed == 1
    assert handler.calls == [first.intent_id, second.intent_id, first.intent_id]
    assert handler.effects == {first.intent_id, second.intent_id}


async def test_reserve_only_crash_uses_atomic_pre_dispatch_failure_recovery() -> None:
    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    claim = _claim(0, now)
    port = FakeReconciliationPort([claim])

    result = await ReconcileExecutionV2(
        port,
        FailClosedExecutionReconciliationHandler(
            port,
            worker_id=WORKER_A,
            current_release_sha="a" * 40,
        ),
        account_id=str(UUID(int=10_001)),
        worker_id=WORKER_A,
        current_release_sha="a" * 40,
        lease_provider=lambda: _lease(now),
        clock=lambda: now,
    ).run_once()

    assert result == ExecutionReconciliationRunResult(1, 1, 0, 0)
    assert port.completions == []
    assert port.pre_dispatch_calls == [
        {
            "intent_id": claim.intent_id,
            "worker_id": WORKER_A,
            "fencing_token": 7,
            "control_epoch": 3,
            "release_sha": "a" * 40,
            "reason_code": "worker_restart_before_dispatch",
        }
    ]


async def test_old_release_reserve_only_is_taken_over_by_current_release() -> None:
    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    claim = replace(
        _claim(0, now),
        intent_release_sha="b" * 40,
        recovery_disposition="pre_dispatch_release_takeover",
    )
    port = FakeReconciliationPort([claim])

    result = await ReconcileExecutionV2(
        port,
        FailClosedExecutionReconciliationHandler(
            port,
            worker_id=WORKER_A,
            current_release_sha="a" * 40,
        ),
        account_id=str(UUID(int=10_001)),
        worker_id=WORKER_A,
        current_release_sha="a" * 40,
        lease_provider=lambda: _lease(now),
        clock=lambda: now,
    ).run_once()

    assert result == ExecutionReconciliationRunResult(1, 1, 0, 0)
    assert port.pre_dispatch_calls[0]["release_sha"] == "a" * 40
    assert port.pre_dispatch_calls[0]["reason_code"] == (
        "worker_restart_before_dispatch_release_takeover"
    )


async def test_paper_partial_reschedules_until_next_bar_or_expiry() -> None:
    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    claim = _partial_claim(now, expires_at=now + timedelta(minutes=1))
    port = FakeReconciliationPort([claim])

    result = await ReconcileExecutionV2(
        port,
        FailClosedExecutionReconciliationHandler(
            port,
            worker_id=WORKER_A,
            current_release_sha="a" * 40,
        ),
        account_id=str(UUID(int=10_001)),
        worker_id=WORKER_A,
        current_release_sha="a" * 40,
        lease_provider=lambda: _lease(now),
        clock=lambda: now,
    ).run_once()

    assert result == ExecutionReconciliationRunResult(1, 0, 1, 0)
    assert port.expiry_calls == []
    assert port.completion_reasons == [
        (
            claim.intent_id,
            "reschedule",
            "paper_execution_waiting_for_next_eligible_bar",
        )
    ]
    assert port.completion_schedules == [now + timedelta(minutes=1)]


async def test_paper_open_reschedules_to_next_eligible_bar_without_manualizing() -> None:
    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    claim = replace(
        _partial_claim(now, expires_at=now + timedelta(minutes=3)),
        latest_status="open",
        latest_cumulative_quantity=0,
        latest_cumulative_gross_krw=0,
        latest_cumulative_commission_krw=0,
        latest_cumulative_tax_krw=0,
    )
    port = FakeReconciliationPort([claim])

    result = await ReconcileExecutionV2(
        port,
        FailClosedExecutionReconciliationHandler(
            port,
            worker_id=WORKER_A,
            current_release_sha="a" * 40,
        ),
        account_id=str(UUID(int=10_001)),
        worker_id=WORKER_A,
        current_release_sha="a" * 40,
        lease_provider=lambda: _lease(now),
        clock=lambda: now,
    ).run_once()

    assert result == ExecutionReconciliationRunResult(1, 0, 1, 0)
    assert port.expiry_calls == []
    assert port.completion_schedules == [now + timedelta(minutes=1)]


async def test_paper_partial_atomically_expires_remainder_after_window() -> None:
    now = datetime(2026, 7, 14, 9, 1, tzinfo=UTC)
    claim = _partial_claim(now - timedelta(minutes=1), expires_at=now)
    port = FakeReconciliationPort([claim])

    result = await ReconcileExecutionV2(
        port,
        FailClosedExecutionReconciliationHandler(
            port,
            worker_id=WORKER_A,
            current_release_sha="a" * 40,
        ),
        account_id=str(UUID(int=10_001)),
        worker_id=WORKER_A,
        current_release_sha="a" * 40,
        lease_provider=lambda: _lease(now),
        clock=lambda: now,
    ).run_once()

    assert result == ExecutionReconciliationRunResult(1, 1, 0, 0)
    assert port.completions == []
    assert port.expiry_calls == [
        {
            "intent_id": claim.intent_id,
            "worker_id": WORKER_A,
            "fencing_token": 7,
            "control_epoch": 3,
            "release_sha": "a" * 40,
            "reason_code": "paper_day_limit_remainder_expired_after_recovery",
        }
    ]


async def test_old_release_dispatch_is_manualized_without_automatic_replay() -> None:
    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    claim = replace(
        _partial_claim(now, expires_at=now + timedelta(minutes=1)),
        intent_release_sha="b" * 40,
        recovery_disposition="manual_release_takeover",
    )
    port = FakeReconciliationPort([claim])

    result = await ReconcileExecutionV2(
        port,
        FailClosedExecutionReconciliationHandler(
            port,
            worker_id=WORKER_A,
            current_release_sha="a" * 40,
        ),
        account_id=str(UUID(int=10_001)),
        worker_id=WORKER_A,
        current_release_sha="a" * 40,
        lease_provider=lambda: _lease(now),
        clock=lambda: now,
    ).run_once()

    assert result == ExecutionReconciliationRunResult(1, 0, 0, 1)
    assert port.expiry_calls == []
    assert port.completion_reasons == [
        (
            claim.intent_id,
            "manual",
            "paper_dispatch_release_takeover_requires_manual",
        )
    ]


async def test_contract_dispatch_started_claim_remains_manual_without_replay_evidence() -> None:
    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    claim = replace(
        _claim(0, now),
        attempt_id=str(UUID(int=90_000)),
        environment="contract_test",
    )
    port = FakeReconciliationPort([claim])

    result = await ReconcileExecutionV2(
        port,
        FailClosedExecutionReconciliationHandler(
            port,
            worker_id=WORKER_A,
            current_release_sha="a" * 40,
        ),
        account_id=str(UUID(int=10_001)),
        worker_id=WORKER_A,
        current_release_sha="a" * 40,
        lease_provider=lambda: _lease(now),
        clock=lambda: now,
    ).run_once()

    assert result == ExecutionReconciliationRunResult(1, 0, 0, 1)
    assert port.pre_dispatch_calls == []


def _claim(index: int, now: datetime) -> ExecutionReconciliationClaim:
    intent_id = str(UUID(int=index + 1))
    return ExecutionReconciliationClaim(
        intent_id=intent_id,
        attempt_id=None,
        provider_order_id=None,
        latest_observation_id=None,
        latest_sequence=None,
        latest_status=None,
        latest_observed_at=None,
        latest_cumulative_quantity=None,
        latest_cumulative_gross_krw=None,
        latest_cumulative_commission_krw=None,
        latest_cumulative_tax_krw=None,
        observation_history_sha256=None,
        environment="paper",
        account_id=str(UUID(int=10_001)),
        symbol="005930",
        side="buy",
        quantity=1,
        limit_price_krw=10_000,
        semantic_key_sha256=f"{index + 1:064x}",
        decision_id=str(UUID(int=20_000 + index)),
        risk_result_id=str(UUID(int=30_000 + index)),
        execution_policy_version="paper-minute-v1",
        cost_schedule_version="fees-v1",
        risk_policy_sha256="a" * 64,
        provider_contract_version=None,
        provider_openapi_sha256=None,
        position_cost_basis_method=None,
        position_quantity_snapshot=None,
        position_average_cost_krw=None,
        position_total_cost_krw=None,
        position_projection_version=None,
        position_cost_basis_sha256=None,
        lease_fencing_token=7,
        reservation_fencing_token=2,
        control_epoch=3,
        reservation_control_epoch=1,
        intent_release_sha="a" * 40,
        lease_release_sha="a" * 40,
        recovery_disposition="same_release",
        eligible_at=now,
        expires_at=now + timedelta(minutes=5),
        priority=0 if index < 50 else 1,
        next_reconcile_at=now,
        lease_expires_at=now + timedelta(seconds=30),
    )


def _partial_claim(
    now: datetime,
    *,
    expires_at: datetime,
) -> ExecutionReconciliationClaim:
    claim = _claim(0, now)
    return replace(
        claim,
        attempt_id=str(UUID(int=90_000)),
        provider_order_id=f"paper:{claim.intent_id}",
        latest_observation_id=str(UUID(int=91_000)),
        latest_sequence=1,
        latest_status="partial_filled",
        latest_observed_at=now,
        latest_cumulative_quantity=1,
        latest_cumulative_gross_krw=9_000,
        latest_cumulative_commission_krw=2,
        latest_cumulative_tax_krw=0,
        observation_history_sha256="c" * 64,
        expires_at=expires_at,
    )


class IdempotentHandler:
    def __init__(self, *, crash_once: bool = False) -> None:
        self.crash_once = crash_once
        self.calls: list[str] = []
        self.effects: set[str] = set()

    async def reconcile_claim(
        self,
        claim: ExecutionReconciliationClaim,
        *,
        now: datetime,
    ) -> ExecutionReconciliationDecision:
        del now
        self.calls.append(claim.intent_id)
        if self.crash_once:
            self.crash_once = False
            raise RuntimeError("reconciliation_handler_crash")
        self.effects.add(claim.intent_id)
        return ExecutionReconciliationDecision("complete", "reconciled")


class FakeReconciliationPort:
    def __init__(
        self,
        claims: list[ExecutionReconciliationClaim],
        *,
        completion_crash_once: bool = False,
    ) -> None:
        self.pending = {item.intent_id: item for item in claims}
        self.completion_crash_once = completion_crash_once
        self.claim_cursors: list[tuple[int | None, str | None]] = []
        self.claim_gates: list[tuple[str, str, str, int]] = []
        self.completions: list[str] = []
        self.completion_claims: list[tuple[str, str, int]] = []
        self.completion_reasons: list[tuple[str, str, str]] = []
        self.completion_schedules: list[datetime | None] = []
        self.pre_dispatch_calls: list[dict[str, object]] = []
        self.expiry_calls: list[dict[str, object]] = []

    async def claim_execution_reconciliation_batch(
        self,
        *,
        account_id: str,
        worker_id: str,
        release_sha: str,
        fencing_token: int,
        now: datetime,
        limit: int,
        after_priority: int | None,
        after_intent_id: str | None,
        lease_ttl: timedelta,
    ) -> tuple[ExecutionReconciliationClaim, ...]:
        self.claim_gates.append(
            (account_id, worker_id, release_sha, fencing_token)
        )
        self.claim_cursors.append((after_priority, after_intent_id))
        cursor = (
            (after_priority, after_intent_id)
            if after_priority is not None and after_intent_id is not None
            else None
        )
        eligible = sorted(self.pending.values(), key=lambda item: item.cursor)
        if cursor is not None:
            eligible = [item for item in eligible if item.cursor > cursor]
        return tuple(
            replace(item, lease_expires_at=now + lease_ttl)
            for item in eligible[:limit]
        )
    async def complete_execution_reconciliation(
        self,
        *,
        intent_id: str,
        worker_id: str,
        release_sha: str,
        fencing_token: int,
        now: datetime,
        outcome: str,
        next_reconcile_at: datetime | None,
        reason_code: str,
    ) -> ExecutionReconciliationCompletion:
        del worker_id
        if self.completion_crash_once:
            self.completion_crash_once = False
            raise RuntimeError("reconciliation_completion_crash")
        self.completions.append(intent_id)
        self.completion_claims.append((intent_id, release_sha, fencing_token))
        self.completion_reasons.append((intent_id, outcome, reason_code))
        self.completion_schedules.append(next_reconcile_at)
        self.pending.pop(intent_id)
        if outcome == "reschedule":
            assert next_reconcile_at is not None
            return ExecutionReconciliationCompletion(
                intent_id,
                "pending",
                next_reconcile_at,
            )
        return ExecutionReconciliationCompletion(
            intent_id,
            "manual" if outcome == "manual" else "complete",
            None,
        )

    async def fail_reserved_intent_pre_dispatch(
        self,
        *,
        intent_id: str,
        worker_id: str,
        fencing_token: int,
        control_epoch: int,
        release_sha: str,
        now: datetime,
        reason_code: str,
    ) -> PreDispatchFailureResult:
        self.pre_dispatch_calls.append(
            {
                "intent_id": intent_id,
                "worker_id": worker_id,
                "fencing_token": fencing_token,
                "control_epoch": control_epoch,
                "release_sha": release_sha,
                "reason_code": reason_code,
            }
        )
        self.pending.pop(intent_id)
        return PreDispatchFailureResult(
            intent_id=intent_id,
            observation_id=str(UUID(int=80_000)),
            state="complete",
            reason_code=reason_code,
            idempotent=False,
        )

    async def expire_paper_intent_remainder(
        self,
        *,
        intent_id: str,
        worker_id: str,
        fencing_token: int,
        control_epoch: int,
        release_sha: str,
        now: datetime,
        reason_code: str,
    ) -> ExpiredPaperIntentResult:
        del now
        claim = self.pending.pop(intent_id)
        self.expiry_calls.append(
            {
                "intent_id": intent_id,
                "worker_id": worker_id,
                "fencing_token": fencing_token,
                "control_epoch": control_epoch,
                "release_sha": release_sha,
                "reason_code": reason_code,
            }
        )
        return ExpiredPaperIntentResult(
            intent_id=intent_id,
            observation_id=str(UUID(int=80_001)),
            sequence=(claim.latest_sequence or 0) + 1,
            state="complete",
            reason_code=reason_code,
            idempotent=False,
        )


def _lease(now: datetime, *, holder_id: str = WORKER_A) -> WorkerLease:
    return WorkerLease(
        account_id=str(UUID(int=10_001)),
        holder_id=holder_id,
        fencing_token=7,
        acquired_at=now - timedelta(seconds=5),
        expires_at=now + timedelta(seconds=30),
    )
