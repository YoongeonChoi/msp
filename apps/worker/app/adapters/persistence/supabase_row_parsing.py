from __future__ import annotations

import math
import re
from datetime import datetime
from typing import cast
from uuid import UUID

from app.domain.common.json import JsonObject
from app.domain.strategy.entities import StrategyVersion
from app.domain.trading.entities import BotSettings, TradingMode
from app.domain.trading.policies import settings_validation_reasons
from app.domain.trading.value_objects import StrategyWeights

STRATEGY_WEIGHT_KEYS = {
    "technical",
    "fundamental",
    "market_sector",
    "news_event",
    "portfolio",
}


def bot_settings_from_row(row: JsonObject) -> BotSettings:
    try:
        if _required_string(row, "id") != "singleton":
            raise ValueError("bot_settings_singleton_invalid")
        mode_value = _required_string(row, "mode")
        if mode_value not in {"paper", "live"}:
            raise ValueError("bot_settings_mode_invalid")
        deployment_target_sha = _optional_deployment_target_sha(
            row.get("deployment_target_sha")
        )
        settings = BotSettings(
            enabled=_required_bool(row, "enabled"),
            mode=cast(TradingMode, mode_value),
            live_order_allowed=_required_bool(row, "live_order_allowed"),
            deployment_lock=_required_bool(row, "deployment_lock"),
            deployment_target_sha=deployment_target_sha,
            max_order_amount_krw=_required_int(row, "max_order_amount_krw"),
            max_daily_loss_pct=_required_finite_number(row, "max_daily_loss_pct"),
            max_daily_order_count=_required_int(row, "max_daily_order_count"),
            max_position_pct=_required_finite_number(row, "max_position_pct"),
            max_sector_pct=_required_finite_number(row, "max_sector_pct"),
            loop_interval_sec=_required_int(row, "loop_interval_sec"),
        )
        reasons = settings_validation_reasons(settings)
        if reasons:
            raise ValueError(",".join(reasons))
        return settings
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError("bot_settings_row_invalid") from exc


def strategy_version_from_row(row: JsonObject) -> StrategyVersion:
    try:
        weights = _required_mapping(row, "weights")
        if set(weights) != STRATEGY_WEIGHT_KEYS:
            raise ValueError("strategy_weight_keys_invalid")
        params = _required_mapping(row, "params")
        version = _required_version(row)
        status = _required_string(row, "status")
        if status not in {"draft", "paper", "active", "retired"}:
            raise ValueError("strategy_status_invalid")
        strategy_type = _required_string(row, "strategy_type")
        if strategy_type != "WeightedFactorStrategyV1":
            raise ValueError("strategy_type_unsupported")
        parsed_weights = StrategyWeights(
            technical=_required_finite_number(weights, "technical"),
            fundamental=_required_finite_number(weights, "fundamental"),
            market_sector=_required_finite_number(weights, "market_sector"),
            news_event=_required_finite_number(weights, "news_event"),
            portfolio=_required_finite_number(weights, "portfolio"),
        )
        if any(
            value < 0 or value > 1
            for value in (
                parsed_weights.technical,
                parsed_weights.fundamental,
                parsed_weights.market_sector,
                parsed_weights.news_event,
                parsed_weights.portfolio,
            )
        ) or not math.isclose(parsed_weights.total(), 1.0, abs_tol=1e-6):
            raise ValueError("strategy_weights_invalid")
        buy_threshold = _required_finite_number(params, "buy_threshold")
        sell_threshold = _required_finite_number(params, "sell_threshold")
        if not 0 <= sell_threshold < buy_threshold <= 1:
            raise ValueError("strategy_thresholds_invalid")
        approved_by = _optional_uuid(row.get("approved_by"))
        approved_at = _optional_datetime(row.get("approved_at"))
        if status == "active" and (approved_by is None or approved_at is None):
            raise ValueError("active_strategy_requires_approval")
        return StrategyVersion(
            id=UUID(_required_string(row, "id")),
            version=version,
            status=status,
            strategy_type=strategy_type,
            weights=parsed_weights,
            buy_threshold=buy_threshold,
            sell_threshold=sell_threshold,
            approved_by=approved_by,
            approved_at=approved_at,
        )
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError("strategy_version_row_invalid") from exc


def _required_mapping(row: JsonObject, key: str) -> JsonObject:
    value = row.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"strategy_{key}_must_be_object")
    return value


def _required_bool(row: JsonObject, key: str) -> bool:
    value = row.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{key}_must_be_boolean")
    return value


def _required_int(row: JsonObject, key: str) -> int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key}_must_be_integer")
    return value


def _required_string(row: JsonObject, key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"strategy_{key}_must_be_string")
    return value.strip()


def _required_finite_number(row: JsonObject, key: str) -> float:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"strategy_{key}_must_be_number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"strategy_{key}_must_be_finite")
    return number


def _required_version(row: JsonObject) -> str:
    value = row.get("version")
    if not isinstance(value, str) or not value.strip():
        value = row.get("version_name")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("strategy_version_missing")
    return value.strip()


def _optional_uuid(value: object) -> UUID | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("strategy_approved_by_invalid")
    return UUID(value)


def _optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("strategy_approved_at_invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("strategy_approved_at_timezone_missing")
    return parsed


def _optional_deployment_target_sha(value: object) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[0-9a-f]{40}([0-9a-f]{24})?", value) is None
    ):
        raise ValueError("deployment_target_sha_invalid")
    return value
