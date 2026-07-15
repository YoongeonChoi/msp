from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Literal
from uuid import UUID

import httpx

from app.domain.operations.models import OperationsInvariantError

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REASON_RE = re.compile(r"^[a-z0-9_]{3,120}$")


class DeadManWebhookDestination:
    """Direct failure-domain alert path, independent from the DB outbox."""

    def __init__(
        self,
        webhook_url: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_sec: float = 5.0,
    ) -> None:
        if not webhook_url.strip():
            raise OperationsInvariantError("dead_man_webhook_url_is_required")
        self.webhook_url = webhook_url
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=timeout_sec)

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
        dedupe_key = _dedupe_key(account_id, episode_id, event, reason_codes)
        response = await self.client.post(
            self.webhook_url,
            headers={"Idempotency-Key": dedupe_key},
            json={
                "schema_version": 2,
                "dedupe_key": dedupe_key,
                "episode_id": episode_id,
                "event_type": "dead_man_monitor_" + event,
                "aggregate_type": "trading_account",
                "aggregate_id": account_id,
                "payload": {
                    "observed_at": observed_at.isoformat(),
                    "reason_codes": list(reason_codes),
                    "severity": "critical" if event == "unhealthy" else "warning",
                },
            },
        )
        response.raise_for_status()
        try:
            receipt = response.json()
        except ValueError as exc:
            raise OperationsInvariantError("dead_man_alert_receipt_is_missing") from exc
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
        if self._owns_client:
            await self.client.aclose()


def _dedupe_key(
    account_id: str,
    episode_id: str,
    event: Literal["unhealthy", "recovered"],
    reason_codes: tuple[str, ...],
) -> str:
    material = json.dumps(
        {
            "account_id": account_id,
            "episode_id": episode_id,
            "event": event,
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
