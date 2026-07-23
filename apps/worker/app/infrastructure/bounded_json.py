from __future__ import annotations

import json

import httpx


class BoundedJsonError(ValueError):
    """Signal an untrusted JSON response that is unsafe to interpret."""


async def bounded_json_response(
    response: httpx.Response,
    *,
    max_bytes: int,
) -> object:
    """Read one identity-encoded JSON response within an explicit byte bound."""

    if type(max_bytes) is not int or max_bytes <= 0:
        raise BoundedJsonError("bounded_json_limit_invalid")
    encoding = response.headers.get("content-encoding", "").strip().lower()
    if encoding not in {"", "identity"}:
        raise BoundedJsonError("bounded_json_content_encoding_invalid")
    _validate_content_length(response, max_bytes=max_bytes)

    body = bytearray()
    try:
        if response.is_stream_consumed:
            if len(response.content) > max_bytes:
                raise BoundedJsonError("bounded_json_response_too_large")
            body.extend(response.content)
        else:
            async for chunk in response.aiter_raw():
                if len(body) + len(chunk) > max_bytes:
                    raise BoundedJsonError("bounded_json_response_too_large")
                body.extend(chunk)
        return json.loads(
            body,
            object_pairs_hook=_json_object_without_duplicates,
            parse_constant=_reject_nonstandard_constant,
        )
    except BoundedJsonError:
        raise
    except (
        RecursionError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        raise BoundedJsonError("bounded_json_invalid") from exc


def _validate_content_length(
    response: httpx.Response,
    *,
    max_bytes: int,
) -> None:
    raw_value = response.headers.get("content-length")
    if raw_value is None:
        return
    if not raw_value.isascii() or not raw_value.isdigit():
        raise BoundedJsonError("bounded_json_content_length_invalid")
    normalized = raw_value.lstrip("0") or "0"
    maximum = str(max_bytes)
    if len(normalized) > len(maximum) or (len(normalized) == len(maximum) and normalized > maximum):
        raise BoundedJsonError("bounded_json_response_too_large")


def _json_object_without_duplicates(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise BoundedJsonError("bounded_json_duplicate_key")
        result[key] = value
    return result


def _reject_nonstandard_constant(_value: str) -> None:
    raise BoundedJsonError("bounded_json_nonstandard_constant")
