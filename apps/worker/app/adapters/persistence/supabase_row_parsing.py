from __future__ import annotations

from uuid import UUID

from app.domain.common.json import JsonObject
from app.domain.strategy.entities import StrategyVersion
from app.domain.trading.value_objects import StrategyWeights


def strategy_version_from_row(row: JsonObject) -> StrategyVersion:
    weights = _mapping_value(row, "weights")
    params = _mapping_value(row, "params")
    version = _string_value(row, "version", _string_value(row, "version_name", "unknown"))
    return StrategyVersion(
        id=UUID(_string_value(row, "id", "00000000-0000-0000-0000-000000000000")),
        version=version,
        status=_string_value(row, "status", "paper"),
        strategy_type=_string_value(row, "strategy_type", "WeightedFactorStrategyV1"),
        weights=StrategyWeights(
            technical=_float_value(weights, "technical", 0.35),
            fundamental=_float_value(weights, "fundamental", 0.25),
            market_sector=_float_value(weights, "market_sector", 0.15),
            news_event=_float_value(weights, "news_event", 0.15),
            portfolio=_float_value(weights, "portfolio", 0.10),
        ),
        buy_threshold=_float_value(params, "buy_threshold", 0.68),
        sell_threshold=_float_value(params, "sell_threshold", 0.25),
    )


def _mapping_value(row: JsonObject, key: str) -> JsonObject:
    value = row.get(key)
    return value if isinstance(value, dict) else {}


def _string_value(row: JsonObject, key: str, default: str) -> str:
    value = row.get(key)
    if value is None:
        return default
    return str(value)


def _float_value(row: JsonObject, key: str, default: float) -> float:
    value = row.get(key)
    match value:
        case bool() | None:
            return default
        case int() | float() as number:
            return float(number)
        case str() as text:
            return _parse_float(text, default)
        case _:
            return default


def _parse_float(value: str, default: float) -> float:
    try:
        return float(value)
    except ValueError:
        return default
