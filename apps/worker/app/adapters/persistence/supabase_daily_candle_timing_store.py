from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID

import httpx

from app.application.ports.daily_candle_timing_store_port import (
    DailyCandleTimingStoreError,
    DailyCandleTimingWriteReceipt,
    DailyCandleTimingWriteStatus,
)
from app.config import Settings
from app.domain.common.json import JsonObject
from app.domain.market_data.daily_candle_timing import (
    DailyCandleTimeWindowError,
    PointInTimeDailyCandleTimingEvidenceV1,
)
from app.domain.market_data.point_in_time_calendar import (
    PointInTimeCalendarError,
    PointInTimeKrDailySessionV1,
)
from app.infrastructure.supabase_headers import supabase_api_headers

PITDailyCandleTimingRpc = Literal[
    "append_pit_daily_candle_timing_evidence_v1"
]

PIT_DAILY_CANDLE_TIMING_RPC_ALLOWLIST: frozenset[str] = frozenset(
    {"append_pit_daily_candle_timing_evidence_v1"}
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_QUARANTINE_REASONS = frozenset(
    {
        "pit_calendar_observation_time_regressed",
        "pit_calendar_historical_hash_recurrence_ambiguous",
        "pit_calendar_revision_time_not_increasing",
        "pit_timing_request_idempotency_conflict",
        "pit_timing_candle_revision_missing",
        "pit_timing_calendar_revision_missing",
        "pit_timing_source_binding_mismatch",
        "pit_timing_available_at_mismatch",
        "pit_timing_canonical_sha256_mismatch",
        "pit_timing_observation_time_regressed",
        "pit_timing_historical_hash_recurrence_ambiguous",
        "pit_timing_revision_time_not_increasing",
    }
)
_RECEIPT_FIELDS = {
    "status",
    "request_idempotency_key",
    "timing_idempotency_key",
    "canonical_timing_evidence_sha256",
    "calendar_revision",
    "timing_revision",
    "calendar_inserted",
    "timing_inserted",
    "evidence_available_at",
    "quarantine_id",
    "reason_code",
}


class SupabaseDailyCandleTimingStore:
    """Persist calendar revisions and their bound candle timing evidence."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not settings.supabase_url or settings.supabase_secret_key is None:
            raise DailyCandleTimingStoreError(
                "daily_candle_timing_store_credentials_missing"
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

    async def append_timing_evidence(
        self,
        request_idempotency_key: str,
        calendar: PointInTimeKrDailySessionV1,
        timing: PointInTimeDailyCandleTimingEvidenceV1,
    ) -> DailyCandleTimingWriteReceipt:
        request_key = _request_idempotency_key(request_idempotency_key)
        canonical_calendar = _canonical_calendar(calendar)
        canonical_timing = _canonical_timing(timing)
        _validate_calendar_binding(canonical_calendar, canonical_timing)

        row = _singleton_row(
            await self._rpc(
                "append_pit_daily_candle_timing_evidence_v1",
                {
                    "p_request_idempotency_key": request_key,
                    "p_calendar": canonical_calendar.to_payload(),
                    "p_timing": canonical_timing.to_payload(),
                },
            )
        )
        if set(row) != _RECEIPT_FIELDS:
            raise DailyCandleTimingStoreError(
                "daily_candle_timing_store_rpc_result_shape_invalid"
            )

        status = _required_text(row, "status")
        returned_request_key = _sha256(row, "request_idempotency_key")
        if returned_request_key != request_key:
            raise DailyCandleTimingStoreError(
                "daily_candle_timing_store_rpc_request_identity_mismatch"
            )
        calendar_inserted = _boolean(row, "calendar_inserted")
        timing_inserted = _boolean(row, "timing_inserted")

        if status == "quarantined":
            if timing_inserted:
                raise DailyCandleTimingStoreError(
                    "daily_candle_timing_store_quarantine_receipt_invalid"
                )
            self._raise_quarantine(
                row,
                timing=canonical_timing,
            )

        if status not in {"stored", "replayed"}:
            raise DailyCandleTimingStoreError(
                "daily_candle_timing_store_rpc_status_invalid"
            )
        if (
            row.get("quarantine_id") is not None
            or row.get("reason_code") is not None
        ):
            raise DailyCandleTimingStoreError(
                "daily_candle_timing_store_rpc_result_shape_invalid"
            )

        timing_idempotency_key = _sha256(row, "timing_idempotency_key")
        timing_evidence_sha256 = _sha256(
            row,
            "canonical_timing_evidence_sha256",
        )
        calendar_revision = _positive_int(row, "calendar_revision")
        timing_revision = _positive_int(row, "timing_revision")
        evidence_available_at = _aware_datetime(row, "evidence_available_at")

        if timing_idempotency_key != canonical_timing.idempotency_key:
            raise DailyCandleTimingStoreError(
                "daily_candle_timing_store_rpc_timing_identity_mismatch"
            )
        if (
            timing_evidence_sha256
            != canonical_timing.canonical_timing_evidence_sha256
        ):
            raise DailyCandleTimingStoreError(
                "daily_candle_timing_store_rpc_timing_hash_mismatch"
            )
        if evidence_available_at != canonical_timing.evidence_available_at:
            raise DailyCandleTimingStoreError(
                "daily_candle_timing_store_rpc_available_at_mismatch"
            )
        if (status, timing_inserted) not in {
            ("stored", True),
            ("replayed", False),
        }:
            raise DailyCandleTimingStoreError(
                "daily_candle_timing_store_rpc_status_invalid"
            )

        return DailyCandleTimingWriteReceipt(
            status=cast(DailyCandleTimingWriteStatus, status),
            request_idempotency_key=returned_request_key,
            timing_idempotency_key=timing_idempotency_key,
            canonical_timing_evidence_sha256=timing_evidence_sha256,
            calendar_revision=calendar_revision,
            timing_revision=timing_revision,
            calendar_inserted=calendar_inserted,
            timing_inserted=timing_inserted,
            evidence_available_at=evidence_available_at,
            quarantine_id=None,
            reason_code=None,
        )

    def _raise_quarantine(
        self,
        row: Mapping[str, object],
        *,
        timing: PointInTimeDailyCandleTimingEvidenceV1,
    ) -> None:
        _canonical_uuid(row.get("quarantine_id"))
        reason_code = row.get("reason_code")
        if not isinstance(reason_code, str) or reason_code not in _QUARANTINE_REASONS:
            raise DailyCandleTimingStoreError(
                "daily_candle_timing_store_quarantine_receipt_invalid"
            )

        timing_key = _optional_sha256(row, "timing_idempotency_key")
        if timing_key is not None and timing_key != timing.idempotency_key:
            raise DailyCandleTimingStoreError(
                "daily_candle_timing_store_quarantine_receipt_invalid"
            )
        timing_sha = _optional_sha256(
            row,
            "canonical_timing_evidence_sha256",
        )
        if (
            timing_sha is not None
            and timing_sha != timing.canonical_timing_evidence_sha256
        ):
            raise DailyCandleTimingStoreError(
                "daily_candle_timing_store_quarantine_receipt_invalid"
            )
        _optional_positive_int(row, "calendar_revision")
        _optional_positive_int(row, "timing_revision")
        available_at = _optional_aware_datetime(row, "evidence_available_at")
        if available_at is not None and available_at != timing.evidence_available_at:
            raise DailyCandleTimingStoreError(
                "daily_candle_timing_store_quarantine_receipt_invalid"
            )
        raise DailyCandleTimingStoreError(reason_code)

    async def _rpc(
        self,
        rpc: PITDailyCandleTimingRpc,
        payload: JsonObject,
    ) -> object:
        if rpc not in PIT_DAILY_CANDLE_TIMING_RPC_ALLOWLIST:
            raise DailyCandleTimingStoreError(
                "daily_candle_timing_store_rpc_not_allowed"
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
            raise DailyCandleTimingStoreError(
                "daily_candle_timing_store_rpc_failed_or_returned_invalid_json"
            ) from exc


def _request_idempotency_key(value: object) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise DailyCandleTimingStoreError(
            "daily_candle_timing_store_request_idempotency_key_invalid"
        )
    return value


def _canonical_calendar(value: object) -> PointInTimeKrDailySessionV1:
    if type(value) is not PointInTimeKrDailySessionV1:
        raise DailyCandleTimingStoreError(
            "daily_candle_timing_store_calendar_invalid"
        )
    try:
        canonical = PointInTimeKrDailySessionV1.from_payload(value.to_payload())
    except (
        AttributeError,
        OverflowError,
        TypeError,
        ValueError,
        PointInTimeCalendarError,
    ) as exc:
        raise DailyCandleTimingStoreError(
            "daily_candle_timing_store_calendar_invalid"
        ) from exc
    if canonical != value:
        raise DailyCandleTimingStoreError(
            "daily_candle_timing_store_calendar_invalid"
        )
    return canonical


def _canonical_timing(value: object) -> PointInTimeDailyCandleTimingEvidenceV1:
    if type(value) is not PointInTimeDailyCandleTimingEvidenceV1:
        raise DailyCandleTimingStoreError(
            "daily_candle_timing_store_timing_invalid"
        )
    try:
        canonical = PointInTimeDailyCandleTimingEvidenceV1.from_payload(
            value.to_payload()
        )
    except (
        AttributeError,
        OverflowError,
        TypeError,
        ValueError,
        DailyCandleTimeWindowError,
    ) as exc:
        raise DailyCandleTimingStoreError(
            "daily_candle_timing_store_timing_invalid"
        ) from exc
    if canonical != value:
        raise DailyCandleTimingStoreError(
            "daily_candle_timing_store_timing_invalid"
        )
    return canonical


def _validate_calendar_binding(
    calendar: PointInTimeKrDailySessionV1,
    timing: PointInTimeDailyCandleTimingEvidenceV1,
) -> None:
    expected = (
        calendar.provider,
        calendar.market,
        calendar.session_date,
        calendar.regular_start_at,
        calendar.regular_end_at,
        calendar.next_business_date,
        calendar.next_regular_start_at,
        calendar.observed_at,
        calendar.idempotency_key,
        calendar.provider_contract_sha256,
        calendar.canonical_evidence_sha256,
    )
    actual = (
        timing.provider,
        timing.market,
        timing.session_date,
        timing.regular_start_at,
        timing.regular_end_at,
        timing.next_business_date,
        timing.cutoff_at,
        timing.calendar_observed_at,
        timing.calendar_idempotency_key,
        timing.calendar_provider_contract_sha256,
        timing.calendar_canonical_evidence_sha256,
    )
    if actual != expected:
        raise DailyCandleTimingStoreError(
            "daily_candle_timing_store_calendar_binding_mismatch"
        )


def _singleton_row(value: object) -> Mapping[str, object]:
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise DailyCandleTimingStoreError(
            "daily_candle_timing_store_rpc_result_invalid"
        )
    return value[0]


def _required_text(row: Mapping[str, object], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise DailyCandleTimingStoreError(
            f"daily_candle_timing_store_rpc_{key}_invalid"
        )
    return value


def _sha256(row: Mapping[str, object], key: str) -> str:
    value = _required_text(row, key)
    if _SHA256.fullmatch(value) is None:
        raise DailyCandleTimingStoreError(
            f"daily_candle_timing_store_rpc_{key}_invalid"
        )
    return value


def _optional_sha256(row: Mapping[str, object], key: str) -> str | None:
    if row.get(key) is None:
        return None
    return _sha256(row, key)


def _positive_int(row: Mapping[str, object], key: str) -> int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DailyCandleTimingStoreError(
            f"daily_candle_timing_store_rpc_{key}_invalid"
        )
    return value


def _optional_positive_int(row: Mapping[str, object], key: str) -> int | None:
    if row.get(key) is None:
        return None
    return _positive_int(row, key)


def _boolean(row: Mapping[str, object], key: str) -> bool:
    value = row.get(key)
    if not isinstance(value, bool):
        raise DailyCandleTimingStoreError(
            f"daily_candle_timing_store_rpc_{key}_invalid"
        )
    return value


def _aware_datetime(row: Mapping[str, object], key: str) -> datetime:
    value = _required_text(row, key)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DailyCandleTimingStoreError(
            f"daily_candle_timing_store_rpc_{key}_invalid"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DailyCandleTimingStoreError(
            f"daily_candle_timing_store_rpc_{key}_invalid"
        )
    return parsed.astimezone(UTC)


def _optional_aware_datetime(
    row: Mapping[str, object],
    key: str,
) -> datetime | None:
    if row.get(key) is None:
        return None
    return _aware_datetime(row, key)


def _canonical_uuid(value: object) -> str:
    if not isinstance(value, str):
        raise DailyCandleTimingStoreError(
            "daily_candle_timing_store_quarantine_receipt_invalid"
        )
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise DailyCandleTimingStoreError(
            "daily_candle_timing_store_quarantine_receipt_invalid"
        ) from exc
    if str(parsed) != value or parsed.version not in {1, 2, 3, 4, 5}:
        raise DailyCandleTimingStoreError(
            "daily_candle_timing_store_quarantine_receipt_invalid"
        )
    return value
