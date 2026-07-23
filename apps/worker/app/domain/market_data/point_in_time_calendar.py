from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime

from app.domain.common.errors import KnownFailClosedError
from app.domain.common.json import JsonObject
from app.domain.common.time import KST

_PROVIDER_RE = re.compile(r"[a-z][a-z0-9._-]{0,63}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SESSION_FIELDS = frozenset(
    {
        "schema_version",
        "provider",
        "market",
        "session_date",
        "is_open",
        "regular_start_at",
        "regular_end_at",
        "next_business_date",
        "next_regular_start_at",
        "next_regular_end_at",
        "observed_at",
        "provider_contract_sha256",
        "canonical_evidence_sha256",
    }
)


class PointInTimeCalendarError(KnownFailClosedError):
    def __init__(self, safe_message: str) -> None:
        super().__init__("point_in_time_calendar", safe_message)


@dataclass(frozen=True, slots=True)
class PointInTimeKrDailySessionV1:
    provider: str
    market: str
    session_date: date
    is_open: bool
    regular_start_at: datetime | None
    regular_end_at: datetime | None
    next_business_date: date
    next_regular_start_at: datetime
    next_regular_end_at: datetime
    observed_at: datetime
    provider_contract_sha256: str
    canonical_evidence_sha256: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        _require_schema_version(self.schema_version)
        _require_provider(self.provider)
        _require_market(self.market)
        _require_date(self.session_date, "session_date")
        _require_date(self.next_business_date, "next_business_date")
        _require_bool(self.is_open, "is_open")
        _validate_regular_session(
            session_date=self.session_date,
            is_open=self.is_open,
            start_at=self.regular_start_at,
            end_at=self.regular_end_at,
        )
        _validate_next_session(
            session_date=self.session_date,
            next_business_date=self.next_business_date,
            start_at=self.next_regular_start_at,
            end_at=self.next_regular_end_at,
        )
        _require_aware(self.observed_at, "observed_at")
        _require_sha256(self.provider_contract_sha256, "provider_contract_sha256")
        _require_sha256(self.canonical_evidence_sha256, "canonical_evidence_sha256")
        expected = _build_canonical_evidence_sha256(
            provider=self.provider,
            market=self.market,
            session_date=self.session_date,
            is_open=self.is_open,
            regular_start_at=self.regular_start_at,
            regular_end_at=self.regular_end_at,
            next_business_date=self.next_business_date,
            next_regular_start_at=self.next_regular_start_at,
            next_regular_end_at=self.next_regular_end_at,
            provider_contract_sha256=self.provider_contract_sha256,
        )
        if self.canonical_evidence_sha256 != expected:
            raise PointInTimeCalendarError(
                "point_in_time_kr_session_evidence_sha256_mismatch"
            )

    @classmethod
    def create(
        cls,
        *,
        provider: object,
        market: object,
        session_date: object,
        is_open: object,
        regular_start_at: object,
        regular_end_at: object,
        next_business_date: object,
        next_regular_start_at: object,
        next_regular_end_at: object,
        observed_at: object,
        provider_contract_sha256: object,
    ) -> PointInTimeKrDailySessionV1:
        valid_provider = _require_provider(provider)
        valid_market = _require_market(market)
        valid_session_date = _require_date(session_date, "session_date")
        valid_is_open = _require_bool(is_open, "is_open")
        valid_start = _require_optional_aware(regular_start_at, "regular_start_at")
        valid_end = _require_optional_aware(regular_end_at, "regular_end_at")
        valid_next_date = _require_date(next_business_date, "next_business_date")
        valid_next_start = _require_aware(
            next_regular_start_at,
            "next_regular_start_at",
        )
        valid_next_end = _require_aware(
            next_regular_end_at,
            "next_regular_end_at",
        )
        valid_observed_at = _require_aware(observed_at, "observed_at")
        valid_contract_sha = _require_sha256(
            provider_contract_sha256,
            "provider_contract_sha256",
        )
        _validate_regular_session(
            session_date=valid_session_date,
            is_open=valid_is_open,
            start_at=valid_start,
            end_at=valid_end,
        )
        _validate_next_session(
            session_date=valid_session_date,
            next_business_date=valid_next_date,
            start_at=valid_next_start,
            end_at=valid_next_end,
        )
        evidence_sha = _build_canonical_evidence_sha256(
            provider=valid_provider,
            market=valid_market,
            session_date=valid_session_date,
            is_open=valid_is_open,
            regular_start_at=valid_start,
            regular_end_at=valid_end,
            next_business_date=valid_next_date,
            next_regular_start_at=valid_next_start,
            next_regular_end_at=valid_next_end,
            provider_contract_sha256=valid_contract_sha,
        )
        return cls(
            provider=valid_provider,
            market=valid_market,
            session_date=valid_session_date,
            is_open=valid_is_open,
            regular_start_at=valid_start,
            regular_end_at=valid_end,
            next_business_date=valid_next_date,
            next_regular_start_at=valid_next_start,
            next_regular_end_at=valid_next_end,
            observed_at=valid_observed_at,
            provider_contract_sha256=valid_contract_sha,
            canonical_evidence_sha256=evidence_sha,
        )

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, object],
    ) -> PointInTimeKrDailySessionV1:
        if set(payload) != _SESSION_FIELDS:
            raise PointInTimeCalendarError(
                "point_in_time_kr_session_fields_mismatch"
            )
        _require_schema_version(payload["schema_version"])
        expected_sha = _require_sha256(
            payload["canonical_evidence_sha256"],
            "canonical_evidence_sha256",
        )
        evidence = cls.create(
            provider=payload["provider"],
            market=payload["market"],
            session_date=_parse_canonical_date(
                payload["session_date"],
                "session_date",
            ),
            is_open=payload["is_open"],
            regular_start_at=_parse_optional_canonical_timestamp(
                payload["regular_start_at"],
                "regular_start_at",
            ),
            regular_end_at=_parse_optional_canonical_timestamp(
                payload["regular_end_at"],
                "regular_end_at",
            ),
            next_business_date=_parse_canonical_date(
                payload["next_business_date"],
                "next_business_date",
            ),
            next_regular_start_at=_parse_canonical_timestamp(
                payload["next_regular_start_at"],
                "next_regular_start_at",
            ),
            next_regular_end_at=_parse_canonical_timestamp(
                payload["next_regular_end_at"],
                "next_regular_end_at",
            ),
            observed_at=_parse_canonical_timestamp(
                payload["observed_at"],
                "observed_at",
            ),
            provider_contract_sha256=payload["provider_contract_sha256"],
        )
        if evidence.canonical_evidence_sha256 != expected_sha:
            raise PointInTimeCalendarError(
                "point_in_time_kr_session_evidence_sha256_mismatch"
            )
        return evidence

    @property
    def idempotency_key(self) -> str:
        return kr_daily_session_idempotency_key(
            provider=self.provider,
            market=self.market,
            session_date=self.session_date,
            schema_version=self.schema_version,
        )

    def to_payload(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "provider": self.provider,
            "market": self.market,
            "session_date": self.session_date.isoformat(),
            "is_open": self.is_open,
            "regular_start_at": _optional_canonical_timestamp(
                self.regular_start_at
            ),
            "regular_end_at": _optional_canonical_timestamp(self.regular_end_at),
            "next_business_date": self.next_business_date.isoformat(),
            "next_regular_start_at": _canonical_timestamp(
                self.next_regular_start_at
            ),
            "next_regular_end_at": _canonical_timestamp(self.next_regular_end_at),
            "observed_at": _canonical_timestamp(self.observed_at),
            "provider_contract_sha256": self.provider_contract_sha256,
            "canonical_evidence_sha256": self.canonical_evidence_sha256,
        }


def kr_daily_session_idempotency_key(
    *,
    provider: str,
    market: str,
    session_date: date,
    schema_version: int = 1,
) -> str:
    """Derive the canonical identity for one provider/market/session date."""
    _require_schema_version(schema_version)
    _require_provider(provider)
    _require_market(market)
    _require_date(session_date, "session_date")
    identity: JsonObject = {
        "schema_version": schema_version,
        "provider": provider,
        "market": market,
        "session_date": session_date.isoformat(),
    }
    return hashlib.sha256(_canonical_json(identity)).hexdigest()


def _build_canonical_evidence_sha256(
    *,
    provider: str,
    market: str,
    session_date: date,
    is_open: bool,
    regular_start_at: datetime | None,
    regular_end_at: datetime | None,
    next_business_date: date,
    next_regular_start_at: datetime,
    next_regular_end_at: datetime,
    provider_contract_sha256: str,
) -> str:
    evidence: JsonObject = {
        "schema_version": 1,
        "provider": provider,
        "market": market,
        "session_date": session_date.isoformat(),
        "is_open": is_open,
        "regular_start_at": _optional_canonical_timestamp(regular_start_at),
        "regular_end_at": _optional_canonical_timestamp(regular_end_at),
        "next_business_date": next_business_date.isoformat(),
        "next_regular_start_at": _canonical_timestamp(next_regular_start_at),
        "next_regular_end_at": _canonical_timestamp(next_regular_end_at),
        "provider_contract_sha256": provider_contract_sha256,
    }
    return hashlib.sha256(_canonical_json(evidence)).hexdigest()


def _validate_regular_session(
    *,
    session_date: date,
    is_open: bool,
    start_at: datetime | None,
    end_at: datetime | None,
) -> None:
    if not is_open:
        if start_at is not None or end_at is not None:
            raise PointInTimeCalendarError(
                "point_in_time_kr_session_closed_day_has_regular_hours"
            )
        return
    if start_at is None or end_at is None:
        raise PointInTimeCalendarError(
            "point_in_time_kr_session_open_day_missing_regular_hours"
        )
    _validate_session_times(
        session_date,
        start_at,
        end_at,
        prefix="point_in_time_kr_session_regular",
    )


def _validate_next_session(
    *,
    session_date: date,
    next_business_date: date,
    start_at: datetime,
    end_at: datetime,
) -> None:
    if next_business_date <= session_date:
        raise PointInTimeCalendarError(
            "point_in_time_kr_session_next_business_date_not_later"
        )
    _validate_session_times(
        next_business_date,
        start_at,
        end_at,
        prefix="point_in_time_kr_session_next_regular",
    )


def _validate_session_times(
    session_date: date,
    start_at: datetime,
    end_at: datetime,
    *,
    prefix: str,
) -> None:
    _require_aware(start_at, f"{prefix}_start_at")
    _require_aware(end_at, f"{prefix}_end_at")
    if start_at >= end_at:
        raise PointInTimeCalendarError(f"{prefix}_time_order_invalid")
    if start_at.astimezone(KST).date() != session_date:
        raise PointInTimeCalendarError(f"{prefix}_start_date_mismatch")
    if end_at.astimezone(KST).date() != session_date:
        raise PointInTimeCalendarError(f"{prefix}_end_date_mismatch")


def _canonical_json(value: JsonObject) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _canonical_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _optional_canonical_timestamp(value: datetime | None) -> str | None:
    return None if value is None else _canonical_timestamp(value)


def _parse_canonical_timestamp(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise PointInTimeCalendarError(
            f"point_in_time_kr_session_{field_name}_must_be_timestamp"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PointInTimeCalendarError(
            f"point_in_time_kr_session_{field_name}_must_be_timestamp"
        ) from exc
    _require_aware(parsed, field_name)
    if _canonical_timestamp(parsed) != value:
        raise PointInTimeCalendarError(
            f"point_in_time_kr_session_{field_name}_must_be_canonical_utc"
        )
    return parsed


def _parse_optional_canonical_timestamp(
    value: object,
    field_name: str,
) -> datetime | None:
    if value is None:
        return None
    return _parse_canonical_timestamp(value, field_name)


def _parse_canonical_date(value: object, field_name: str) -> date:
    if not isinstance(value, str):
        raise PointInTimeCalendarError(
            f"point_in_time_kr_session_{field_name}_must_be_date"
        )
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise PointInTimeCalendarError(
            f"point_in_time_kr_session_{field_name}_must_be_date"
        ) from exc
    if parsed.isoformat() != value:
        raise PointInTimeCalendarError(
            f"point_in_time_kr_session_{field_name}_must_be_canonical_date"
        )
    return parsed


def _require_schema_version(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value != 1:
        raise PointInTimeCalendarError(
            "point_in_time_kr_session_schema_version_must_be_1"
        )
    return value


def _require_provider(value: object) -> str:
    if not isinstance(value, str) or _PROVIDER_RE.fullmatch(value) is None:
        raise PointInTimeCalendarError(
            "point_in_time_kr_session_provider_invalid"
        )
    return value


def _require_market(value: object) -> str:
    if not isinstance(value, str) or value != "KR":
        raise PointInTimeCalendarError(
            "point_in_time_kr_session_market_must_be_kr"
        )
    return value


def _require_date(value: object, field_name: str) -> date:
    if type(value) is not date:
        raise PointInTimeCalendarError(
            f"point_in_time_kr_session_{field_name}_must_be_date"
        )
    return value


def _require_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise PointInTimeCalendarError(
            f"point_in_time_kr_session_{field_name}_must_be_boolean"
        )
    return value


def _require_optional_aware(
    value: object,
    field_name: str,
) -> datetime | None:
    if value is None:
        return None
    return _require_aware(value, field_name)


def _require_aware(value: object, field_name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise PointInTimeCalendarError(
            f"point_in_time_kr_session_{field_name}_requires_timezone"
        )
    return value


def _require_sha256(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise PointInTimeCalendarError(
            f"point_in_time_kr_session_{field_name}_must_be_sha256_hex"
        )
    return value
