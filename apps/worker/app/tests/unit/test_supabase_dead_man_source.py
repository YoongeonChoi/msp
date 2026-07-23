from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr

from app.adapters.persistence.supabase_dead_man_source import SupabaseDeadManSource
from app.dead_man_config import DeadManSettings
from app.domain.operations.models import OperationsInvariantError

NOW = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)


async def test_source_calls_only_monitor_rpc_and_parses_strict_projection() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[_row()])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = SupabaseDeadManSource(
            _settings(),
            release_sha="b" * 40,
            client=client,
        )
        snapshot = await source.get_dead_man_snapshot(
            account_id="paper-primary",
            observed_at=NOW,
        )

    assert seen[0].url.path == "/rest/v1/rpc/get_dead_man_snapshot_v1"
    assert snapshot.latest_heartbeat_status == "ok"
    assert snapshot.dead_letter_count == 0


async def test_source_rejects_unknown_response_field() -> None:
    row = _row() | {"unexpected": True}

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[row])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = SupabaseDeadManSource(
            _settings(),
            release_sha="b" * 40,
            client=client,
        )
        with pytest.raises(
            OperationsInvariantError,
            match="snapshot_response_is_invalid",
        ):
            await source.get_dead_man_snapshot(
                account_id="paper-primary",
                observed_at=NOW,
            )


def _settings() -> DeadManSettings:
    return DeadManSettings(
        SUPABASE_URL="https://example.supabase.co",
        SUPABASE_SECRET_KEY=SecretStr("test-secret"),
        DEAD_MAN_ACCOUNT_ID="paper-primary",
        DEAD_MAN_ALERT_WEBHOOK_URL=SecretStr("https://alerts.example.invalid"),
        DEAD_MAN_ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_ID="test-current",
        DEAD_MAN_ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64=SecretStr(
            base64.b64encode(b"d" * 32).decode("ascii")
        ),
    )


def _row() -> dict[str, object]:
    return {
        "observed_at": NOW.isoformat(),
        "latest_heartbeat_at": (NOW - timedelta(seconds=1)).isoformat(),
        "latest_heartbeat_status": "ok",
        "latest_heartbeat_release_sha": "a" * 40,
        "commands_last_completed_at": (NOW - timedelta(seconds=1)).isoformat(),
        "execution_last_completed_at": (NOW - timedelta(seconds=1)).isoformat(),
        "settlement_last_completed_at": (NOW - timedelta(seconds=1)).isoformat(),
        "reconciliation_last_completed_at": (NOW - timedelta(seconds=1)).isoformat(),
        "outbox_last_completed_at": (NOW - timedelta(seconds=1)).isoformat(),
        "lease_holder_id": "worker-a",
        "lease_expires_at": (NOW + timedelta(minutes=1)).isoformat(),
        "lease_release_sha": "a" * 40,
        "oldest_pending_outbox_at": None,
        "dead_letter_count": 0,
        "active_incident_opened_at": None,
        "active_incident_acknowledged_at": None,
    }
