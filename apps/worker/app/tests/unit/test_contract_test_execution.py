from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.adapters.broker.contract_test_broker import (
    ContractStatusOutcome,
    ContractTestBroker,
)
from app.adapters.broker.toss_mock import TossMock
from app.adapters.persistence.execution_kernel_v2 import InMemoryExecutionKernelV2
from app.adapters.persistence.sql_repository import InMemoryRepository
from app.application.ports.broker_port import BrokerOrderRequest, BrokerOrderResult
from app.application.services.execution_service import ExecutionService
from app.application.services.paper_execution_v2 import (
    build_fill_accounting_transaction,
)
from app.application.services.risk_service import RiskService
from app.domain.common.errors import (
    KnownFailClosedError,
    ProviderSchemaError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    ProviderUnknownError,
)
from app.domain.execution_v2.models import (
    AccountingTransaction,
    ExecutionCostSchedule,
    ExecutionGate,
    ExecutionIntent,
    ExecutionInvariantError,
    ExecutionObservation,
    PaperExecutionEvidence,
    PaperFill,
    PaperPositionCostBasis,
)
from app.domain.trading.entities import BotSettings


async def test_contract_dispatch_uses_local_broker_and_records_observation() -> None:
    now = datetime(2026, 7, 14, 9, 0, 30, tzinfo=UTC)
    kernel, intent = await _ready_contract_kernel(now)
    broker = ContractTestBroker(["filled"])
    service = ExecutionService(
        broker,
        InMemoryRepository(BotSettings()),
        RiskService(),
    )

    result = await service.dispatch_contract_test_order(
        intent,
        kernel,
        cost_schedule=_contract_cost_schedule(intent),
        execution_evidence=_contract_execution_evidence(intent),
        now=intent.eligible_at,
    )
    observations = await kernel.observations_for(intent.id)

    assert result.status == "filled"
    assert broker.place_order_calls == 1
    assert broker.network_enabled is False
    assert broker.production_order_capable is False
    assert len(observations) == 1
    assert observations[0].status == "filled"
    assert observations[0].cumulative_quantity == intent.quantity
    account = await kernel.account_snapshot(intent.account_id)
    assert account.reserved_cash_krw == 0
    assert account.quantity_for(intent.symbol) == intent.quantity
    transactions = await kernel.accounting_transactions_for(intent.id)
    assert len(transactions) == 1
    assert transactions[0].total_debit_krw == transactions[0].total_credit_krw


async def test_exact_terminal_fill_replay_is_noop_and_mismatch_is_rejected() -> None:
    now = datetime(2026, 7, 14, 9, 0, 30, tzinfo=UTC)
    kernel, intent = await _ready_contract_kernel(now)
    service = ExecutionService(
        ContractTestBroker(["filled"]),
        InMemoryRepository(BotSettings()),
        RiskService(),
    )
    await service.dispatch_contract_test_order(
        intent,
        kernel,
        cost_schedule=_contract_cost_schedule(intent),
        execution_evidence=_contract_execution_evidence(intent),
        now=intent.eligible_at,
    )
    observation = (await kernel.observations_for(intent.id))[0]
    transaction = (await kernel.accounting_transactions_for(intent.id))[0]
    snapshot = await kernel.account_snapshot(intent.account_id)

    await kernel.record_execution_observation(
        intent,
        observation,
        accounting_transaction=transaction,
        now=intent.eligible_at,
    )

    assert await kernel.observations_for(intent.id) == (observation,)
    assert await kernel.accounting_transactions_for(intent.id) == (transaction,)
    assert await kernel.account_snapshot(intent.account_id) == snapshot

    reordered_transaction = replace(
        transaction,
        postings=tuple(reversed(transaction.postings)),
    )
    with pytest.raises(
        ExecutionInvariantError,
        match="contract_observation_replay_conflict",
    ):
        await kernel.record_execution_observation(
            intent,
            observation,
            accounting_transaction=reordered_transaction,
            now=intent.eligible_at,
        )

    assert await kernel.observations_for(intent.id) == (observation,)
    assert await kernel.accounting_transactions_for(intent.id) == (transaction,)
    assert await kernel.account_snapshot(intent.account_id) == snapshot


async def test_provider_execution_identity_cannot_cross_fill_boundaries() -> None:
    now = datetime(2026, 7, 14, 9, 0, 30, tzinfo=UTC)
    kernel, intent = await _ready_contract_kernel(now)
    await kernel.mark_dispatch_started(intent, now=intent.eligible_at)
    settlement_date = intent.eligible_at.date() + timedelta(days=2)
    shared_execution_id = "provider-shared-execution"
    first = ExecutionObservation.create(
        intent_id=intent.id,
        sequence=1,
        status="partial_filled",
        observed_at=intent.eligible_at,
        provider_order_id="provider-order-a",
        provider_execution_id=shared_execution_id,
        cumulative_quantity=1,
        cumulative_gross_krw=intent.limit_price_krw,
        cumulative_commission_krw=0,
        cumulative_tax_krw=0,
        last_fill_quantity=1,
        last_fill_price_krw=intent.limit_price_krw,
        last_fill_settlement_date=settlement_date,
    )
    first_transaction = build_fill_accounting_transaction(
        intent,
        PaperFill(
            sequence=1,
            filled_at=first.observed_at,
            quantity=1,
            price_krw=intent.limit_price_krw,
            commission_krw=0,
            tax_krw=0,
            settlement_date=settlement_date,
        ),
        first.sequence,
    )
    await kernel.record_execution_observation(
        intent,
        first,
        accounting_transaction=first_transaction,
        now=first.observed_at,
    )

    second = ExecutionObservation.create(
        intent_id=intent.id,
        sequence=2,
        status="filled",
        observed_at=intent.eligible_at + timedelta(seconds=1),
        provider_order_id="provider-order-a",
        provider_execution_id=shared_execution_id,
        cumulative_quantity=2,
        cumulative_gross_krw=2 * intent.limit_price_krw,
        cumulative_commission_krw=0,
        cumulative_tax_krw=0,
        last_fill_quantity=1,
        last_fill_price_krw=intent.limit_price_krw,
        last_fill_settlement_date=settlement_date,
    )
    second_transaction = build_fill_accounting_transaction(
        intent,
        PaperFill(
            sequence=2,
            filled_at=second.observed_at,
            quantity=1,
            price_krw=intent.limit_price_krw,
            commission_krw=0,
            tax_krw=0,
            settlement_date=settlement_date,
        ),
        second.sequence,
    )
    with pytest.raises(
        ExecutionInvariantError,
        match="provider_execution_identity_reused",
    ):
        await kernel.record_execution_observation(
            intent,
            second,
            accounting_transaction=second_transaction,
            now=second.observed_at,
        )

    second_intent = ExecutionIntent.create(
        account_id=intent.account_id,
        environment="contract_test",
        decision_id=str(uuid4()),
        risk_result_id=str(uuid4()),
        decision_feature_sha256="d" * 64,
        risk_allowed=True,
        risk_reason_codes=(),
        risk_evaluated_at=now,
        risk_expires_at=now + timedelta(hours=1),
        strategy_version_id="strategy-v2",
        symbol=intent.symbol,
        side="buy",
        quantity=1,
        limit_price_krw=intent.limit_price_krw,
        decision_at=now,
        signal_valid_from=now - timedelta(minutes=1),
        signal_valid_until=now + timedelta(hours=7),
        execution_policy_version=intent.execution_policy_version,
        cost_schedule=_contract_cost_schedule(intent),
        expires_at=intent.expires_at,
        gate_epoch=intent.gate_epoch,
        lease_holder_id=intent.lease_holder_id,
        lease_fencing_token=intent.lease_fencing_token,
    )
    assert await kernel.reserve_intent(second_intent, now=now)
    await kernel.mark_dispatch_started(second_intent, now=second_intent.eligible_at)
    cross_intent = ExecutionObservation.create(
        intent_id=second_intent.id,
        sequence=1,
        status="filled",
        observed_at=second_intent.eligible_at + timedelta(seconds=2),
        provider_order_id="provider-order-b",
        provider_execution_id=shared_execution_id,
        cumulative_quantity=1,
        cumulative_gross_krw=second_intent.limit_price_krw,
        cumulative_commission_krw=0,
        cumulative_tax_krw=0,
        last_fill_quantity=1,
        last_fill_price_krw=second_intent.limit_price_krw,
        last_fill_settlement_date=settlement_date,
    )
    cross_transaction = build_fill_accounting_transaction(
        second_intent,
        PaperFill(
            sequence=1,
            filled_at=cross_intent.observed_at,
            quantity=1,
            price_krw=second_intent.limit_price_krw,
            commission_krw=0,
            tax_krw=0,
            settlement_date=settlement_date,
        ),
        cross_intent.sequence,
    )
    with pytest.raises(
        ExecutionInvariantError,
        match="provider_execution_identity_reused",
    ):
        await kernel.record_execution_observation(
            second_intent,
            cross_intent,
            accounting_transaction=cross_transaction,
            now=cross_intent.observed_at,
        )

    assert await kernel.observations_for(intent.id) == (first,)
    assert await kernel.observations_for(second_intent.id) == ()
    assert await kernel.accounting_transactions_for(intent.id) == (
        first_transaction,
    )
    assert await kernel.accounting_transactions_for(second_intent.id) == ()


async def test_historical_sell_partial_replay_uses_persisted_transaction() -> None:
    now = datetime(2026, 7, 14, 9, 0, 30, tzinfo=UTC)
    kernel, intent = await _ready_contract_kernel(now, side="sell")
    await kernel.mark_dispatch_started(intent, now=intent.eligible_at)
    settlement_date = intent.eligible_at.date() + timedelta(days=2)

    observations: list[ExecutionObservation] = []
    transactions: list[AccountingTransaction] = []
    for sequence, status in ((1, "partial_filled"), (2, "filled")):
        observed_at = intent.eligible_at + timedelta(seconds=sequence - 1)
        observation = ExecutionObservation.create(
            intent_id=intent.id,
            sequence=sequence,
            status=status,  # type: ignore[arg-type]
            observed_at=observed_at,
            provider_order_id="provider-sell-order",
            provider_execution_id=f"provider-sell-fill-{sequence}",
            cumulative_quantity=sequence,
            cumulative_gross_krw=sequence * intent.limit_price_krw,
            cumulative_commission_krw=0,
            cumulative_tax_krw=0,
            last_fill_quantity=1,
            last_fill_price_krw=intent.limit_price_krw,
            last_fill_settlement_date=settlement_date,
        )
        transaction = build_fill_accounting_transaction(
            intent,
            PaperFill(
                sequence=sequence,
                filled_at=observed_at,
                quantity=1,
                price_krw=intent.limit_price_krw,
                commission_krw=0,
                tax_krw=0,
                settlement_date=settlement_date,
                position_cost_relief_krw=8_000,
                realized_pnl_krw=2_000,
            ),
            sequence,
        )
        await kernel.record_execution_observation(
            intent,
            observation,
            accounting_transaction=transaction,
            now=observed_at,
        )
        observations.append(observation)
        transactions.append(transaction)

    terminal_snapshot = await kernel.account_snapshot(intent.account_id)
    await kernel.record_execution_observation(
        intent,
        observations[0],
        accounting_transaction=transactions[0],
        now=observations[0].observed_at,
    )

    assert await kernel.observations_for(intent.id) == tuple(observations)
    assert await kernel.accounting_transactions_for(intent.id) == tuple(transactions)
    assert await kernel.account_snapshot(intent.account_id) == terminal_snapshot


async def test_contract_dispatch_does_not_invent_fill_without_provider_identity() -> None:
    now = datetime(2026, 7, 14, 9, 0, 30, tzinfo=UTC)
    kernel, intent = await _ready_contract_kernel(now)
    service = ExecutionService(
        _MissingIdentityFilledBroker(),
        InMemoryRepository(BotSettings()),
        RiskService(),
    )

    with pytest.raises(ProviderSchemaError, match="create_response_identity_invalid"):
        await service.dispatch_contract_test_order(
            intent,
            kernel,
            cost_schedule=_contract_cost_schedule(intent),
            execution_evidence=_contract_execution_evidence(intent),
            now=intent.eligible_at,
        )

    observations = await kernel.observations_for(intent.id)
    assert [item.status for item in observations] == ["unknown_requires_manual_check"]
    assert observations[0].cumulative_quantity == 0
    snapshot = await kernel.account_snapshot(intent.account_id)
    assert snapshot.quantity_for(intent.symbol) == 0
    assert snapshot.reserved_cash_krw == intent.cash_commitment_krw
    assert await kernel.accounting_transactions_for(intent.id) == ()


async def test_contract_validation_failure_preserves_provider_order_identity() -> None:
    now = datetime(2026, 7, 14, 9, 0, 30, tzinfo=UTC)
    kernel, intent = await _ready_contract_kernel(now)
    service = ExecutionService(
        _MalformedFilledBrokerWithIdentity(),
        InMemoryRepository(BotSettings()),
        RiskService(),
    )

    with pytest.raises(ProviderSchemaError, match="create_response_fields_invalid"):
        await service.dispatch_contract_test_order(
            intent,
            kernel,
            cost_schedule=_contract_cost_schedule(intent),
            execution_evidence=_contract_execution_evidence(intent),
            now=intent.eligible_at,
        )

    observations = await kernel.observations_for(intent.id)
    assert len(observations) == 1
    assert observations[0].status == "unknown_requires_manual_check"
    assert observations[0].provider_order_id == "provider-order-preserved"
    assert await kernel.accounting_transactions_for(intent.id) == ()


async def test_contract_kernel_rejects_provider_order_identity_change() -> None:
    now = datetime(2026, 7, 14, 9, 0, 30, tzinfo=UTC)
    kernel, intent = await _ready_contract_kernel(now)
    await kernel.mark_dispatch_started(intent, now=intent.eligible_at)
    await kernel.record_execution_observation(
        intent,
        ExecutionObservation.create(
            intent_id=intent.id,
            sequence=1,
            status="open",
            observed_at=intent.eligible_at,
            provider_order_id="provider-a",
            provider_execution_id=None,
            cumulative_quantity=0,
            cumulative_gross_krw=0,
            cumulative_commission_krw=0,
            cumulative_tax_krw=0,
        ),
        now=intent.eligible_at,
    )
    filled_at = intent.eligible_at + timedelta(seconds=1)
    changed_identity = ExecutionObservation.create(
        intent_id=intent.id,
        sequence=2,
        status="filled",
        observed_at=filled_at,
        provider_order_id="provider-b",
        provider_execution_id="provider-b-fill-1",
        cumulative_quantity=intent.quantity,
        cumulative_gross_krw=intent.quantity * intent.limit_price_krw,
        cumulative_commission_krw=0,
        cumulative_tax_krw=0,
        last_fill_quantity=intent.quantity,
        last_fill_price_krw=intent.limit_price_krw,
        last_fill_settlement_date=filled_at.date() + timedelta(days=2),
    )

    with pytest.raises(ExecutionInvariantError, match="provider_order_identity_mismatch"):
        await kernel.record_execution_observation(
            intent,
            changed_identity,
            accounting_transaction=None,
            now=filled_at,
        )

    snapshot = await kernel.account_snapshot(intent.account_id)
    assert snapshot.quantity_for(intent.symbol) == 0
    assert snapshot.reserved_cash_krw == intent.cash_commitment_krw


async def test_ambiguous_contract_result_enters_manual_blocking_state() -> None:
    now = datetime(2026, 7, 14, 9, 0, 30, tzinfo=UTC)
    kernel, intent = await _ready_contract_kernel(now)
    broker = ContractTestBroker(["timeout_after_accept"])
    service = ExecutionService(
        broker,
        InMemoryRepository(BotSettings()),
        RiskService(),
    )

    with pytest.raises(ProviderTimeoutError, match="contract_test_timeout_after_accept"):
        await service.dispatch_contract_test_order(
            intent,
            kernel,
            cost_schedule=_contract_cost_schedule(intent),
            execution_evidence=_contract_execution_evidence(intent),
            now=intent.eligible_at,
        )

    observations = await kernel.observations_for(intent.id)
    assert observations[-1].status == "unknown_requires_manual_check"
    assert (await kernel.account_snapshot(intent.account_id)).reserved_cash_krw == (
        intent.cash_commitment_krw
    )
    with pytest.raises(ExecutionInvariantError, match="blocking_state"):
        await kernel.record_execution_observation(
            intent,
            ExecutionObservation.create(
                intent_id=intent.id,
                sequence=2,
                status="open",
                observed_at=now,
                provider_order_id="contract-manual-check",
                provider_execution_id=None,
                cumulative_quantity=0,
                cumulative_gross_krw=0,
                cumulative_commission_krw=0,
                cumulative_tax_krw=0,
            ),
            now=now,
        )


async def test_contract_dispatch_refuses_any_non_contract_broker() -> None:
    now = datetime(2026, 7, 14, 9, 0, 30, tzinfo=UTC)
    kernel, intent = await _ready_contract_kernel(now)
    service = ExecutionService(
        TossMock(),
        InMemoryRepository(BotSettings()),
        RiskService(),
    )

    with pytest.raises(
        KnownFailClosedError,
        match="contract_dispatch_refuses_order_capable_or_network_broker",
    ):
        await service.dispatch_contract_test_order(
            intent,
            kernel,
            cost_schedule=_contract_cost_schedule(intent),
            execution_evidence=_contract_execution_evidence(intent),
            now=intent.eligible_at,
        )

    assert await kernel.observations_for(intent.id) == ()


async def test_contract_sell_fails_before_dispatch_without_verified_cost_basis() -> None:
    now = datetime(2026, 7, 14, 9, 0, 30, tzinfo=UTC)
    kernel, intent = await _ready_contract_kernel(now, side="sell")
    broker = ContractTestBroker(["filled"])
    service = ExecutionService(
        broker,
        InMemoryRepository(BotSettings()),
        RiskService(),
    )

    with pytest.raises(KnownFailClosedError, match="sell_cost_basis_is_invalid"):
        await service.dispatch_contract_test_order(
            intent,
            kernel,
            cost_schedule=_contract_cost_schedule(intent),
            execution_evidence=_contract_execution_evidence(intent),
            now=intent.eligible_at,
        )

    assert broker.place_order_calls == 0
    assert [item.status for item in await kernel.observations_for(intent.id)] == [
        "failed_pre_dispatch"
    ]
    snapshot = await kernel.account_snapshot(intent.account_id)
    assert snapshot.quantity_for(intent.symbol) == 10
    assert snapshot.reserved_quantity_for(intent.symbol) == 0


async def test_contract_sell_records_balanced_journal_with_pinned_cost_basis() -> None:
    now = datetime(2026, 7, 14, 9, 0, 30, tzinfo=UTC)
    kernel, intent = await _ready_contract_kernel(now, side="sell")
    cost_basis = PaperPositionCostBasis(
        symbol=intent.symbol,
        quantity=10,
        total_cost_krw=80_000,
    )
    service = ExecutionService(
        ContractTestBroker(["filled"]),
        InMemoryRepository(BotSettings()),
        RiskService(),
    )

    await service.dispatch_contract_test_order(
        intent,
        kernel,
        cost_schedule=_contract_cost_schedule(intent),
        execution_evidence=_contract_execution_evidence(intent),
        position_cost_basis=cost_basis,
        now=intent.eligible_at,
    )

    snapshot = await kernel.account_snapshot(intent.account_id)
    assert snapshot.quantity_for(intent.symbol) == 8
    assert snapshot.reserved_quantity_for(intent.symbol) == 0
    transactions = await kernel.accounting_transactions_for(intent.id)
    assert len(transactions) == 1
    assert transactions[0].total_debit_krw == transactions[0].total_credit_krw


async def test_contract_sell_rejects_journal_that_disagrees_with_projection() -> None:
    now = datetime(2026, 7, 14, 9, 0, 30, tzinfo=UTC)
    kernel, intent = await _ready_contract_kernel(now, side="sell")
    broker = ContractTestBroker(["filled"])
    service = ExecutionService(
        broker,
        InMemoryRepository(BotSettings()),
        RiskService(),
    )
    mismatched_cost_basis = PaperPositionCostBasis(
        symbol=intent.symbol,
        quantity=10,
        total_cost_krw=90_000,
    )

    with pytest.raises(
        KnownFailClosedError,
        match="contract_dispatch_record_failed_manual_check_required",
    ):
        await service.dispatch_contract_test_order(
            intent,
            kernel,
            cost_schedule=_contract_cost_schedule(intent),
            execution_evidence=_contract_execution_evidence(intent),
            position_cost_basis=mismatched_cost_basis,
            now=intent.eligible_at,
        )

    assert broker.place_order_calls == 1
    snapshot = await kernel.account_snapshot(intent.account_id)
    assert snapshot.cash_krw == 1_000_000
    assert snapshot.quantity_for(intent.symbol) == 10
    assert snapshot.average_cost_for(intent.symbol) == Decimal("8000")
    assert snapshot.reserved_quantity_for(intent.symbol) == intent.quantity
    observations = await kernel.observations_for(intent.id)
    assert [item.status for item in observations] == [
        "unknown_requires_manual_check"
    ]
    assert observations[0].provider_order_id == "contract-00000001"
    assert await kernel.accounting_transactions_for(intent.id) == ()


async def test_contract_record_and_manual_blocking_failure_stays_fail_closed() -> None:
    now = datetime(2026, 7, 14, 9, 0, 30, tzinfo=UTC)
    kernel, intent = await _ready_contract_kernel(now)
    broker = ContractTestBroker(["filled"])
    coordination = _AlwaysFailingRecordCoordination(kernel)
    service = ExecutionService(
        broker,
        InMemoryRepository(BotSettings()),
        RiskService(),
    )

    with pytest.raises(
        KnownFailClosedError,
        match="contract_dispatch_record_and_manual_blocking_failed",
    ):
        await service.dispatch_contract_test_order(
            intent,
            coordination,
            cost_schedule=_contract_cost_schedule(intent),
            execution_evidence=_contract_execution_evidence(intent),
            now=intent.eligible_at,
        )

    assert broker.place_order_calls == 1
    assert coordination.record_calls == 2
    assert await kernel.observations_for(intent.id) == ()
    assert await kernel.accounting_transactions_for(intent.id) == ()

    manual_observation = ExecutionObservation.create(
        intent_id=intent.id,
        sequence=1,
        status="unknown_requires_manual_check",
        observed_at=intent.eligible_at,
        provider_order_id="contract-00000001",
        provider_execution_id=None,
        cumulative_quantity=0,
        cumulative_gross_krw=0,
        cumulative_commission_krw=0,
        cumulative_tax_krw=0,
        reason="manual_reconciliation_after_record_outage",
    )
    await kernel.record_execution_observation(
        intent,
        manual_observation,
        now=intent.eligible_at,
    )
    assert await kernel.observations_for(intent.id) == (manual_observation,)


async def test_shutdown_is_recorded_before_contract_dispatch() -> None:
    now = datetime(2026, 7, 14, 9, 0, 30, tzinfo=UTC)
    kernel, intent = await _ready_contract_kernel(now)
    broker = ContractTestBroker()
    service = ExecutionService(
        broker,
        InMemoryRepository(BotSettings()),
        RiskService(),
        shutdown_requested=lambda: True,
    )

    with pytest.raises(KnownFailClosedError, match="shutdown_requested"):
        await service.dispatch_contract_test_order(
            intent,
            kernel,
            cost_schedule=_contract_cost_schedule(intent),
            execution_evidence=_contract_execution_evidence(intent),
            now=intent.eligible_at,
        )

    assert broker.place_order_calls == 0
    assert (await kernel.observations_for(intent.id))[-1].status == "failed_pre_dispatch"
    assert (await kernel.account_snapshot(intent.account_id)).reserved_cash_krw == 0


async def test_contract_broker_progresses_open_partial_duplicate_to_terminal() -> None:
    broker = ContractTestBroker(
        ["sent"],
        status_outcomes=["open", "partial_filled", "duplicate", "filled"],
    )
    request = _broker_request()

    created = await broker.place_order(request)
    duplicate_create = await broker.place_order(request)
    assert duplicate_create == created
    assert created.provider_order_id is not None

    opened = await broker.get_order_status(created.provider_order_id)
    partial = await broker.get_order_status(created.provider_order_id)
    duplicate_partial = await broker.get_order_status(created.provider_order_id)
    filled = await broker.get_order_status(created.provider_order_id)

    assert opened.status == "sent"
    assert partial.status == "partial_filled"
    assert partial.raw_summary["cumulative_quantity"] == 2
    assert duplicate_partial == partial
    assert filled.status == "filled"
    assert filled.raw_summary["cumulative_quantity"] == request.quantity
    assert broker.place_order_calls == 2
    assert broker.get_order_status_calls == 4


@pytest.mark.parametrize(
    ("outcome", "expected_status", "mismatched_identity"),
    [
        ("rejected", "rejected", False),
        ("mismatched_identity", "sent", True),
    ],
)
async def test_contract_broker_maps_terminal_and_identity_statuses_explicitly(
    outcome: ContractStatusOutcome,
    expected_status: str,
    mismatched_identity: bool,
) -> None:
    broker = ContractTestBroker(["sent"], status_outcomes=[outcome])
    created = await broker.place_order(_broker_request())
    assert created.provider_order_id is not None

    result = await broker.get_order_status(created.provider_order_id)

    assert result.status == expected_status
    expected_order_id = (
        f"mismatch-{created.provider_order_id}"
        if mismatched_identity
        else created.provider_order_id
    )
    assert result.provider_order_id == expected_order_id


async def test_contract_broker_cancel_lifecycle_is_idempotent() -> None:
    broker = ContractTestBroker(
        ["sent"],
        status_outcomes=["partial_filled", "canceled"],
        cancel_outcomes=["canceled", "duplicate"],
    )
    created = await broker.place_order(_broker_request())
    assert created.provider_order_id is not None
    assert (await broker.get_order_status(created.provider_order_id)).status == "partial_filled"

    canceled = await broker.cancel_order(created.provider_order_id)
    duplicate = await broker.cancel_order(created.provider_order_id)

    assert duplicate == canceled
    assert (await broker.get_order_status(created.provider_order_id)).status == "canceled"
    assert canceled.raw_summary == {"contract_cancel_status": "accepted"}


@pytest.mark.parametrize(
    ("outcome", "error_type", "safe_message"),
    [
        ("timeout_before_accept", ProviderTimeoutError, "cancel_timeout_before_accept"),
        ("timeout_after_accept", ProviderTimeoutError, "cancel_timeout_after_accept"),
        ("http_5xx", ProviderUnavailableError, "cancel_http_5xx"),
        ("malformed", ProviderSchemaError, "malformed_cancel_response"),
        ("unknown_after_accept", ProviderUnknownError, "cancel_unknown_after_accept"),
    ],
)
async def test_contract_broker_cancel_fault_scripts_fail_closed(
    outcome: str,
    error_type: type[Exception],
    safe_message: str,
) -> None:
    broker = ContractTestBroker(["sent"], cancel_outcomes=[outcome])  # type: ignore[list-item]
    created = await broker.place_order(_broker_request())
    assert created.provider_order_id is not None

    with pytest.raises(error_type, match=safe_message):
        await broker.cancel_order(created.provider_order_id)


@pytest.mark.parametrize(
    ("outcome", "error_type", "safe_message"),
    [
        ("timeout", ProviderTimeoutError, "status_timeout"),
        ("http_5xx", ProviderUnavailableError, "status_http_5xx"),
        ("malformed", ProviderSchemaError, "malformed_status_response"),
    ],
)
async def test_contract_broker_status_fault_scripts_fail_closed(
    outcome: str,
    error_type: type[Exception],
    safe_message: str,
) -> None:
    broker = ContractTestBroker(["sent"], status_outcomes=[outcome])  # type: ignore[list-item]
    created = await broker.place_order(_broker_request())
    assert created.provider_order_id is not None

    with pytest.raises(error_type, match=safe_message):
        await broker.get_order_status(created.provider_order_id)


async def test_contract_broker_unknown_status_is_safe_summary_only() -> None:
    broker = ContractTestBroker(["sent"], status_outcomes=["unknown"])
    created = await broker.place_order(_broker_request())
    assert created.provider_order_id is not None

    result = await broker.get_order_status(created.provider_order_id)

    assert result.status == "unknown_requires_manual_check"
    assert result.raw_summary == {"contract_status": "unknown"}
    assert "secret" not in str(result.raw_summary).lower()


async def test_partial_fill_then_cancel_consumes_and_releases_reservation() -> None:
    now = datetime(2026, 7, 14, 9, 0, 30, tzinfo=UTC)
    kernel, intent = await _ready_contract_kernel(now)
    await kernel.mark_dispatch_started(intent, now=intent.eligible_at)
    settlement_date = intent.eligible_at.date() + timedelta(days=2)
    partial = ExecutionObservation.create(
        intent_id=intent.id,
        sequence=1,
        status="partial_filled",
        observed_at=intent.eligible_at,
        provider_order_id="contract-00000001",
        provider_execution_id="contract-00000001-execution-1",
        cumulative_quantity=1,
        cumulative_gross_krw=intent.limit_price_krw,
        cumulative_commission_krw=0,
        cumulative_tax_krw=0,
        last_fill_quantity=1,
        last_fill_price_krw=intent.limit_price_krw,
        last_fill_settlement_date=settlement_date,
    )
    await kernel.record_execution_observation(
        intent,
        partial,
        accounting_transaction=build_fill_accounting_transaction(
            intent,
            PaperFill(
                sequence=1,
                filled_at=partial.observed_at,
                quantity=1,
                price_krw=intent.limit_price_krw,
                commission_krw=0,
                tax_krw=0,
                settlement_date=settlement_date,
            ),
            partial.sequence,
        ),
        now=intent.eligible_at,
    )
    canceled = ExecutionObservation.create(
        intent_id=intent.id,
        sequence=2,
        status="canceled",
        observed_at=intent.eligible_at + timedelta(seconds=1),
        provider_order_id="contract-00000001",
        provider_execution_id=None,
        cumulative_quantity=1,
        cumulative_gross_krw=intent.limit_price_krw,
        cumulative_commission_krw=0,
        cumulative_tax_krw=0,
        reason="contract_cancel_confirmed",
    )
    await kernel.record_execution_observation(
        intent,
        canceled,
        now=intent.eligible_at + timedelta(seconds=1),
    )

    snapshot = await kernel.account_snapshot(intent.account_id)
    assert snapshot.quantity_for(intent.symbol) == 1
    assert snapshot.reserved_cash_krw == 0
    assert [item.status for item in await kernel.observations_for(intent.id)] == [
        "partial_filled",
        "canceled",
    ]


@pytest.mark.parametrize("operation", ["create", "status", "cancel"])
async def test_contract_artifact_hash_mismatch_blocks_every_order_operation(
    operation: str,
) -> None:
    broker = ContractTestBroker(contract_artifact_sha256="0" * 64)

    with pytest.raises(ProviderUnavailableError, match="artifact_hash_mismatch"):
        if operation == "create":
            await broker.place_order(_broker_request())
        elif operation == "status":
            await broker.get_order_status("contract-00000001")
        else:
            await broker.cancel_order("contract-00000001")

    assert broker.network_enabled is False


async def _ready_contract_kernel(
    now: datetime,
    *,
    side: str = "buy",
) -> tuple[InMemoryExecutionKernelV2, ExecutionIntent]:
    kernel = InMemoryExecutionKernelV2("contract_test")
    await kernel.configure_account(
        "contract-account",
        cash_krw=1_000_000,
        positions=(
            (
                PaperPositionCostBasis(
                    symbol="005930",
                    quantity=10,
                    total_cost_krw=80_000,
                ),
            )
            if side == "sell"
            else None
        ),
    )
    await kernel.replace_gate(
        ExecutionGate(
            account_id="contract-account",
            environment="contract_test",
            enabled=True,
            control_epoch=1,
            effective_at=now,
            expires_at=now + timedelta(hours=8),
        )
    )
    lease = await kernel.acquire_lease(
        account_id="contract-account",
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
        account_id="contract-account",
        environment="contract_test",
        decision_id=str(uuid4()),
        risk_result_id=str(uuid4()),
        decision_feature_sha256="e" * 64,
        risk_allowed=True,
        risk_reason_codes=(),
        risk_evaluated_at=now,
        risk_expires_at=now + timedelta(hours=1),
        strategy_version_id="strategy-v1",
        symbol="005930",
        side=side,  # type: ignore[arg-type]
        quantity=2,
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
    assert await kernel.reserve_intent(intent, now=now)
    return kernel, intent


def _broker_request() -> BrokerOrderRequest:
    return BrokerOrderRequest(
        symbol="005930",
        side="buy",
        amount_krw=40_000,
        idempotency_key="contract-key-1",
        quantity=4,
        limit_price_krw=10_000,
    )


def _contract_cost_schedule(intent: ExecutionIntent) -> ExecutionCostSchedule:
    return ExecutionCostSchedule(
        version=intent.cost_schedule_version,
        effective_from=intent.decision_at - timedelta(days=1),
        effective_until=intent.expires_at + timedelta(days=1),
        evidence_sha256=intent.cost_schedule_evidence_sha256,
        settlement_days=2,
        settlement_evidence_sha256="f" * 64,
        buy_commission_rate=Decimal("0.00015"),
        sell_commission_rate=Decimal("0.00015"),
        sell_tax_rate=Decimal("0.0018"),
    )


def _contract_execution_evidence(intent: ExecutionIntent) -> PaperExecutionEvidence:
    return PaperExecutionEvidence(
        version="verified-contract-fixture-v1",
        execution_policy_version=intent.execution_policy_version,
        effective_from=intent.eligible_at - timedelta(days=1),
        effective_until=intent.expires_at + timedelta(days=1),
        tick_rule_version="krx-tick-fixture-v1",
        tick_size_krw=1,
        tick_rule_evidence_sha256="b" * 64,
        volume_source="verified_contract_fixture",
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


class _MissingIdentityFilledBroker(ContractTestBroker):
    async def place_order(self, request: BrokerOrderRequest) -> BrokerOrderResult:
        return BrokerOrderResult(
            provider_order_id=None,
            status="filled",
            raw_summary={
                "contract_outcome": "filled",
                "cumulative_quantity": request.quantity,
                "fill_price_krw": request.limit_price_krw,
                "provider_execution_id": "fill-without-order-identity",
            },
        )


class _MalformedFilledBrokerWithIdentity(ContractTestBroker):
    async def place_order(self, request: BrokerOrderRequest) -> BrokerOrderResult:
        return BrokerOrderResult(
            provider_order_id="provider-order-preserved",
            status="filled",
            raw_summary={
                "contract_outcome": "filled",
                "cumulative_quantity": request.quantity,
            },
        )


class _AlwaysFailingRecordCoordination:
    def __init__(self, kernel: InMemoryExecutionKernelV2) -> None:
        self.kernel = kernel
        self.record_calls = 0

    async def mark_dispatch_started(
        self,
        intent: ExecutionIntent,
        *,
        now: datetime,
    ) -> None:
        await self.kernel.mark_dispatch_started(intent, now=now)

    async def record_execution_observation(
        self,
        intent: ExecutionIntent,
        observation: ExecutionObservation,
        *,
        accounting_transaction: AccountingTransaction | None = None,
        now: datetime,
    ) -> None:
        del intent, observation, accounting_transaction, now
        self.record_calls += 1
        raise ExecutionInvariantError("simulated_record_outage")
