from __future__ import annotations

from typing import Any

from app.domain.risk.entities import RiskResult
from app.domain.trading.entities import DecisionSnapshot, Order


def decision_to_row(snapshot: DecisionSnapshot) -> dict[str, Any]:
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


def order_to_row(order: Order, risk_result: RiskResult | None = None) -> dict[str, Any]:
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
        "risk_result": risk_result.to_dict() if risk_result else None,
        "created_at": order.created_at.isoformat(),
    }

