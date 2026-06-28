from datetime import datetime
from uuid import UUID, uuid4

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

    async def provider_health(self) -> bool:
        return True

    async def place_order(self, request: BrokerOrderRequest) -> BrokerOrderResult:
        self.place_order_calls += 1
        return BrokerOrderResult(
            provider_order_id="recording-broker-order",
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
        return BrokerCancelOrderResult(
            original_provider_order_id=provider_order_id,
            cancel_provider_order_id="unused-cancel-order",
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


class PrePersistAssertingBroker(RecordingBroker):
    def __init__(self, repository: InMemoryRepository) -> None:
        super().__init__()
        self.repository = repository

    async def place_order(self, request: BrokerOrderRequest) -> BrokerOrderResult:
        assert len(self.repository.orders) == 1
        pending = self.repository.orders[0]
        assert pending.status == "unknown_requires_manual_check"
        assert pending.reason == "live_broker_order_result_pending"
        assert pending.idempotency_key == request.idempotency_key
        return await super().place_order(request)


async def test_live_order_missing_decision_evidence_blocks_before_broker() -> None:
    now = now_utc()
    repository = InMemoryRepository(BotSettings(enabled=True, mode="live", live_order_allowed=True))
    broker = RecordingBroker()
    service = ExecutionService(broker, repository, RiskService())
    strategy_version_id = uuid4()
    signal = Signal(
        symbol="005930",
        action="buy",
        final_score=0.8,
        confidence=0.8,
        order_amount_krw=100_000,
        sector="technology",
        reason_json={},
    )
    decision = DecisionSnapshot.create(
        cycle_id=uuid4(),
        signal=signal,
        strategy_version_id=strategy_version_id,
        created_at=now,
        feature_snapshot={},
        risk_snapshot={},
    )

    order, risk_result = await service.propose_live_order(
        decision, _risk_input(now, signal, strategy_version_id)
    )

    assert risk_result.allowed is True
    assert broker.place_order_calls == 0
    assert order.status == "blocked"
    assert order.reason is not None
    assert "missing_reason_json" in order.reason
    assert "missing_feature_snapshot" in order.reason
    assert "missing_risk_snapshot" in order.reason
    assert repository.orders == [order]
    assert repository.engine_events[-1]["message"] == "live_order_blocked_missing_evidence"


async def test_live_order_persists_manual_check_record_before_broker_call() -> None:
    now = now_utc()
    repository = InMemoryRepository(BotSettings(enabled=True, mode="live", live_order_allowed=True))
    broker = PrePersistAssertingBroker(repository)
    service = ExecutionService(broker, repository, RiskService())
    strategy_version_id = uuid4()
    signal = Signal(
        symbol="005930",
        action="buy",
        final_score=0.8,
        confidence=0.8,
        order_amount_krw=100_000,
        sector="technology",
        reason_json={"score": 0.8},
    )
    decision = DecisionSnapshot.create(
        cycle_id=uuid4(),
        signal=signal,
        strategy_version_id=strategy_version_id,
        created_at=now,
        feature_snapshot=_live_ready_feature_snapshot(),
        risk_snapshot={"allowed": True},
    )

    order, risk_result = await service.propose_live_order(
        decision, _risk_input(now, signal, strategy_version_id)
    )

    assert risk_result.allowed is True
    assert broker.place_order_calls == 1
    assert order.status == "sent"
    assert repository.orders == [order]


async def test_live_order_with_verified_inputs_records_provider_result() -> None:
    now = now_utc()
    repository = InMemoryRepository(BotSettings(enabled=True, mode="live", live_order_allowed=True))
    broker = RecordingBroker()
    service = ExecutionService(broker, repository, RiskService())
    strategy_version_id = uuid4()
    signal = Signal(
        symbol="005930",
        action="buy",
        final_score=0.8,
        confidence=0.8,
        order_amount_krw=100_000,
        sector="technology",
        reason_json={"score": 0.8},
    )
    decision = DecisionSnapshot.create(
        cycle_id=uuid4(),
        signal=signal,
        strategy_version_id=strategy_version_id,
        created_at=now,
        feature_snapshot=_live_ready_feature_snapshot(),
        risk_snapshot={"allowed": True},
    )

    order, risk_result = await service.propose_live_order(
        decision, _risk_input(now, signal, strategy_version_id)
    )

    assert risk_result.allowed is True
    assert broker.place_order_calls == 1
    assert order.status == "sent"
    assert order.provider_order_id == "recording-broker-order"
    assert repository.orders == [order]
    assert repository.engine_events[-1]["message"] == "live_broker_order_result_recorded"


def _live_ready_feature_snapshot() -> dict[str, object]:
    return {
        "technical_score": 0.8,
        "raw": {
            "feature_source": "verified_unit_fixture",
            "live_trading_ready": True,
        },
    }


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
        critical_news_risk=False,
        liquidity_ok=True,
        volatility_ok=True,
        cooldown_active=False,
        duplicate_order=False,
        strategy_version_id=strategy_version_id,
    )
