from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from app.infrastructure.authenticated_webhook import (
    AuthenticatedWebhookError,
    AuthenticatedWebhookTransport,
    ReceiverAckKeyRing,
    validate_webhook_target,
)

NOW = datetime(2026, 7, 23, 9, 0, tzinfo=UTC)
CURRENT_KEY = b"c" * 32
PREVIOUS_KEY = b"p" * 32
CURRENT_KEY_B64 = base64.b64encode(CURRENT_KEY).decode("ascii")
PREVIOUS_KEY_B64 = base64.b64encode(PREVIOUS_KEY).decode("ascii")


@pytest.mark.parametrize(
    "value",
    [
        "",
        " https://alerts.example.test/events",
        "https://alerts.example.test/events ",
        "https://alerts.example.test/line\nbreak",
        "https://alerts.example.test/has\\backslash",
        "http://alerts.example.test/events",
        "http://127.0.0.1/events",
        "//alerts.example.test/events",
        "/relative/events",
        "https:///missing-host",
        "https://user@alerts.example.test/events",
        "https://user:pass@alerts.example.test/events",
        "https://alerts.example.test/events#fragment",
        "https://alerts.example.test/\x80control",
        "https://alerts.example.test/\x9fcontrol",
        "https://alerts.example.test:0/events",
        "https://alerts.example.test:65536/events",
        "https://alerts.example.test:not-a-port/events",
    ],
)
def test_webhook_target_rejects_ambiguous_or_insecure_urls(value: str) -> None:
    with pytest.raises(AuthenticatedWebhookError, match="webhook_url_is_invalid"):
        validate_webhook_target(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://alerts.example.test", "https://alerts.example.test/"),
        (
            "HTTPS://Alerts.Example.Test:8443/events/v1?tenant=blue",
            "https://alerts.example.test:8443/events/v1?tenant=blue",
        ),
        (
            "https://alerts.example.test/events/%2Fraw?mode=a%2Fb",
            "https://alerts.example.test/events/%2Fraw?mode=a%2Fb",
        ),
        (
            "https://alerts.example.test:443/events",
            "https://alerts.example.test/events",
        ),
        (
            "https://alerts.example.test/한글?q=눈",
            "https://alerts.example.test/%ED%95%9C%EA%B8%80?q=%EB%88%88",
        ),
    ],
)
def test_webhook_target_preserves_valid_path_query_and_custom_port(
    value: str,
    expected: str,
) -> None:
    target = validate_webhook_target(value)

    assert target.url == expected
    assert "tenant=blue" not in repr(target)


def test_receiver_key_ring_accepts_exact_current_and_previous_keys() -> None:
    ring = ReceiverAckKeyRing.from_base64(
        current_key_id="receiver-current",
        current_key_b64=CURRENT_KEY_B64,
        previous_key_id="receiver-previous",
        previous_key_b64=PREVIOUS_KEY_B64,
    )

    assert ring.current_key_id == "receiver-current"
    assert ring.accepted_key_ids == ("receiver-current", "receiver-previous")
    assert CURRENT_KEY_B64 not in repr(ring)
    assert PREVIOUS_KEY_B64 not in repr(ring)


def test_receiver_key_ring_direct_construction_cannot_bypass_invariants() -> None:
    with pytest.raises(
        AuthenticatedWebhookError,
        match="receiver_ack_current_key_is_invalid",
    ):
        ReceiverAckKeyRing("current", b"short")

    with pytest.raises(
        AuthenticatedWebhookError,
        match="receiver_ack_previous_key_pair_is_incomplete",
    ):
        ReceiverAckKeyRing("current", CURRENT_KEY, "previous", None)

    with pytest.raises(
        AuthenticatedWebhookError,
        match="receiver_ack_current_key_id_is_invalid",
    ):
        ReceiverAckKeyRing(1, CURRENT_KEY)  # type: ignore[arg-type]

    with pytest.raises(
        AuthenticatedWebhookError,
        match="receiver_ack_previous_key_id_is_invalid",
    ):
        ReceiverAckKeyRing(
            "current",
            CURRENT_KEY,
            2,  # type: ignore[arg-type]
            PREVIOUS_KEY,
        )


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"current_key_id": None}, "current_key_id_is_invalid"),
        ({"current_key_id": " bad"}, "current_key_id_is_invalid"),
        ({"current_key_id": "a" * 65}, "current_key_id_is_invalid"),
        (
            {"current_key_b64": base64.b64encode(b"x" * 31).decode("ascii")},
            "current_key_is_invalid",
        ),
        (
            {"current_key_b64": base64.b64encode(b"x" * 33).decode("ascii")},
            "current_key_is_invalid",
        ),
        ({"current_key_b64": CURRENT_KEY_B64.rstrip("=")}, "current_key_is_invalid"),
        ({"current_key_b64": f" {CURRENT_KEY_B64}"}, "current_key_is_invalid"),
        ({"previous_key_id": "previous"}, "previous_key_pair_is_incomplete"),
        ({"previous_key_b64": PREVIOUS_KEY_B64}, "previous_key_pair_is_incomplete"),
        (
            {
                "previous_key_id": "current",
                "previous_key_b64": PREVIOUS_KEY_B64,
            },
            "key_ids_must_differ",
        ),
        (
            {
                "previous_key_id": "previous",
                "previous_key_b64": CURRENT_KEY_B64,
            },
            "keys_must_differ",
        ),
    ],
)
def test_receiver_key_ring_rejects_noncanonical_or_ambiguous_material(
    overrides: dict[str, str | None],
    error: str,
) -> None:
    values: dict[str, str | None] = {
        "current_key_id": "current",
        "current_key_b64": CURRENT_KEY_B64,
        "previous_key_id": None,
        "previous_key_b64": None,
    }
    values.update(overrides)

    with pytest.raises(AuthenticatedWebhookError, match=error):
        ReceiverAckKeyRing.from_base64(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("timeout", [True, 0, -1, float("nan"), float("inf"), "5"])
def test_transport_rejects_invalid_timeout_types_and_values(timeout: object) -> None:
    with pytest.raises(
        AuthenticatedWebhookError,
        match="authenticated_webhook_timeout_is_invalid",
    ):
        AuthenticatedWebhookTransport(
            "https://alerts.example.test/events",
            key_ring=_key_ring(),
            timeout_sec=timeout,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("clock_value", [datetime(2026, 7, 23, 9, 0), "not-a-clock"])
async def test_transport_rejects_invalid_clock_values(clock_value: object) -> None:
    def invalid_clock() -> datetime:
        return clock_value  # type: ignore[return-value]

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(500))
    ) as client:
        transport = AuthenticatedWebhookTransport(
            "https://alerts.example.test/events",
            key_ring=_key_ring(),
            client=client,
            clock=invalid_clock,
        )
        with pytest.raises(
            AuthenticatedWebhookError,
            match="authenticated_webhook_clock_is_invalid",
        ):
            await transport.post_json(
                context="legacy_alert",
                payload={"message": "clock-check"},
                binding={"message": "clock-check"},
            )


@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Length": "1"},
        {"Host": "other.example.test"},
        {"X-Receiver-Ack-Version": "1"},
        {"Idempotency-Key": ""},
        {"Idempotency-Key": "a b"},
        {"Idempotency-Key": "x" * 257},
    ],
)
async def test_transport_allows_only_a_bounded_idempotency_header(
    headers: dict[str, str],
) -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = AuthenticatedWebhookTransport(
            "https://alerts.example.test/events",
            key_ring=_key_ring(),
            client=client,
            clock=lambda: NOW,
        )
        with pytest.raises(
            AuthenticatedWebhookError,
            match="authenticated_webhook_request_headers_are_invalid",
        ):
            await transport.post_json(
                context="outbox",
                payload={"event": "header-check"},
                binding={"event": "header-check"},
                headers=headers,
            )

    assert requests == 0


@pytest.mark.parametrize(
    ("key_id", "key"),
    [("current", CURRENT_KEY), ("previous", PREVIOUS_KEY)],
)
async def test_authenticated_transport_accepts_current_and_previous_golden_ack(
    key_id: str,
    key: bytes,
) -> None:
    binding = {
        "dedupe_key": "incident:case-1",
        "destination_type": "incident_alert",
        "outbox_id": "00000000-0000-4000-8000-000000000001",
    }
    response_body = _json_bytes(
        {
            "accepted_dedupe_key": "incident:case-1",
            "immutable_receipt_id": "receipt-1",
            "receipt_sha256": "a" * 64,
        }
    )
    seen_signatures: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        response = _signed_response(
            request,
            body=response_body,
            context="outbox",
            binding=binding,
            key_id=key_id,
            key=key,
            acknowledged_at=_epoch(NOW),
            status_code=202,
        )
        seen_signatures.append(response.headers["X-Receiver-Ack-Signature"])
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = AuthenticatedWebhookTransport(
            "HTTPS://Alerts.Example.Test:8443/events?tenant=blue",
            key_ring=_key_ring(),
            client=client,
            clock=lambda: NOW,
        )
        response = await transport.post_json(
            context="outbox",
            payload={"event": "incident"},
            binding=binding,
            headers={"Idempotency-Key": "incident:case-1"},
        )

    assert response.status_code == 202
    assert response.content == response_body
    assert response.json() == json.loads(response_body)
    if key_id == "current":
        assert seen_signatures == ["Giz26ZtAeSvVH7bQaeMbMgk5JUWMDtmw0EU84Ls/slY="]


@pytest.mark.parametrize(
    "attack",
    [
        "unsigned",
        "duplicate_signature",
        "duplicate_version",
        "duplicate_key",
        "duplicate_timestamp",
        "folded_version",
        "malformed_signature",
        "malformed_timestamp",
        "unknown_key",
        "wrong_mac",
        "wrong_context",
        "wrong_binding",
        "wrong_target",
        "wrong_method",
        "wrong_status",
        "wrong_request_digest",
        "wrong_response_digest",
        "future_timestamp",
        "stale_timestamp",
    ],
)
async def test_authenticated_transport_rejects_unsigned_or_tampered_ack(
    attack: str,
) -> None:
    binding = {
        "dedupe_key": "incident:case-1",
        "destination_type": "incident_alert",
        "outbox_id": "00000000-0000-4000-8000-000000000001",
    }
    body = b'{"ok":true}'

    def handler(request: httpx.Request) -> httpx.Response:
        if attack == "unsigned":
            return httpx.Response(202, content=body, request=request)
        signed_binding = binding | (
            {"outbox_id": "00000000-0000-4000-8000-000000000099"}
            if attack == "wrong_binding"
            else {}
        )
        response = _signed_response(
            request,
            body=(b'{"ok":false}' if attack == "wrong_response_digest" else body),
            signed_response_body=body if attack == "wrong_response_digest" else None,
            context="dead_man" if attack == "wrong_context" else "outbox",
            binding=signed_binding,
            key_id="unknown" if attack == "unknown_key" else "current",
            key=b"z" * 32 if attack == "wrong_mac" else CURRENT_KEY,
            acknowledged_at=(
                _epoch(NOW + timedelta(minutes=2))
                if attack == "future_timestamp"
                else _epoch(NOW - timedelta(minutes=10))
                if attack == "stale_timestamp"
                else _epoch(NOW)
            ),
            status_code=201 if attack == "wrong_status" else 202,
            signed_status_code=202 if attack == "wrong_status" else None,
            method="GET" if attack == "wrong_method" else "POST",
            target=("https://other.example.test/events" if attack == "wrong_target" else None),
            request_digest=("0" * 64 if attack == "wrong_request_digest" else None),
            duplicate_signature=attack == "duplicate_signature",
        )
        duplicate_header = {
            "duplicate_version": b"x-receiver-ack-version",
            "duplicate_key": b"x-receiver-ack-key-id",
            "duplicate_timestamp": b"x-receiver-ack-timestamp",
        }.get(attack)
        if duplicate_header is not None:
            headers = list(response.headers.raw)
            headers.append(next(pair for pair in headers if pair[0].lower() == duplicate_header))
            return httpx.Response(
                response.status_code,
                content=body,
                headers=headers,
                request=request,
            )
        if attack == "folded_version":
            response.headers["X-Receiver-Ack-Version"] = "1, 1"
        elif attack == "malformed_signature":
            response.headers["X-Receiver-Ack-Signature"] = "not-base64"
        elif attack == "malformed_timestamp":
            response.headers["X-Receiver-Ack-Timestamp"] = "01"
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = AuthenticatedWebhookTransport(
            "https://alerts.example.test/events",
            key_ring=_key_ring(),
            client=client,
            clock=lambda: NOW,
        )
        with pytest.raises(
            AuthenticatedWebhookError,
            match="receiver_ack_authentication_failed",
        ):
            await transport.post_json(
                context="outbox",
                payload={"event": "incident"},
                binding=binding,
            )


@pytest.mark.parametrize(
    ("key_id", "key"),
    (("current", CURRENT_KEY), ("previous", PREVIOUS_KEY)),
)
async def test_retry_accepts_old_ack_for_active_key_only_for_same_exact_request(
    key_id: str,
    key: bytes,
) -> None:
    binding = {
        "dedupe_key": "incident:case-1",
        "destination_type": "incident_alert",
        "outbox_id": "00000000-0000-4000-8000-000000000001",
    }

    cached_headers: list[tuple[bytes, bytes]] | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal cached_headers
        if cached_headers is None:
            signed = _signed_response(
                request,
                body=b'{"ok":true}',
                context="outbox",
                binding=binding,
                key_id=key_id,
                key=key,
                acknowledged_at=_epoch(NOW - timedelta(days=2)),
                status_code=202,
            )
            cached_headers = list(signed.headers.raw)
        return httpx.Response(
            202,
            content=b'{"ok":true}',
            headers=cached_headers,
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = AuthenticatedWebhookTransport(
            "https://alerts.example.test/events",
            key_ring=_key_ring(),
            client=client,
            clock=lambda: NOW,
        )
        with pytest.raises(
            AuthenticatedWebhookError,
            match="receiver_ack_authentication_failed",
        ):
            await transport.post_json(
                context="outbox",
                payload={"event": "incident"},
                binding=binding,
            )

        response = await transport.post_json(
            context="outbox",
            payload={"event": "incident"},
            binding=binding,
            allow_stale_ack=True,
        )

        assert response.status_code == 202

        with pytest.raises(
            AuthenticatedWebhookError,
            match="receiver_ack_authentication_failed",
        ):
            await transport.post_json(
                context="outbox",
                payload={"event": "tampered"},
                binding=binding,
                allow_stale_ack=True,
            )


async def test_transport_does_not_follow_redirects() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            302,
            headers={"Location": "https://other.example.test/collect"},
            request=request,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    ) as client:
        transport = AuthenticatedWebhookTransport(
            "https://alerts.example.test/events",
            key_ring=_key_ring(),
            client=client,
            clock=lambda: NOW,
        )
        with pytest.raises(
            AuthenticatedWebhookError,
            match="authenticated_webhook_transport_failed",
        ):
            await transport.post_json(
                context="legacy_alert",
                payload={"message": "test"},
                binding={"message": "test"},
            )

    assert len(requests) == 1


@pytest.mark.parametrize(
    ("target", "expected_target"),
    [
        (
            "https://alerts.example.test:443/events",
            "https://alerts.example.test/events",
        ),
        (
            "https://alerts.example.test/한글?q=눈",
            "https://alerts.example.test/%ED%95%9C%EA%B8%80?q=%EB%88%88",
        ),
    ],
)
async def test_transport_hmac_uses_the_exact_serialized_request_target(
    target: str,
    expected_target: str,
) -> None:
    seen_targets: list[str] = []
    binding = {"message": "target-check"}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_targets.append(str(request.url))
        assert request.headers["Accept-Encoding"] == "identity"
        return _signed_response(
            request,
            body=b"",
            context="legacy_alert",
            binding=binding,
            key_id="current",
            key=CURRENT_KEY,
            acknowledged_at=_epoch(NOW),
            status_code=204,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await AuthenticatedWebhookTransport(
            target,
            key_ring=_key_ring(),
            client=client,
            clock=lambda: NOW,
        ).post_json(
            context="legacy_alert",
            payload={"message": "target-check"},
            binding=binding,
        )

    assert response.status_code == 204
    assert seen_targets == [expected_target]


@pytest.mark.parametrize(
    ("headers", "error"),
    [
        ({"Content-Encoding": "gzip"}, "authenticated_webhook_transport_failed"),
        ({"Content-Length": "not-a-number"}, "response_is_invalid"),
        ({"Content-Length": str(64 * 1024 + 1)}, "response_is_too_large"),
    ],
)
async def test_transport_rejects_ambiguous_or_oversized_response_metadata(
    headers: dict[str, str],
    error: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{}", headers=headers, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AuthenticatedWebhookError, match=error):
            await AuthenticatedWebhookTransport(
                "https://alerts.example.test/events",
                key_ring=_key_ring(),
                client=client,
                clock=lambda: NOW,
            ).post_json(
                context="legacy_alert",
                payload={"message": "metadata-check"},
                binding={"message": "metadata-check"},
            )


async def test_transport_rejects_duplicate_content_length() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"{}",
            headers=[("Content-Length", "2"), ("Content-Length", "2")],
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AuthenticatedWebhookError, match="response_is_invalid"):
            await AuthenticatedWebhookTransport(
                "https://alerts.example.test/events",
                key_ring=_key_ring(),
                client=client,
                clock=lambda: NOW,
            ).post_json(
                context="legacy_alert",
                payload={"message": "duplicate-length"},
                binding={"message": "duplicate-length"},
            )


async def test_transport_enforces_request_and_stream_response_size_limits() -> None:
    requests = 0

    def unused_handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(unused_handler)) as client:
        with pytest.raises(AuthenticatedWebhookError, match="request_is_too_large"):
            await AuthenticatedWebhookTransport(
                "https://alerts.example.test/events",
                key_ring=_key_ring(),
                client=client,
                clock=lambda: NOW,
            ).post_json(
                context="legacy_alert",
                payload={"message": "x" * (256 * 1024)},
                binding={"message": "large"},
            )
    assert requests == 0

    def overflow_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=_ChunkStream((b"x" * (64 * 1024), b"y")),
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(overflow_handler)) as client:
        with pytest.raises(AuthenticatedWebhookError, match="response_is_too_large"):
            await AuthenticatedWebhookTransport(
                "https://alerts.example.test/events",
                key_ring=_key_ring(),
                client=client,
                clock=lambda: NOW,
            ).post_json(
                context="legacy_alert",
                payload={"message": "stream-overflow"},
                binding={"message": "stream-overflow"},
            )


async def test_transport_enforces_total_wall_clock_deadline_for_drip_feed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_SlowStream(), request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(
            AuthenticatedWebhookError,
            match="authenticated_webhook_transport_failed",
        ):
            await AuthenticatedWebhookTransport(
                "https://alerts.example.test/events",
                key_ring=_key_ring(),
                client=client,
                timeout_sec=0.01,
                clock=lambda: NOW,
            ).post_json(
                context="legacy_alert",
                payload={"message": "slow"},
                binding={"message": "slow"},
            )


def _key_ring() -> ReceiverAckKeyRing:
    return ReceiverAckKeyRing.from_base64(
        current_key_id="current",
        current_key_b64=CURRENT_KEY_B64,
        previous_key_id="previous",
        previous_key_b64=PREVIOUS_KEY_B64,
    )


def _signed_response(
    request: httpx.Request,
    *,
    body: bytes,
    context: str,
    binding: dict[str, str],
    key_id: str,
    key: bytes,
    acknowledged_at: str,
    status_code: int,
    signed_status_code: int | None = None,
    signed_response_body: bytes | None = None,
    method: str = "POST",
    target: str | None = None,
    request_digest: str | None = None,
    duplicate_signature: bool = False,
) -> httpx.Response:
    material = _json_bytes(
        {
            "acknowledged_at": acknowledged_at,
            "binding": binding,
            "context": context,
            "key_id": key_id,
            "method": method,
            "protocol": "kr-auto-trading-lab.receiver-ack.v1",
            "request_sha256": request_digest or hashlib.sha256(request.content).hexdigest(),
            "response_sha256": hashlib.sha256(
                signed_response_body if signed_response_body is not None else body
            ).hexdigest(),
            "status": signed_status_code or status_code,
            "target": target or str(request.url),
            "version": 1,
        }
    )
    signature = base64.b64encode(hmac.new(key, material, hashlib.sha256).digest()).decode("ascii")
    response_headers = [
        ("X-Receiver-Ack-Version", "1"),
        ("X-Receiver-Ack-Key-Id", key_id),
        ("X-Receiver-Ack-Timestamp", acknowledged_at),
        ("X-Receiver-Ack-Signature", signature),
    ]
    if duplicate_signature:
        response_headers.append(("X-Receiver-Ack-Signature", signature))
    return httpx.Response(
        status_code,
        content=body,
        headers=response_headers,
        request=request,
    )


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _epoch(value: datetime) -> str:
    return str(int(value.timestamp()))


class _ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        return None


class _SlowStream(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        await asyncio.sleep(0.05)
        yield b"{}"

    async def aclose(self) -> None:
        return None
