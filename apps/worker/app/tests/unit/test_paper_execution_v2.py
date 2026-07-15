from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.application.services.paper_execution_v2 import DeterministicPaperExecutionSimulator
from app.domain.execution_v2.models import (
    ExecutionCostSchedule,
    ExecutionIntent,
    ExecutionInvariantError,
    MinuteBar,
    PaperExecutionEvidence,
    PaperPositionCostBasis,
)


def test_paper_limit_day_uses_next_minute_and_one_percent_participation() -> None:
    intent = _intent(quantity=3, limit_price_krw=10_000)
    bars = [
        _bar(intent.decision_at.replace(second=0), volume=10_000, open_krw=9_000),
        _bar(intent.eligible_at, volume=250, open_krw=9_000),
        _bar(intent.eligible_at + timedelta(minutes=1), volume=100, open_krw=9_000),
    ]

    result = DeterministicPaperExecutionSimulator().simulate(
        intent,
        bars,
        cost_schedule=_cost_schedule(intent),
        execution_evidence=_execution_evidence(intent),
        now=_simulation_now(intent),
    )

    assert [fill.quantity for fill in result.fills] == [2, 1]
    assert [fill.price_krw for fill in result.fills] == [9_009, 9_009]
    assert result.observations[-1].status == "filled"
    assert result.observations[-1].cumulative_quantity == 3
    assert all(
        transaction.total_debit_krw == transaction.total_credit_krw
        for transaction in result.accounting_transactions
    )


def test_paper_limit_price_caps_adverse_slippage() -> None:
    intent = _intent(quantity=1, limit_price_krw=10_000)
    bar = MinuteBar(
        symbol=intent.symbol,
        minute=intent.eligible_at,
        completed_at=intent.eligible_at + timedelta(minutes=1, seconds=1),
        as_of=intent.eligible_at + timedelta(minutes=1, seconds=1),
        source_sha256="c" * 64,
        is_complete=True,
        open_krw=10_100,
        high_krw=10_200,
        low_krw=9_990,
        close_krw=10_050,
        volume=100,
    )

    result = DeterministicPaperExecutionSimulator().simulate(
        intent,
        [bar],
        cost_schedule=_cost_schedule(intent),
        execution_evidence=_execution_evidence(intent),
        now=_simulation_now(intent),
    )

    assert result.fills[0].price_krw == intent.limit_price_krw


def test_partial_fill_expires_remaining_day_quantity() -> None:
    intent = _intent(quantity=3, limit_price_krw=10_000)

    result = DeterministicPaperExecutionSimulator().simulate(
        intent,
        [_bar(intent.eligible_at, volume=100, open_krw=9_000)],
        cost_schedule=_cost_schedule(intent),
        execution_evidence=_execution_evidence(intent),
        now=_simulation_now(intent),
    )

    assert len(result.fills) == 1
    assert result.fills[0].quantity == 1
    assert [item.status for item in result.observations] == ["partial_filled", "expired"]
    assert result.observations[-1].cumulative_quantity == 1


def test_partial_fill_remains_pending_before_expiry() -> None:
    intent = _intent(quantity=3, limit_price_krw=10_000)
    bar = _bar(intent.eligible_at, volume=100, open_krw=9_000)

    result = DeterministicPaperExecutionSimulator().simulate(
        intent,
        [bar],
        cost_schedule=_cost_schedule(intent),
        execution_evidence=_execution_evidence(intent),
        now=bar.completed_at,
    )

    assert [fill.quantity for fill in result.fills] == [1]
    assert [item.status for item in result.observations] == ["partial_filled"]
    assert result.observations[-1].cumulative_quantity == 1


def test_existing_other_intent_fill_reduces_bar_capacity() -> None:
    intent = _intent(quantity=2, limit_price_krw=10_000)
    bar = _bar(intent.eligible_at, volume=100, open_krw=9_000)
    bar = replace(bar, other_intent_filled_quantity=1)

    result = DeterministicPaperExecutionSimulator().simulate(
        intent,
        [bar],
        cost_schedule=_cost_schedule(intent),
        execution_evidence=_execution_evidence(intent),
        now=bar.completed_at,
    )

    assert result.fills == ()


@pytest.mark.parametrize(
    ("offset", "expected_statuses"),
    [
        (timedelta(microseconds=-1), []),
        (timedelta(0), ["expired"]),
        (timedelta(microseconds=1), ["expired"]),
    ],
)
def test_unfilled_order_expires_only_at_or_after_expiry(
    offset: timedelta,
    expected_statuses: list[str],
) -> None:
    intent = _intent(quantity=1, limit_price_krw=10_000)

    result = DeterministicPaperExecutionSimulator().simulate(
        intent,
        [],
        cost_schedule=_cost_schedule(intent),
        execution_evidence=_execution_evidence(intent),
        now=intent.expires_at + offset,
    )

    assert [item.status for item in result.observations] == expected_statuses
    if result.observations:
        assert result.observations[-1].observed_at == intent.expires_at


def test_paper_simulator_fails_without_effective_cost_evidence() -> None:
    intent = _intent(quantity=1, limit_price_krw=10_000)
    simulator = DeterministicPaperExecutionSimulator()

    with pytest.raises(ExecutionInvariantError, match="cost_schedule_is_required"):
        simulator.simulate(
            intent,
            [],
            cost_schedule=None,
            execution_evidence=_execution_evidence(intent),
            now=_simulation_now(intent),
        )

    expired_schedule = ExecutionCostSchedule(
        version="expired",
        effective_from=intent.eligible_at - timedelta(days=2),
        effective_until=intent.eligible_at - timedelta(days=1),
        evidence_sha256="b" * 64,
        settlement_days=2,
        settlement_evidence_sha256="f" * 64,
        buy_commission_rate=Decimal(0),
        sell_commission_rate=Decimal(0),
        sell_tax_rate=Decimal(0),
    )
    with pytest.raises(ExecutionInvariantError, match="cost_schedule_is_not_effective"):
        simulator.simulate(
            intent,
            [],
            cost_schedule=expired_schedule,
            execution_evidence=_execution_evidence(intent),
            now=_simulation_now(intent),
        )


def test_paper_simulator_requires_execution_evidence() -> None:
    intent = _intent(quantity=1, limit_price_krw=10_000)

    with pytest.raises(ExecutionInvariantError, match="execution_evidence_is_required"):
        DeterministicPaperExecutionSimulator().simulate(
            intent,
            [],
            cost_schedule=_cost_schedule(intent),
            execution_evidence=None,
            now=_simulation_now(intent),
        )


def test_tick_evidence_is_applied_to_inputs_and_slippage() -> None:
    intent = _intent(quantity=1, limit_price_krw=10_000)
    evidence = _execution_evidence(intent, tick_size_krw=10)
    result = DeterministicPaperExecutionSimulator().simulate(
        intent,
        [_bar(intent.eligible_at, volume=100, open_krw=9_000)],
        cost_schedule=_cost_schedule(intent),
        execution_evidence=evidence,
        now=_simulation_now(intent),
    )

    assert result.fills[0].price_krw == 9_010

    invalid_bar = _bar(intent.eligible_at, volume=100, open_krw=9_001)
    with pytest.raises(ExecutionInvariantError, match="bar_price_violates_tick_rule"):
        DeterministicPaperExecutionSimulator().simulate(
            intent,
            [invalid_bar],
            cost_schedule=_cost_schedule(intent),
            execution_evidence=evidence,
            now=_simulation_now(intent),
        )


def test_incomplete_or_future_bar_fails_closed() -> None:
    intent = _intent(quantity=1, limit_price_krw=10_000)
    complete_bar = _bar(intent.eligible_at, volume=100, open_krw=9_000)

    with pytest.raises(ExecutionInvariantError, match="incomplete_or_from_future"):
        DeterministicPaperExecutionSimulator().simulate(
            intent,
            [complete_bar],
            cost_schedule=_cost_schedule(intent),
            execution_evidence=_execution_evidence(intent),
            now=complete_bar.completed_at - timedelta(microseconds=1),
        )

    incomplete_bar = MinuteBar(
        symbol=intent.symbol,
        minute=intent.eligible_at,
        completed_at=intent.eligible_at + timedelta(seconds=30),
        as_of=intent.eligible_at + timedelta(seconds=30),
        source_sha256="c" * 64,
        is_complete=False,
        open_krw=9_000,
        high_krw=9_100,
        low_krw=8_900,
        close_krw=9_000,
        volume=100,
    )
    with pytest.raises(ExecutionInvariantError, match="incomplete_or_from_future"):
        DeterministicPaperExecutionSimulator().simulate(
            intent,
            [incomplete_bar],
            cost_schedule=_cost_schedule(intent),
            execution_evidence=_execution_evidence(intent),
            now=_simulation_now(intent),
        )


def test_bar_completed_after_intent_expiry_cannot_fill() -> None:
    original = _intent(quantity=1, limit_price_krw=10_000)
    bar = _bar(original.eligible_at, volume=100, open_krw=9_000)
    intent = replace(
        original,
        expires_at=bar.completed_at - timedelta(microseconds=1),
    )

    result = DeterministicPaperExecutionSimulator().simulate(
        intent,
        [bar],
        cost_schedule=_cost_schedule(intent),
        execution_evidence=_execution_evidence(intent),
        now=bar.completed_at,
    )

    assert result.fills == ()
    assert [item.status for item in result.observations] == ["expired"]
    assert result.observations[0].observed_at == intent.expires_at

def test_partial_sell_relief_uses_moving_weighted_average_and_balances() -> None:
    intent = _intent(
        quantity=4,
        limit_price_krw=9_000,
        side="sell",
    )
    result = DeterministicPaperExecutionSimulator().simulate(
        intent,
        [_bar(intent.eligible_at, volume=200, open_krw=10_000)],
        cost_schedule=_cost_schedule(intent),
        execution_evidence=_execution_evidence(intent),
        now=_simulation_now(intent),
        position_cost_basis=PaperPositionCostBasis(
            symbol=intent.symbol,
            quantity=10,
            total_cost_krw=80_000,
        ),
    )

    fill = result.fills[0]
    assert fill.quantity == 2
    assert fill.position_cost_relief_krw == 16_000
    assert fill.realized_pnl_krw == (
        fill.gross_amount_krw
        - fill.position_cost_relief_krw
        - fill.commission_krw
        - fill.tax_krw
    )
    assert result.observations[-1].status == "expired"
    assert result.accounting_transactions[0].total_debit_krw == (
        result.accounting_transactions[0].total_credit_krw
    )
    assert {posting.account for posting in result.accounting_transactions[0].postings} == {
        "CASH",
        "FEES",
        "TAXES",
        "POSITION_COST",
        "REALIZED_PNL",
    }


@given(volume=st.integers(min_value=0, max_value=100_000), quantity=st.integers(1, 1_000))
def test_fill_never_exceeds_participation_or_order_quantity(volume: int, quantity: int) -> None:
    intent = _intent(quantity=quantity, limit_price_krw=10_000)
    result = DeterministicPaperExecutionSimulator().simulate(
        intent,
        [_bar(intent.eligible_at, volume=volume, open_krw=9_000)],
        cost_schedule=_cost_schedule(intent),
        execution_evidence=_execution_evidence(intent),
        now=_simulation_now(intent),
    )
    filled = sum(fill.quantity for fill in result.fills)

    assert filled <= volume // 100
    assert filled <= quantity
    assert all(fill.price_krw <= intent.limit_price_krw for fill in result.fills)
    assert all(
        transaction.total_debit_krw == transaction.total_credit_krw
        for transaction in result.accounting_transactions
    )


def _intent(
    *,
    quantity: int,
    limit_price_krw: int,
    side: str = "buy",
) -> ExecutionIntent:
    decision_at = datetime(2026, 7, 14, 9, 0, 30, tzinfo=UTC)
    expires_at = decision_at.replace(hour=15, minute=30, second=0)
    cost_schedule = _cost_schedule_for_window(decision_at, expires_at)
    return ExecutionIntent.create(
        account_id="paper-account",
        environment="paper",
        decision_id=str(uuid4()),
        risk_result_id=str(uuid4()),
        decision_feature_sha256="e" * 64,
        risk_allowed=True,
        risk_reason_codes=(),
        risk_evaluated_at=decision_at,
        risk_expires_at=decision_at + timedelta(hours=1),
        strategy_version_id="strategy-v1",
        symbol="005930",
        side=side,  # type: ignore[arg-type]
        quantity=quantity,
        limit_price_krw=limit_price_krw,
        decision_at=decision_at,
        signal_valid_from=decision_at - timedelta(minutes=1),
        signal_valid_until=decision_at + timedelta(hours=7),
        execution_policy_version="limit-day-v1",
        cost_schedule=cost_schedule,
        expires_at=expires_at,
        gate_epoch=1,
        lease_holder_id="worker-a",
        lease_fencing_token=1,
    )


def _cost_schedule(intent: ExecutionIntent) -> ExecutionCostSchedule:
    return _cost_schedule_for_window(intent.decision_at, intent.expires_at)


def _cost_schedule_for_window(
    decision_at: datetime,
    expires_at: datetime,
) -> ExecutionCostSchedule:
    return ExecutionCostSchedule(
        version="fees-2026-07",
        effective_from=decision_at - timedelta(days=1),
        effective_until=expires_at + timedelta(days=1),
        evidence_sha256="a" * 64,
        settlement_days=2,
        settlement_evidence_sha256="f" * 64,
        buy_commission_rate=Decimal("0.00015"),
        sell_commission_rate=Decimal("0.00015"),
        sell_tax_rate=Decimal("0.0018"),
    )


def _execution_evidence(
    intent: ExecutionIntent,
    *,
    tick_size_krw: int = 1,
) -> PaperExecutionEvidence:
    return PaperExecutionEvidence(
        version="verified-fixture-v1",
        execution_policy_version=intent.execution_policy_version,
        effective_from=intent.eligible_at - timedelta(days=1),
        effective_until=intent.expires_at + timedelta(days=1),
        tick_rule_version="krx-tick-fixture-v1",
        tick_size_krw=tick_size_krw,
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


def _bar(minute: datetime, *, volume: int, open_krw: int) -> MinuteBar:
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


def _simulation_now(intent: ExecutionIntent) -> datetime:
    return intent.expires_at + timedelta(minutes=2)
