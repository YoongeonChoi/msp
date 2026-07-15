from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.adapters.persistence.execution_kernel_v2 import InMemoryExecutionKernelV2
from app.domain.execution_v2.models import (
    ExecutionCostSchedule,
    ExecutionGate,
    ExecutionIntent,
    ExecutionInvariantError,
    MinuteBar,
    PaperExecutionEvidence,
    PaperPositionCostBasis,
    WorkerLease,
)


async def test_semantic_reservation_is_atomic_under_concurrency() -> None:
    now = datetime(2026, 7, 14, 9, 0, 30, tzinfo=UTC)
    kernel, lease = await _ready_kernel(now)
    first = _intent(now, fencing_token=lease.fencing_token)
    second = replace(first, id=str(uuid4()))

    results = await asyncio.gather(
        kernel.reserve_intent(first, now=now),
        kernel.reserve_intent(second, now=now),
    )

    assert sorted(results) == [False, True]
    assert await kernel.intent_for_semantic_key(first.semantic_key) in {first, second}


async def test_paper_execution_updates_cash_position_and_ledger_once() -> None:
    now = datetime(2026, 7, 14, 9, 0, 30, tzinfo=UTC)
    kernel, lease = await _ready_kernel(now)
    intent = _intent(now, fencing_token=lease.fencing_token, quantity=2)
    assert await kernel.reserve_intent(intent, now=now)
    await kernel.mark_dispatch_started(intent, now=intent.eligible_at)
    bars = [_bar(intent.eligible_at, volume=200)]
    schedule = _cost_schedule(intent)

    first = await kernel.execute_paper(
        intent,
        bars,
        cost_schedule=schedule,
        execution_evidence=_execution_evidence(intent),
        now=_execution_now(intent),
    )
    second = await kernel.execute_paper(
        intent,
        bars,
        cost_schedule=schedule,
        execution_evidence=_execution_evidence(intent),
        now=_execution_now(intent),
    )
    account = await kernel.account_snapshot(intent.account_id)
    transactions = await kernel.accounting_transactions_for(intent.id)

    assert first is second
    assert account.quantity_for(intent.symbol) == 2
    expected_spend = sum(
        fill.gross_amount_krw + fill.commission_krw for fill in first.fills
    )
    assert account.cash_krw == 1_000_000 - expected_spend
    assert account.reserved_cash_krw == 0
    assert account.average_cost_for(intent.symbol) == Decimal("9009")
    assert len(transactions) == len(first.fills) == 1


async def test_stale_gate_epoch_and_fencing_token_fail_closed() -> None:
    now = datetime(2026, 7, 14, 9, 0, 30, tzinfo=UTC)
    kernel, lease = await _ready_kernel(now)
    stale_gate_intent = _intent(now, fencing_token=lease.fencing_token)
    await kernel.replace_gate(
        ExecutionGate(
            account_id="paper-account",
            environment="paper",
            enabled=True,
            control_epoch=2,
            effective_at=now,
            expires_at=now + timedelta(hours=8),
        )
    )

    with pytest.raises(ExecutionInvariantError, match="stale_gate_epoch"):
        await kernel.reserve_intent(stale_gate_intent, now=now)

    current = replace(stale_gate_intent, id=str(uuid4()), gate_epoch=2)
    renewed = await kernel.acquire_lease(
        account_id="paper-account",
        holder_id="worker-a",
        now=now,
        ttl=timedelta(hours=8),
    )
    assert renewed.fencing_token > lease.fencing_token
    with pytest.raises(ExecutionInvariantError, match="stale_fencing_token"):
        await kernel.reserve_intent(current, now=now)


async def test_insufficient_cash_leaves_account_and_observations_unchanged() -> None:
    now = datetime(2026, 7, 14, 9, 0, 30, tzinfo=UTC)
    kernel = InMemoryExecutionKernelV2("paper")
    await kernel.configure_account("paper-account", cash_krw=1)
    await kernel.replace_gate(
        ExecutionGate(
            account_id="paper-account",
            environment="paper",
            enabled=True,
            control_epoch=1,
            effective_at=now,
            expires_at=now + timedelta(hours=8),
        )
    )
    lease = await kernel.acquire_lease(
        account_id="paper-account",
        holder_id="worker-a",
        now=now,
        ttl=timedelta(hours=8),
    )
    intent = _intent(now, fencing_token=lease.fencing_token)
    with pytest.raises(ExecutionInvariantError, match="insufficient_available_cash"):
        await kernel.reserve_intent(intent, now=now)

    assert (await kernel.account_snapshot(intent.account_id)).cash_krw == 1
    assert await kernel.observations_for(intent.id) == ()
    assert await kernel.accounting_transactions_for(intent.id) == ()


async def test_concurrent_buy_reservations_cannot_oversubscribe_cash() -> None:
    now = datetime(2026, 7, 14, 9, 0, 30, tzinfo=UTC)
    kernel, lease = await _ready_kernel(now, cash_krw=15_000)
    first = _intent(now, fencing_token=lease.fencing_token, strategy_version="strategy-a")
    second = _intent(now, fencing_token=lease.fencing_token, strategy_version="strategy-b")

    results = await asyncio.gather(
        kernel.reserve_intent(first, now=now),
        kernel.reserve_intent(second, now=now),
        return_exceptions=True,
    )

    assert sum(result is True for result in results) == 1
    assert sum(isinstance(result, ExecutionInvariantError) for result in results) == 1
    snapshot = await kernel.account_snapshot("paper-account")
    assert snapshot.reserved_cash_krw in {
        first.cash_commitment_krw,
        second.cash_commitment_krw,
    }
    assert snapshot.cash_krw == 15_000


async def test_concurrent_sell_reservations_cannot_oversubscribe_position() -> None:
    now = datetime(2026, 7, 14, 9, 0, 30, tzinfo=UTC)
    kernel, lease = await _ready_kernel(
        now,
        positions=(
            PaperPositionCostBasis(
                symbol="005930",
                quantity=5,
                total_cost_krw=40_000,
            ),
        ),
    )
    first = _intent(
        now,
        fencing_token=lease.fencing_token,
        quantity=4,
        side="sell",
        strategy_version="strategy-a",
    )
    second = _intent(
        now,
        fencing_token=lease.fencing_token,
        quantity=4,
        side="sell",
        strategy_version="strategy-b",
    )

    results = await asyncio.gather(
        kernel.reserve_intent(first, now=now),
        kernel.reserve_intent(second, now=now),
        return_exceptions=True,
    )

    assert sum(result is True for result in results) == 1
    assert sum(isinstance(result, ExecutionInvariantError) for result in results) == 1
    snapshot = await kernel.account_snapshot("paper-account")
    assert snapshot.reserved_quantity_for("005930") == 4
    assert snapshot.quantity_for("005930") == 5


async def test_sell_reservations_serialize_until_terminal_release() -> None:
    now = datetime(2026, 7, 14, 9, 0, 30, tzinfo=UTC)
    kernel, lease = await _ready_kernel(
        now,
        positions=(
            PaperPositionCostBasis(
                symbol="005930",
                quantity=3,
                total_cost_krw=100,
            ),
        ),
    )
    first = _intent(
        now,
        fencing_token=lease.fencing_token,
        quantity=1,
        side="sell",
        strategy_version="strategy-a",
        limit_price_krw=9_000,
    )
    second = _intent(
        now,
        fencing_token=lease.fencing_token,
        quantity=1,
        side="sell",
        strategy_version="strategy-b",
        limit_price_krw=9_000,
    )

    results = await asyncio.gather(
        kernel.reserve_intent(first, now=now),
        kernel.reserve_intent(second, now=now),
        return_exceptions=True,
    )

    assert sum(result is True for result in results) == 1
    errors = [result for result in results if isinstance(result, Exception)]
    assert len(errors) == 1
    assert isinstance(errors[0], ExecutionInvariantError)
    assert str(errors[0]) == "paper_account_has_active_sell_reservation"
    winner = first if results[0] is True else second
    blocked = second if winner is first else first
    assert await kernel.reserve_intent(winner, now=now)

    await kernel.mark_dispatch_started(winner, now=winner.eligible_at)
    result = await kernel.execute_paper(
        winner,
        [_bar(winner.eligible_at, volume=100, open_krw=10_000)],
        cost_schedule=_cost_schedule(winner),
        execution_evidence=_execution_evidence(winner),
        now=_execution_now(winner),
    )
    assert result.observations[-1].status == "filled"
    assert result.fills[0].position_cost_relief_krw == 33

    assert await kernel.reserve_intent(blocked, now=_execution_now(winner))
    snapshot = await kernel.account_snapshot("paper-account")
    assert snapshot.quantity_for("005930") == 2
    assert snapshot.reserved_quantity_for("005930") == 1


async def test_partial_fill_consumes_reservation_and_expiry_releases_residual() -> None:
    now = datetime(2026, 7, 14, 9, 0, 30, tzinfo=UTC)
    kernel, lease = await _ready_kernel(now)
    intent = _intent(now, fencing_token=lease.fencing_token, quantity=3)
    assert await kernel.reserve_intent(intent, now=now)
    await kernel.mark_dispatch_started(intent, now=intent.eligible_at)

    result = await kernel.execute_paper(
        intent,
        [_bar(intent.eligible_at, volume=100)],
        cost_schedule=_cost_schedule(intent),
        execution_evidence=_execution_evidence(intent),
        now=_execution_now(intent),
    )

    snapshot = await kernel.account_snapshot(intent.account_id)
    assert [item.status for item in result.observations] == ["partial_filled", "expired"]
    assert snapshot.quantity_for(intent.symbol) == 1
    assert snapshot.reserved_cash_krw == 0


async def test_partial_result_keeps_reservation_until_later_expiry_replay() -> None:
    now = datetime(2026, 7, 14, 9, 0, 30, tzinfo=UTC)
    kernel, lease = await _ready_kernel(now)
    intent = _intent(now, fencing_token=lease.fencing_token, quantity=3)
    assert await kernel.reserve_intent(intent, now=now)
    await kernel.mark_dispatch_started(intent, now=intent.eligible_at)
    bar = _bar(intent.eligible_at, volume=100)

    partial = await kernel.execute_paper(
        intent,
        [bar],
        cost_schedule=_cost_schedule(intent),
        execution_evidence=_execution_evidence(intent),
        now=bar.completed_at,
    )
    partial_snapshot = await kernel.account_snapshot(intent.account_id)

    expired = await kernel.execute_paper(
        intent,
        [bar],
        cost_schedule=_cost_schedule(intent),
        execution_evidence=_execution_evidence(intent),
        now=_execution_now(intent),
    )
    expired_snapshot = await kernel.account_snapshot(intent.account_id)

    assert [item.status for item in partial.observations] == ["partial_filled"]
    assert partial_snapshot.quantity_for(intent.symbol) == 1
    assert partial_snapshot.reserved_cash_krw > 0
    assert [item.status for item in expired.observations] == [
        "partial_filled",
        "expired",
    ]
    assert expired_snapshot.quantity_for(intent.symbol) == 1
    assert expired_snapshot.reserved_cash_krw == 0
    assert len(await kernel.accounting_transactions_for(intent.id)) == 1


async def test_partial_sell_uses_moving_weighted_average_and_releases_residual() -> None:
    now = datetime(2026, 7, 14, 9, 0, 30, tzinfo=UTC)
    kernel, lease = await _ready_kernel(
        now,
        positions=(
            PaperPositionCostBasis(
                symbol="005930",
                quantity=10,
                total_cost_krw=80_000,
            ),
        ),
    )
    intent = _intent(
        now,
        fencing_token=lease.fencing_token,
        quantity=4,
        side="sell",
        limit_price_krw=9_000,
    )
    assert await kernel.reserve_intent(intent, now=now)
    await kernel.mark_dispatch_started(intent, now=intent.eligible_at)

    result = await kernel.execute_paper(
        intent,
        [_bar(intent.eligible_at, volume=200, open_krw=10_000)],
        cost_schedule=_cost_schedule(intent),
        execution_evidence=_execution_evidence(intent),
        now=_execution_now(intent),
    )

    snapshot = await kernel.account_snapshot(intent.account_id)
    assert result.fills[0].position_cost_relief_krw == 16_000
    assert snapshot.quantity_for(intent.symbol) == 8
    assert snapshot.average_cost_for(intent.symbol) == Decimal("8000")
    assert snapshot.reserved_quantity_for(intent.symbol) == 0


async def test_paper_simulator_cannot_run_before_durable_dispatch_marker() -> None:
    now = datetime(2026, 7, 14, 9, 0, 30, tzinfo=UTC)
    kernel, lease = await _ready_kernel(now)
    intent = _intent(now, fencing_token=lease.fencing_token)
    assert await kernel.reserve_intent(intent, now=now)

    with pytest.raises(ExecutionInvariantError, match="requires_dispatch_marker"):
        await kernel.execute_paper(
            intent,
            [_bar(intent.eligible_at, volume=100)],
            cost_schedule=_cost_schedule(intent),
            execution_evidence=_execution_evidence(intent),
            now=_execution_now(intent),
        )

    assert await kernel.observations_for(intent.id) == ()
    assert await kernel.accounting_transactions_for(intent.id) == ()


async def test_moving_weighted_average_v1_matches_accounting_golden_vector() -> None:
    now = datetime(2026, 7, 14, 9, 0, 30, tzinfo=UTC)
    kernel, lease = await _ready_kernel(
        now,
        positions=(
            PaperPositionCostBasis(
                symbol="005930",
                quantity=10,
                total_cost_krw=80_000,
            ),
        ),
    )
    buy = _intent(now, fencing_token=lease.fencing_token, quantity=2)
    assert await kernel.reserve_intent(buy, now=now)
    await kernel.mark_dispatch_started(buy, now=buy.eligible_at)
    buy_result = await kernel.execute_paper(
        buy,
        [_bar(buy.eligible_at, volume=200, open_krw=9_000)],
        cost_schedule=_cost_schedule(buy),
        execution_evidence=_execution_evidence(buy),
        now=_execution_now(buy),
    )
    after_buy = await kernel.account_snapshot(buy.account_id)
    assert buy_result.fills[0].commission_krw == 3
    assert after_buy.quantity_for(buy.symbol) == 12
    bought_position = next(
        item for item in after_buy.positions if item.symbol == buy.symbol
    )
    assert bought_position.total_cost_krw == 98_018

    sell = _intent(
        now + timedelta(minutes=1),
        fencing_token=lease.fencing_token,
        quantity=4,
        side="sell",
        strategy_version="strategy-sell-v1",
        limit_price_krw=9_000,
    )
    assert await kernel.reserve_intent(sell, now=now)
    await kernel.mark_dispatch_started(sell, now=sell.eligible_at)
    sell_result = await kernel.execute_paper(
        sell,
        [_bar(sell.eligible_at, volume=400, open_krw=10_000)],
        cost_schedule=_cost_schedule(sell),
        execution_evidence=_execution_evidence(sell),
        now=_execution_now(sell),
    )
    fill = sell_result.fills[0]
    after_sell = await kernel.account_snapshot(sell.account_id)

    assert (fill.price_krw, fill.gross_amount_krw) == (9_990, 39_960)
    assert (fill.commission_krw, fill.tax_krw) == (6, 72)
    assert fill.position_cost_relief_krw == 32_672
    assert fill.realized_pnl_krw == 7_210
    position = next(item for item in after_sell.positions if item.symbol == sell.symbol)
    assert (position.quantity, position.total_cost_krw) == (8, 65_346)
    assert all(
        transaction.total_debit_krw == transaction.total_credit_krw
        for transaction in (
            *buy_result.accounting_transactions,
            *sell_result.accounting_transactions,
        )
    )


async def _ready_kernel(
    now: datetime,
    *,
    cash_krw: int = 1_000_000,
    positions: tuple[PaperPositionCostBasis, ...] = (),
) -> tuple[InMemoryExecutionKernelV2, WorkerLease]:
    kernel = InMemoryExecutionKernelV2("paper")
    await kernel.configure_account("paper-account", cash_krw=cash_krw, positions=positions)
    await kernel.replace_gate(
        ExecutionGate(
            account_id="paper-account",
            environment="paper",
            enabled=True,
            control_epoch=1,
            effective_at=now,
            expires_at=now + timedelta(hours=8),
        )
    )
    lease = await kernel.acquire_lease(
        account_id="paper-account",
        holder_id="worker-a",
        now=now,
        ttl=timedelta(hours=8),
    )
    return kernel, lease


def _intent(
    now: datetime,
    *,
    fencing_token: int,
    quantity: int = 1,
    side: str = "buy",
    strategy_version: str = "strategy-v1",
    limit_price_krw: int = 10_000,
) -> ExecutionIntent:
    expires_at = now.replace(hour=15, minute=30, second=0)
    schedule = _cost_schedule_for_window(now, expires_at)
    return ExecutionIntent.create(
        account_id="paper-account",
        environment="paper",
        decision_id=str(uuid4()),
        risk_result_id=str(uuid4()),
        decision_feature_sha256="e" * 64,
        risk_allowed=True,
        risk_reason_codes=(),
        risk_evaluated_at=now,
        risk_expires_at=now + timedelta(hours=1),
        strategy_version_id=strategy_version,
        symbol="005930",
        side=side,  # type: ignore[arg-type]
        quantity=quantity,
        limit_price_krw=limit_price_krw,
        decision_at=now,
        signal_valid_from=now - timedelta(minutes=1),
        signal_valid_until=now + timedelta(hours=7),
        execution_policy_version="limit-day-v1",
        cost_schedule=schedule,
        expires_at=expires_at,
        gate_epoch=1,
        lease_holder_id="worker-a",
        lease_fencing_token=fencing_token,
    )


def _bar(minute: datetime, *, volume: int, open_krw: int = 9_000) -> MinuteBar:
    completed_at = minute + timedelta(minutes=1, seconds=1)
    return MinuteBar(
        symbol="005930",
        minute=minute,
        completed_at=completed_at,
        as_of=completed_at,
        source_sha256="c" * 64,
        is_complete=True,
        open_krw=open_krw,
        high_krw=open_krw + 100,
        low_krw=open_krw - 100,
        close_krw=open_krw,
        volume=volume,
    )


def _execution_now(intent: ExecutionIntent) -> datetime:
    return intent.expires_at + timedelta(minutes=2)


def _cost_schedule(intent: ExecutionIntent) -> ExecutionCostSchedule:
    return _cost_schedule_for_window(intent.decision_at, intent.expires_at)


def _cost_schedule_for_window(
    decision_at: datetime,
    expires_at: datetime,
) -> ExecutionCostSchedule:
    return ExecutionCostSchedule(
        version="fees-v1",
        effective_from=decision_at - timedelta(days=1),
        effective_until=expires_at + timedelta(days=1),
        evidence_sha256="a" * 64,
        settlement_days=2,
        settlement_evidence_sha256="f" * 64,
        buy_commission_rate=Decimal("0.00015"),
        sell_commission_rate=Decimal("0.00015"),
        sell_tax_rate=Decimal("0.0018"),
    )


def _execution_evidence(intent: ExecutionIntent) -> PaperExecutionEvidence:
    return PaperExecutionEvidence(
        version="verified-fixture-v1",
        execution_policy_version=intent.execution_policy_version,
        effective_from=intent.eligible_at - timedelta(days=1),
        effective_until=intent.expires_at + timedelta(days=1),
        tick_rule_version="krx-tick-fixture-v1",
        tick_size_krw=1,
        tick_rule_evidence_sha256="b" * 64,
        volume_source="verified_fixture",
        volume_unit="shares",
        volume_evidence_sha256="c" * 64,
        corporate_action_status="not_required",
        corporate_action_evidence_sha256="d" * 64,
        market_calendar_version="krx-calendar-fixture-v1",
        market_calendar_status="open_sessions_verified",
        market_calendar_evidence_sha256="f" * 64,
        open_session_dates=tuple(
            intent.eligible_at.date() + timedelta(days=offset) for offset in range(11)
        ),
    )
