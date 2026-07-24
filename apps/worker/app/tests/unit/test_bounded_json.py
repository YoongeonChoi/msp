from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from app.infrastructure.bounded_json import (
    BoundedJsonError,
    bounded_json_response,
)


class ChunkedStream(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk


async def test_bounded_json_reads_unconsumed_identity_stream_at_exact_limit() -> None:
    body = b'{"status":"ok"}'
    response = httpx.Response(
        200,
        headers={"content-encoding": "identity"},
        stream=ChunkedStream(body[:5], body[5:]),
    )
    try:
        result = await bounded_json_response(response, max_bytes=len(body))
    finally:
        await response.aclose()

    assert result == {"status": "ok"}


async def test_bounded_json_rejects_stream_before_crossing_limit() -> None:
    response = httpx.Response(
        200,
        stream=ChunkedStream(b'{"status":', b'"ok"}'),
    )
    try:
        with pytest.raises(BoundedJsonError, match="response_too_large"):
            await bounded_json_response(response, max_bytes=10)
    finally:
        await response.aclose()


@pytest.mark.parametrize(
    "content_length",
    ["invalid", "9" * 10_000],
)
async def test_bounded_json_rejects_invalid_or_unbounded_content_length(
    content_length: str,
) -> None:
    response = httpx.Response(
        200,
        headers={"content-length": content_length},
        stream=ChunkedStream(b"{}"),
    )
    try:
        with pytest.raises(BoundedJsonError):
            await bounded_json_response(response, max_bytes=64)
    finally:
        await response.aclose()


async def test_bounded_json_rejects_duplicate_keys_in_nested_objects() -> None:
    response = httpx.Response(
        200,
        content=b'{"outer":{"key":1,"key":2}}',
    )

    with pytest.raises(BoundedJsonError, match="duplicate_key"):
        await bounded_json_response(response, max_bytes=64)


@pytest.mark.parametrize(
    "body",
    [
        b'{"value":NaN}',
        b"[" * 10_000 + b"0" + b"]" * 10_000,
    ],
    ids=["nonstandard-constant", "overnested"],
)
async def test_bounded_json_rejects_nonstandard_or_overnested_json(
    body: bytes,
) -> None:
    response = httpx.Response(200, content=body)

    with pytest.raises(BoundedJsonError):
        await bounded_json_response(response, max_bytes=len(body))


async def test_bounded_json_rejects_non_identity_before_reading_body() -> None:
    response = httpx.Response(
        200,
        headers={"content-encoding": "gzip"},
        stream=ChunkedStream(b"not-read"),
    )
    try:
        with pytest.raises(BoundedJsonError, match="content_encoding_invalid"):
            await bounded_json_response(response, max_bytes=64)
    finally:
        await response.aclose()
