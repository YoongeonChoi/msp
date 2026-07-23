from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from uuid import UUID

import httpx

from app.application.ports.calendar_observation_store_port import (
    CalendarObservationStatus,
    CalendarObservationStoreError,
    CalendarObservationWriteReceipt,
)
from app.config import Settings
from app.domain.common.json import JsonObject
from app.domain.market_data.point_in_time_calendar import (
    PointInTimeCalendarError,
    PointInTimeKrDailySessionV1,
)
from app.infrastructure.supabase_headers import supabase_api_headers

PITKrDailySessionRpc = Literal[
    "append_pit_kr_daily_session_observation_v1"
]

PIT_KR_DAILY_SESSION_RPC_ALLOWLIST: frozenset[str] = frozenset(
    {"append_pit_kr_daily_session_observation_v1"}
)
PIT_KR_DAILY_SESSION_MAX_RPC_RESPONSE_BYTES = 64 * 1024

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_QUARANTINE_REASONS = frozenset(
    {
        "pit_calendar_observation_time_regressed",
        "pit_calendar_revision_time_not_increasing",
        "pit_calendar_historical_hash_recurrence_ambiguous",
    }
)
_RECEIPT_FIELDS = {
    "status",
    "calendar_idempotency_key",
    "canonical_evidence_sha256",
    "revision",
    "revision_inserted",
    "occurrence_id",
    "occurrence_inserted",
    "observed_at",
    "quarantine_id",
    "reason_code",
}


class SupabaseCalendarObservationStore:
    """Persist independent open or closed KR calendar observations.

    Construction is explicit. It is available only to the dedicated manual
    calendar runtime and is not wired into the normal worker loop, scheduler,
    research, strategy, or order paths.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not settings.supabase_url or settings.supabase_secret_key is None:
            raise CalendarObservationStoreError(
                "calendar_observation_store_credentials_missing"
            )
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
        session: PointInTimeKrDailySessionV1,
    ) -> CalendarObservationWriteReceipt:
        canonical = _canonical_session(session)
        row = _singleton_row(
            await self._rpc(
                "append_pit_kr_daily_session_observation_v1",
                {"p_session": canonical.to_payload()},
            )
        )
        if set(row) != _RECEIPT_FIELDS:
            raise CalendarObservationStoreError(
                "calendar_observation_store_rpc_result_shape_invalid"
            )

        status = _required_text(row, "status")
        calendar_idempotency_key = _sha256(
            row,
            "calendar_idempotency_key",
        )
        canonical_evidence_sha256 = _sha256(
            row,
            "canonical_evidence_sha256",
        )
        revision = _positive_int(row, "revision")
        revision_inserted = _boolean(row, "revision_inserted")
        occurrence_inserted = _boolean(row, "occurrence_inserted")
        observed_at = _utc_datetime(row, "observed_at")

        if calendar_idempotency_key != canonical.idempotency_key:
            raise CalendarObservationStoreError(
                "calendar_observation_store_rpc_identity_mismatch"
            )
        if canonical_evidence_sha256 != canonical.canonical_evidence_sha256:
            raise CalendarObservationStoreError(
                "calendar_observation_store_rpc_evidence_hash_mismatch"
            )
        if observed_at != canonical.observed_at.astimezone(UTC):
            raise CalendarObservationStoreError(
                "calendar_observation_store_rpc_observed_at_mismatch"
            )

        quarantine_id_value = row.get("quarantine_id")
        reason_code = row.get("reason_code")
        occurrence_id_value = row.get("occurrence_id")
        if status == "quarantined":
            _uuid4(quarantine_id_value, "quarantine_id")
            if (
                occurrence_id_value is not None
                or revision_inserted
                or occurrence_inserted
                or type(reason_code) is not str
                or reason_code not in _QUARANTINE_REASONS
            ):
                raise CalendarObservationStoreError(
                    "calendar_observation_store_quarantine_receipt_invalid"
                )
            raise CalendarObservationStoreError(reason_code)

        if quarantine_id_value is not None or reason_code is not None:
            raise CalendarObservationStoreError(
                "calendar_observation_store_rpc_result_shape_invalid"
            )
        occurrence_id = _uuid4(occurrence_id_value, "occurrence_id")
        if status == "stored":
            if not revision_inserted or not occurrence_inserted:
                raise CalendarObservationStoreError(
                    "calendar_observation_store_rpc_status_invalid"
                )
        elif status == "replayed":
            if revision_inserted:
                raise CalendarObservationStoreError(
                    "calendar_observation_store_rpc_status_invalid"
                )
        else:
            raise CalendarObservationStoreError(
                "calendar_observation_store_rpc_status_invalid"
            )

        return CalendarObservationWriteReceipt(
            status=cast(CalendarObservationStatus, status),
            calendar_idempotency_key=calendar_idempotency_key,
            canonical_evidence_sha256=canonical_evidence_sha256,
            revision=revision,
            revision_inserted=revision_inserted,
            occurrence_id=occurrence_id,
            occurrence_inserted=occurrence_inserted,
            observed_at=observed_at,
        )

    async def _rpc(
        self,
        rpc: PITKrDailySessionRpc,
        payload: JsonObject,
    ) -> object:
        if rpc not in PIT_KR_DAILY_SESSION_RPC_ALLOWLIST:
            raise CalendarObservationStoreError(
                "calendar_observation_store_rpc_not_allowed"
            )
        result: object = None
        failed = False
        try:
            async with self.client.stream(
                "POST",
                f"{self.base_url}/{rpc}",
                headers=self.headers,
                json=payload,
            ) as response:
                content_encoding = response.headers.get(
                    "content-encoding", "identity"
                ).strip().lower()
                if content_encoding not in {"", "identity"}:
                    failed = True
                body = bytearray()
                if not failed:
                    async for chunk in response.aiter_bytes():
                        if (
                            len(body) + len(chunk)
                            > PIT_KR_DAILY_SESSION_MAX_RPC_RESPONSE_BYTES
                        ):
                            failed = True
                            break
                        body.extend(chunk)
                if not failed and response.is_success:
                    result = json.loads(
                        body,
                        object_pairs_hook=_json_object_without_duplicates,
                    )
                else:
                    failed = True
        except Exception:
            failed = True
        if failed:
            raise CalendarObservationStoreError(
                "calendar_observation_store_rpc_failed_or_returned_invalid_json"
            )
        return result


def _json_object_without_duplicates(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _canonical_session(value: object) -> PointInTimeKrDailySessionV1:
    canonical: PointInTimeKrDailySessionV1 | None = None
    failed = False
    if type(value) is not PointInTimeKrDailySessionV1:
        failed = True
    else:
        try:
            canonical = PointInTimeKrDailySessionV1.from_payload(
                value.to_payload()
            )
        except (
            AttributeError,
            OverflowError,
            RuntimeError,
            TypeError,
            ValueError,
            PointInTimeCalendarError,
        ):
            failed = True
    if failed or canonical is None or canonical != value:
        raise CalendarObservationStoreError(
            "calendar_observation_store_item_invalid"
        )
    return canonical


def _singleton_row(value: object) -> Mapping[str, object]:
    if (
        type(value) is not list
        or len(value) != 1
        or type(value[0]) is not dict
    ):
        raise CalendarObservationStoreError(
            "calendar_observation_store_rpc_result_invalid"
        )
    return value[0]


def _required_text(row: Mapping[str, object], key: str) -> str:
    value = row.get(key)
    if type(value) is not str or not value:
        raise CalendarObservationStoreError(
            f"calendar_observation_store_rpc_{key}_invalid"
        )
    return value


def _sha256(row: Mapping[str, object], key: str) -> str:
    value = _required_text(row, key)
    if _SHA256.fullmatch(value) is None:
        raise CalendarObservationStoreError(
            f"calendar_observation_store_rpc_{key}_invalid"
        )
    return value


def _positive_int(row: Mapping[str, object], key: str) -> int:
    value = row.get(key)
    if type(value) is not int or value <= 0:
        raise CalendarObservationStoreError(
            f"calendar_observation_store_rpc_{key}_invalid"
        )
    return value


def _boolean(row: Mapping[str, object], key: str) -> bool:
    value = row.get(key)
    if type(value) is not bool:
        raise CalendarObservationStoreError(
            f"calendar_observation_store_rpc_{key}_invalid"
        )
    return value


def _utc_datetime(row: Mapping[str, object], key: str) -> datetime:
    value = row.get(key)
    parsed: datetime | None = None
    if type(value) is str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (OverflowError, TypeError, ValueError):
            parsed = None
    if (
        parsed is None
        or parsed.tzinfo is None
        or parsed.utcoffset() != timedelta(0)
    ):
        raise CalendarObservationStoreError(
            f"calendar_observation_store_rpc_{key}_invalid"
        )
    canonical_with_offset = parsed.isoformat()
    canonical_with_z = canonical_with_offset.replace("+00:00", "Z")
    if value not in {canonical_with_offset, canonical_with_z}:
        raise CalendarObservationStoreError(
            f"calendar_observation_store_rpc_{key}_invalid"
        )
    return parsed.astimezone(UTC)


def _uuid4(value: object, key: str) -> UUID:
    parsed: UUID | None = None
    if type(value) is str:
        try:
            parsed = UUID(value)
        except (AttributeError, TypeError, ValueError):
            parsed = None
    if (
        parsed is None
        or parsed.version != 4
        or str(parsed) != value
    ):
        raise CalendarObservationStoreError(
            f"calendar_observation_store_rpc_{key}_invalid"
        )
    return parsed
