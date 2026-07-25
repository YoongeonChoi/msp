from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import math
import re
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.application.services.scheduler_invocation_deadline import (
    SchedulerInvocationEffectAuthorization,
    require_scheduler_invocation_effect_authorization,
)

ReceiverAckContext = Literal["legacy_alert", "outbox", "dead_man"]

_PROTOCOL = "kr-auto-trading-lab.receiver-ack.v1"
_VERSION = "1"
_KEY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_HOST_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_ACK_TIMESTAMP_RE = re.compile(r"^(?:0|[1-9][0-9]{0,10})$")
_IDEMPOTENCY_KEY_RE = re.compile(r"^[!-~]{1,256}$")
_HEADER_VERSION = b"x-receiver-ack-version"
_HEADER_KEY_ID = b"x-receiver-ack-key-id"
_HEADER_TIMESTAMP = b"x-receiver-ack-timestamp"
_HEADER_SIGNATURE = b"x-receiver-ack-signature"
_RESPONSE_MAX_BYTES = 64 * 1024
_REQUEST_MAX_BYTES = 256 * 1024
_MAX_ACK_AGE = timedelta(minutes=5)
_MAX_FUTURE_SKEW = timedelta(seconds=30)


class AuthenticatedWebhookError(ValueError):
    """Fixed-code failure at the authenticated receiver boundary."""


@dataclass(frozen=True, slots=True, repr=False)
class ValidatedWebhookTarget:
    url: str


@dataclass(frozen=True, slots=True, repr=False)
class ReceiverAckKeyRing:
    current_key_id: str
    _current_key: bytes = field(repr=False)
    previous_key_id: str | None = None
    _previous_key: bytes | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if (
            type(self.current_key_id) is not str
            or _KEY_ID_RE.fullmatch(self.current_key_id) is None
        ):
            raise AuthenticatedWebhookError("receiver_ack_current_key_id_is_invalid")
        if type(self._current_key) is not bytes or len(self._current_key) != 32:
            raise AuthenticatedWebhookError("receiver_ack_current_key_is_invalid")
        if (self.previous_key_id is None) != (self._previous_key is None):
            raise AuthenticatedWebhookError("receiver_ack_previous_key_pair_is_incomplete")
        if self.previous_key_id is None or self._previous_key is None:
            return
        if (
            type(self.previous_key_id) is not str
            or _KEY_ID_RE.fullmatch(self.previous_key_id) is None
        ):
            raise AuthenticatedWebhookError("receiver_ack_previous_key_id_is_invalid")
        if self.previous_key_id == self.current_key_id:
            raise AuthenticatedWebhookError("receiver_ack_key_ids_must_differ")
        if type(self._previous_key) is not bytes or len(self._previous_key) != 32:
            raise AuthenticatedWebhookError("receiver_ack_previous_key_is_invalid")
        if hmac.compare_digest(self._previous_key, self._current_key):
            raise AuthenticatedWebhookError("receiver_ack_keys_must_differ")

    @classmethod
    def from_base64(
        cls,
        *,
        current_key_id: str,
        current_key_b64: str,
        previous_key_id: str | None = None,
        previous_key_b64: str | None = None,
    ) -> ReceiverAckKeyRing:
        if type(current_key_id) is not str or _KEY_ID_RE.fullmatch(current_key_id) is None:
            raise AuthenticatedWebhookError("receiver_ack_current_key_id_is_invalid")
        current_key = _decode_key(
            current_key_b64,
            error_code="receiver_ack_current_key_is_invalid",
        )
        if (previous_key_id is None) != (previous_key_b64 is None):
            raise AuthenticatedWebhookError("receiver_ack_previous_key_pair_is_incomplete")
        if previous_key_id is None or previous_key_b64 is None:
            return cls(current_key_id, current_key)
        if type(previous_key_id) is not str or _KEY_ID_RE.fullmatch(previous_key_id) is None:
            raise AuthenticatedWebhookError("receiver_ack_previous_key_id_is_invalid")
        previous_key = _decode_key(
            previous_key_b64,
            error_code="receiver_ack_previous_key_is_invalid",
        )
        if previous_key_id == current_key_id:
            raise AuthenticatedWebhookError("receiver_ack_key_ids_must_differ")
        if hmac.compare_digest(previous_key, current_key):
            raise AuthenticatedWebhookError("receiver_ack_keys_must_differ")
        return cls(
            current_key_id,
            current_key,
            previous_key_id,
            previous_key,
        )

    @property
    def accepted_key_ids(self) -> tuple[str, ...]:
        if self.previous_key_id is None:
            return (self.current_key_id,)
        return (self.current_key_id, self.previous_key_id)

    def key_for_id(self, key_id: str) -> bytes | None:
        if key_id == self.current_key_id:
            return self._current_key
        if key_id == self.previous_key_id:
            return self._previous_key
        return None


@dataclass(frozen=True, slots=True)
class AuthenticatedWebhookResponse:
    status_code: int
    content: bytes = field(repr=False)

    def json(self) -> object:
        try:
            return json.loads(
                self.content,
                object_pairs_hook=_json_object_without_duplicates,
                parse_constant=_reject_nonstandard_constant,
            )
        except AuthenticatedWebhookError:
            raise
        except (
            RecursionError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
        ):
            raise AuthenticatedWebhookError(
                "authenticated_webhook_response_json_is_invalid"
            ) from None


class AuthenticatedWebhookTransport:
    """HTTPS-only POST transport that authenticates a receiver acknowledgement."""

    def __init__(
        self,
        webhook_url: str,
        *,
        key_ring: ReceiverAckKeyRing,
        client: httpx.AsyncClient | None = None,
        timeout_sec: float = 5.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if (
            isinstance(timeout_sec, bool)
            or not isinstance(timeout_sec, (int, float))
            or not math.isfinite(timeout_sec)
            or timeout_sec <= 0
        ):
            raise AuthenticatedWebhookError("authenticated_webhook_timeout_is_invalid")
        self._target = validate_webhook_target(webhook_url)
        self._key_ring = key_ring
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=timeout_sec,
            trust_env=False,
        )
        self._timeout_sec = timeout_sec
        self._clock = clock or (lambda: datetime.now(UTC))

    async def post_json(
        self,
        *,
        context: ReceiverAckContext,
        payload: Mapping[str, object],
        binding: Mapping[str, str],
        headers: Mapping[str, str] | None = None,
        allow_stale_ack: bool = False,
        scheduler_authorization: SchedulerInvocationEffectAuthorization | None = None,
    ) -> AuthenticatedWebhookResponse:
        if type(context) is not str or context not in {
            "legacy_alert",
            "outbox",
            "dead_man",
        }:
            raise AuthenticatedWebhookError("authenticated_webhook_context_is_invalid")
        if type(allow_stale_ack) is not bool:
            raise AuthenticatedWebhookError("authenticated_webhook_stale_policy_is_invalid")
        normalized_binding = _validated_binding(binding)
        request_body = _canonical_json_bytes(
            payload,
            error_code="authenticated_webhook_request_is_invalid",
        )
        if len(request_body) > _REQUEST_MAX_BYTES:
            raise AuthenticatedWebhookError("authenticated_webhook_request_is_too_large")
        request_headers = _request_headers(
            context=context,
            current_key_id=self._key_ring.current_key_id,
            extra=headers,
        )
        _require_scheduler_transport_authorization(
            scheduler_authorization,
            context=context,
        )
        try:
            request = self._client.build_request(
                "POST",
                self._target.url,
                headers=request_headers,
                content=request_body,
            )
        except (TypeError, ValueError, httpx.HTTPError):
            raise AuthenticatedWebhookError("authenticated_webhook_request_is_invalid") from None
        if request.method != "POST" or str(request.url) != self._target.url:
            raise AuthenticatedWebhookError("authenticated_webhook_request_is_invalid")
        sent_at = self._now()
        response: httpx.Response | None = None
        try:
            async with asyncio.timeout(self._timeout_sec):
                _require_scheduler_transport_authorization(
                    scheduler_authorization,
                    context=context,
                )
                response = await self._client.send(
                    request,
                    stream=True,
                    follow_redirects=False,
                )
                response_body = await _bounded_response_body(response)
                _require_scheduler_transport_authorization(
                    scheduler_authorization,
                    context=context,
                )
                if response.status_code < 200 or response.status_code >= 300:
                    raise AuthenticatedWebhookError("authenticated_webhook_transport_failed")
                received_at = self._now()
                _verify_receiver_ack(
                    response=response,
                    key_ring=self._key_ring,
                    context=context,
                    target=str(request.url),
                    request_body=request_body,
                    response_body=response_body,
                    binding=normalized_binding,
                    sent_at=sent_at,
                    received_at=received_at,
                    allow_stale_ack=allow_stale_ack,
                )
                return AuthenticatedWebhookResponse(
                    status_code=response.status_code,
                    content=response_body,
                )
        except (TimeoutError, httpx.HTTPError):
            raise AuthenticatedWebhookError("authenticated_webhook_transport_failed") from None
        finally:
            if response is not None:
                with suppress(httpx.HTTPError):
                    await response.aclose()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise AuthenticatedWebhookError("authenticated_webhook_clock_is_invalid")
        return value.astimezone(UTC)


def _require_scheduler_transport_authorization(
    authorization: SchedulerInvocationEffectAuthorization | None,
    *,
    context: ReceiverAckContext,
) -> None:
    if authorization is None:
        return
    if context != "outbox":
        raise AuthenticatedWebhookError(
            "authenticated_webhook_scheduler_authorization_context_is_invalid"
        )
    require_scheduler_invocation_effect_authorization(
        authorization,
        expected_job_key="operations.outbox",
    )


def validate_webhook_target(value: object) -> ValidatedWebhookTarget:
    if type(value) is not str or not value or value != value.strip():
        raise AuthenticatedWebhookError("webhook_url_is_invalid")
    if any(
        character == "\\"
        or character.isspace()
        or ord(character) < 32
        or 127 <= ord(character) <= 159
        for character in value
    ):
        raise AuthenticatedWebhookError("webhook_url_is_invalid")
    try:
        parts = urlsplit(value)
        hostname = parts.hostname
        port = parts.port
    except (AttributeError, TypeError, UnicodeError, ValueError):
        raise AuthenticatedWebhookError("webhook_url_is_invalid") from None
    if (
        parts.scheme.casefold() != "https"
        or not parts.netloc
        or hostname is None
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
        or port == 0
    ):
        raise AuthenticatedWebhookError("webhook_url_is_invalid")
    canonical_host = _canonical_host(hostname)
    if ":" in canonical_host:
        canonical_host = f"[{canonical_host}]"
    authority = canonical_host if port is None else f"{canonical_host}:{port}"
    canonical = urlunsplit(
        (
            "https",
            authority,
            parts.path or "/",
            parts.query,
            "",
        )
    )
    try:
        normalized = str(httpx.URL(canonical))
    except (httpx.InvalidURL, TypeError, ValueError):
        raise AuthenticatedWebhookError("webhook_url_is_invalid") from None
    return ValidatedWebhookTarget(url=normalized)


def _canonical_host(value: str) -> str:
    if not value or value.endswith("."):
        raise AuthenticatedWebhookError("webhook_url_is_invalid")
    try:
        if ":" in value:
            return ipaddress.IPv6Address(value).compressed
        try:
            return str(ipaddress.IPv4Address(value))
        except ipaddress.AddressValueError:
            pass
        ascii_host = value.encode("idna").decode("ascii").casefold()
    except (UnicodeError, ipaddress.AddressValueError):
        raise AuthenticatedWebhookError("webhook_url_is_invalid") from None
    if len(ascii_host) > 253:
        raise AuthenticatedWebhookError("webhook_url_is_invalid")
    labels = ascii_host.split(".")
    if not labels or any(_HOST_LABEL_RE.fullmatch(label) is None for label in labels):
        raise AuthenticatedWebhookError("webhook_url_is_invalid")
    return ascii_host


def _decode_key(value: object, *, error_code: str) -> bytes:
    if type(value) is not str or value != value.strip() or not value.isascii():
        raise AuthenticatedWebhookError(error_code)
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise AuthenticatedWebhookError(error_code) from None
    if len(decoded) != 32 or base64.b64encode(decoded).decode("ascii") != value:
        raise AuthenticatedWebhookError(error_code)
    return decoded


def _validated_binding(binding: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(binding, Mapping) or not binding:
        raise AuthenticatedWebhookError("authenticated_webhook_binding_is_invalid")
    normalized = dict(binding)
    if any(
        type(key) is not str or not key or type(value) is not str or not value
        for key, value in normalized.items()
    ):
        raise AuthenticatedWebhookError("authenticated_webhook_binding_is_invalid")
    return normalized


def _request_headers(
    *,
    context: ReceiverAckContext,
    current_key_id: str,
    extra: Mapping[str, str] | None,
) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "Content-Type": "application/json",
        "X-Receiver-Ack-Version": _VERSION,
        "X-Receiver-Ack-Context": context,
        "X-Receiver-Ack-Requested-Key-Id": current_key_id,
    }
    reserved = {name.casefold() for name in headers}
    if extra is None:
        return headers
    if not isinstance(extra, Mapping):
        raise AuthenticatedWebhookError("authenticated_webhook_request_headers_are_invalid")
    for name, value in extra.items():
        normalized_name = name.casefold() if type(name) is str else ""
        if (
            type(name) is not str
            or not name
            or normalized_name in reserved
            or normalized_name != "idempotency-key"
            or type(value) is not str
            or _IDEMPOTENCY_KEY_RE.fullmatch(value) is None
        ):
            raise AuthenticatedWebhookError("authenticated_webhook_request_headers_are_invalid")
        headers[name] = value
    return headers


async def _bounded_response_body(response: httpx.Response) -> bytes:
    encoding = response.headers.get("content-encoding", "").strip().casefold()
    if encoding not in {"", "identity"}:
        raise AuthenticatedWebhookError("authenticated_webhook_response_is_invalid")
    lengths = _raw_header_values(response.headers, b"content-length")
    if len(lengths) > 1:
        raise AuthenticatedWebhookError("authenticated_webhook_response_is_invalid")
    if lengths:
        length = lengths[0]
        if not length.isascii() or not length.isdigit():
            raise AuthenticatedWebhookError("authenticated_webhook_response_is_invalid")
        normalized = length.lstrip(b"0") or b"0"
        maximum = str(_RESPONSE_MAX_BYTES).encode("ascii")
        if len(normalized) > len(maximum) or (
            len(normalized) == len(maximum) and normalized > maximum
        ):
            raise AuthenticatedWebhookError("authenticated_webhook_response_is_too_large")
    if response.is_stream_consumed:
        if len(response.content) > _RESPONSE_MAX_BYTES:
            raise AuthenticatedWebhookError("authenticated_webhook_response_is_too_large")
        return response.content
    body = bytearray()
    try:
        async for chunk in response.aiter_raw():
            if len(body) + len(chunk) > _RESPONSE_MAX_BYTES:
                raise AuthenticatedWebhookError("authenticated_webhook_response_is_too_large")
            body.extend(chunk)
    except httpx.HTTPError:
        raise AuthenticatedWebhookError("authenticated_webhook_transport_failed") from None
    return bytes(body)


def _verify_receiver_ack(
    *,
    response: httpx.Response,
    key_ring: ReceiverAckKeyRing,
    context: ReceiverAckContext,
    target: str,
    request_body: bytes,
    response_body: bytes,
    binding: Mapping[str, str],
    sent_at: datetime,
    received_at: datetime,
    allow_stale_ack: bool,
) -> None:
    try:
        version = _single_ascii_header(response.headers, _HEADER_VERSION)
        key_id = _single_ascii_header(response.headers, _HEADER_KEY_ID)
        acknowledged_at = _single_ascii_header(response.headers, _HEADER_TIMESTAMP)
        encoded_signature = _single_ascii_header(
            response.headers,
            _HEADER_SIGNATURE,
        )
        if version != _VERSION or _KEY_ID_RE.fullmatch(key_id) is None:
            raise ValueError
        key = key_ring.key_for_id(key_id)
        if key is None:
            raise ValueError
        signature = _decode_signature(encoded_signature)
        acknowledged_time = _acknowledged_time(acknowledged_at)
        if acknowledged_time > received_at + _MAX_FUTURE_SKEW:
            raise ValueError
        if not allow_stale_ack and acknowledged_time < sent_at - _MAX_ACK_AGE:
            raise ValueError
        message = _canonical_json_bytes(
            {
                "acknowledged_at": acknowledged_at,
                "binding": dict(binding),
                "context": context,
                "key_id": key_id,
                "method": "POST",
                "protocol": _PROTOCOL,
                "request_sha256": hashlib.sha256(request_body).hexdigest(),
                "response_sha256": hashlib.sha256(response_body).hexdigest(),
                "status": response.status_code,
                "target": target,
                "version": 1,
            },
            error_code="receiver_ack_authentication_failed",
        )
        expected = hmac.new(key, message, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, signature):
            raise ValueError
    except (AuthenticatedWebhookError, OSError, OverflowError, ValueError):
        raise AuthenticatedWebhookError("receiver_ack_authentication_failed") from None


def _single_ascii_header(headers: httpx.Headers, name: bytes) -> str:
    values = _raw_header_values(headers, name)
    if len(values) != 1:
        raise ValueError
    try:
        value = values[0].decode("ascii")
    except UnicodeDecodeError:
        raise ValueError from None
    if not value or value != value.strip() or "," in value:
        raise ValueError
    if any(ord(character) < 33 or ord(character) == 127 for character in value):
        raise ValueError
    return value


def _raw_header_values(headers: httpx.Headers, name: bytes) -> list[bytes]:
    normalized_name = name.lower()
    return [value for raw_name, value in headers.raw if raw_name.lower() == normalized_name]


def _decode_signature(value: str) -> bytes:
    try:
        signature = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError from None
    if len(signature) != 32 or base64.b64encode(signature).decode("ascii") != value:
        raise ValueError
    return signature


def _acknowledged_time(value: str) -> datetime:
    if _ACK_TIMESTAMP_RE.fullmatch(value) is None:
        raise ValueError
    return datetime.fromtimestamp(int(value), tz=UTC)


def _canonical_json_bytes(value: object, *, error_code: str) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (RecursionError, TypeError, ValueError):
        raise AuthenticatedWebhookError(error_code) from None


def _json_object_without_duplicates(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise AuthenticatedWebhookError("authenticated_webhook_response_json_is_invalid")
        result[key] = value
    return result


def _reject_nonstandard_constant(_value: str) -> None:
    raise AuthenticatedWebhookError("authenticated_webhook_response_json_is_invalid")
