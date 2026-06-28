from __future__ import annotations

SENSITIVE_TOKENS = ("key", "secret", "token", "password", "credential", "authorization")


def redact_value(value: object) -> object:
    if value is None:
        return None
    return "<redacted>"


def redact_nested(value: object) -> object:
    if isinstance(value, dict):
        return redact_mapping(value)
    if isinstance(value, list):
        return [redact_nested(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_nested(item) for item in value)
    return value


def redact_mapping(data: dict[str, object]) -> dict[str, object]:
    redacted: dict[str, object] = {}
    for key, value in data.items():
        lowered = key.lower()
        if any(token in lowered for token in SENSITIVE_TOKENS):
            redacted[key] = redact_value(value)
        else:
            redacted[key] = redact_nested(value)
    return redacted
