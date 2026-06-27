from __future__ import annotations

from typing import Literal
from uuid import uuid4

from app.application.ports.broker_port import BrokerOrderRequest, BrokerPort
from app.application.ports.repository_port import RepositoryPort
from app.application.services.risk_service import RiskService
from app.domain.common.errors import KnownFailClosedError
from app.domain.risk.entities import RiskResult
from app.domain.risk.value_objects import RiskInput
from app.domain.trading.entities import DecisionSnapshot, Order
from app.infrastructure.idempotency import build_idempotency_key


class ExecutionService:
    def __init__(
        self,
        broker: BrokerPort,
        repository: RepositoryPort,
        risk_service: RiskService,
    ) -> None:
        self.broker = broker
        self.repository = repository
        self.risk_service = risk_service

    async def create_paper_order(
        self,
        decision: DecisionSnapshot,
        reason: str | None = None,
    ) -> Order | None:
        if decision.signal.action == "buy":
            action: Literal["buy", "sell"] = "buy"
        elif decision.signal.action == "sell":
            action = "sell"
        else:
            return None
        key = build_idempotency_key(
            mode="paper",
            decision_id=str(decision.id),
            symbol=decision.signal.symbol,
            action=action,
            amount_krw=decision.signal.order_amount_krw,
        )
        order = Order(
            id=uuid4(),
            decision_id=decision.id,
            symbol=decision.signal.symbol,
            action=action,
            mode="paper",
            status="paper",
            amount_krw=decision.signal.order_amount_krw,
            idempotency_key=key,
            reason=reason,
            created_at=decision.created_at,
        )
        await self.repository.persist_order(order)
        return order

    async def propose_live_order(
        self,
        decision: DecisionSnapshot,
        risk_input: RiskInput,
    ) -> tuple[Order, RiskResult]:
        if decision.signal.action == "buy":
            action: Literal["buy", "sell"] = "buy"
        elif decision.signal.action == "sell":
            action = "sell"
        else:
            raise KnownFailClosedError("execution", "live_order_requires_buy_or_sell_decision")
        final_risk = self.risk_service.evaluate_live_order(risk_input)
        key = build_idempotency_key(
            mode="live",
            decision_id=str(decision.id),
            symbol=decision.signal.symbol,
            action=action,
            amount_krw=decision.signal.order_amount_krw,
        )
        if await self.repository.idempotency_key_exists(key):
            blocked = Order(
                id=uuid4(),
                decision_id=decision.id,
                symbol=decision.signal.symbol,
                action=action,
                mode="live",
                status="blocked",
                amount_krw=decision.signal.order_amount_krw,
                idempotency_key=key,
                reason="duplicate_idempotency_key",
                created_at=decision.created_at,
            )
            await self.repository.persist_order(blocked, final_risk)
            return blocked, final_risk
        proposed = Order(
            id=uuid4(),
            decision_id=decision.id,
            symbol=decision.signal.symbol,
            action=action,
            mode="live",
            status="proposed" if final_risk.allowed else "blocked",
            amount_krw=decision.signal.order_amount_krw,
            idempotency_key=key,
            reason=None if final_risk.allowed else final_risk.safe_message,
            created_at=decision.created_at,
        )
        await self.repository.persist_order(proposed, final_risk)
        if not final_risk.allowed:
            return proposed, final_risk
        broker_result = await self.broker.place_order(
            BrokerOrderRequest(
                symbol=proposed.symbol,
                side=proposed.action,
                amount_krw=proposed.amount_krw,
                idempotency_key=proposed.idempotency_key,
            )
        )
        sent = Order(
            id=proposed.id,
            decision_id=proposed.decision_id,
            symbol=proposed.symbol,
            action=proposed.action,
            mode="live",
            status=broker_result.status,
            amount_krw=proposed.amount_krw,
            idempotency_key=proposed.idempotency_key,
            reason=None,
            created_at=proposed.created_at,
        )
        await self.repository.persist_order(sent, final_risk)
        return sent, final_risk
