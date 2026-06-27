from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime

from app.application.services.outcome_tracking_models import (
    DecisionForOutcome,
    PaperOrderForOutcome,
    PricePoint,
    TradeSide,
)
from app.domain.common.json import JsonObject, JsonValue

PRICE_AT_DECISION_KEYS = (
    "price_at_decision",
    "quote_price_krw",
    "current_price_krw",
    "price_krw",
    "close_price",
    "close",
)
CLOSE_PRICE_KEYS = ("close_price", "close", "price_krw", "current_price_krw", "price")
ORDER_PRICE_KEYS = ("price", "paper_price_krw", "price_krw", "filled_price_krw")
ORDER_QUANTITY_KEYS = ("quantity", "paper_quantity", "filled_quantity")


def parse_decision(row: JsonObject) -> DecisionForOutcome:
    feature_snapshot = json_object_value(row.get("feature_snapshot")) or json_object_value(
        row.get("feature_snapshot_json")
    )
    action = string_value(row.get("action"))
    return DecisionForOutcome(
        decision_id=string_value(row.get("id")),
        symbol=string_value(row.get("symbol")),
        side=trade_side(action),
        decided_at=datetime_value(row.get("decided_at")) or datetime_value(row.get("created_at")),
        price_at_decision=number_from(feature_snapshot, PRICE_AT_DECISION_KEYS),
        feature_snapshot=feature_snapshot,
    )


def trade_side(action: str) -> TradeSide | None:
    match action:
        case "buy":
            return TradeSide.BUY
        case "sell":
            return TradeSide.SELL
        case _:
            return None


def group_prices(rows: list[JsonObject]) -> dict[str, list[PricePoint]]:
    grouped: dict[str, list[PricePoint]] = defaultdict(list)
    for row in rows:
        point = parse_price_point(row)
        if point is not None:
            grouped[point.symbol].append(point)
    for points in grouped.values():
        points.sort(key=lambda point: point.trade_date)
    return grouped


def parse_price_point(row: JsonObject) -> PricePoint | None:
    trade_date = date_value(row.get("trade_date"))
    close_price = number_from(row, CLOSE_PRICE_KEYS)
    raw_snapshot = json_object_value(row.get("raw_snapshot"))
    if close_price is None:
        close_price = number_from(raw_snapshot, CLOSE_PRICE_KEYS)
    if trade_date is None or close_price is None or close_price <= 0:
        return None
    return PricePoint(
        symbol=string_value(row.get("symbol")),
        trade_date=trade_date,
        close_price=close_price,
    )


def paper_orders_by_decision(rows: list[JsonObject]) -> dict[str, PaperOrderForOutcome]:
    orders: dict[str, PaperOrderForOutcome] = {}
    for row in rows:
        order = parse_order(row)
        if order is not None:
            orders.setdefault(order.decision_id, order)
    return orders


def parse_order(row: JsonObject) -> PaperOrderForOutcome | None:
    status = string_value(row.get("status"))
    if status not in {"paper", "proposed"}:
        return None
    decision_id = string_value(row.get("decision_id"))
    if not decision_id:
        return None
    return PaperOrderForOutcome(
        order_id=string_value(row.get("id")),
        decision_id=decision_id,
        status=status,
        price=number_from(row, ORDER_PRICE_KEYS),
        quantity=number_from(row, ORDER_QUANTITY_KEYS),
        amount_krw=number_value(row.get("amount_krw")),
    )


def number_from(row: JsonObject, keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = number_value(row.get(key))
        if value is not None:
            return value
    return None


def number_value(value: JsonValue | None) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def string_value(value: JsonValue | None) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)


def json_object_value(value: JsonValue | None) -> JsonObject:
    if isinstance(value, dict):
        return value
    return {}


def datetime_value(value: JsonValue | None) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def date_value(value: JsonValue | None) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
