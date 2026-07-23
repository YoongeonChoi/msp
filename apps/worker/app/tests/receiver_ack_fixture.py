from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from datetime import UTC, datetime

import httpx

from app.infrastructure.authenticated_webhook import ReceiverAckKeyRing

TEST_ACK_NOW = datetime(2026, 7, 23, 9, 0, tzinfo=UTC)
TEST_CURRENT_KEY = b"c" * 32
TEST_PREVIOUS_KEY = b"p" * 32
TEST_CURRENT_KEY_B64 = base64.b64encode(TEST_CURRENT_KEY).decode("ascii")
TEST_PREVIOUS_KEY_B64 = base64.b64encode(TEST_PREVIOUS_KEY).decode("ascii")


def receiver_key_ring_fixture() -> ReceiverAckKeyRing:
    return ReceiverAckKeyRing.from_base64(
        current_key_id="test-current",
        current_key_b64=TEST_CURRENT_KEY_B64,
        previous_key_id="test-previous",
        previous_key_b64=TEST_PREVIOUS_KEY_B64,
    )


def signed_receiver_response(
    request: httpx.Request,
    *,
    context: str,
    binding: Mapping[str, str],
    status_code: int,
    body: bytes = b"",
    key_id: str = "test-current",
    key: bytes = TEST_CURRENT_KEY,
    acknowledged_at: datetime = TEST_ACK_NOW,
) -> httpx.Response:
    timestamp = str(int(acknowledged_at.timestamp()))
    material = _json_bytes(
        {
            "acknowledged_at": timestamp,
            "binding": dict(binding),
            "context": context,
            "key_id": key_id,
            "method": "POST",
            "protocol": "kr-auto-trading-lab.receiver-ack.v1",
            "request_sha256": hashlib.sha256(request.content).hexdigest(),
            "response_sha256": hashlib.sha256(body).hexdigest(),
            "status": status_code,
            "target": str(request.url),
            "version": 1,
        }
    )
    signature = base64.b64encode(hmac.new(key, material, hashlib.sha256).digest()).decode("ascii")
    return httpx.Response(
        status_code,
        content=body,
        headers={
            "X-Receiver-Ack-Version": "1",
            "X-Receiver-Ack-Key-Id": key_id,
            "X-Receiver-Ack-Timestamp": timestamp,
            "X-Receiver-Ack-Signature": signature,
        },
        request=request,
    )


def json_response_body(value: object) -> bytes:
    return _json_bytes(value)


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
