from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from uuid import UUID, uuid4

from app.adapters.broker.contract_test_broker import ContractTestBroker
from app.adapters.persistence.sql_repository import InMemoryRepository
from app.application.ports.broker_port import (
    BrokerCancelOrderResult,
    BrokerOrderRequest,
    BrokerOrderResult,
    BrokerOrderStatusResult,
)
from app.application.services.execution_service import ExecutionService
from app.application.services.risk_service import RiskService
from app.domain.common.time import now_utc
from app.domain.risk.value_objects import RiskInput
from app.domain.trading.entities import AccountState, BotSettings, DecisionSnapshot, Quote, Signal


class RecordingBroker:
    def __init__(self) -> None:
        self.place_order_calls = 0
        self.cancel_order_calls = 0

    async def provider_health(self) -> bool:
        return True

    async def place_order(self, request: BrokerOrderRequest) -> BrokerOrderResult:
        self.place_order_calls += 1
        return BrokerOrderResult(
            provider_order_id="must-not-be-used",
            status="sent",
            raw_summary={"symbol": request.symbol},
        )

    async def get_order_status(self, provider_order_id: str) -> BrokerOrderStatusResult:
        return BrokerOrderStatusResult(
            provider_order_id=provider_order_id,
            status="sent",
            raw_summary={"provider_order_id": provider_order_id},
        )

    async def cancel_order(self, provider_order_id: str) -> BrokerCancelOrderResult:
        self.cancel_order_calls += 1
        return BrokerCancelOrderResult(
            original_provider_order_id=provider_order_id,
            cancel_provider_order_id="must-not-be-used",
            raw_summary={"provider_order_id": provider_order_id},
        )

    async def get_account_state(self, now: datetime) -> AccountState:
        return AccountState(
            synced=True,
            cash_krw=1_000_000,
            equity_krw=10_000_000,
            daily_loss_pct=0.0,
            daily_order_count=0,
            synced_at=now,
        )


async def test_paper_order_persists_shared_whole_share_execution_details() -> None:
    now = now_utc()
    repository = InMemoryRepository(
        BotSettings(enabled=True, mode="paper", live_order_allowed=False)
    )
    broker = RecordingBroker()
    service = ExecutionService(broker, repository, RiskService())
    strategy_version_id = uuid4()
    signal = _signal(reason_json={"score": 0.8})
    decision = DecisionSnapshot.create(
        cycle_id=uuid4(),
        signal=signal,
        strategy_version_id=strategy_version_id,
        created_at=now,
        feature_snapshot={"price_at_decision": 75_000},
        risk_snapshot={"allowed": True},
    )
    risk_input = replace(
        _risk_input(now, signal, strategy_version_id),
        settings=BotSettings(enabled=True, mode="paper", live_order_allowed=False),
        strategy_status="paper",
        strategy_approved=False,
    )
    risk_result = RiskService().evaluate_paper_order(risk_input)

    order = await service.create_paper_order(decision, risk_result, "paper-order-key")

    assert risk_result.allowed is True
    assert order is not None
    assert order.status == "paper"
    assert order.amount_krw == 100_000
    assert order.price_krw == 75_000
    assert order.quantity == 1
    assert repository.orders == [order]
    assert broker.place_order_calls == 0


async def test_paper_order_blocks_when_decision_price_is_not_valid() -> None:
    now = now_utc()
    repository = InMemoryRepository(
        BotSettings(enabled=True, mode="paper", live_order_allowed=False)
    )
    service = ExecutionService(RecordingBroker(), repository, RiskService())
    strategy_version_id = uuid4()
    signal = _signal(reason_json={"score": 0.8})
    decision = DecisionSnapshot.create(
        cycle_id=uuid4(),
        signal=signal,
        strategy_version_id=strategy_version_id,
        created_at=now,
        feature_snapshot={"technical_score": 0.8},
        risk_snapshot={"allowed": True},
    )
    risk_input = replace(
        _risk_input(now, signal, strategy_version_id),
        settings=BotSettings(enabled=True, mode="paper", live_order_allowed=False),
        strategy_status="paper",
        strategy_approved=False,
    )

    order = await service.create_paper_order(
        decision,
        RiskService().evaluate_paper_order(risk_input),
        "paper-order-key",
    )

    assert order is not None
    assert order.status == "blocked"
    assert order.reason == "invalid_paper_execution_price"
    assert order.price_krw is None
    assert order.quantity is None


async def test_legacy_live_order_is_quarantined_before_any_broker_call() -> None:
    now = now_utc()
    repository = InMemoryRepository(BotSettings(enabled=True, mode="live", live_order_allowed=True))
    broker = RecordingBroker()
    service = ExecutionService(broker, repository, RiskService())
    strategy_version_id = uuid4()
    signal = _signal(reason_json={"score": 0.8})
    decision = DecisionSnapshot.create(
        cycle_id=uuid4(),
        signal=signal,
        strategy_version_id=strategy_version_id,
        created_at=now,
        feature_snapshot={"raw": {"feature_source": "verified", "live_trading_ready": True}},
        risk_snapshot={"allowed": True},
    )

    order, risk_result = await service.propose_live_order(
        decision,
        _risk_input(now, signal, strategy_version_id),
    )

    assert risk_result.allowed is True
    assert order.status == "blocked"
    assert order.reason == "legacy_live_order_write_quarantined"
    assert broker.place_order_calls == 0
    assert broker.cancel_order_calls == 0
    assert repository.orders == [order]
    assert repository.engine_events[-1]["message"] == "legacy_live_order_write_quarantined"
    details = repository.engine_events[-1]["details"]
    assert isinstance(details, dict)
    assert details["broker_dispatch_attempted"] is False


async def test_repeated_legacy_live_proposal_preserves_single_quarantine_record() -> None:
    now = now_utc()
    repository = InMemoryRepository(BotSettings(enabled=True, mode="live", live_order_allowed=True))
    broker = RecordingBroker()
    service = ExecutionService(broker, repository, RiskService())
    strategy_version_id = uuid4()
    signal = _signal(reason_json={"score": 0.8})
    decision = DecisionSnapshot.create(
        cycle_id=uuid4(),
        signal=signal,
        strategy_version_id=strategy_version_id,
        created_at=now,
        feature_snapshot={"raw": {"feature_source": "verified", "live_trading_ready": True}},
        risk_snapshot={"allowed": True},
    )
    risk_input = _risk_input(now, signal, strategy_version_id)

    first, _ = await service.propose_live_order(decision, risk_input)
    second, _ = await service.propose_live_order(decision, risk_input)

    assert first.status == second.status == "blocked"
    assert first.reason == second.reason == "legacy_live_order_write_quarantined"
    assert repository.orders == [first]
    assert broker.place_order_calls == 0


async def test_legacy_live_proposal_does_not_use_contract_broker_escape_hatch() -> None:
    now = now_utc()
    repository = InMemoryRepository(BotSettings(enabled=True, mode="live", live_order_allowed=True))
    broker = ContractTestBroker(["filled"])
    service = ExecutionService(broker, repository, RiskService())
    strategy_version_id = uuid4()
    signal = _signal(reason_json={"score": 0.8})
    decision = DecisionSnapshot.create(
        cycle_id=uuid4(),
        signal=signal,
        strategy_version_id=strategy_version_id,
        created_at=now,
        feature_snapshot={"raw": {"feature_source": "verified", "live_trading_ready": True}},
        risk_snapshot={"allowed": True},
    )

    order, _ = await service.propose_live_order(
        decision,
        _risk_input(now, signal, strategy_version_id),
    )

    assert order.status == "blocked"
    assert order.reason == "legacy_live_order_write_quarantined"
    assert broker.place_order_calls == 0


def _signal(*, reason_json: dict[str, object]) -> Signal:
    return Signal(
        symbol="005930",
        action="buy",
        final_score=0.8,
        confidence=0.8,
        order_amount_krw=100_000,
        sector="technology",
        reason_json=reason_json,
    )


def _risk_input(now: datetime, signal: Signal, strategy_version_id: UUID) -> RiskInput:
    return RiskInput(
        settings=BotSettings(enabled=True, mode="live", live_order_allowed=True),
        signal=signal,
        account_state=AccountState(
            synced=True,
            cash_krw=1_000_000,
            equity_krw=10_000_000,
            daily_loss_pct=0.0,
            daily_order_count=0,
            synced_at=now,
        ),
        quote=Quote(symbol="005930", price_krw=75_000, as_of=now),
        now=now,
        provider_health={"supabase": True, "toss": True},
        market_open=True,
        existing_position_pct=0.0,
        sector_position_pct=0.0,
        available_position_quantity=10,
        critical_news_risk=False,
        liquidity_ok=True,
        volatility_ok=True,
        cooldown_active=False,
        duplicate_order=False,
        strategy_version_id=strategy_version_id,
        strategy_status="active",
        strategy_approved=True,
    )
