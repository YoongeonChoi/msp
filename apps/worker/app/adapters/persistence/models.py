from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from hashlib import sha256
from typing import Literal, cast
from uuid import UUID

from app.domain.common.json import JsonObject, json_object
from app.domain.common.time import KST
from app.domain.portfolio.entities import Position
from app.domain.risk.entities import RiskResult
from app.domain.strategy.research import AIUpgradeCandidate
from app.domain.trading.entities import DecisionSnapshot, Order, OrderStatus, TradingMode


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
        "provider_order_id": order.provider_order_id,
        "reason": order.reason,
        "risk_result": json_object(risk_result.to_dict()) if risk_result else None,
        "provider_payload_summary": (
            json_object(order.provider_payload_summary)
            if order.provider_payload_summary is not None
            else None
        ),
        "created_at": order.created_at.isoformat(),
    }


def fundamentals_row_from_decision(snapshot: DecisionSnapshot) -> JsonObject | None:
    raw = _feature_raw(snapshot)
    fundamentals = _mapping_value(raw, "fundamentals")
    if fundamentals is None:
        return None
    observed_at = snapshot.created_at.astimezone(KST)
    fiscal_quarter = ((observed_at.month - 1) // 3) + 1
    return {
        "symbol": snapshot.signal.symbol,
        "fiscal_year": observed_at.year,
        "fiscal_quarter": fiscal_quarter,
        "per": _optional_float_value(fundamentals, "per"),
        "pbr": _optional_float_value(fundamentals, "pbr"),
        "roe": _optional_float_value(fundamentals, "roe"),
        "operating_margin": _optional_float_value(fundamentals, "operating_margin"),
        "debt_ratio": _optional_float_value(fundamentals, "debt_ratio"),
        "source": _string_value(raw, "fundamentals_source", "unknown"),
        "raw_snapshot": json_object(fundamentals),
        "updated_at": snapshot.created_at.isoformat(),
    }


def position_to_row(position: Position, synced_at: datetime) -> JsonObject:
    unrealized_pnl_krw = (
        position.current_price_krw - position.avg_price_krw
    ) * position.quantity
    unrealized_pnl_pct = (
        (position.current_price_krw - position.avg_price_krw) / position.avg_price_krw
        if position.avg_price_krw > 0
        else 0.0
    )
    return {
        "symbol": position.symbol,
        "quantity": position.quantity,
        "avg_price_krw": position.avg_price_krw,
        "current_price_krw": position.current_price_krw,
        "market_value_krw": position.market_value_krw,
        "unrealized_pnl_krw": unrealized_pnl_krw,
        "unrealized_pnl_pct": unrealized_pnl_pct,
        "sector": position.sector,
        "synced_at": synced_at.isoformat(),
        "updated_at": synced_at.isoformat(),
    }


def news_rows_from_decision(snapshot: DecisionSnapshot) -> list[JsonObject]:
    raw = _feature_raw(snapshot)
    events = raw.get("news_events")
    if not isinstance(events, Iterable) or isinstance(events, str | bytes):
        return []
    rows: list[JsonObject] = []
    for event in events:
        if not isinstance(event, Mapping):
            continue
        title = _string_value(event, "title")
        if not title:
            continue
        rows.append(
            {
                "symbol": _string_value(event, "symbol", snapshot.signal.symbol),
                "title": title,
                "source": _string_value(event, "source", "unknown"),
                "published_at": _optional_string_value(event, "published_at"),
                "title_hash": _title_hash(title),
                "relevance_score": _optional_float_value(event, "relevance_score"),
                "sentiment": _optional_string_value(event, "sentiment"),
                "event_type": _optional_string_value(event, "event_type"),
                "risk_level": _optional_string_value(event, "risk_level"),
                "summary_short": _optional_string_value(event, "summary_short"),
                "trading_relevance": _optional_float_value(event, "trading_relevance"),
                "confidence": _optional_float_value(event, "confidence"),
                "linked_decision_id": str(snapshot.id),
            }
        )
    return rows


def order_from_row(row: Mapping[str, object]) -> Order:
    action = _order_action_value(row, "side")
    return Order(
        id=UUID(_string_value(row, "id")),
        decision_id=UUID(_string_value(row, "decision_id")),
        symbol=_string_value(row, "symbol"),
        action=action,
        mode=cast(TradingMode, _string_value(row, "mode", "paper")),
        status=cast(OrderStatus, _string_value(row, "status", "unknown_requires_manual_check")),
        amount_krw=_int_value(row, "amount_krw", 0),
        idempotency_key=_string_value(row, "idempotency_key"),
        provider_order_id=_optional_string_value(row, "provider_order_id"),
        reason=_optional_string_value(row, "reason"),
        provider_payload_summary=_optional_json_object(row, "provider_payload_summary"),
        created_at=_datetime_value(row, "created_at"),
    )


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


def _string_value(row: Mapping[str, object], key: str, default: str = "") -> str:
    value = row.get(key)
    if value is None:
        return default
    return str(value)


def _optional_string_value(row: Mapping[str, object], key: str) -> str | None:
    value = row.get(key)
    if value is None:
        return None
    return str(value)


def _optional_float_value(row: Mapping[str, object], key: str) -> float | None:
    value = row.get(key)
    match value:
        case bool() | None:
            return None
        case int() | float() as number:
            return float(number)
        case str() as text:
            try:
                return float(text)
            except ValueError:
                return None
        case _:
            return None


def _int_value(row: Mapping[str, object], key: str, default: int) -> int:
    value = row.get(key)
    match value:
        case bool() | None:
            return default
        case int() as number:
            return number
        case float() as number:
            return int(number)
        case str() as text:
            try:
                return int(text)
            except ValueError:
                return default
        case _:
            return default


def _datetime_value(row: Mapping[str, object], key: str) -> datetime:
    value = row.get(key)
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise ValueError(f"{key}_datetime_missing")


def _optional_json_object(row: Mapping[str, object], key: str) -> dict[str, object] | None:
    value = row.get(key)
    if value is None:
        return None
    return dict(json_object(value))


def _feature_raw(snapshot: DecisionSnapshot) -> JsonObject:
    raw = snapshot.feature_snapshot.get("raw")
    if raw is None:
        return {}
    return json_object(raw)


def _mapping_value(row: Mapping[str, object], key: str) -> JsonObject | None:
    value = row.get(key)
    if value is None:
        return None
    return json_object(value)


def _title_hash(title: str) -> str:
    normalized = " ".join(title.casefold().split())
    return sha256(normalized.encode("utf-8")).hexdigest()


def _order_action_value(row: Mapping[str, object], key: str) -> Literal["buy", "sell"]:
    value = _string_value(row, key, "buy")
    if value not in {"buy", "sell"}:
        raise ValueError(f"{key}_invalid_order_action")
    return cast(Literal["buy", "sell"], value)
