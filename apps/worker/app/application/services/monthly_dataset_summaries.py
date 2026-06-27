from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass

from app.application.services.monthly_dataset_utils import (
    average,
    count_by_key,
    known_fields,
    number_value,
    numbers,
    numeric_summary,
    redact_json,
    string_value,
    sum_numbers,
    top_counts,
)
from app.domain.common.json import JsonObject, JsonValue
from app.domain.strategy.research import MonthlyResearchRows, MonthPeriod

LIVE_LIKE_ORDER_STATUSES = {"sent", "filled", "partial_filled"}


@dataclass(frozen=True, slots=True)
class SymbolReturn:
    symbol: str
    return_20d: float


def build_monthly_payload(period: MonthPeriod, rows: MonthlyResearchRows) -> JsonObject:
    payload: JsonObject = {
        "month": period.value,
        "base_strategy_version": rows.base_strategy_version,
        "decision_count_by_action": count_by_key(rows.decisions, "action"),
        "order_count_by_status": count_by_key(rows.orders, "status"),
        "outcome_return_summaries": _summarize_outcome_returns(rows.outcomes),
        "top_winning_symbols": _top_symbol_returns(rows.outcomes, descending=True),
        "top_losing_symbols": _top_symbol_returns(rows.outcomes, descending=False),
        "risk_block_reason_counts": _risk_block_reason_counts(rows),
        "provider_health_summary": _summarize_api_health(rows.api_health),
        "news_sentiment_distribution": count_by_key(rows.news_events, "sentiment"),
        "news_event_distribution": count_by_key(rows.news_events, "event_type"),
        "strategy_version_performance": _summarize_strategy_performance(rows),
        "backtest_summary": _summarize_backtests(rows.backtest_runs),
        "data_quality_warnings": _data_quality_warnings(rows),
        "decision_summary": _summarize_decisions(rows.decisions),
        "outcome_summary": _summarize_outcomes(rows.outcomes),
        "order_summary": _summarize_orders(rows.orders),
        "news_events": [_safe_news_event(row) for row in rows.news_events[:100]],
        "features_daily_summary": _summarize_features(rows.features_daily),
        "safety_constraints": _safety_constraints(),
    }
    safe_payload = redact_json(payload)
    return safe_payload if isinstance(safe_payload, dict) else {}


def _summarize_decisions(rows: list[JsonObject]) -> JsonObject:
    return {
        "count": len(rows),
        "action_counts": count_by_key(rows, "action"),
        "top_symbols": top_counts(
            Counter(string_value(row, "symbol", "unknown") for row in rows)
        ),
        "average_final_score": average(numbers(rows, "final_score")),
    }


def _summarize_outcomes(rows: list[JsonObject]) -> JsonObject:
    returns = numbers(rows, "return_pct")
    pnls = numbers(rows, "pnl_krw") + numbers(rows, "realized_pnl_krw")
    return {
        "count": len(rows),
        "average_return_pct": average(returns),
        "total_pnl_krw": sum_numbers(pnls),
    }


def _summarize_orders(rows: list[JsonObject]) -> JsonObject:
    total_paper_amount = 0.0
    for row in rows:
        status = string_value(row, "status", "unknown")
        if status in {"paper", "proposed", "blocked"}:
            total_paper_amount += number_value(row, "amount_krw") or 0.0
    return {
        "count": len(rows),
        "status_counts": count_by_key(rows, "status"),
        "side_counts": count_by_key(rows, "side"),
        "paper_amount_krw": round(total_paper_amount, 2),
    }


def _summarize_outcome_returns(rows: list[JsonObject]) -> JsonObject:
    return {
        "return_1d": numeric_summary(numbers(rows, "return_1d")),
        "return_5d": numeric_summary(numbers(rows, "return_5d")),
        "return_20d": numeric_summary(numbers(rows, "return_20d")),
        "max_drawdown_20d": numeric_summary(numbers(rows, "max_drawdown_20d")),
        "realized_pnl_krw": numeric_summary(numbers(rows, "realized_pnl_krw")),
    }


def _top_symbol_returns(rows: list[JsonObject], descending: bool) -> list[JsonValue]:
    returns = [
        SymbolReturn(symbol=string_value(row, "symbol", "unknown"), return_20d=value)
        for row in rows
        if (value := number_value(row, "return_20d")) is not None
        and _matches_return_direction(value, descending)
    ]
    ordered = sorted(returns, key=lambda item: item.return_20d, reverse=descending)
    return [
        {"symbol": item.symbol, "return_20d": round(item.return_20d, 6)}
        for item in ordered[:5]
    ]


def _matches_return_direction(value: float, descending: bool) -> bool:
    if descending:
        return value > 0
    return value < 0


def _risk_block_reason_counts(rows: MonthlyResearchRows) -> JsonObject:
    counts: Counter[str] = Counter()
    for row in [*rows.orders, *rows.decisions]:
        for reason in _extract_reasons(row):
            counts[reason] += 1
    return dict(counts)


def _extract_reasons(row: JsonObject) -> list[str]:
    reasons: list[str] = []
    for field in ("risk_result", "risk_snapshot", "reason_json"):
        reasons.extend(_reason_strings(row.get(field)))
    reason = row.get("reason")
    if isinstance(reason, str) and reason:
        reasons.append(reason)
    return reasons


def _reason_strings(value: JsonValue | None) -> list[str]:
    match value:
        case {"reasons": list() as reasons}:
            return [str(reason) for reason in reasons if isinstance(reason, str)]
        case {"reason": str() as reason}:
            return [reason]
        case {"policy_results": list() as policies}:
            return _policy_reasons(policies)
        case str() as reason if reason:
            return [reason]
        case _:
            return []


def _policy_reasons(policies: list[JsonValue]) -> list[str]:
    reasons: list[str] = []
    for policy in policies:
        match policy:
            case {"reason": str() as reason}:
                reasons.append(reason)
            case {"reasons": list() as nested}:
                reasons.extend(str(reason) for reason in nested if isinstance(reason, str))
            case _:
                continue
    return reasons


def _summarize_api_health(rows: list[JsonObject]) -> JsonObject:
    grouped: dict[str, list[JsonObject]] = defaultdict(list)
    for row in rows:
        grouped[string_value(row, "provider", "unknown")].append(row)
    return {
        provider: {
            "checks": len(provider_rows),
            "healthy_checks": sum(1 for row in provider_rows if row.get("healthy") is True),
            "degraded_checks": sum(1 for row in provider_rows if row.get("healthy") is False),
            "latest_status": string_value(provider_rows[-1], "status", "unknown"),
        }
        for provider, provider_rows in sorted(grouped.items())
    }


def _summarize_strategy_performance(rows: MonthlyResearchRows) -> JsonObject:
    return {
        "base_strategy_version": rows.base_strategy_version,
        "decision_count": len(rows.decisions),
        "order_count": len(rows.orders),
        "average_final_score": average(numbers(rows.decisions, "final_score")),
        "outcome_return_summaries": _summarize_outcome_returns(rows.outcomes),
    }


def _summarize_backtests(rows: list[JsonObject]) -> JsonObject:
    return {"count": len(rows), "latest": _latest_backtest(rows)}


def _latest_backtest(rows: list[JsonObject]) -> JsonObject | None:
    if not rows:
        return None
    latest = sorted(rows, key=lambda row: string_value(row, "created_at", ""), reverse=True)[0]
    return known_fields(
        latest,
        (
            "strategy",
            "period_start",
            "period_end",
            "total_return",
            "cagr",
            "max_drawdown",
            "win_rate",
            "turnover",
        ),
    )


def _safe_news_event(row: JsonObject) -> JsonObject:
    safe_event = redact_json(
        {
            "symbol": string_value(row, "symbol", "unknown"),
            "title": string_value(row, "title", ""),
            "source": string_value(row, "source", ""),
            "published_at": string_value(row, "published_at", ""),
            "sentiment": string_value(row, "sentiment", "unknown"),
            "event_type": string_value(row, "event_type", "other"),
            "risk_level": string_value(row, "risk_level", "unknown"),
            "summary_short": string_value(row, "summary_short", ""),
            "trading_relevance": number_value(row, "trading_relevance"),
            "confidence": number_value(row, "confidence"),
        }
    )
    return safe_event if isinstance(safe_event, dict) else {}


def _summarize_features(rows: list[JsonObject]) -> JsonObject:
    grouped: dict[str, list[JsonObject]] = defaultdict(list)
    for row in rows:
        grouped[string_value(row, "symbol", "unknown")].append(row)
    return {
        symbol: {
            "days": len(symbol_rows),
            "avg_r_1d": average(numbers(symbol_rows, "r_1d")),
            "avg_r_5d": average(numbers(symbol_rows, "r_5d")),
            "avg_r_20d": average(numbers(symbol_rows, "r_20d")),
            "avg_volatility_20": average(numbers(symbol_rows, "volatility_20")),
            "avg_rsi_14": average(numbers(symbol_rows, "rsi_14")),
            "avg_turnover_krw": average(numbers(symbol_rows, "turnover_krw")),
        }
        for symbol, symbol_rows in sorted(grouped.items())
    }


def _data_quality_warnings(rows: MonthlyResearchRows) -> list[JsonValue]:
    warnings: list[JsonValue] = []
    if not rows.decisions:
        warnings.append("no_decisions")
    if rows.decisions and not rows.outcomes:
        warnings.append("no_outcomes_for_decisions")
    if rows.outcomes and len(rows.outcomes) < len(rows.decisions):
        warnings.append("outcomes_less_than_decisions")
    if not rows.features_daily:
        warnings.append("no_features_daily")
    if not rows.backtest_runs:
        warnings.append("no_backtest_runs")
    warnings.extend(_provider_health_warnings(rows.api_health))
    if _count_live_like_orders(rows.orders) > 0:
        warnings.append("live_like_order_status_detected")
    return warnings


def _provider_health_warnings(rows: list[JsonObject]) -> list[JsonValue]:
    providers = {
        string_value(row, "provider", "unknown")
        for row in rows
        if row.get("healthy") is False
    }
    return [f"provider_health_degraded:{provider}" for provider in sorted(providers)]


def _count_live_like_orders(rows: list[JsonObject]) -> int:
    return sum(
        1
        for row in rows
        if string_value(row, "status", "unknown") in LIVE_LIKE_ORDER_STATUSES
    )


def _safety_constraints() -> JsonObject:
    return {
        "openai_may_execute_trades": False,
        "openai_may_deploy_strategy": False,
        "openai_may_change_live_order_allowed": False,
        "openai_may_call_broker": False,
        "candidate_status_required": "proposed",
        "approval_required": True,
        "paper_first": True,
    }
