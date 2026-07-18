from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any

import pytest

from app.domain.common.time import KST
from app.domain.market_data.point_in_time_calendar import (
    PointInTimeCalendarError,
    PointInTimeKrDailySessionV1,
)

CONTRACT_SHA = "a" * 64
SESSION_DATE = date(2026, 3, 25)
REGULAR_START = datetime(2026, 3, 25, 9, 0, tzinfo=KST)
REGULAR_END = datetime(2026, 3, 25, 15, 30, tzinfo=KST)
NEXT_DATE = date(2026, 3, 26)
NEXT_START = datetime(2026, 3, 26, 9, 0, tzinfo=KST)
NEXT_END = datetime(2026, 3, 26, 15, 30, tzinfo=KST)
OBSERVED_AT = datetime(2026, 3, 24, 23, 0, tzinfo=UTC)


def test_open_session_round_trips_canonical_payload() -> None:
    evidence = _session()

    payload = evidence.to_payload()
    restored = PointInTimeKrDailySessionV1.from_payload(payload)

    assert restored == evidence
    assert payload["regular_start_at"] == "2026-03-25T00:00:00Z"
    assert payload["regular_end_at"] == "2026-03-25T06:30:00Z"
    assert payload["next_regular_start_at"] == "2026-03-26T00:00:00Z"
    assert len(evidence.idempotency_key) == 64
    assert len(evidence.canonical_evidence_sha256) == 64
    assert "is_final" not in payload


def test_session_contract_uses_stable_golden_digests() -> None:
    evidence = _session()
    changed = _session(
        next_regular_end_at=datetime(2026, 3, 26, 16, 0, tzinfo=KST)
    )

    assert evidence.idempotency_key == (
        "dcc571036310de8eedf71c3ca96c93796ad503ae9899c0e8ff7c253ee69fd619"
    )
    assert evidence.canonical_evidence_sha256 == (
        "094ea1fd69c9aafb5d469c666de147ac6a378683751b4cb389d84e7cfb948d3f"
    )
    assert changed.canonical_evidence_sha256 != evidence.canonical_evidence_sha256


def test_direct_constructor_cannot_bypass_timezone_invariant() -> None:
    evidence = _session()

    _assert_rejected(
        "point_in_time_kr_session_regular_start_at_requires_timezone",
        lambda: replace(
            evidence,
            regular_start_at=datetime(2026, 3, 25, 9, 0),
        ),
    )


def test_closed_session_requires_no_current_regular_hours() -> None:
    evidence = _session(
        session_date=date(2026, 5, 5),
        is_open=False,
        regular_start_at=None,
        regular_end_at=None,
        next_business_date=date(2026, 5, 6),
        next_regular_start_at=datetime(2026, 5, 6, 9, 0, tzinfo=KST),
        next_regular_end_at=datetime(2026, 5, 6, 15, 30, tzinfo=KST),
    )

    assert evidence.is_open is False
    assert evidence.regular_start_at is None
    assert evidence.regular_end_at is None
    assert PointInTimeKrDailySessionV1.from_payload(evidence.to_payload()) == evidence


def test_observation_time_does_not_change_identity_or_evidence_hash() -> None:
    first = _session()
    replay = _session(observed_at=datetime(2026, 3, 26, 1, 0, tzinfo=UTC))

    assert first.idempotency_key == replay.idempotency_key
    assert first.canonical_evidence_sha256 == replay.canonical_evidence_sha256


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"provider": "Toss"}, "point_in_time_kr_session_provider_invalid"),
        ({"market": "US"}, "point_in_time_kr_session_market_must_be_kr"),
        (
            {"session_date": datetime(2026, 3, 25, tzinfo=UTC)},
            "point_in_time_kr_session_session_date_must_be_date",
        ),
        (
            {"is_open": 1},
            "point_in_time_kr_session_is_open_must_be_boolean",
        ),
        (
            {"provider_contract_sha256": "x" * 64},
            "point_in_time_kr_session_provider_contract_sha256_must_be_sha256_hex",
        ),
        (
            {"observed_at": datetime(2026, 3, 25, 8, 0)},
            "point_in_time_kr_session_observed_at_requires_timezone",
        ),
    ],
)
def test_session_rejects_invalid_scalar_fields(
    overrides: dict[str, object],
    reason: str,
) -> None:
    _assert_rejected(reason, lambda: _session(**overrides))


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        (
            {"regular_start_at": None},
            "point_in_time_kr_session_open_day_missing_regular_hours",
        ),
        (
            {"regular_end_at": None},
            "point_in_time_kr_session_open_day_missing_regular_hours",
        ),
        (
            {
                "is_open": False,
                "regular_start_at": REGULAR_START,
                "regular_end_at": REGULAR_END,
            },
            "point_in_time_kr_session_closed_day_has_regular_hours",
        ),
        (
            {"regular_start_at": datetime(2026, 3, 25, 9, 0)},
            "point_in_time_kr_session_regular_start_at_requires_timezone",
        ),
        (
            {"regular_end_at": REGULAR_START},
            "point_in_time_kr_session_regular_time_order_invalid",
        ),
        (
            {
                "regular_start_at": datetime(2026, 3, 24, 23, 0, tzinfo=KST)
            },
            "point_in_time_kr_session_regular_start_date_mismatch",
        ),
        (
            {"regular_end_at": datetime(2026, 3, 26, 0, 1, tzinfo=KST)},
            "point_in_time_kr_session_regular_end_date_mismatch",
        ),
    ],
)
def test_session_rejects_invalid_current_regular_hours(
    overrides: dict[str, object],
    reason: str,
) -> None:
    _assert_rejected(reason, lambda: _session(**overrides))


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        (
            {"next_business_date": SESSION_DATE},
            "point_in_time_kr_session_next_business_date_not_later",
        ),
        (
            {"next_regular_start_at": datetime(2026, 3, 26, 9, 0)},
            "point_in_time_kr_session_next_regular_start_at_requires_timezone",
        ),
        (
            {"next_regular_end_at": NEXT_START},
            "point_in_time_kr_session_next_regular_time_order_invalid",
        ),
        (
            {"next_regular_start_at": REGULAR_START},
            "point_in_time_kr_session_next_regular_start_date_mismatch",
        ),
        (
            {"next_regular_end_at": datetime(2026, 3, 27, 0, 1, tzinfo=KST)},
            "point_in_time_kr_session_next_regular_end_date_mismatch",
        ),
    ],
)
def test_session_rejects_invalid_next_business_session(
    overrides: dict[str, object],
    reason: str,
) -> None:
    _assert_rejected(reason, lambda: _session(**overrides))


def test_payload_requires_exact_fields_and_canonical_values() -> None:
    payload = _session().to_payload()
    extra = {**payload, "is_final": True}
    missing = dict(payload)
    del missing["next_regular_end_at"]
    noncanonical_time = {
        **payload,
        "regular_start_at": "2026-03-25T00:00:00+00:00",
    }
    noncanonical_date = {**payload, "session_date": "2026-3-25"}

    _assert_rejected(
        "point_in_time_kr_session_fields_mismatch",
        lambda: PointInTimeKrDailySessionV1.from_payload(extra),
    )
    _assert_rejected(
        "point_in_time_kr_session_fields_mismatch",
        lambda: PointInTimeKrDailySessionV1.from_payload(missing),
    )
    _assert_rejected(
        "point_in_time_kr_session_regular_start_at_must_be_canonical_utc",
        lambda: PointInTimeKrDailySessionV1.from_payload(noncanonical_time),
    )
    _assert_rejected(
        "point_in_time_kr_session_session_date_must_be_date",
        lambda: PointInTimeKrDailySessionV1.from_payload(noncanonical_date),
    )


def test_payload_rejects_tampered_evidence_hash_and_schema_version() -> None:
    payload = _session().to_payload()

    _assert_rejected(
        "point_in_time_kr_session_evidence_sha256_mismatch",
        lambda: PointInTimeKrDailySessionV1.from_payload(
            {**payload, "canonical_evidence_sha256": "0" * 64}
        ),
    )
    _assert_rejected(
        "point_in_time_kr_session_schema_version_must_be_1",
        lambda: PointInTimeKrDailySessionV1.from_payload(
            {**payload, "schema_version": 2}
        ),
    )


def _session(**overrides: object) -> PointInTimeKrDailySessionV1:
    values: dict[str, object] = {
        "provider": "toss",
        "market": "KR",
        "session_date": SESSION_DATE,
        "is_open": True,
        "regular_start_at": REGULAR_START,
        "regular_end_at": REGULAR_END,
        "next_business_date": NEXT_DATE,
        "next_regular_start_at": NEXT_START,
        "next_regular_end_at": NEXT_END,
        "observed_at": OBSERVED_AT,
        "provider_contract_sha256": CONTRACT_SHA,
    }
    values.update(overrides)
    return PointInTimeKrDailySessionV1.create(**values)


def _assert_rejected(reason: str, action: Any) -> None:
    with pytest.raises(PointInTimeCalendarError, match=reason):
        action()
