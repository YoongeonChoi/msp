from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Literal
from uuid import UUID

import httpx

from app.application.ports.paper_execution_producer_port import (
    PaperBarFixtureReceipt,
    PaperCandidateReceipt,
)
from app.config import Settings
from app.domain.common.json import JsonObject
from app.domain.execution_v2.models import ExecutionInvariantError
from app.infrastructure.release_metadata import worker_release_metadata
from app.infrastructure.supabase_headers import supabase_api_headers

PaperProducerRpc = Literal[
    "ingest_paper_bar_fixture_v1",
    "enqueue_paper_execution_candidate_v1",
]

_RPC_ALLOWLIST: frozenset[str] = frozenset(
    {
        "ingest_paper_bar_fixture_v1",
        "enqueue_paper_execution_candidate_v1",
    }
)
_RELEASE_SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class SupabasePaperExecutionProducer:
    """RPC-only publisher for immutable Paper evidence and candidates."""

    def __init__(
        self,
        settings: Settings,
        *,
        account_id: str,
        release_sha: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not settings.execution_v2_enabled or not settings.execution_v2_worker_api_enabled:
            raise ExecutionInvariantError("paper_producer_worker_api_is_not_enabled")
        if not settings.execution_v2_paper_source_input_enabled:
            raise ExecutionInvariantError("paper_producer_input_is_not_enabled")
        if settings.execution_v2_environment != "paper":
            raise ExecutionInvariantError("paper_producer_requires_paper_environment")
        if account_id != settings.execution_v2_account_id:
            raise ExecutionInvariantError("paper_producer_configured_account_mismatch")
        if not settings.supabase_url or settings.supabase_secret_key is None:
            raise ExecutionInvariantError("paper_producer_credentials_are_missing")
        resolved_release_sha = release_sha or worker_release_metadata().get("release_sha")
        if (
            not isinstance(resolved_release_sha, str)
            or _RELEASE_SHA.fullmatch(resolved_release_sha) is None
        ):
            raise ExecutionInvariantError("paper_producer_release_sha_is_missing")
        secret = settings.supabase_secret_key.get_secret_value()
        self.account_id = account_id
        self.release_sha = resolved_release_sha.lower()
        self.base_url = settings.supabase_url.rstrip("/") + "/rest/v1/rpc"
        self.headers = supabase_api_headers(secret) | {
            "accept-profile": "worker_api",
            "content-profile": "worker_api",
            "content-type": "application/json",
        }
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=10.0, headers=self.headers)

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def ingest_bar_fixture(
        self,
        fixture: JsonObject,
        *,
        worker_id: str,
        now: datetime,
    ) -> PaperBarFixtureReceipt:
        _require_uuid(worker_id, "paper_producer_worker_id_is_invalid")
        _require_aware(now)
        row = _singleton_row(
            await self._rpc(
                "ingest_paper_bar_fixture_v1",
                {
                    "p_fixture": fixture,
                    "p_worker_id": worker_id,
                    "p_release_sha": self.release_sha,
                    "p_now": now.isoformat(),
                },
            )
        )
        _require_exact_keys(
            row,
            {
                "series_id",
                "fixture_set_id",
                "batch_sequence",
                "bar_count",
                "fixture_sha256",
                "idempotent",
            },
        )
        receipt = PaperBarFixtureReceipt(
            series_id=_uuid_value(row, "series_id"),
            fixture_set_id=_uuid_value(row, "fixture_set_id"),
            batch_sequence=_positive_int(row, "batch_sequence"),
            bar_count=_positive_int(row, "bar_count"),
            fixture_sha256=_sha256_value(row, "fixture_sha256"),
            idempotent=_bool_value(row, "idempotent"),
        )
        if receipt.series_id != fixture.get("series_id"):
            raise ExecutionInvariantError("paper_producer_fixture_series_mismatch")
        return receipt

    async def enqueue_candidate(
        self,
        candidate: JsonObject,
        *,
        worker_id: str,
        fencing_token: int,
        control_epoch: int,
        now: datetime,
    ) -> PaperCandidateReceipt:
        _require_uuid(worker_id, "paper_producer_worker_id_is_invalid")
        _require_positive_int(fencing_token, "paper_producer_fencing_token_is_invalid")
        _require_positive_int(control_epoch, "paper_producer_control_epoch_is_invalid")
        _require_aware(now)
        if candidate.get("account_id") != self.account_id:
            raise ExecutionInvariantError("paper_producer_candidate_account_mismatch")
        row = _singleton_row(
            await self._rpc(
                "enqueue_paper_execution_candidate_v1",
                {
                    "p_candidate": candidate,
                    "p_worker_id": worker_id,
                    "p_fencing_token": fencing_token,
                    "p_control_epoch": control_epoch,
                    "p_release_sha": self.release_sha,
                    "p_now": now.isoformat(),
                },
            )
        )
        _require_exact_keys(
            row,
            {
                "command_id",
                "intent_id",
                "state",
                "source_revision",
                "semantic_key_sha256",
                "idempotent",
            },
        )
        receipt = PaperCandidateReceipt(
            command_id=_uuid_value(row, "command_id"),
            intent_id=_uuid_value(row, "intent_id"),
            state=_text_value(row, "state"),
            source_revision=_positive_int(row, "source_revision"),
            semantic_key_sha256=_sha256_value(row, "semantic_key_sha256"),
            idempotent=_bool_value(row, "idempotent"),
        )
        if receipt.intent_id != candidate.get("intent_id"):
            raise ExecutionInvariantError("paper_producer_candidate_identity_mismatch")
        return receipt

    async def _rpc(self, rpc: PaperProducerRpc, payload: JsonObject) -> object:
        if rpc not in _RPC_ALLOWLIST:
            raise ExecutionInvariantError("paper_producer_rpc_is_not_allowed")
        try:
            response = await self.client.post(
                f"{self.base_url}/{rpc}",
                headers=self.headers,
                json=payload,
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ExecutionInvariantError(
                "paper_producer_rpc_failed_or_returned_invalid_json"
            ) from exc


def _singleton_row(value: object) -> Mapping[str, object]:
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise ExecutionInvariantError("paper_producer_rpc_result_is_invalid")
    return value[0]


def _require_exact_keys(row: Mapping[str, object], expected: set[str]) -> None:
    if set(row) != expected:
        raise ExecutionInvariantError("paper_producer_rpc_result_shape_is_invalid")


def _require_uuid(value: str, reason: str) -> None:
    try:
        parsed = UUID(value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ExecutionInvariantError(reason) from exc
    if str(parsed) != value or parsed.version not in {1, 2, 3, 4, 5}:
        raise ExecutionInvariantError(reason)


def _uuid_value(row: Mapping[str, object], key: str) -> str:
    value = _text_value(row, key)
    _require_uuid(value, f"paper_producer_{key}_is_invalid")
    return value


def _text_value(row: Mapping[str, object], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ExecutionInvariantError(f"paper_producer_{key}_is_invalid")
    return value


def _sha256_value(row: Mapping[str, object], key: str) -> str:
    value = _text_value(row, key)
    if _SHA256.fullmatch(value) is None:
        raise ExecutionInvariantError(f"paper_producer_{key}_is_invalid")
    return value


def _positive_int(row: Mapping[str, object], key: str) -> int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ExecutionInvariantError(f"paper_producer_{key}_is_invalid")
    return value


def _require_positive_int(value: object, reason: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ExecutionInvariantError(reason)


def _bool_value(row: Mapping[str, object], key: str) -> bool:
    value = row.get(key)
    if not isinstance(value, bool):
        raise ExecutionInvariantError(f"paper_producer_{key}_is_invalid")
    return value


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ExecutionInvariantError("paper_producer_clock_must_be_timezone_aware")
