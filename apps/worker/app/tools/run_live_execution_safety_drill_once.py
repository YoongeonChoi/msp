from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class LiveExecutionSafetyDrillResult:
    missing_evidence_blocked: int
    pre_broker_manual_check: int
    provider_result_recorded: int
    duplicate_blocked: int
    broker_calls: int


class DrillBroker:
    def __init__(self, repository: InMemoryRepository) -> None:
        self.repository = repository
        self.place_order_calls = 0
        self.pre_broker_manual_check_observed = False

    async def provider_health(self) -> bool:
        return True

    async def place_order(self, request: BrokerOrderRequest) -> BrokerOrderResult:
        self.place_order_calls += 1
        if len(self.repository.orders) != 1:
            raise RuntimeError("pending_order_not_persisted_before_broker_call")
        pending = self.repository.orders[0]
        if pending.status != "unknown_requires_manual_check":
            raise RuntimeError("pending_order_status_not_manual_check")
        if pending.reason != "live_broker_order_result_pending":
            raise RuntimeError("pending_order_reason_not_result_pending")
        if pending.idempotency_key != request.idempotency_key:
            raise RuntimeError("pending_order_idempotency_key_mismatch")
        self.pre_broker_manual_check_observed = True
        return BrokerOrderResult(
            provider_order_id="drill-provider-order",
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
            cancel_provider_order_id="unused-drill-cancel-order",
            raw_summary={"provider_order_id": provider_order_id},
        )

    async def get_account_state(self, now: datetime) -> AccountState:
        return _account_state(now)


async def run_live_execution_safety_drill() -> LiveExecutionSafetyDrillResult:
    missing_evidence_blocked = await _drill_missing_evidence_block()
    repository = InMemoryRepository(_live_settings())
    broker = DrillBroker(repository)
    service = ExecutionService(broker, repository, RiskService())
    now = now_utc()
    strategy_version_id = uuid4()
    signal = _signal()
    decision = _decision(now, signal, strategy_version_id, include_evidence=True)
    risk_input = _risk_input(now, signal, strategy_version_id)

    order, risk_result = await service.propose_live_order(decision, risk_input)
    if not risk_result.allowed:
        raise RuntimeError("live_risk_result_not_allowed")
    provider_result_recorded = int(
        order.status == "sent"
        and order.provider_order_id == "drill-provider-order"
        and len(repository.orders) == 1
        and repository.orders[0].status == "sent"
    )
    if provider_result_recorded != 1:
        raise RuntimeError("provider_result_not_recorded")

    duplicate, _duplicate_risk = await service.propose_live_order(decision, risk_input)
    duplicate_blocked = int(
        duplicate.status == "blocked"
        and duplicate.reason == "duplicate_idempotency_key"
        and broker.place_order_calls == 1
    )
    if duplicate_blocked != 1:
        raise RuntimeError("duplicate_idempotency_key_not_blocked")

    return LiveExecutionSafetyDrillResult(
        missing_evidence_blocked=missing_evidence_blocked,
        pre_broker_manual_check=int(broker.pre_broker_manual_check_observed),
        provider_result_recorded=provider_result_recorded,
        duplicate_blocked=duplicate_blocked,
        broker_calls=broker.place_order_calls,
    )


def format_live_execution_safety_drill_result(
    result: LiveExecutionSafetyDrillResult,
) -> str:
    return (
        "FINAL=PASS live_execution_safety_drill "
        f"missing_evidence_blocked={result.missing_evidence_blocked} "
        f"pre_broker_manual_check={result.pre_broker_manual_check} "
        f"provider_result_recorded={result.provider_result_recorded} "
        f"duplicate_blocked={result.duplicate_blocked} "
        f"broker_calls={result.broker_calls}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run local live execution safety invariants without provider I/O."
    )
    parser.parse_args(argv)
    try:
        result = asyncio.run(run_live_execution_safety_drill())
    except Exception as exc:
        print(f"FINAL=FAIL live_execution_safety_drill reason={_safe_reason(str(exc))}")
        return 1
    print(format_live_execution_safety_drill_result(result))
    return 0


async def _drill_missing_evidence_block() -> int:
    repository = InMemoryRepository(_live_settings())
    broker = DrillBroker(repository)
    service = ExecutionService(broker, repository, RiskService())
    now = now_utc()
    strategy_version_id = uuid4()
    signal = _signal()
    decision = _decision(now, signal, strategy_version_id, include_evidence=False)

    order, risk_result = await service.propose_live_order(
        decision,
        _risk_input(now, signal, strategy_version_id),
    )
    blocked = int(
        risk_result.allowed
        and order.status == "blocked"
        and order.reason is not None
        and "missing_live_decision_evidence" in order.reason
        and broker.place_order_calls == 0
    )
    if blocked != 1:
        raise RuntimeError("missing_evidence_not_blocked_before_broker")
    return blocked


def _live_settings() -> BotSettings:
    return BotSettings(enabled=True, mode="live", live_order_allowed=True)


def _signal() -> Signal:
    return Signal(
        symbol="005930",
        action="buy",
        final_score=0.8,
        confidence=0.8,
        order_amount_krw=100_000,
        sector="technology",
        reason_json={"score": 0.8},
    )


def _decision(
    now: datetime,
    signal: Signal,
    strategy_version_id: UUID,
    *,
    include_evidence: bool,
) -> DecisionSnapshot:
    return DecisionSnapshot.create(
        cycle_id=uuid4(),
        signal=signal,
        strategy_version_id=strategy_version_id,
        created_at=now,
        feature_snapshot=_live_ready_feature_snapshot() if include_evidence else {},
        risk_snapshot={"allowed": True} if include_evidence else {},
    )


def _live_ready_feature_snapshot() -> dict[str, object]:
    return {
        "technical_score": 0.8,
        "raw": {
            "feature_source": "verified_drill_fixture",
            "live_trading_ready": True,
        },
    }


def _risk_input(now: datetime, signal: Signal, strategy_version_id: UUID) -> RiskInput:
    return RiskInput(
        settings=_live_settings(),
        signal=signal,
        account_state=_account_state(now),
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


def _account_state(now: datetime) -> AccountState:
    return AccountState(
        synced=True,
        cash_krw=1_000_000,
        equity_krw=10_000_000,
        daily_loss_pct=0.0,
        daily_order_count=0,
        synced_at=now,
    )


def _safe_reason(reason: str) -> str:
    return reason.replace("\r", " ").replace("\n", " ")[:300]


if __name__ == "__main__":
    raise SystemExit(main())
