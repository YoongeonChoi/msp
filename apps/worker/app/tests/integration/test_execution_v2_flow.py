from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from app.adapters.persistence.execution_kernel_v2 import InMemoryExecutionKernelV2
from app.application.use_cases.run_execution_v2 import (
    PaperExecutionV2Command,
    RunExecutionV2,
)
from app.domain.execution_v2.models import (
    ExecutionCostSchedule,
    ExecutionGate,
    ExecutionIntent,
    MinuteBar,
    PaperExecutionEvidence,
)


async def test_paper_intent_flows_through_gate_lease_fill_and_accounting() -> None:
    now = datetime(2026, 7, 14, 9, 0, 30, tzinfo=UTC)
    kernel = InMemoryExecutionKernelV2("paper")
    await kernel.configure_account("paper-account", cash_krw=1_000_000)
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
    expires_at = now.replace(hour=15, minute=30, second=0)
    cost_schedule = ExecutionCostSchedule(
        version="fees-v1",
        effective_from=now - timedelta(days=1),
        effective_until=expires_at + timedelta(days=1),
        evidence_sha256="a" * 64,
        settlement_days=2,
        settlement_evidence_sha256="f" * 64,
        buy_commission_rate=Decimal("0.00015"),
        sell_commission_rate=Decimal("0.00015"),
        sell_tax_rate=Decimal("0.0018"),
    )
    intent = ExecutionIntent.create(
        account_id="paper-account",
        environment="paper",
        decision_id=str(uuid4()),
        risk_result_id=str(uuid4()),
        decision_feature_sha256="e" * 64,
        risk_allowed=True,
        risk_reason_codes=(),
        risk_evaluated_at=now,
        risk_expires_at=now + timedelta(hours=1),
        strategy_version_id="strategy-v1",
        symbol="005930",
        side="buy",
        quantity=3,
        limit_price_krw=10_000,
        decision_at=now,
        signal_valid_from=now - timedelta(minutes=1),
        signal_valid_until=now + timedelta(hours=7),
        execution_policy_version="limit-day-v1",
        cost_schedule=cost_schedule,
        expires_at=expires_at,
        gate_epoch=1,
        lease_holder_id=lease.holder_id,
        lease_fencing_token=lease.fencing_token,
    )
    execution_evidence = PaperExecutionEvidence(
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
    bars = (
        MinuteBar(
            symbol=intent.symbol,
            minute=intent.eligible_at,
            completed_at=intent.eligible_at + timedelta(minutes=1, seconds=1),
            as_of=intent.eligible_at + timedelta(minutes=1, seconds=1),
            source_sha256="c" * 64,
            is_complete=True,
            open_krw=9_000,
            high_krw=9_100,
            low_krw=8_900,
            close_krw=9_000,
            volume=200,
        ),
        MinuteBar(
            symbol=intent.symbol,
            minute=intent.eligible_at + timedelta(minutes=1),
            completed_at=intent.eligible_at + timedelta(minutes=2, seconds=1),
            as_of=intent.eligible_at + timedelta(minutes=2, seconds=1),
            source_sha256="c" * 64,
            is_complete=True,
            open_krw=9_100,
            high_krw=9_200,
            low_krw=9_000,
            close_krw=9_100,
            volume=100,
        ),
    )

    outcome = await RunExecutionV2(kernel).execute_paper(
        PaperExecutionV2Command.create(
            intent=intent,
            bars=bars,
            cost_schedule=cost_schedule,
            execution_evidence=execution_evidence,
            dispatch_at=intent.eligible_at,
            evaluated_at=intent.expires_at + timedelta(minutes=2),
        )
    )
    assert outcome.result is not None
    result = outcome.result
    account = await kernel.account_snapshot(intent.account_id)
    observations = await kernel.observations_for(intent.id)
    transactions = await kernel.accounting_transactions_for(intent.id)

    assert outcome.status == "completed"
    assert result.observations[-1].status == "filled"
    assert [item.status for item in observations] == ["partial_filled", "filled"]
    assert account.quantity_for(intent.symbol) == 3
    assert account.cash_krw < 1_000_000
    assert account.reserved_cash_krw == 0
    assert account.average_cost_for(intent.symbol) is not None
    assert len(transactions) == 2
    assert all(item.total_debit_krw == item.total_credit_krw for item in transactions)
