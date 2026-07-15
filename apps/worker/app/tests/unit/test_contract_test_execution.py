from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.adapters.broker.contract_test_broker import ContractTestBroker
from app.adapters.broker.toss_mock import TossMock
from app.adapters.persistence.execution_kernel_v2 import InMemoryExecutionKernelV2
from app.adapters.persistence.sql_repository import InMemoryRepository
from app.application.ports.broker_port import BrokerOrderRequest
from app.application.services.execution_service import ExecutionService
from app.application.services.risk_service import RiskService
from app.domain.common.errors import (
    KnownFailClosedError,
    ProviderSchemaError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    ProviderUnknownError,
)
from app.domain.execution_v2.models import (
    ExecutionCostSchedule,
    ExecutionGate,
    ExecutionIntent,
    ExecutionInvariantError,
    ExecutionObservation,
    PaperExecutionEvidence,
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
) -> tuple[InMemoryExecutionKernelV2, ExecutionIntent]:
    kernel = InMemoryExecutionKernelV2("contract_test")
    await kernel.configure_account("contract-account", cash_krw=1_000_000)
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
        side="buy",
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
