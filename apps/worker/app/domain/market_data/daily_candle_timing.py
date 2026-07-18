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
from app.domain.market_data.point_in_time import PointInTimeCandleV1
from app.domain.market_data.point_in_time_calendar import (
    PointInTimeKrDailySessionV1,
)

_CHECK_KIND = "kr_daily_candle_observed_at_not_before_next_regular_start"
_PROVIDER_RE = re.compile(r"[a-z][a-z0-9._-]{0,63}")
_KR_SYMBOL_RE = re.compile(r"[0-9]{6}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_EVIDENCE_FIELDS = frozenset(
    {
        "schema_version",
        "check_kind",
        "provider",
        "market",
        "symbol",
        "interval",
        "adjusted",
        "session_date",
        "candle_provider_event_at",
        "regular_start_at",
        "regular_end_at",
        "next_business_date",
        "cutoff_at",
        "candle_observed_at",
        "calendar_observed_at",
        "evidence_available_at",
        "candle_idempotency_key",
        "calendar_idempotency_key",
        "candle_provider_contract_sha256",
        "calendar_provider_contract_sha256",
        "candle_canonical_observation_sha256",
        "calendar_canonical_evidence_sha256",
        "canonical_timing_evidence_sha256",
    }
)


class DailyCandleTimeWindowError(KnownFailClosedError):
    def __init__(self, safe_message: str) -> None:
        super().__init__("daily_candle_time_window", safe_message)


@dataclass(frozen=True, slots=True)
class PointInTimeDailyCandleTimingEvidenceV1:
    provider: str
    market: str
    symbol: str
    interval: str
    adjusted: bool
    session_date: date
    candle_provider_event_at: datetime
    regular_start_at: datetime
    regular_end_at: datetime
    next_business_date: date
    cutoff_at: datetime
    candle_observed_at: datetime
    calendar_observed_at: datetime
    evidence_available_at: datetime
    candle_idempotency_key: str
    calendar_idempotency_key: str
    candle_provider_contract_sha256: str
    calendar_provider_contract_sha256: str
    candle_canonical_observation_sha256: str
    calendar_canonical_evidence_sha256: str
    canonical_timing_evidence_sha256: str
    check_kind: str = _CHECK_KIND
    schema_version: int = 1

    def __post_init__(self) -> None:
        _validate_evidence_fields(self)
        expected = _build_canonical_timing_evidence_sha256(
            provider=self.provider,
            market=self.market,
            symbol=self.symbol,
            interval=self.interval,
            adjusted=self.adjusted,
            session_date=self.session_date,
            candle_provider_event_at=self.candle_provider_event_at,
            regular_start_at=self.regular_start_at,
            regular_end_at=self.regular_end_at,
            next_business_date=self.next_business_date,
            cutoff_at=self.cutoff_at,
            candle_observed_at=self.candle_observed_at,
            calendar_observed_at=self.calendar_observed_at,
            evidence_available_at=self.evidence_available_at,
            candle_idempotency_key=self.candle_idempotency_key,
            calendar_idempotency_key=self.calendar_idempotency_key,
            candle_provider_contract_sha256=(
                self.candle_provider_contract_sha256
            ),
            calendar_provider_contract_sha256=(
                self.calendar_provider_contract_sha256
            ),
            candle_canonical_observation_sha256=(
                self.candle_canonical_observation_sha256
            ),
            calendar_canonical_evidence_sha256=(
                self.calendar_canonical_evidence_sha256
            ),
        )
        if self.canonical_timing_evidence_sha256 != expected:
            raise DailyCandleTimeWindowError(
                "daily_candle_timing_evidence_sha256_mismatch"
            )

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, object],
    ) -> PointInTimeDailyCandleTimingEvidenceV1:
        if set(payload) != _EVIDENCE_FIELDS:
            raise DailyCandleTimeWindowError(
                "daily_candle_timing_evidence_fields_mismatch"
            )
        return cls(
            provider=_require_provider(payload["provider"]),
            market=_require_literal(payload["market"], "KR", "market"),
            symbol=_require_kr_symbol(payload["symbol"]),
            interval=_require_literal(payload["interval"], "1d", "interval"),
            adjusted=_require_bool(payload["adjusted"], "adjusted"),
            session_date=_parse_canonical_date(
                payload["session_date"],
                "session_date",
            ),
            candle_provider_event_at=_parse_canonical_timestamp(
                payload["candle_provider_event_at"],
                "candle_provider_event_at",
            ),
            regular_start_at=_parse_canonical_timestamp(
                payload["regular_start_at"],
                "regular_start_at",
            ),
            regular_end_at=_parse_canonical_timestamp(
                payload["regular_end_at"],
                "regular_end_at",
            ),
            next_business_date=_parse_canonical_date(
                payload["next_business_date"],
                "next_business_date",
            ),
            cutoff_at=_parse_canonical_timestamp(
                payload["cutoff_at"],
                "cutoff_at",
            ),
            candle_observed_at=_parse_canonical_timestamp(
                payload["candle_observed_at"],
                "candle_observed_at",
            ),
            calendar_observed_at=_parse_canonical_timestamp(
                payload["calendar_observed_at"],
                "calendar_observed_at",
            ),
            evidence_available_at=_parse_canonical_timestamp(
                payload["evidence_available_at"],
                "evidence_available_at",
            ),
            candle_idempotency_key=_require_sha256(
                payload["candle_idempotency_key"],
                "candle_idempotency_key",
            ),
            calendar_idempotency_key=_require_sha256(
                payload["calendar_idempotency_key"],
                "calendar_idempotency_key",
            ),
            candle_provider_contract_sha256=_require_sha256(
                payload["candle_provider_contract_sha256"],
                "candle_provider_contract_sha256",
            ),
            calendar_provider_contract_sha256=_require_sha256(
                payload["calendar_provider_contract_sha256"],
                "calendar_provider_contract_sha256",
            ),
            candle_canonical_observation_sha256=_require_sha256(
                payload["candle_canonical_observation_sha256"],
                "candle_canonical_observation_sha256",
            ),
            calendar_canonical_evidence_sha256=_require_sha256(
                payload["calendar_canonical_evidence_sha256"],
                "calendar_canonical_evidence_sha256",
            ),
            canonical_timing_evidence_sha256=_require_sha256(
                payload["canonical_timing_evidence_sha256"],
                "canonical_timing_evidence_sha256",
            ),
            check_kind=_require_literal(
                payload["check_kind"],
                _CHECK_KIND,
                "check_kind",
            ),
            schema_version=_require_schema_version(payload["schema_version"]),
        )

    @property
    def idempotency_key(self) -> str:
        identity: JsonObject = {
            "schema_version": self.schema_version,
            "check_kind": self.check_kind,
            "candle_idempotency_key": self.candle_idempotency_key,
            "calendar_idempotency_key": self.calendar_idempotency_key,
        }
        return hashlib.sha256(_canonical_json(identity)).hexdigest()

    def to_payload(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "check_kind": self.check_kind,
            "provider": self.provider,
            "market": self.market,
            "symbol": self.symbol,
            "interval": self.interval,
            "adjusted": self.adjusted,
            "session_date": self.session_date.isoformat(),
            "candle_provider_event_at": _canonical_timestamp(
                self.candle_provider_event_at
            ),
            "regular_start_at": _canonical_timestamp(self.regular_start_at),
            "regular_end_at": _canonical_timestamp(self.regular_end_at),
            "next_business_date": self.next_business_date.isoformat(),
            "cutoff_at": _canonical_timestamp(self.cutoff_at),
            "candle_observed_at": _canonical_timestamp(self.candle_observed_at),
            "calendar_observed_at": _canonical_timestamp(
                self.calendar_observed_at
            ),
            "evidence_available_at": _canonical_timestamp(
                self.evidence_available_at
            ),
            "candle_idempotency_key": self.candle_idempotency_key,
            "calendar_idempotency_key": self.calendar_idempotency_key,
            "candle_provider_contract_sha256": (
                self.candle_provider_contract_sha256
            ),
            "calendar_provider_contract_sha256": (
                self.calendar_provider_contract_sha256
            ),
            "candle_canonical_observation_sha256": (
                self.candle_canonical_observation_sha256
            ),
            "calendar_canonical_evidence_sha256": (
                self.calendar_canonical_evidence_sha256
            ),
            "canonical_timing_evidence_sha256": (
                self.canonical_timing_evidence_sha256
            ),
        }


def build_daily_candle_timing_evidence(
    candle: object,
    session: object,
) -> PointInTimeDailyCandleTimingEvidenceV1:
    if type(candle) is not PointInTimeCandleV1:
        raise DailyCandleTimeWindowError("daily_candle_timing_candle_invalid")
    if type(session) is not PointInTimeKrDailySessionV1:
        raise DailyCandleTimeWindowError("daily_candle_timing_session_invalid")
    if candle.provider != session.provider:
        raise DailyCandleTimeWindowError("daily_candle_timing_provider_mismatch")
    if candle.market != session.market:
        raise DailyCandleTimeWindowError("daily_candle_timing_market_mismatch")
    if not session.is_open:
        raise DailyCandleTimeWindowError("daily_candle_timing_session_is_not_open")
    regular_start = session.regular_start_at
    regular_end = session.regular_end_at
    if regular_start is None or regular_end is None:
        raise DailyCandleTimeWindowError(
            "daily_candle_timing_open_session_hours_missing"
        )
    if candle.provider_event_at.astimezone(KST).date() != session.session_date:
        raise DailyCandleTimeWindowError(
            "daily_candle_timing_session_date_mismatch"
        )
    cutoff_at = session.next_regular_start_at
    if candle.observed_at < cutoff_at:
        raise DailyCandleTimeWindowError(
            "daily_candle_timing_candle_observed_before_cutoff"
        )
    if session.observed_at < cutoff_at:
        raise DailyCandleTimeWindowError(
            "daily_candle_timing_calendar_observed_before_cutoff"
        )
    available_at = max(candle.observed_at, session.observed_at)
    canonical_sha = _build_canonical_timing_evidence_sha256(
        provider=candle.provider,
        market=candle.market,
        symbol=candle.symbol,
        interval=candle.interval,
        adjusted=candle.adjusted,
        session_date=session.session_date,
        candle_provider_event_at=candle.provider_event_at,
        regular_start_at=regular_start,
        regular_end_at=regular_end,
        next_business_date=session.next_business_date,
        cutoff_at=cutoff_at,
        candle_observed_at=candle.observed_at,
        calendar_observed_at=session.observed_at,
        evidence_available_at=available_at,
        candle_idempotency_key=candle.idempotency_key,
        calendar_idempotency_key=session.idempotency_key,
        candle_provider_contract_sha256=candle.provider_contract_sha256,
        calendar_provider_contract_sha256=session.provider_contract_sha256,
        candle_canonical_observation_sha256=(
            candle.canonical_observation_sha256
        ),
        calendar_canonical_evidence_sha256=(
            session.canonical_evidence_sha256
        ),
    )
    return PointInTimeDailyCandleTimingEvidenceV1(
        provider=candle.provider,
        market=candle.market,
        symbol=candle.symbol,
        interval=candle.interval,
        adjusted=candle.adjusted,
        session_date=session.session_date,
        candle_provider_event_at=candle.provider_event_at,
        regular_start_at=regular_start,
        regular_end_at=regular_end,
        next_business_date=session.next_business_date,
        cutoff_at=cutoff_at,
        candle_observed_at=candle.observed_at,
        calendar_observed_at=session.observed_at,
        evidence_available_at=available_at,
        candle_idempotency_key=candle.idempotency_key,
        calendar_idempotency_key=session.idempotency_key,
        candle_provider_contract_sha256=candle.provider_contract_sha256,
        calendar_provider_contract_sha256=session.provider_contract_sha256,
        candle_canonical_observation_sha256=(
            candle.canonical_observation_sha256
        ),
        calendar_canonical_evidence_sha256=(
            session.canonical_evidence_sha256
        ),
        canonical_timing_evidence_sha256=canonical_sha,
    )


def _validate_evidence_fields(
    evidence: PointInTimeDailyCandleTimingEvidenceV1,
) -> None:
    _require_schema_version(evidence.schema_version)
    _require_literal(evidence.check_kind, _CHECK_KIND, "check_kind")
    _require_provider(evidence.provider)
    _require_literal(evidence.market, "KR", "market")
    _require_kr_symbol(evidence.symbol)
    _require_literal(evidence.interval, "1d", "interval")
    _require_bool(evidence.adjusted, "adjusted")
    _require_date(evidence.session_date, "session_date")
    _require_date(evidence.next_business_date, "next_business_date")
    if evidence.next_business_date <= evidence.session_date:
        raise DailyCandleTimeWindowError(
            "daily_candle_timing_next_business_date_not_later"
        )
    timestamps = {
        "candle_provider_event_at": evidence.candle_provider_event_at,
        "regular_start_at": evidence.regular_start_at,
        "regular_end_at": evidence.regular_end_at,
        "cutoff_at": evidence.cutoff_at,
        "candle_observed_at": evidence.candle_observed_at,
        "calendar_observed_at": evidence.calendar_observed_at,
        "evidence_available_at": evidence.evidence_available_at,
    }
    for field_name, value in timestamps.items():
        _require_aware(value, field_name)
    if evidence.candle_provider_event_at.astimezone(KST).date() != evidence.session_date:
        raise DailyCandleTimeWindowError(
            "daily_candle_timing_session_date_mismatch"
        )
    if evidence.regular_start_at.astimezone(KST).date() != evidence.session_date:
        raise DailyCandleTimeWindowError(
            "daily_candle_timing_regular_start_date_mismatch"
        )
    if evidence.regular_end_at.astimezone(KST).date() != evidence.session_date:
        raise DailyCandleTimeWindowError(
            "daily_candle_timing_regular_end_date_mismatch"
        )
    if evidence.cutoff_at.astimezone(KST).date() != evidence.next_business_date:
        raise DailyCandleTimeWindowError(
            "daily_candle_timing_cutoff_date_mismatch"
        )
    if evidence.regular_start_at >= evidence.regular_end_at:
        raise DailyCandleTimeWindowError(
            "daily_candle_timing_regular_time_order_invalid"
        )
    if evidence.regular_end_at >= evidence.cutoff_at:
        raise DailyCandleTimeWindowError(
            "daily_candle_timing_cutoff_not_after_regular_end"
        )
    if evidence.candle_observed_at < evidence.cutoff_at:
        raise DailyCandleTimeWindowError(
            "daily_candle_timing_candle_observed_before_cutoff"
        )
    if evidence.calendar_observed_at < evidence.cutoff_at:
        raise DailyCandleTimeWindowError(
            "daily_candle_timing_calendar_observed_before_cutoff"
        )
    expected_available_at = max(
        evidence.candle_observed_at,
        evidence.calendar_observed_at,
    )
    if evidence.evidence_available_at != expected_available_at:
        raise DailyCandleTimeWindowError(
            "daily_candle_timing_available_at_mismatch"
        )
    sha_fields = {
        "candle_idempotency_key": evidence.candle_idempotency_key,
        "calendar_idempotency_key": evidence.calendar_idempotency_key,
        "candle_provider_contract_sha256": (
            evidence.candle_provider_contract_sha256
        ),
        "calendar_provider_contract_sha256": (
            evidence.calendar_provider_contract_sha256
        ),
        "candle_canonical_observation_sha256": (
            evidence.candle_canonical_observation_sha256
        ),
        "calendar_canonical_evidence_sha256": (
            evidence.calendar_canonical_evidence_sha256
        ),
        "canonical_timing_evidence_sha256": (
            evidence.canonical_timing_evidence_sha256
        ),
    }
    for field_name, sha_value in sha_fields.items():
        _require_sha256(sha_value, field_name)
    expected_candle_identity = _build_candle_identity_sha256(
        provider=evidence.provider,
        symbol=evidence.symbol,
        market=evidence.market,
        interval=evidence.interval,
        adjusted=evidence.adjusted,
        provider_event_at=evidence.candle_provider_event_at,
    )
    if evidence.candle_idempotency_key != expected_candle_identity:
        raise DailyCandleTimeWindowError(
            "daily_candle_timing_candle_identity_mismatch"
        )
    expected_calendar_identity = _build_calendar_identity_sha256(
        provider=evidence.provider,
        market=evidence.market,
        session_date=evidence.session_date,
    )
    if evidence.calendar_idempotency_key != expected_calendar_identity:
        raise DailyCandleTimeWindowError(
            "daily_candle_timing_calendar_identity_mismatch"
        )


def _build_candle_identity_sha256(
    *,
    provider: str,
    symbol: str,
    market: str,
    interval: str,
    adjusted: bool,
    provider_event_at: datetime,
) -> str:
    identity: JsonObject = {
        "schema_version": 1,
        "provider": provider,
        "symbol": symbol,
        "market": market,
        "interval": interval,
        "adjusted": adjusted,
        "provider_event_at": _canonical_timestamp(provider_event_at),
    }
    return hashlib.sha256(_canonical_json(identity)).hexdigest()


def _build_calendar_identity_sha256(
    *,
    provider: str,
    market: str,
    session_date: date,
) -> str:
    identity: JsonObject = {
        "schema_version": 1,
        "provider": provider,
        "market": market,
        "session_date": session_date.isoformat(),
    }
    return hashlib.sha256(_canonical_json(identity)).hexdigest()


def _build_canonical_timing_evidence_sha256(
    *,
    provider: str,
    market: str,
    symbol: str,
    interval: str,
    adjusted: bool,
    session_date: date,
    candle_provider_event_at: datetime,
    regular_start_at: datetime,
    regular_end_at: datetime,
    next_business_date: date,
    cutoff_at: datetime,
    candle_observed_at: datetime,
    calendar_observed_at: datetime,
    evidence_available_at: datetime,
    candle_idempotency_key: str,
    calendar_idempotency_key: str,
    candle_provider_contract_sha256: str,
    calendar_provider_contract_sha256: str,
    candle_canonical_observation_sha256: str,
    calendar_canonical_evidence_sha256: str,
) -> str:
    content: JsonObject = {
        "schema_version": 1,
        "check_kind": _CHECK_KIND,
        "provider": provider,
        "market": market,
        "symbol": symbol,
        "interval": interval,
        "adjusted": adjusted,
        "session_date": session_date.isoformat(),
        "candle_provider_event_at": _canonical_timestamp(
            candle_provider_event_at
        ),
        "regular_start_at": _canonical_timestamp(regular_start_at),
        "regular_end_at": _canonical_timestamp(regular_end_at),
        "next_business_date": next_business_date.isoformat(),
        "cutoff_at": _canonical_timestamp(cutoff_at),
        "candle_observed_at": _canonical_timestamp(candle_observed_at),
        "calendar_observed_at": _canonical_timestamp(calendar_observed_at),
        "evidence_available_at": _canonical_timestamp(evidence_available_at),
        "candle_idempotency_key": candle_idempotency_key,
        "calendar_idempotency_key": calendar_idempotency_key,
        "candle_provider_contract_sha256": candle_provider_contract_sha256,
        "calendar_provider_contract_sha256": calendar_provider_contract_sha256,
        "candle_canonical_observation_sha256": (
            candle_canonical_observation_sha256
        ),
        "calendar_canonical_evidence_sha256": (
            calendar_canonical_evidence_sha256
        ),
    }
    return hashlib.sha256(_canonical_json(content)).hexdigest()


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


def _parse_canonical_timestamp(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise DailyCandleTimeWindowError(
            f"daily_candle_timing_{field_name}_must_be_timestamp"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DailyCandleTimeWindowError(
            f"daily_candle_timing_{field_name}_must_be_timestamp"
        ) from exc
    _require_aware(parsed, field_name)
    if _canonical_timestamp(parsed) != value:
        raise DailyCandleTimeWindowError(
            f"daily_candle_timing_{field_name}_must_be_canonical_utc"
        )
    return parsed


def _parse_canonical_date(value: object, field_name: str) -> date:
    if not isinstance(value, str):
        raise DailyCandleTimeWindowError(
            f"daily_candle_timing_{field_name}_must_be_date"
        )
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise DailyCandleTimeWindowError(
            f"daily_candle_timing_{field_name}_must_be_date"
        ) from exc
    if parsed.isoformat() != value:
        raise DailyCandleTimeWindowError(
            f"daily_candle_timing_{field_name}_must_be_canonical_date"
        )
    return parsed


def _require_schema_version(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value != 1:
        raise DailyCandleTimeWindowError(
            "daily_candle_timing_schema_version_must_be_1"
        )
    return value


def _require_provider(value: object) -> str:
    if not isinstance(value, str) or _PROVIDER_RE.fullmatch(value) is None:
        raise DailyCandleTimeWindowError("daily_candle_timing_provider_invalid")
    return value


def _require_kr_symbol(value: object) -> str:
    if not isinstance(value, str) or _KR_SYMBOL_RE.fullmatch(value) is None:
        raise DailyCandleTimeWindowError("daily_candle_timing_symbol_invalid")
    return value


def _require_literal(value: object, expected: str, field_name: str) -> str:
    if not isinstance(value, str) or value != expected:
        raise DailyCandleTimeWindowError(
            f"daily_candle_timing_{field_name}_must_be_{expected.lower()}"
        )
    return value


def _require_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise DailyCandleTimeWindowError(
            f"daily_candle_timing_{field_name}_must_be_boolean"
        )
    return value


def _require_date(value: object, field_name: str) -> date:
    if type(value) is not date:
        raise DailyCandleTimeWindowError(
            f"daily_candle_timing_{field_name}_must_be_date"
        )
    return value


def _require_aware(value: object, field_name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise DailyCandleTimeWindowError(
            f"daily_candle_timing_{field_name}_requires_timezone"
        )
    return value


def _require_sha256(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise DailyCandleTimeWindowError(
            f"daily_candle_timing_{field_name}_must_be_sha256_hex"
        )
    return value
