from __future__ import annotations

SENSITIVE_TOKENS = ("key", "secret", "token", "password", "credential", "authorization")


def redact_value(value: object) -> object:
    if value is None:
        return None
    text = str(value)
    if len(text) <= 6:
        return "***"
    return text[:6] + "***"


def redact_mapping(data: dict[str, object]) -> dict[str, object]:
    redacted: dict[str, object] = {}
    for key, value in data.items():
        lowered = key.lower()
        if any(token in lowered for token in SENSITIVE_TOKENS):
            redacted[key] = redact_value(value)
        elif isinstance(value, dict):
            redacted[key] = redact_mapping(value)
        else:
            redacted[key] = value
    return redacted

