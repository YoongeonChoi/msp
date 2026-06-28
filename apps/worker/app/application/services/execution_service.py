from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, assert_never
from uuid import uuid4

from app.application.ports.broker_port import BrokerOrderRequest, BrokerPort
from app.application.ports.repository_port import RepositoryPort
from app.application.services.risk_service import RiskService
from app.domain.common.errors import (
    KnownFailClosedError,
    ProviderTimeoutError,
    ProviderUnknownError,
)
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
                reason="duplicate_idempotency_key",
                created_at=decision.created_at,
            )
            await self.repository.persist_order(blocked, final_risk)
            await self.repository.record_engine_event(
                "warning",
                "live_execution",
                "live_order_blocked_duplicate_idempotency_key",
                {"symbol": blocked.symbol, "idempotency_key": key},
            )
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
        if not final_risk.allowed:
            await self.repository.persist_order(proposed, final_risk)
            await self.repository.record_engine_event(
                "warning",
                "live_execution",
                "live_order_blocked_by_risk",
                {"symbol": proposed.symbol, "risk_reasons": final_risk.reasons},
            )
            return proposed, final_risk
        evidence_reasons = _live_decision_evidence_reasons(decision)
        if evidence_reasons:
            blocked = Order(
                id=proposed.id,
                decision_id=proposed.decision_id,
                symbol=proposed.symbol,
                action=proposed.action,
                mode="live",
                status="blocked",
                amount_krw=proposed.amount_krw,
                idempotency_key=proposed.idempotency_key,
                reason="missing_live_decision_evidence:" + ",".join(evidence_reasons),
                created_at=proposed.created_at,
            )
            await self.repository.persist_order(blocked, final_risk)
            await self.repository.record_engine_event(
                "critical",
                "live_execution",
                "live_order_blocked_missing_evidence",
                {"symbol": blocked.symbol, "reasons": evidence_reasons},
            )
            return blocked, final_risk
        try:
            if risk_input.quote is None:
                raise KnownFailClosedError("execution", "live_order_missing_quote")
            if risk_input.quote.price_krw <= 0:
                raise KnownFailClosedError("execution", "live_order_invalid_quote_price")
            quantity = proposed.amount_krw // risk_input.quote.price_krw
            if quantity <= 0:
                raise KnownFailClosedError("execution", "live_order_amount_below_quote_price")
            pending = Order(
                id=proposed.id,
                decision_id=proposed.decision_id,
                symbol=proposed.symbol,
                action=proposed.action,
                mode="live",
                status="unknown_requires_manual_check",
                amount_krw=proposed.amount_krw,
                idempotency_key=proposed.idempotency_key,
                reason="live_broker_order_result_pending",
                created_at=proposed.created_at,
            )
            await self.repository.persist_order(pending, final_risk)
            broker_result = await self.broker.place_order(
                BrokerOrderRequest(
                    symbol=proposed.symbol,
                    side=proposed.action,
                    amount_krw=proposed.amount_krw,
                    idempotency_key=proposed.idempotency_key,
                    quantity=quantity,
                    limit_price_krw=risk_input.quote.price_krw,
                )
            )
        except KnownFailClosedError as exc:
            failed_status: Literal["failed", "unknown_requires_manual_check"] = (
                "unknown_requires_manual_check"
                if isinstance(exc, ProviderTimeoutError | ProviderUnknownError)
                else "failed"
            )
            failed = Order(
                id=proposed.id,
                decision_id=proposed.decision_id,
                symbol=proposed.symbol,
                action=proposed.action,
                mode="live",
                status=failed_status,
                amount_krw=proposed.amount_krw,
                idempotency_key=proposed.idempotency_key,
                reason=exc.safe_message,
                created_at=proposed.created_at,
            )
            if await self.repository.idempotency_key_exists(failed.idempotency_key):
                await self.repository.update_order_status(
                    order_id=str(failed.id),
                    status=failed.status,
                    reason=failed.reason,
                    provider_payload_summary=failed.provider_payload_summary,
                )
            else:
                await self.repository.persist_order(failed, final_risk)
            await self.repository.record_engine_event(
                "critical",
                "live_execution",
                "live_broker_order_failed_closed",
                {
                    "symbol": failed.symbol,
                    "status": failed.status,
                    "component": exc.component,
                    "reason": exc.safe_message,
                },
            )
            return failed, final_risk
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
            provider_order_id=broker_result.provider_order_id,
            provider_payload_summary=broker_result.raw_summary,
        )
        await self.repository.update_order_status(
            order_id=str(sent.id),
            status=sent.status,
            reason=sent.reason,
            provider_payload_summary=sent.provider_payload_summary,
            provider_order_id=sent.provider_order_id,
        )
        await self.repository.record_engine_event(
            "info",
            "live_execution",
            "live_broker_order_result_recorded",
            {
                "symbol": sent.symbol,
                "status": sent.status,
                "provider_order_id_present": sent.provider_order_id is not None,
            },
        )
        return sent, final_risk


def _live_decision_evidence_reasons(decision: DecisionSnapshot) -> list[str]:
    reasons: list[str] = []
    if not decision.signal.reason_json:
        reasons.append("missing_reason_json")
    if not decision.feature_snapshot:
        reasons.append("missing_feature_snapshot")
    else:
        reasons.extend(_live_feature_snapshot_reasons(decision.feature_snapshot))
    if not decision.risk_snapshot:
        reasons.append("missing_risk_snapshot")
    return reasons


def _live_feature_snapshot_reasons(
    feature_snapshot: Mapping[str, object],
) -> list[str]:
    raw = feature_snapshot.get("raw")
    if not isinstance(raw, Mapping):
        return ["missing_feature_raw_snapshot"]
    feature_source = raw.get("feature_source")
    if feature_source in {"mock_static", "mock"}:
        return ["mock_strategy_features_not_live_ready"]
    if raw.get("live_trading_ready") is not True:
        return ["feature_snapshot_not_live_ready"]
    if not isinstance(feature_source, str) or not feature_source.strip():
        return ["feature_snapshot_source_unverified"]
    return []
