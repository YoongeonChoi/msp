from __future__ import annotations

from collections import Counter

from app.domain.common.json import JsonObject, JsonValue

SENSITIVE_KEY_TOKENS = (
    "account",
    "api_key",
    "apikey",
    "authorization",
    "client_id",
    "client_secret",
    "credential",
    "password",
    "private_key",
    "secret",
    "service_role",
    "token",
)
NEWS_BODY_KEYS = ("body", "content", "article", "raw_body", "raw_content", "full_text")


def redact_json(value: JsonValue) -> JsonValue:
    match value:
        case list() as items:
            return [redact_json(item) for item in items]
        case dict() as mapping:
            return {
                key: "[REDACTED]" if _redacts_key(key) else redact_json(item)
                for key, item in mapping.items()
            }
        case str() as text:
            return _redact_string(text)
        case int() | float() | bool() | None:
            return value


def count_by_key(rows: list[JsonObject], key: str) -> JsonObject:
    return dict(Counter(string_value(row, key, "unknown") for row in rows))


def known_fields(row: JsonObject, fields: tuple[str, ...]) -> JsonObject:
    return {field: row[field] for field in fields if row.get(field) is not None}


def numeric_summary(values: list[float]) -> JsonObject:
    if not values:
        return {"count": 0, "average": None, "min": None, "max": None}
    return {
        "count": len(values),
        "average": average(values),
        "min": round(min(values), 6),
        "max": round(max(values), 6),
    }


def string_value(row: JsonObject, key: str, default: str) -> str:
    value = row.get(key)
    match value:
        case str() as text:
            return text
        case None:
            return default
        case _:
            return str(value)


def number_value(row: JsonObject, key: str) -> float | None:
    value = row.get(key)
    match value:
        case bool() | None:
            return None
        case int() | float() as number:
            return float(number)
        case str() as text:
            return _parse_float(text)
        case _:
            return None


def numbers(rows: list[JsonObject], key: str) -> list[float]:
    return [value for value in (number_value(row, key) for row in rows) if value is not None]


def average(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 6)


def sum_numbers(values: list[float]) -> float:
    return round(sum(values), 2)


def top_counts(counts: Counter[str]) -> list[JsonValue]:
    return [{"key": key, "count": count} for key, count in counts.most_common(10)]


def _redacts_key(key: str) -> bool:
    lowered = key.lower()
    return any(token in lowered for token in SENSITIVE_KEY_TOKENS + NEWS_BODY_KEYS)


def _redact_string(value: str) -> str:
    if value.startswith(("sk-", "Bearer ")):
        return "[REDACTED]"
    if value.isdigit() and len(value) >= 8:
        return "[REDACTED]"
    return value


def _parse_float(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None
