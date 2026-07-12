from __future__ import annotations

import copy
from collections.abc import Callable
from typing import cast
from uuid import uuid4

import pytest

from app.adapters.persistence.supabase_row_parsing import (
    bot_settings_from_row,
    strategy_version_from_row,
)
from app.domain.common.json import JsonObject


def test_strategy_row_parser_accepts_approved_active_strategy() -> None:
    strategy = strategy_version_from_row(_active_row())

    assert strategy.status == "active"
    assert strategy.approved_by is not None
    assert strategy.approved_at is not None
    assert strategy.weights.total() == pytest.approx(1.0)


def test_bot_settings_parser_accepts_exact_safe_types() -> None:
    settings = bot_settings_from_row(_bot_settings_row())

    assert settings.enabled is False
    assert settings.mode == "paper"
    assert settings.live_order_allowed is False
    assert settings.deployment_lock is False


@pytest.mark.parametrize(
    "mutation",
    [
        lambda row: row.__setitem__("enabled", "false"),
        lambda row: row.__setitem__("mode", "production"),
        lambda row: row.__setitem__("live_order_allowed", True),
        lambda row: row.__setitem__("deployment_target_sha", "a" * 40),
        lambda row: row.__setitem__("max_daily_loss_pct", float("inf")),
        lambda row: row.__setitem__("max_daily_order_count", True),
        lambda row: row.__setitem__("loop_interval_sec", 3601),
        lambda row: row.pop("deployment_lock"),
    ],
)
def test_bot_settings_parser_rejects_ambiguous_or_unsafe_rows(
    mutation: Callable[[JsonObject], object],
) -> None:
    row = copy.deepcopy(_bot_settings_row())
    mutation(row)

    with pytest.raises(ValueError, match="bot_settings_row_invalid"):
        bot_settings_from_row(row)


def _set_nested_value(
    section: str,
    key: str,
    value: object,
) -> Callable[[JsonObject], None]:
    def mutate(row: JsonObject) -> None:
        cast(dict[str, object], row[section])[key] = value

    return mutate


@pytest.mark.parametrize(
    "mutation",
    [
        _set_nested_value("weights", "technical", float("nan")),
        _set_nested_value("weights", "technical", 10**10_000),
        _set_nested_value("weights", "technical", 0.9),
        _set_nested_value("weights", "unexpected", 0.0),
        _set_nested_value("params", "buy_threshold", 0.1),
        lambda row: row.__setitem__("approved_by", None),
        lambda row: row.__setitem__("approved_at", None),
        lambda row: row.__setitem__("approved_at", "2026-07-12T00:00:00"),
    ],
)
def test_strategy_row_parser_rejects_malformed_or_unapproved_active_rows(
    mutation: Callable[[JsonObject], None],
) -> None:
    row = copy.deepcopy(_active_row())
    mutation(row)

    with pytest.raises(ValueError, match="strategy_version_row_invalid"):
        strategy_version_from_row(row)


def _active_row() -> JsonObject:
    return {
        "id": str(uuid4()),
        "version": "strategy_v1_weighted_factor",
        "version_name": "strategy_v1_weighted_factor",
        "status": "active",
        "strategy_type": "WeightedFactorStrategyV1",
        "weights": {
            "technical": 0.35,
            "fundamental": 0.25,
            "market_sector": 0.15,
            "news_event": 0.15,
            "portfolio": 0.10,
        },
        "params": {"buy_threshold": 0.68, "sell_threshold": 0.25},
        "approved_by": str(uuid4()),
        "approved_at": "2026-07-12T00:00:00Z",
    }


def _bot_settings_row() -> JsonObject:
    return {
        "id": "singleton",
        "enabled": False,
        "mode": "paper",
        "live_order_allowed": False,
        "deployment_lock": False,
        "deployment_target_sha": None,
        "max_order_amount_krw": 100_000,
        "max_daily_loss_pct": 0.02,
        "max_daily_order_count": 10,
        "max_position_pct": 0.1,
        "max_sector_pct": 0.3,
        "loop_interval_sec": 30,
    }
