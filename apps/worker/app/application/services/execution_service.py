from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from math import isfinite
from typing import Literal, assert_never
from uuid import uuid4

from app.application.ports.broker_port import BrokerOrderRequest, BrokerOrderResult, BrokerPort
from app.application.ports.execution_kernel_port import ContractDispatchPort
from app.application.ports.repository_port import RepositoryPort
from app.application.services.risk_service import RiskService
from app.domain.common.errors import (
    KnownFailClosedError,
)
from app.domain.execution_v2.models import (
    ExecutionCostSchedule,
    ExecutionIntent,
    ExecutionObservation,
    ExecutionStatus,
    PaperExecutionEvidence,
)
from app.domain.risk.entities import RiskResult
from app.domain.risk.value_objects import RiskInput
from app.domain.trading.entities import DecisionSnapshot, Order
from app.domain.trading.order_calculations import calculate_order_quantity
from app.infrastructure.idempotency import build_idempotency_key


class ExecutionService:
    def __init__(
        self,
        broker: BrokerPort,
        repository: RepositoryPort,
        risk_service: RiskService,
        shutdown_requested: Callable[[], bool] | None = None,
    ) -> None:
        self.broker = broker
        self.repository = repository
        self.risk_service = risk_service
        self.shutdown_requested = shutdown_requested or (lambda: False)

    async def create_paper_order(
        self,
        decision: DecisionSnapshot,
        risk_result: RiskResult,
        idempotency_key: str,
    ) -> Order | None:
        match decision.signal.action:
            case "buy":
                action: Literal["buy", "sell"] = "buy"
            case "sell":
                action = "sell"
            case "hold":
                return None
            case unreachable:
                assert_never(unreachable)
        key = idempotency_key
        reason = None if risk_result.allowed else risk_result.safe_message
        status: Literal["paper", "blocked"] = "paper" if risk_result.allowed else "blocked"
        if await self.repository.idempotency_key_exists(key):
            key = build_idempotency_key(
                mode="paper_blocked",
                decision_id=str(decision.id),
                symbol=decision.signal.symbol,
                action=action,
                amount_krw=decision.signal.order_amount_krw,
            )
            reason = reason or "duplicate_idempotency_key"
            status = "blocked"
        if not decision.signal.reason_json:
            reason = "missing_reason_json"
            status = "blocked"
        if not decision.feature_snapshot:
            reason = "missing_feature_snapshot"
            status = "blocked"
        if not decision.risk_snapshot:
            reason = "missing_risk_snapshot"
            status = "blocked"
        paper_price_krw: int | None = None
        paper_quantity: int | None = None
        if status == "paper":
            paper_price_krw = _paper_decision_price(decision)
            paper_quantity = (
                calculate_order_quantity(decision.signal.order_amount_krw, paper_price_krw)
                if paper_price_krw is not None
                else None
            )
            if paper_price_krw is None or paper_quantity is None:
                reason = "invalid_paper_execution_price"
                status = "blocked"
        order = Order(
            id=uuid4(),
            decision_id=decision.id,
            symbol=decision.signal.symbol,
            action=action,
            mode="paper",
            status=status,
            amount_krw=decision.signal.order_amount_krw,
            idempotency_key=key,
            reason=reason,
            created_at=decision.created_at,
            quantity=paper_quantity,
            price_krw=paper_price_krw,
        )
        await self.repository.persist_order(order, risk_result)
        if order.status == "blocked":
            await self.repository.record_engine_event(
                "warning",
                "paper_execution",
                "paper_order_blocked",
                {
                    "symbol": order.symbol,
                    "reason": order.reason or "unknown",
                    "risk_reasons": risk_result.reasons,
                },
            )
        return order

    async def dispatch_contract_test_order(
        self,
        intent: ExecutionIntent,
        coordination: ContractDispatchPort,
        *,
        cost_schedule: ExecutionCostSchedule,
        execution_evidence: PaperExecutionEvidence,
        now: datetime,
    ) -> BrokerOrderResult:
        """Dispatch only to the zero-network local contract broker.

        The durable dispatch marker is written before the only broker call in this
        execution path. Any ambiguous post-marker error becomes a manual-blocking
        observation and is re-raised to stop automation.
        """

        if intent.environment != "contract_test":
            raise KnownFailClosedError("execution_v2", "contract_dispatch_requires_contract_test")
        if (
            cost_schedule.version != intent.cost_schedule_version
            or cost_schedule.evidence_sha256 != intent.cost_schedule_evidence_sha256
            or not cost_schedule.covers(intent.eligible_at, intent.expires_at)
        ):
            raise KnownFailClosedError(
                "execution_v2",
                "contract_dispatch_cost_schedule_is_invalid",
            )
        if (
            execution_evidence.execution_policy_version
            != intent.execution_policy_version
            or not execution_evidence.covers(intent.eligible_at, intent.expires_at)
        ):
            raise KnownFailClosedError(
                "execution_v2",
                "contract_dispatch_execution_evidence_is_invalid",
            )
        if (
            getattr(self.broker, "execution_environment", None) != "contract_test"
            or getattr(self.broker, "network_enabled", True) is not False
            or getattr(self.broker, "production_order_capable", True) is not False
        ):
            raise KnownFailClosedError(
                "execution_v2",
                "contract_dispatch_refuses_order_capable_or_network_broker",
            )
        if self.shutdown_requested():
            await coordination.record_execution_observation(
                intent,
                _contract_observation(
                    intent,
                    status="failed_pre_dispatch",
                    observed_at=now,
                    provider_order_id=f"pre-dispatch:{intent.semantic_key}",
                    provider_execution_id=None,
                    settlement_date=None,
                    reason="shutdown_requested",
                ),
                now=now,
            )
            raise KnownFailClosedError("execution_v2", "shutdown_requested")
        await coordination.mark_dispatch_started(intent, now=now)
        try:
            result = await self.broker.place_order(
                BrokerOrderRequest(
                    symbol=intent.symbol,
                    side=intent.side,
                    amount_krw=intent.quantity * intent.limit_price_krw,
                    idempotency_key=intent.semantic_key,
                    quantity=intent.quantity,
                    limit_price_krw=intent.limit_price_krw,
                )
            )
        except KnownFailClosedError:
            await coordination.record_execution_observation(
                intent,
                _contract_observation(
                    intent,
                    status="unknown_requires_manual_check",
                    observed_at=now,
                    provider_order_id=f"unresolved:{intent.semantic_key}",
                    provider_execution_id=None,
                    settlement_date=None,
                    reason="contract_dispatch_result_ambiguous",
                ),
                now=now,
            )
            raise
        status: ExecutionStatus
        if result.status == "sent":
            status = "open"
        elif result.status == "filled":
            status = "filled"
        elif result.status == "rejected":
            status = "rejected"
        else:
            status = "unknown_requires_manual_check"
        provider_order_id = result.provider_order_id or f"unresolved:{intent.semantic_key}"
        provider_execution_id_value = result.raw_summary.get("provider_execution_id")
        provider_execution_id = (
            provider_execution_id_value
            if isinstance(provider_execution_id_value, str)
            and provider_execution_id_value.strip()
            else None
        )
        settlement_date = (
            execution_evidence.settlement_date_for(now, cost_schedule.settlement_days)
            if status == "filled"
            else None
        )
        await coordination.record_execution_observation(
            intent,
            _contract_observation(
                intent,
                status=status,
                observed_at=now,
                provider_order_id=provider_order_id,
                provider_execution_id=provider_execution_id,
                settlement_date=settlement_date,
                reason=(
                    "contract_dispatch_result_ambiguous"
                    if status == "unknown_requires_manual_check"
                    else None
                ),
            ),
            now=now,
        )
        return result

    async def propose_live_order(
        self,
        decision: DecisionSnapshot,
        risk_input: RiskInput,
    ) -> tuple[Order, RiskResult]:
        match decision.signal.action:
            case "buy":
                action: Literal["buy", "sell"] = "buy"
            case "sell":
                action = "sell"
            case "hold":
                raise KnownFailClosedError("execution", "live_order_requires_buy_or_sell_decision")
            case unreachable:
                assert_never(unreachable)
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
                reason="legacy_live_order_write_quarantined",
                created_at=decision.created_at,
            )
            await self.repository.record_engine_event(
                "critical",
                "live_execution",
                "legacy_live_order_write_quarantined",
                {
                    "symbol": blocked.symbol,
                    "existing_order_preserved": True,
                    "broker_dispatch_attempted": False,
                },
            )
            return blocked, final_risk
        blocked = Order(
            id=uuid4(),
            decision_id=decision.id,
            symbol=decision.signal.symbol,
            action=action,
            mode="live",
            status="blocked",
            amount_krw=decision.signal.order_amount_krw,
            idempotency_key=key,
            reason="legacy_live_order_write_quarantined",
            created_at=decision.created_at,
        )
        await self.repository.persist_order(blocked, final_risk)
        await self.repository.record_engine_event(
            "critical",
            "live_execution",
            "legacy_live_order_write_quarantined",
            {
                "symbol": blocked.symbol,
                "risk_allowed": final_risk.allowed,
                "broker_dispatch_attempted": False,
            },
        )
        return blocked, final_risk

def _paper_decision_price(decision: DecisionSnapshot) -> int | None:
    value = decision.feature_snapshot.get("price_at_decision")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    numeric = float(value)
    if not isfinite(numeric) or numeric <= 0 or not numeric.is_integer():
        return None
    return int(numeric)


def _contract_observation(
    intent: ExecutionIntent,
    *,
    status: ExecutionStatus,
    observed_at: datetime,
    provider_order_id: str,
    provider_execution_id: str | None,
    settlement_date: date | None,
    reason: str | None,
) -> ExecutionObservation:
    is_filled = status == "filled"
    return ExecutionObservation.create(
        intent_id=intent.id,
        sequence=1,
        status=status,
        observed_at=observed_at,
        provider_order_id=provider_order_id,
        provider_execution_id=provider_execution_id,
        cumulative_quantity=intent.quantity if is_filled else 0,
        cumulative_gross_krw=(
            intent.quantity * intent.limit_price_krw if is_filled else 0
        ),
        cumulative_commission_krw=0,
        cumulative_tax_krw=0,
        last_fill_quantity=intent.quantity if is_filled else None,
        last_fill_price_krw=intent.limit_price_krw if is_filled else None,
        last_fill_settlement_date=settlement_date,
        reason=reason,
    )
