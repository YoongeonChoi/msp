from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from datetime import datetime
from typing import Literal
from uuid import UUID

import httpx

from app.domain.operations.models import OperationsInvariantError
from app.infrastructure.authenticated_webhook import (
    AuthenticatedWebhookError,
    AuthenticatedWebhookTransport,
    ReceiverAckKeyRing,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REASON_RE = re.compile(r"^[a-z0-9_]{3,120}$")


class DeadManWebhookDestination:
    """Direct failure-domain alert path, independent from the DB outbox."""

    def __init__(
        self,
        webhook_url: str,
        *,
        key_ring: ReceiverAckKeyRing,
        client: httpx.AsyncClient | None = None,
        timeout_sec: float = 5.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._transport = AuthenticatedWebhookTransport(
            webhook_url,
            key_ring=key_ring,
            client=client,
            timeout_sec=timeout_sec,
            clock=clock,
        )

    async def deliver_dead_man_alert(
        self,
        *,
        account_id: str,
        episode_id: str,
        event: Literal["unhealthy", "recovered"],
        reason_codes: tuple[str, ...],
        observed_at: datetime,
    ) -> None:
        if not account_id.strip() or event not in {"unhealthy", "recovered"}:
            raise OperationsInvariantError("dead_man_alert_identity_is_invalid")
        _require_episode_id(episode_id)
        if not reason_codes or any(_REASON_RE.fullmatch(code) is None for code in reason_codes):
            raise OperationsInvariantError("dead_man_alert_reasons_are_invalid")
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise OperationsInvariantError("dead_man_alert_time_must_be_timezone_aware")
        canonical_reason_codes = tuple(sorted(reason_codes))
        dedupe_key = _dedupe_key(
            account_id,
            episode_id,
            event,
            canonical_reason_codes,
            observed_at,
        )
        try:
            response = await self._transport.post_json(
                context="dead_man",
                headers={"Idempotency-Key": dedupe_key},
                payload={
                    "schema_version": 2,
                    "dedupe_key": dedupe_key,
                    "episode_id": episode_id,
                    "event_type": "dead_man_monitor_" + event,
                    "aggregate_type": "trading_account",
                    "aggregate_id": account_id,
                    "payload": {
                        "observed_at": observed_at.isoformat(),
                        "reason_codes": list(canonical_reason_codes),
                        "severity": ("critical" if event == "unhealthy" else "warning"),
                    },
                },
                binding={
                    "episode_id": episode_id,
                    "event": event,
                    "dedupe_key": dedupe_key,
                },
            )
        except AuthenticatedWebhookError:
            raise OperationsInvariantError("dead_man_receiver_authentication_failed") from None
        try:
            receipt = response.json()
        except AuthenticatedWebhookError:
            raise OperationsInvariantError("dead_man_alert_receipt_is_missing") from None
        if not isinstance(receipt, dict) or set(receipt) != {
            "immutable_receipt_id",
            "accepted_dedupe_key",
            "receipt_sha256",
        }:
            raise OperationsInvariantError("dead_man_alert_receipt_is_invalid")
        receipt_id = receipt.get("immutable_receipt_id")
        if not isinstance(receipt_id, str) or not receipt_id.strip():
            raise OperationsInvariantError("dead_man_alert_receipt_is_invalid")
        if receipt.get("accepted_dedupe_key") != dedupe_key:
            raise OperationsInvariantError("dead_man_alert_receipt_dedupe_mismatch")
        expected_sha256 = hashlib.sha256(
            json.dumps(
                {
                    "accepted_dedupe_key": dedupe_key,
                    "immutable_receipt_id": receipt_id,
                },
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        receipt_sha256 = receipt.get("receipt_sha256")
        if (
            not isinstance(receipt_sha256, str)
            or _SHA256_RE.fullmatch(receipt_sha256) is None
            or receipt_sha256 != expected_sha256
        ):
            raise OperationsInvariantError("dead_man_alert_receipt_hash_mismatch")

    async def close(self) -> None:
        await self._transport.close()


def _dedupe_key(
    account_id: str,
    episode_id: str,
    event: Literal["unhealthy", "recovered"],
    reason_codes: tuple[str, ...],
    observed_at: datetime,
) -> str:
    material = json.dumps(
        {
            "account_id": account_id,
            "episode_id": episode_id,
            "event": event,
            "observed_at": observed_at.isoformat(),
            "reason_codes": sorted(reason_codes),
            "schema_version": 2,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "dead-man-v2:" + hashlib.sha256(material).hexdigest()


def _require_episode_id(value: object) -> None:
    if not isinstance(value, str):
        raise OperationsInvariantError("dead_man_alert_episode_id_is_invalid")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise OperationsInvariantError("dead_man_alert_episode_id_is_invalid") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise OperationsInvariantError("dead_man_alert_episode_id_is_invalid")
