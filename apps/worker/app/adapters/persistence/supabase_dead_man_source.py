from __future__ import annotations

import re
from datetime import datetime
from typing import cast

import httpx

from app.application.services.dead_man_service import (
    DeadManHeartbeatStatus,
    DeadManSnapshot,
)
from app.dead_man_config import DeadManSettings
from app.domain.operations.models import OperationsInvariantError
from app.infrastructure.release_metadata import worker_release_metadata
from app.infrastructure.supabase_headers import supabase_api_headers

_RELEASE_SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_SNAPSHOT_KEYS = {
    "observed_at",
    "latest_heartbeat_at",
    "latest_heartbeat_status",
    "latest_heartbeat_release_sha",
    "commands_last_completed_at",
    "execution_last_completed_at",
    "settlement_last_completed_at",
    "reconciliation_last_completed_at",
    "outbox_last_completed_at",
    "lease_holder_id",
    "lease_expires_at",
    "lease_release_sha",
    "oldest_pending_outbox_at",
    "dead_letter_count",
    "active_incident_opened_at",
    "active_incident_acknowledged_at",
}


class SupabaseDeadManSource:
    """Read one monitor projection through a single allowlisted Worker RPC."""

    def __init__(
        self,
        settings: DeadManSettings,
        *,
        release_sha: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        resolved_release_sha = release_sha or worker_release_metadata().get("release_sha")
        if (
            not isinstance(resolved_release_sha, str)
            or _RELEASE_SHA_RE.fullmatch(resolved_release_sha) is None
        ):
            raise OperationsInvariantError("dead_man_release_sha_is_required")
        self.release_sha = resolved_release_sha.lower()
        self.account_id = settings.account_id
        secret = settings.supabase_secret_key.get_secret_value()
        headers = supabase_api_headers(secret) | {
            "accept-profile": "worker_api",
            "content-profile": "worker_api",
            "content-type": "application/json",
        }
        self.rpc_url = (
            settings.supabase_url.rstrip("/")
            + "/rest/v1/rpc/get_dead_man_snapshot_v1"
        )
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=settings.request_timeout_sec,
            headers=headers,
        )

    async def get_dead_man_snapshot(
        self,
        *,
        account_id: str,
        observed_at: datetime,
    ) -> DeadManSnapshot:
        if account_id != self.account_id:
            raise OperationsInvariantError("dead_man_account_identity_mismatch")
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise OperationsInvariantError("dead_man_observed_at_must_be_timezone_aware")
        try:
            response = await self.client.post(
                self.rpc_url,
                json={
                    "p_account_id": account_id,
                    "p_now": observed_at.isoformat(),
                    "p_monitor_release_sha": self.release_sha,
                },
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OperationsInvariantError("dead_man_snapshot_rpc_failed") from exc
        if not isinstance(payload, list) or len(payload) != 1:
            raise OperationsInvariantError("dead_man_snapshot_response_is_invalid")
        row = payload[0]
        if not isinstance(row, dict) or set(row) != _SNAPSHOT_KEYS:
            raise OperationsInvariantError("dead_man_snapshot_response_is_invalid")
        status = row["latest_heartbeat_status"]
        if status is not None and status not in {
            "ok",
            "warning",
            "error",
            "shutting_down",
        }:
            raise OperationsInvariantError("dead_man_snapshot_response_is_invalid")
        return DeadManSnapshot(
            observed_at=_required_datetime(row, "observed_at"),
            latest_heartbeat_at=_optional_datetime(row, "latest_heartbeat_at"),
            latest_heartbeat_status=cast(DeadManHeartbeatStatus | None, status),
            latest_heartbeat_release_sha=_optional_text(
                row, "latest_heartbeat_release_sha"
            ),
            commands_last_completed_at=_optional_datetime(
                row, "commands_last_completed_at"
            ),
            execution_last_completed_at=_optional_datetime(
                row, "execution_last_completed_at"
            ),
            settlement_last_completed_at=_optional_datetime(
                row, "settlement_last_completed_at"
            ),
            reconciliation_last_completed_at=_optional_datetime(
                row, "reconciliation_last_completed_at"
            ),
            outbox_last_completed_at=_optional_datetime(
                row, "outbox_last_completed_at"
            ),
            lease_holder_id=_optional_text(row, "lease_holder_id"),
            lease_expires_at=_optional_datetime(row, "lease_expires_at"),
            lease_release_sha=_optional_text(row, "lease_release_sha"),
            oldest_pending_outbox_at=_optional_datetime(
                row, "oldest_pending_outbox_at"
            ),
            dead_letter_count=_required_nonnegative_int(row, "dead_letter_count"),
            active_incident_opened_at=_optional_datetime(
                row, "active_incident_opened_at"
            ),
            active_incident_acknowledged_at=_optional_datetime(
                row, "active_incident_acknowledged_at"
            ),
        )

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()


def _required_datetime(row: dict[object, object], key: str) -> datetime:
    value = _optional_datetime(row, key)
    if value is None:
        raise OperationsInvariantError("dead_man_snapshot_response_is_invalid")
    return value


def _optional_datetime(row: dict[object, object], key: str) -> datetime | None:
    value = row.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise OperationsInvariantError("dead_man_snapshot_response_is_invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise OperationsInvariantError("dead_man_snapshot_response_is_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OperationsInvariantError("dead_man_snapshot_response_is_invalid")
    return parsed


def _optional_text(row: dict[object, object], key: str) -> str | None:
    value = row.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise OperationsInvariantError("dead_man_snapshot_response_is_invalid")
    return value


def _required_nonnegative_int(row: dict[object, object], key: str) -> int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OperationsInvariantError("dead_man_snapshot_response_is_invalid")
    return value
