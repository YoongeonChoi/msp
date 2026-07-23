from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Callable
from datetime import UTC, datetime

import httpx

from app.infrastructure.authenticated_webhook import ReceiverAckKeyRing

_DRILL_KEY_ID = "local-drill-v1"
_DRILL_KEY = hashlib.sha256(b"kr-auto-trading-lab-local-drill-v1").digest()


def drill_receiver_key_ring() -> ReceiverAckKeyRing:
    """Return a process-local key ring used only by the no-network drill receiver."""

    return ReceiverAckKeyRing.from_base64(
        current_key_id=_DRILL_KEY_ID,
        current_key_b64=base64.b64encode(_DRILL_KEY).decode("ascii"),
    )


def signed_legacy_alert_handler(
    requests: list[httpx.Request] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Build a no-network receiver that independently signs the exact request."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method != "POST":
            raise ValueError("drill_receiver_method_is_invalid")
        if request.headers.get("X-Receiver-Ack-Context") != "legacy_alert":
            raise ValueError("drill_receiver_context_is_invalid")
        if request.headers.get("X-Receiver-Ack-Requested-Key-Id") != _DRILL_KEY_ID:
            raise ValueError("drill_receiver_key_id_is_invalid")
        payload = json.loads(request.content)
        if not isinstance(payload, dict):
            raise ValueError("drill_receiver_payload_is_invalid")
        binding: dict[str, str] = {}
        for name in ("level", "component", "message"):
            value = payload.get(name)
            if not isinstance(value, str) or not value:
                raise ValueError("drill_receiver_payload_is_invalid")
            binding[name] = value
        if requests is not None:
            requests.append(request)
        body = b""
        status = 204
        timestamp = str(int(datetime.now(UTC).timestamp()))
        material = _canonical_json_bytes(
            {
                "acknowledged_at": timestamp,
                "binding": binding,
                "context": "legacy_alert",
                "key_id": _DRILL_KEY_ID,
                "method": "POST",
                "protocol": "kr-auto-trading-lab.receiver-ack.v1",
                "request_sha256": hashlib.sha256(request.content).hexdigest(),
                "response_sha256": hashlib.sha256(body).hexdigest(),
                "status": status,
                "target": str(request.url),
                "version": 1,
            }
        )
        signature = base64.b64encode(
            hmac.new(_DRILL_KEY, material, hashlib.sha256).digest()
        ).decode("ascii")
        return httpx.Response(
            status,
            content=body,
            headers={
                "X-Receiver-Ack-Version": "1",
                "X-Receiver-Ack-Key-Id": _DRILL_KEY_ID,
                "X-Receiver-Ack-Timestamp": timestamp,
                "X-Receiver-Ack-Signature": signature,
            },
            request=request,
        )

    return handler


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
