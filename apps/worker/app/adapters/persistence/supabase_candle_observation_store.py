from __future__ import annotations

import re
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

import httpx

from app.application.ports.candle_observation_store_port import (
    CandleObservationStoreError,
    CandleObservationStorePersistenceKind,
    CandleObservationWriteReceipt,
)
from app.application.ports.persistence_authority import (
    PersistenceAuthority,
    persistence_authority_fingerprint,
)
from app.config import Settings
from app.domain.common.json import JsonObject
from app.domain.market_data.point_in_time import (
    PointInTimeCandleV1,
)
from app.infrastructure.bounded_json import (
    BoundedJsonError,
    bounded_json_response,
)
from app.infrastructure.supabase_headers import supabase_api_headers

PITCandleRpc = Literal["append_pit_candle_observation_v1"]

PIT_CANDLE_RPC_ALLOWLIST: frozenset[str] = frozenset({"append_pit_candle_observation_v1"})
PIT_CANDLE_MAX_RPC_RESPONSE_BYTES = 64 * 1024

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

    persistence_kind: CandleObservationStorePersistenceKind = "durable"

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not settings.supabase_url or settings.supabase_secret_key is None:
            raise CandleObservationStoreError("candle_observation_store_credentials_missing")
        try:
            self.persistence_authority: PersistenceAuthority = persistence_authority_fingerprint(
                namespace="supabase-worker-api",
                origin=settings.supabase_url,
                profile="worker_api",
            )
        except ValueError:
            raise CandleObservationStoreError("candle_observation_store_origin_invalid") from None
        secret = settings.supabase_secret_key.get_secret_value()
        self.base_url = settings.supabase_url.rstrip("/") + "/rest/v1/rpc"
        self.headers = supabase_api_headers(secret) | {
            "accept-profile": "worker_api",
            "accept-encoding": "identity",
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
            raise CandleObservationStoreError("candle_observation_store_rpc_result_shape_invalid")

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
            raise CandleObservationStoreError("candle_observation_store_rpc_identity_mismatch")
        if observation_sha256 != canonical.canonical_observation_sha256:
            raise CandleObservationStoreError(
                "candle_observation_store_rpc_observation_hash_mismatch"
            )
        quarantine_id = row.get("quarantine_id")
        reason_code = row.get("reason_code")
        if status == "quarantined":
            _canonical_uuid(quarantine_id)
            if type(reason_code) is not str or reason_code not in _QUARANTINE_REASONS or inserted:
                raise CandleObservationStoreError(
                    "candle_observation_store_quarantine_receipt_invalid"
                )
            raise CandleObservationStoreError(reason_code)

        if quarantine_id is not None or reason_code is not None:
            raise CandleObservationStoreError("candle_observation_store_rpc_result_shape_invalid")
        if (status, inserted) not in {("stored", True), ("replayed", False)}:
            raise CandleObservationStoreError("candle_observation_store_rpc_status_invalid")
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
            raise CandleObservationStoreError("candle_observation_store_rpc_not_allowed")
        result: object = None
        failed = False
        try:
            async with self.client.stream(
                "POST",
                f"{self.base_url}/{rpc}",
                headers=self.headers,
                json=payload,
            ) as response:
                try:
                    result = await bounded_json_response(
                        response,
                        max_bytes=PIT_CANDLE_MAX_RPC_RESPONSE_BYTES,
                    )
                except BoundedJsonError:
                    failed = True
                if not response.is_success:
                    failed = True
        except Exception:
            failed = True
        if failed:
            raise CandleObservationStoreError(
                "candle_observation_store_rpc_failed_or_returned_invalid_json"
            ) from None
        return result


def _canonical_candle(value: object) -> PointInTimeCandleV1:
    if type(value) is not PointInTimeCandleV1:
        raise CandleObservationStoreError("candle_observation_store_item_invalid")
    if not _candle_fields_are_exact(value):
        raise CandleObservationStoreError("candle_observation_store_item_invalid")
    canonical: PointInTimeCandleV1 | None = None
    with suppress(Exception):
        canonical = PointInTimeCandleV1.from_payload(value.to_payload())
    if canonical is None or canonical != value:
        raise CandleObservationStoreError("candle_observation_store_item_invalid")
    return canonical


def _candle_fields_are_exact(value: PointInTimeCandleV1) -> bool:
    return (
        type(value.schema_version) is int
        and type(value.provider) is str
        and type(value.symbol) is str
        and type(value.market) is str
        and type(value.interval) is str
        and type(value.adjusted) is bool
        and type(value.provider_event_at) is datetime
        and value.provider_event_at.tzinfo is not None
        and value.provider_event_at.utcoffset() is not None
        and type(value.observed_at) is datetime
        and value.observed_at.tzinfo is not None
        and value.observed_at.utcoffset() is not None
        and type(value.currency) is str
        and type(value.open_krw) is int
        and type(value.high_krw) is int
        and type(value.low_krw) is int
        and type(value.close_krw) is int
        and type(value.volume) is int
        and type(value.provider_contract_sha256) is str
        and type(value.canonical_observation_sha256) is str
    )


def _singleton_row(value: object) -> Mapping[str, object]:
    if type(value) is not list or len(value) != 1 or type(value[0]) is not dict:
        raise CandleObservationStoreError("candle_observation_store_rpc_result_invalid")
    return value[0]


def _required_text(row: Mapping[str, object], key: str) -> str:
    value = row.get(key)
    if type(value) is not str or not value:
        raise CandleObservationStoreError(f"candle_observation_store_rpc_{key}_invalid")
    return value


def _sha256(row: Mapping[str, object], key: str) -> str:
    value = _required_text(row, key)
    if _SHA256.fullmatch(value) is None:
        raise CandleObservationStoreError(f"candle_observation_store_rpc_{key}_invalid")
    return value


def _positive_int(row: Mapping[str, object], key: str) -> int:
    value = row.get(key)
    if type(value) is not int or value <= 0:
        raise CandleObservationStoreError(f"candle_observation_store_rpc_{key}_invalid")
    return value


def _boolean(row: Mapping[str, object], key: str) -> bool:
    value = row.get(key)
    if type(value) is not bool:
        raise CandleObservationStoreError(f"candle_observation_store_rpc_{key}_invalid")
    return value


def _aware_datetime(row: Mapping[str, object], key: str) -> datetime:
    value = _required_text(row, key)
    parsed: datetime | None = None
    with suppress(ValueError):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if (
        parsed is None
        or parsed.tzinfo is None
        or parsed.utcoffset() != timedelta(0)
        or value
        not in {
            parsed.isoformat(),
            parsed.isoformat().replace("+00:00", "Z"),
        }
    ):
        raise CandleObservationStoreError(f"candle_observation_store_rpc_{key}_invalid")
    return parsed.astimezone(UTC)


def _canonical_uuid(value: object) -> str:
    if type(value) is not str:
        raise CandleObservationStoreError("candle_observation_store_quarantine_receipt_invalid")
    parsed: UUID | None = None
    with suppress(ValueError):
        parsed = UUID(value)
    if parsed is None or str(parsed) != value or parsed.version not in {1, 2, 3, 4, 5}:
        raise CandleObservationStoreError("candle_observation_store_quarantine_receipt_invalid")
    return value
