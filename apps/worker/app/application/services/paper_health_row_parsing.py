from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, time, timedelta, timezone

from app.application.services.paper_health_models import EngineEventSummary, ProviderHealthSummary
from app.domain.common.json import JsonObject, JsonValue

KST = timezone(timedelta(hours=9))
PROVIDER_DETAIL_KEYS = ("error_type", "reason", "status", "code")


def latest_providers(rows: Sequence[JsonObject]) -> tuple[ProviderHealthSummary, ...]:
    by_provider: dict[str, ProviderHealthSummary] = {}
    for row in rows:
        provider = string_value(row.get("provider"))
        if provider is None or provider in by_provider:
            continue
        by_provider[provider] = ProviderHealthSummary(
            provider=provider,
            healthy=bool_value(row.get("healthy")),
            status=string_value(row.get("status")) or "unknown",
            checked_at=datetime_value(row.get("checked_at")),
            detail_summary=provider_detail_summary(row),
        )
    return tuple(by_provider.values())


def event_summaries(rows: Sequence[JsonObject]) -> tuple[EngineEventSummary, ...]:
    return tuple(
        EngineEventSummary(
            level=string_value(row.get("level")) or "unknown",
            component=string_value(row.get("component")) or "unknown",
            message=string_value(row.get("message")) or "",
            created_at=datetime_value(row.get("created_at")),
        )
        for row in rows
    )


def count_by(rows: Sequence[JsonObject], key: str) -> Mapping[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = string_value(row.get(key))
        if value is not None:
            counts[value] = counts.get(value, 0) + 1
    return counts


def duplicate_idempotency_keys(rows: Sequence[JsonObject]) -> tuple[str, ...]:
    counts: dict[str, int] = {}
    for row in rows:
        key = string_value(row.get("idempotency_key"))
        if key:
            counts[key] = counts.get(key, 0) + 1
    return tuple(sorted(key for key, count in counts.items() if count > 1))


def missing_key_count(rows: Sequence[JsonObject]) -> int:
    return sum(1 for row in rows if not string_value(row.get("idempotency_key")))


def missing_optional_json_count(rows: Sequence[JsonObject], keys: Sequence[str]) -> int | None:
    if not any(any(key in row for key in keys) for row in rows):
        return None
    return sum(1 for row in rows if is_missing(first_present(row, keys)))


def missing_optional_outcomes(
    orders: Sequence[JsonObject],
    outcomes: Sequence[JsonObject],
    now: datetime,
) -> int:
    outcome_order_ids = {string_value(row.get("order_id")) for row in outcomes}
    due_before = now - timedelta(hours=20)
    count = 0
    for order in orders:
        order_id = string_value(order.get("id"))
        if order_id is None or order_id in outcome_order_ids:
            continue
        created_at = datetime_value(order.get("created_at"))
        if created_at is not None and created_at < due_before:
            count += 1
    return count


def age_seconds(row: JsonObject | None, key: str, now: datetime) -> int | None:
    if row is None:
        return None
    value = datetime_value(row.get(key))
    if value is None:
        return None
    return max(0, int((now - value).total_seconds()))


def first_present(row: Mapping[str, JsonValue], keys: Sequence[str]) -> JsonValue:
    for key in keys:
        if key in row:
            return row[key]
    return None


def is_missing(value: JsonValue) -> bool:
    return value is None or value == "" or value == {} or value == []


def is_market_hours(now: datetime) -> bool:
    kst_now = now.astimezone(KST)
    return kst_now.weekday() < 5 and time(9, 0) <= kst_now.time() <= time(15, 30)


def utc_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    return now.astimezone(UTC)


def datetime_value(value: JsonValue | None) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def string_value(value: JsonValue | None) -> str | None:
    if isinstance(value, str):
        return value
    return None


def bool_value(value: JsonValue | None) -> bool:
    return value is True


def provider_detail_summary(row: JsonObject) -> str | None:
    details = row.get("details")
    if not isinstance(details, Mapping):
        return None
    items: list[str] = []
    for key in PROVIDER_DETAIL_KEYS:
        value = details.get(key)
        if isinstance(value, str) and value:
            safe_value = provider_detail_value(value)
            if safe_value is not None:
                items.append(f"{key}={safe_value}")
        elif isinstance(value, (bool, int, float)) and not isinstance(value, bool):
            items.append(f"{key}={value}")
    if not items:
        return None
    return " ".join(items)


def provider_detail_value(value: str) -> str | None:
    compact = " ".join(value.split())
    if not compact:
        return None
    return compact[:160]
