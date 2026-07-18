from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.domain.market_data.point_in_time import (
    PointInTimeCandleV1,
    PointInTimeDataError,
    assert_idempotent_candle_replay,
    validate_point_in_time_candle_page,
)


def test_point_in_time_candle_round_trips_canonical_payload() -> None:
    candle = _candle()

    parsed = PointInTimeCandleV1.from_payload(candle.to_payload())

    assert parsed == candle
    assert len(candle.canonical_observation_sha256) == 64
    assert len(candle.idempotency_key) == 64
    assert candle.to_payload()["provider_event_at"] == "2026-03-24T15:00:00Z"


def test_point_in_time_candle_hash_is_timezone_independent() -> None:
    kst = ZoneInfo("Asia/Seoul")
    utc_candle = _candle()
    kst_candle = _candle(
        provider_event_at=datetime(2026, 3, 25, 0, 0, tzinfo=kst),
        observed_at=datetime(2026, 3, 25, 0, 0, 5, tzinfo=kst),
    )

    assert (
        kst_candle.canonical_observation_sha256
        == utc_candle.canonical_observation_sha256
    )
    assert kst_candle.idempotency_key == utc_candle.idempotency_key


def test_point_in_time_candle_rejects_unknown_and_missing_fields() -> None:
    payload = _candle().to_payload()
    with_unknown = {**payload, "unexpected": True}
    without_contract = dict(payload)
    del without_contract["provider_contract_sha256"]
    wrong_version = {**payload, "schema_version": 2}

    _assert_rejected(
        "point_in_time_candle_fields_mismatch",
        lambda: PointInTimeCandleV1.from_payload(with_unknown),
    )
    _assert_rejected(
        "point_in_time_candle_fields_mismatch",
        lambda: PointInTimeCandleV1.from_payload(without_contract),
    )
    _assert_rejected(
        "point_in_time_candle_schema_version_must_be_1",
        lambda: PointInTimeCandleV1.from_payload(wrong_version),
    )


def test_point_in_time_candle_rejects_invalid_contract_identity() -> None:
    _assert_rejected(
        "point_in_time_candle_provider_is_invalid",
        lambda: _candle(provider="Toss"),
    )
    _assert_rejected(
        "point_in_time_candle_symbol_is_invalid",
        lambda: _candle(symbol="5930"),
    )
    _assert_rejected(
        "point_in_time_candle_market_must_be_kr",
        lambda: _candle(market="US"),
    )
    _assert_rejected(
        "point_in_time_candle_interval_must_be_1d",
        lambda: _candle(interval="1m"),
    )
    _assert_rejected(
        "point_in_time_candle_adjusted_must_be_boolean",
        lambda: _candle(adjusted=1),
    )
    _assert_rejected(
        "point_in_time_candle_currency_must_be_krw",
        lambda: _candle(currency="USD"),
    )
    _assert_rejected(
        "point_in_time_candle_provider_contract_sha256_must_be_sha256_hex",
        lambda: _candle(provider_contract_sha256="A" * 64),
    )


def test_point_in_time_candle_rejects_time_invariants() -> None:
    _assert_rejected(
        "point_in_time_candle_provider_event_at_requires_timezone",
        lambda: _candle(provider_event_at=datetime(2026, 3, 24, 15, 0)),
    )
    _assert_rejected(
        "point_in_time_candle_observed_before_provider_event",
        lambda: _candle(
            observed_at=datetime(2026, 3, 24, 14, 59, 59, tzinfo=UTC)
        ),
    )


def test_point_in_time_candle_rejects_invalid_ohlcv() -> None:
    _assert_rejected(
        "point_in_time_candle_open_krw_must_be_positive_integer",
        lambda: _candle(open_krw=True),
    )
    _assert_rejected(
        "point_in_time_candle_open_krw_must_be_positive_integer",
        lambda: _candle(open_krw=72_000.5),
    )
    _assert_rejected(
        "point_in_time_candle_high_below_open_or_close",
        lambda: _candle(high_krw=71_999),
    )
    _assert_rejected(
        "point_in_time_candle_low_above_open_or_close",
        lambda: _candle(low_krw=72_001),
    )
    _assert_rejected(
        "point_in_time_candle_volume_must_be_nonnegative_integer",
        lambda: _candle(volume=-1),
    )
    _assert_rejected(
        "point_in_time_candle_volume_must_be_nonnegative_integer",
        lambda: _candle(volume=True),
    )
    _assert_rejected(
        "point_in_time_candle_volume_must_be_nonnegative_integer",
        lambda: _candle(volume=1_000.5),
    )


def test_point_in_time_candle_rejects_noncanonical_payload_and_hash() -> None:
    payload = _candle().to_payload()
    noncanonical_time = {
        **payload,
        "provider_event_at": "2026-03-24T15:00:00+00:00",
    }
    wrong_hash = {**payload, "canonical_observation_sha256": "0" * 64}
    malformed_hash = {**payload, "canonical_observation_sha256": "z" * 64}

    _assert_rejected(
        "point_in_time_candle_provider_event_at_must_be_canonical_utc",
        lambda: PointInTimeCandleV1.from_payload(noncanonical_time),
    )
    _assert_rejected(
        "point_in_time_candle_observation_sha256_mismatch",
        lambda: PointInTimeCandleV1.from_payload(wrong_hash),
    )
    _assert_rejected(
        "point_in_time_candle_canonical_observation_sha256_must_be_sha256_hex",
        lambda: PointInTimeCandleV1.from_payload(malformed_hash),
    )


def test_point_in_time_candle_identity_includes_adjustment_mode() -> None:
    adjusted = _candle(adjusted=True)
    unadjusted = _candle(adjusted=False)

    assert adjusted.idempotency_key != unadjusted.idempotency_key
    assert (
        adjusted.canonical_observation_sha256
        != unadjusted.canonical_observation_sha256
    )


def test_point_in_time_candle_identity_includes_provider() -> None:
    toss = _candle(provider="toss")
    alternate = _candle(provider="alternate")

    assert toss.idempotency_key != alternate.idempotency_key
    assert (
        toss.canonical_observation_sha256
        != alternate.canonical_observation_sha256
    )


def test_point_in_time_candle_detects_idempotency_conflict() -> None:
    original = _candle()
    later_exact_replay = _candle(
        observed_at=datetime(2026, 3, 24, 15, 5, tzinfo=UTC)
    )
    conflicting_replay = _candle(close_krw=72_100, high_krw=72_100)
    different_identity = _candle(
        provider_event_at=datetime(2026, 3, 25, 15, 0, tzinfo=UTC),
        observed_at=datetime(2026, 3, 25, 15, 0, 5, tzinfo=UTC),
    )

    assert_idempotent_candle_replay(original, later_exact_replay)
    _assert_rejected(
        "point_in_time_candle_idempotency_conflict",
        lambda: assert_idempotent_candle_replay(original, conflicting_replay),
    )
    _assert_rejected(
        "point_in_time_candle_idempotency_key_mismatch",
        lambda: assert_idempotent_candle_replay(original, different_identity),
    )


def test_point_in_time_candle_page_rejects_duplicate_identity() -> None:
    candle = _candle()
    later_replay = _candle(
        observed_at=datetime(2026, 3, 24, 15, 10, tzinfo=UTC)
    )

    assert validate_point_in_time_candle_page([candle]) == (candle,)
    _assert_rejected(
        "point_in_time_candle_page_duplicate_identity",
        lambda: validate_point_in_time_candle_page([candle, later_replay]),
    )
    _assert_rejected(
        "point_in_time_candle_page_item_is_invalid",
        lambda: validate_point_in_time_candle_page([object()]),
    )


def _candle(
    *,
    provider: object = "toss",
    symbol: object = "005930",
    market: object = "KR",
    interval: object = "1d",
    provider_event_at: datetime = datetime(2026, 3, 24, 15, 0, tzinfo=UTC),
    observed_at: datetime = datetime(2026, 3, 24, 15, 0, 5, tzinfo=UTC),
    adjusted: object = True,
    currency: object = "KRW",
    open_krw: object = 72_000,
    high_krw: object = 72_050,
    low_krw: object = 71_950,
    close_krw: object = 72_000,
    volume: object = 1_000,
    provider_contract_sha256: object = "a" * 64,
) -> PointInTimeCandleV1:
    return PointInTimeCandleV1.create(
        provider=provider,
        symbol=symbol,
        market=market,
        interval=interval,
        adjusted=adjusted,
        provider_event_at=provider_event_at,
        observed_at=observed_at,
        currency=currency,
        open_krw=open_krw,
        high_krw=high_krw,
        low_krw=low_krw,
        close_krw=close_krw,
        volume=volume,
        provider_contract_sha256=provider_contract_sha256,
    )


def _assert_rejected(expected: str, operation: Callable[[], object]) -> None:
    try:
        operation()
    except PointInTimeDataError as exc:
        assert exc.safe_message == expected
    else:
        raise AssertionError(f"expected PointInTimeDataError: {expected}")
