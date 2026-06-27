from __future__ import annotations

from app.domain.common.json import JsonObject, json_object
from app.domain.risk.entities import RiskResult
from app.domain.strategy.research import AIUpgradeCandidate
from app.domain.trading.entities import DecisionSnapshot, Order


def decision_to_row(snapshot: DecisionSnapshot) -> JsonObject:
    return {
        "id": str(snapshot.id),
        "cycle_id": str(snapshot.cycle_id),
        "symbol": snapshot.signal.symbol,
        "action": snapshot.signal.action,
        "final_score": snapshot.signal.final_score,
        "confidence": snapshot.signal.confidence,
        "strategy_version_id": str(snapshot.strategy_version_id),
        "feature_snapshot": snapshot.feature_snapshot,
        "risk_snapshot": snapshot.risk_snapshot,
        "created_at": snapshot.created_at.isoformat(),
    }


def order_to_row(order: Order, risk_result: RiskResult | None = None) -> JsonObject:
    return {
        "id": str(order.id),
        "decision_id": str(order.decision_id),
        "symbol": order.symbol,
        "side": order.action,
        "mode": order.mode,
        "status": order.status,
        "amount_krw": order.amount_krw,
        "idempotency_key": order.idempotency_key,
        "reason": order.reason,
        "risk_result": json_object(risk_result.to_dict()) if risk_result else None,
        "created_at": order.created_at.isoformat(),
    }


def ai_candidate_to_row(candidate: AIUpgradeCandidate) -> JsonObject:
    return {
        "base_strategy_version_id": (
            str(candidate.base_strategy_version_id)
            if candidate.base_strategy_version_id is not None
            else None
        ),
        "candidate_name": candidate.candidate_name,
        "candidate_weights": candidate.candidate_weights.to_json(),
        "candidate_params": candidate.candidate_params,
        "rationale": candidate.rationale,
        "expected_improvement": candidate.expected_improvement,
        "risk_notes": candidate.risk_notes,
        "required_backtests": list(candidate.required_backtests),
        "status": "proposed",
        "approval_required": True,
        "created_at": candidate.created_at.isoformat(),
    }
