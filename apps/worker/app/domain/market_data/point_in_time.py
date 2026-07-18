from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from app.domain.common.errors import KnownFailClosedError
from app.domain.common.json import JsonObject

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_PROVIDER_RE = re.compile(r"[a-z][a-z0-9._-]{0,63}")
_KR_SYMBOL_RE = re.compile(r"[0-9]{6}")
_CANDLE_FIELDS = frozenset(
    {
        "schema_version",
        "provider",
        "symbol",
        "market",
        "interval",
        "adjusted",
        "provider_event_at",
        "observed_at",
        "currency",
        "open_krw",
        "high_krw",
        "low_krw",
        "close_krw",
        "volume",
        "provider_contract_sha256",
        "canonical_observation_sha256",
    }
)


class PointInTimeDataError(KnownFailClosedError):
    def __init__(self, safe_message: str) -> None:
        super().__init__("point_in_time_data", safe_message)


@dataclass(frozen=True, slots=True)
class PointInTimeCandleV1:
    provider: str
    symbol: str
    market: str
    interval: str
    adjusted: bool
    provider_event_at: datetime
    observed_at: datetime
    currency: str
    open_krw: int
    high_krw: int
    low_krw: int
    close_krw: int
    volume: int
    provider_contract_sha256: str
    canonical_observation_sha256: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        _require_schema_version(self.schema_version)
        _require_provider(self.provider)
        _require_kr_symbol(self.symbol)
        _require_literal(self.market, "KR", "market")
        _require_literal(self.interval, "1d", "interval")
        _require_bool(self.adjusted, "adjusted")
        _require_aware(self.provider_event_at, "provider_event_at")
        _require_aware(self.observed_at, "observed_at")
        if self.observed_at < self.provider_event_at:
            raise PointInTimeDataError(
                "point_in_time_candle_observed_before_provider_event"
            )
        _require_literal(self.currency, "KRW", "currency")
        _require_positive_int(self.open_krw, "open_krw")
        _require_positive_int(self.high_krw, "high_krw")
        _require_positive_int(self.low_krw, "low_krw")
        _require_positive_int(self.close_krw, "close_krw")
        _require_nonnegative_int(self.volume, "volume")
        if self.high_krw < max(self.open_krw, self.close_krw):
            raise PointInTimeDataError(
                "point_in_time_candle_high_below_open_or_close"
            )
        if self.low_krw > min(self.open_krw, self.close_krw):
            raise PointInTimeDataError(
                "point_in_time_candle_low_above_open_or_close"
            )
        _require_sha256(self.provider_contract_sha256, "provider_contract_sha256")
        _require_sha256(
            self.canonical_observation_sha256,
            "canonical_observation_sha256",
        )
        expected_sha256 = _build_canonical_observation_sha256(
            provider=self.provider,
            symbol=self.symbol,
            market=self.market,
            interval=self.interval,
            adjusted=self.adjusted,
            provider_event_at=self.provider_event_at,
            currency=self.currency,
            open_krw=self.open_krw,
            high_krw=self.high_krw,
            low_krw=self.low_krw,
            close_krw=self.close_krw,
            volume=self.volume,
            provider_contract_sha256=self.provider_contract_sha256,
        )
        if self.canonical_observation_sha256 != expected_sha256:
            raise PointInTimeDataError(
                "point_in_time_candle_observation_sha256_mismatch"
            )

    @classmethod
    def create(
        cls,
        *,
        provider: object,
        symbol: object,
        market: object,
        interval: object,
        adjusted: object,
        provider_event_at: object,
        observed_at: object,
        currency: object,
        open_krw: object,
        high_krw: object,
        low_krw: object,
        close_krw: object,
        volume: object,
        provider_contract_sha256: object,
    ) -> PointInTimeCandleV1:
        valid_provider = _require_provider(provider)
        valid_symbol = _require_kr_symbol(symbol)
        valid_market = _require_literal(market, "KR", "market")
        valid_interval = _require_literal(interval, "1d", "interval")
        valid_adjusted = _require_bool(adjusted, "adjusted")
        valid_provider_event_at = _require_aware(
            provider_event_at,
            "provider_event_at",
        )
        valid_observed_at = _require_aware(observed_at, "observed_at")
        valid_currency = _require_literal(currency, "KRW", "currency")
        valid_open = _require_positive_int(open_krw, "open_krw")
        valid_high = _require_positive_int(high_krw, "high_krw")
        valid_low = _require_positive_int(low_krw, "low_krw")
        valid_close = _require_positive_int(close_krw, "close_krw")
        valid_volume = _require_nonnegative_int(volume, "volume")
        valid_contract_sha256 = _require_sha256(
            provider_contract_sha256,
            "provider_contract_sha256",
        )
        observation_sha256 = _build_canonical_observation_sha256(
            provider=valid_provider,
            symbol=valid_symbol,
            market=valid_market,
            interval=valid_interval,
            adjusted=valid_adjusted,
            provider_event_at=valid_provider_event_at,
            currency=valid_currency,
            open_krw=valid_open,
            high_krw=valid_high,
            low_krw=valid_low,
            close_krw=valid_close,
            volume=valid_volume,
            provider_contract_sha256=valid_contract_sha256,
        )
        return cls(
            provider=valid_provider,
            symbol=valid_symbol,
            market=valid_market,
            interval=valid_interval,
            adjusted=valid_adjusted,
            provider_event_at=valid_provider_event_at,
            observed_at=valid_observed_at,
            currency=valid_currency,
            open_krw=valid_open,
            high_krw=valid_high,
            low_krw=valid_low,
            close_krw=valid_close,
            volume=valid_volume,
            provider_contract_sha256=valid_contract_sha256,
            canonical_observation_sha256=observation_sha256,
        )

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> PointInTimeCandleV1:
        if set(payload) != _CANDLE_FIELDS:
            raise PointInTimeDataError("point_in_time_candle_fields_mismatch")
        _require_schema_version(payload["schema_version"])
        provider_event_at = _parse_canonical_timestamp(
            payload["provider_event_at"],
            "provider_event_at",
        )
        observed_at = _parse_canonical_timestamp(
            payload["observed_at"],
            "observed_at",
        )
        expected_sha256 = _require_sha256(
            payload["canonical_observation_sha256"],
            "canonical_observation_sha256",
        )
        candle = cls.create(
            provider=payload["provider"],
            symbol=payload["symbol"],
            market=payload["market"],
            interval=payload["interval"],
            adjusted=payload["adjusted"],
            provider_event_at=provider_event_at,
            observed_at=observed_at,
            currency=payload["currency"],
            open_krw=payload["open_krw"],
            high_krw=payload["high_krw"],
            low_krw=payload["low_krw"],
            close_krw=payload["close_krw"],
            volume=payload["volume"],
            provider_contract_sha256=payload["provider_contract_sha256"],
        )
        if candle.canonical_observation_sha256 != expected_sha256:
            raise PointInTimeDataError(
                "point_in_time_candle_observation_sha256_mismatch"
            )
        return candle

    @property
    def idempotency_key(self) -> str:
        identity: JsonObject = {
            "schema_version": self.schema_version,
            "provider": self.provider,
            "symbol": self.symbol,
            "market": self.market,
            "interval": self.interval,
            "adjusted": self.adjusted,
            "provider_event_at": _canonical_timestamp(self.provider_event_at),
        }
        return hashlib.sha256(_canonical_json(identity)).hexdigest()

    def to_payload(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "provider": self.provider,
            "symbol": self.symbol,
            "market": self.market,
            "interval": self.interval,
            "adjusted": self.adjusted,
            "provider_event_at": _canonical_timestamp(self.provider_event_at),
            "observed_at": _canonical_timestamp(self.observed_at),
            "currency": self.currency,
            "open_krw": self.open_krw,
            "high_krw": self.high_krw,
            "low_krw": self.low_krw,
            "close_krw": self.close_krw,
            "volume": self.volume,
            "provider_contract_sha256": self.provider_contract_sha256,
            "canonical_observation_sha256": self.canonical_observation_sha256,
        }


def _build_canonical_observation_sha256(
    *,
    provider: str,
    symbol: str,
    market: str,
    interval: str,
    adjusted: bool,
    provider_event_at: datetime,
    currency: str,
    open_krw: int,
    high_krw: int,
    low_krw: int,
    close_krw: int,
    volume: int,
    provider_contract_sha256: str,
) -> str:
    observation: JsonObject = {
        "schema_version": 1,
        "provider": provider,
        "symbol": symbol,
        "market": market,
        "interval": interval,
        "adjusted": adjusted,
        "provider_event_at": _canonical_timestamp(provider_event_at),
        "currency": currency,
        "open_krw": open_krw,
        "high_krw": high_krw,
        "low_krw": low_krw,
        "close_krw": close_krw,
        "volume": volume,
        "provider_contract_sha256": provider_contract_sha256,
    }
    return hashlib.sha256(_canonical_json(observation)).hexdigest()


def assert_idempotent_candle_replay(
    existing: PointInTimeCandleV1,
    candidate: PointInTimeCandleV1,
) -> None:
    if existing.idempotency_key != candidate.idempotency_key:
        raise PointInTimeDataError(
            "point_in_time_candle_idempotency_key_mismatch"
        )
    if (
        existing.canonical_observation_sha256
        != candidate.canonical_observation_sha256
    ):
        raise PointInTimeDataError("point_in_time_candle_idempotency_conflict")


def validate_point_in_time_candle_page(
    candles: Sequence[object],
) -> tuple[PointInTimeCandleV1, ...]:
    result: list[PointInTimeCandleV1] = []
    identities: set[str] = set()
    for candle in candles:
        if not isinstance(candle, PointInTimeCandleV1):
            raise PointInTimeDataError(
                "point_in_time_candle_page_item_is_invalid"
            )
        if candle.idempotency_key in identities:
            raise PointInTimeDataError(
                "point_in_time_candle_page_duplicate_identity"
            )
        identities.add(candle.idempotency_key)
        result.append(candle)
    return tuple(result)


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
        raise PointInTimeDataError(
            f"point_in_time_candle_{field_name}_must_be_timestamp"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PointInTimeDataError(
            f"point_in_time_candle_{field_name}_must_be_timestamp"
        ) from exc
    _require_aware(parsed, field_name)
    if _canonical_timestamp(parsed) != value:
        raise PointInTimeDataError(
            f"point_in_time_candle_{field_name}_must_be_canonical_utc"
        )
    return parsed


def _require_schema_version(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value != 1:
        raise PointInTimeDataError(
            "point_in_time_candle_schema_version_must_be_1"
        )
    return value


def _require_provider(value: object) -> str:
    if not isinstance(value, str) or _PROVIDER_RE.fullmatch(value) is None:
        raise PointInTimeDataError("point_in_time_candle_provider_is_invalid")
    return value


def _require_kr_symbol(value: object) -> str:
    if not isinstance(value, str) or _KR_SYMBOL_RE.fullmatch(value) is None:
        raise PointInTimeDataError("point_in_time_candle_symbol_is_invalid")
    return value


def _require_literal(value: object, expected: str, field_name: str) -> str:
    if not isinstance(value, str) or value != expected:
        raise PointInTimeDataError(
            f"point_in_time_candle_{field_name}_must_be_{expected.lower()}"
        )
    return value


def _require_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise PointInTimeDataError(
            f"point_in_time_candle_{field_name}_must_be_boolean"
        )
    return value


def _require_aware(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise PointInTimeDataError(
            f"point_in_time_candle_{field_name}_requires_timezone"
        )
    if value.tzinfo is None or value.utcoffset() is None:
        raise PointInTimeDataError(
            f"point_in_time_candle_{field_name}_requires_timezone"
        )
    return value


def _require_positive_int(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise PointInTimeDataError(
            f"point_in_time_candle_{field_name}_must_be_positive_integer"
        )
    return value


def _require_nonnegative_int(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise PointInTimeDataError(
            f"point_in_time_candle_{field_name}_must_be_nonnegative_integer"
        )
    return value


def _require_sha256(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise PointInTimeDataError(
            f"point_in_time_candle_{field_name}_must_be_sha256_hex"
        )
    return value
