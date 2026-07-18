from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

import httpx

from app.application.ports.candle_observation_store_port import (
    CandleObservationStoreError,
    CandleObservationWriteReceipt,
)
from app.config import Settings
from app.domain.common.json import JsonObject
from app.domain.market_data.point_in_time import (
    PointInTimeCandleV1,
    PointInTimeDataError,
)
from app.infrastructure.supabase_headers import supabase_api_headers

PITCandleRpc = Literal["append_pit_candle_observation_v1"]

PIT_CANDLE_RPC_ALLOWLIST: frozenset[str] = frozenset(
    {"append_pit_candle_observation_v1"}
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_QUARANTINE_REASONS = frozenset(
    {
        "candle_observation_store_observation_time_regressed",
        "candle_observation_store_historical_hash_recurrence_ambiguous",
        "candle_observation_store_revision_time_not_increasing",
    }
)
_RECEIPT_FIELDS = {
    "status",
    "idempotency_key",
    "canonical_observation_sha256",
    "revision",
    "inserted",
    "stored_observed_at",
    "quarantine_id",
    "reason_code",
}


class SupabaseCandleObservationStore:
    """Persist validated daily candle revisions through one Worker-only RPC.

    Construction is explicit and this adapter is not wired into the runtime
    container yet. It stores candle observations only; calendar timing evidence
    and collection completeness remain separate contracts.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not settings.supabase_url or settings.supabase_secret_key is None:
            raise CandleObservationStoreError(
                "candle_observation_store_credentials_missing"
            )
        secret = settings.supabase_secret_key.get_secret_value()
        self.base_url = settings.supabase_url.rstrip("/") + "/rest/v1/rpc"
        self.headers = supabase_api_headers(secret) | {
            "accept-profile": "worker_api",
            "content-profile": "worker_api",
            "content-type": "application/json",
        }
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=10.0,
            headers=self.headers,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def append_observation(
        self,
        candle: PointInTimeCandleV1,
    ) -> CandleObservationWriteReceipt:
        canonical = _canonical_candle(candle)
        row = _singleton_row(
            await self._rpc(
                "append_pit_candle_observation_v1",
                {"p_candle": canonical.to_payload()},
            )
        )
        if set(row) != _RECEIPT_FIELDS:
            raise CandleObservationStoreError(
                "candle_observation_store_rpc_result_shape_invalid"
            )

        status = _required_text(row, "status")
        idempotency_key = _sha256(row, "idempotency_key")
        observation_sha256 = _sha256(
            row,
            "canonical_observation_sha256",
        )
        revision = _positive_int(row, "revision")
        inserted = _boolean(row, "inserted")
        stored_observed_at = _aware_datetime(row, "stored_observed_at")

        if idempotency_key != canonical.idempotency_key:
            raise CandleObservationStoreError(
                "candle_observation_store_rpc_identity_mismatch"
            )
        if observation_sha256 != canonical.canonical_observation_sha256:
            raise CandleObservationStoreError(
                "candle_observation_store_rpc_observation_hash_mismatch"
            )
        quarantine_id = row.get("quarantine_id")
        reason_code = row.get("reason_code")
        if status == "quarantined":
            _canonical_uuid(quarantine_id)
            if (
                not isinstance(reason_code, str)
                or reason_code not in _QUARANTINE_REASONS
                or inserted
            ):
                raise CandleObservationStoreError(
                    "candle_observation_store_quarantine_receipt_invalid"
                )
            raise CandleObservationStoreError(reason_code)

        if quarantine_id is not None or reason_code is not None:
            raise CandleObservationStoreError(
                "candle_observation_store_rpc_result_shape_invalid"
            )
        if (status, inserted) not in {("stored", True), ("replayed", False)}:
            raise CandleObservationStoreError(
                "candle_observation_store_rpc_status_invalid"
            )
        if stored_observed_at > canonical.observed_at.astimezone(UTC):
            raise CandleObservationStoreError(
                "candle_observation_store_rpc_observation_time_invalid"
            )
        if inserted and stored_observed_at != canonical.observed_at.astimezone(UTC):
            raise CandleObservationStoreError(
                "candle_observation_store_rpc_observation_time_invalid"
            )

        return CandleObservationWriteReceipt(
            idempotency_key=idempotency_key,
            canonical_observation_sha256=observation_sha256,
            revision=revision,
            inserted=inserted,
            stored_observed_at=stored_observed_at,
        )

    async def _rpc(self, rpc: PITCandleRpc, payload: JsonObject) -> object:
        if rpc not in PIT_CANDLE_RPC_ALLOWLIST:
            raise CandleObservationStoreError(
                "candle_observation_store_rpc_not_allowed"
            )
        try:
            response = await self.client.post(
                f"{self.base_url}/{rpc}",
                headers=self.headers,
                json=payload,
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise CandleObservationStoreError(
                "candle_observation_store_rpc_failed_or_returned_invalid_json"
            ) from exc


def _canonical_candle(value: object) -> PointInTimeCandleV1:
    if not isinstance(value, PointInTimeCandleV1):
        raise CandleObservationStoreError(
            "candle_observation_store_item_invalid"
        )
    try:
        canonical = PointInTimeCandleV1.from_payload(value.to_payload())
    except (
        AttributeError,
        OverflowError,
        TypeError,
        ValueError,
        PointInTimeDataError,
    ) as exc:
        raise CandleObservationStoreError(
            "candle_observation_store_item_invalid"
        ) from exc
    if canonical != value:
        raise CandleObservationStoreError(
            "candle_observation_store_item_invalid"
        )
    return canonical


def _singleton_row(value: object) -> Mapping[str, object]:
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise CandleObservationStoreError(
            "candle_observation_store_rpc_result_invalid"
        )
    return value[0]


def _required_text(row: Mapping[str, object], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise CandleObservationStoreError(
            f"candle_observation_store_rpc_{key}_invalid"
        )
    return value


def _sha256(row: Mapping[str, object], key: str) -> str:
    value = _required_text(row, key)
    if _SHA256.fullmatch(value) is None:
        raise CandleObservationStoreError(
            f"candle_observation_store_rpc_{key}_invalid"
        )
    return value


def _positive_int(row: Mapping[str, object], key: str) -> int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CandleObservationStoreError(
            f"candle_observation_store_rpc_{key}_invalid"
        )
    return value


def _boolean(row: Mapping[str, object], key: str) -> bool:
    value = row.get(key)
    if not isinstance(value, bool):
        raise CandleObservationStoreError(
            f"candle_observation_store_rpc_{key}_invalid"
        )
    return value


def _aware_datetime(row: Mapping[str, object], key: str) -> datetime:
    value = _required_text(row, key)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CandleObservationStoreError(
            f"candle_observation_store_rpc_{key}_invalid"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CandleObservationStoreError(
            f"candle_observation_store_rpc_{key}_invalid"
        )
    return parsed.astimezone(UTC)


def _canonical_uuid(value: object) -> str:
    if not isinstance(value, str):
        raise CandleObservationStoreError(
            "candle_observation_store_quarantine_receipt_invalid"
        )
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise CandleObservationStoreError(
            "candle_observation_store_quarantine_receipt_invalid"
        ) from exc
    if str(parsed) != value or parsed.version not in {1, 2, 3, 4, 5}:
        raise CandleObservationStoreError(
            "candle_observation_store_quarantine_receipt_invalid"
        )
    return value
