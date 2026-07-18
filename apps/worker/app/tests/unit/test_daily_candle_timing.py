from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from app.domain.common.time import KST
from app.domain.market_data.daily_candle_timing import (
    DailyCandleTimeWindowError,
    PointInTimeDailyCandleTimingEvidenceV1,
    build_daily_candle_timing_evidence,
)
from app.domain.market_data.point_in_time import PointInTimeCandleV1
from app.domain.market_data.point_in_time_calendar import (
    PointInTimeKrDailySessionV1,
)

CANDLE_CONTRACT_SHA = "a" * 64
CALENDAR_CONTRACT_SHA = "b" * 64
SESSION_DATE = date(2026, 3, 25)
CANDLE_EVENT = datetime(2026, 3, 25, 0, 0, tzinfo=KST)
REGULAR_START = datetime(2026, 3, 25, 9, 0, tzinfo=KST)
REGULAR_END = datetime(2026, 3, 25, 15, 30, tzinfo=KST)
NEXT_DATE = date(2026, 3, 26)
CUTOFF = datetime(2026, 3, 26, 9, 0, tzinfo=KST)
NEXT_END = datetime(2026, 3, 26, 15, 30, tzinfo=KST)


def test_builds_timing_only_evidence_with_independent_contract_hashes() -> None:
    candle = _candle()
    session = _session()

    evidence = build_daily_candle_timing_evidence(candle, session)
    payload = evidence.to_payload()

    assert evidence.provider == "toss"
    assert evidence.symbol == "005930"
    assert evidence.session_date == SESSION_DATE
    assert evidence.candle_provider_event_at == CANDLE_EVENT
    assert evidence.regular_end_at == REGULAR_END
    assert evidence.cutoff_at == CUTOFF
    assert evidence.evidence_available_at == CUTOFF
    assert evidence.candle_idempotency_key == candle.idempotency_key
    assert evidence.calendar_idempotency_key == session.idempotency_key
    assert evidence.candle_provider_contract_sha256 == CANDLE_CONTRACT_SHA
    assert evidence.calendar_provider_contract_sha256 == CALENDAR_CONTRACT_SHA
    assert CANDLE_CONTRACT_SHA != CALENDAR_CONTRACT_SHA
    assert evidence.idempotency_key == (
        "4c7fec3654cff51ee87d133801913b5924d1ef40b0d5dd0e4adac15b360b912f"
    )
    assert evidence.canonical_timing_evidence_sha256 == (
        "60d7ad585cf4a6bd7aa3a7a8dc97df2c939640f453957e1cc86377ffab1d0f0d"
    )
    assert PointInTimeDailyCandleTimingEvidenceV1.from_payload(payload) == evidence
    assert {
        "is_closed",
        "is_complete",
        "is_final",
        "immutable",
        "feature_ready",
        "certified",
        "dq_passed",
    }.isdisjoint(payload)


def test_evidence_available_at_is_later_source_observation() -> None:
    candle_observed = CUTOFF + timedelta(hours=1)
    calendar_observed = CUTOFF + timedelta(hours=2)

    evidence = build_daily_candle_timing_evidence(
        _candle(observed_at=candle_observed),
        _session(observed_at=calendar_observed),
    )

    assert evidence.evidence_available_at == calendar_observed


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        (
            "candle_type",
            "daily_candle_timing_candle_invalid",
        ),
        (
            "session_type",
            "daily_candle_timing_session_invalid",
        ),
        (
            "provider",
            "daily_candle_timing_provider_mismatch",
        ),
        (
            "closed",
            "daily_candle_timing_session_is_not_open",
        ),
        (
            "date",
            "daily_candle_timing_session_date_mismatch",
        ),
    ],
)
def test_rejects_invalid_source_join(
    case: str,
    reason: str,
) -> None:
    candle: object = _candle()
    session: object = _session()
    if case == "candle_type":
        candle = object()
    elif case == "session_type":
        session = object()
    elif case == "provider":
        session = _session(provider="other")
    elif case == "closed":
        session = _closed_session()
    elif case == "date":
        candle = _candle(
            provider_event_at=datetime(2026, 3, 24, 9, 0, tzinfo=KST)
        )
    _assert_rejected(
        reason,
        lambda: build_daily_candle_timing_evidence(candle, session),
    )


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        (
            "candle",
            "daily_candle_timing_candle_observed_before_cutoff",
        ),
        (
            "calendar",
            "daily_candle_timing_calendar_observed_before_cutoff",
        ),
    ],
)
def test_rejects_observation_one_microsecond_before_cutoff(
    source: str,
    reason: str,
) -> None:
    candle = _candle(
        observed_at=(
            CUTOFF - timedelta(microseconds=1) if source == "candle" else CUTOFF
        )
    )
    session = _session(
        observed_at=(
            CUTOFF - timedelta(microseconds=1) if source == "calendar" else CUTOFF
        )
    )

    _assert_rejected(
        reason,
        lambda: build_daily_candle_timing_evidence(candle, session),
    )


def test_accepts_both_observations_exactly_at_cutoff() -> None:
    evidence = build_daily_candle_timing_evidence(_candle(), _session())

    assert evidence.candle_observed_at == CUTOFF
    assert evidence.calendar_observed_at == CUTOFF


def test_accepts_provider_anchor_within_same_kst_session_date() -> None:
    provider_anchor = REGULAR_START + timedelta(microseconds=1)

    evidence = build_daily_candle_timing_evidence(
        _candle(provider_event_at=provider_anchor),
        _session(),
    )

    assert evidence.candle_provider_event_at == provider_anchor


def test_accepts_equivalent_event_in_utc() -> None:
    kst_evidence = build_daily_candle_timing_evidence(_candle(), _session())
    utc_evidence = build_daily_candle_timing_evidence(
        _candle(provider_event_at=datetime(2026, 3, 24, 15, 0, tzinfo=UTC)),
        _session(),
    )

    assert utc_evidence.to_payload()["candle_provider_event_at"] == (
        "2026-03-24T15:00:00Z"
    )
    assert utc_evidence.idempotency_key == kst_evidence.idempotency_key
    assert utc_evidence.canonical_timing_evidence_sha256 == (
        kst_evidence.canonical_timing_evidence_sha256
    )


def test_later_reobservation_keeps_identity_but_changes_timing_hash() -> None:
    first = build_daily_candle_timing_evidence(_candle(), _session())
    later = build_daily_candle_timing_evidence(
        _candle(observed_at=CUTOFF + timedelta(hours=1)),
        _session(observed_at=CUTOFF + timedelta(hours=2)),
    )

    assert first.candle_canonical_observation_sha256 == (
        later.candle_canonical_observation_sha256
    )
    assert first.calendar_canonical_evidence_sha256 == (
        later.calendar_canonical_evidence_sha256
    )
    assert first.idempotency_key == later.idempotency_key
    assert (
        first.canonical_timing_evidence_sha256
        != later.canonical_timing_evidence_sha256
    )


def test_candle_correction_keeps_identity_but_changes_timing_hash() -> None:
    original = build_daily_candle_timing_evidence(_candle(), _session())
    corrected = build_daily_candle_timing_evidence(
        _candle(close_krw=72_100),
        _session(),
    )

    assert original.candle_idempotency_key == corrected.candle_idempotency_key
    assert original.idempotency_key == corrected.idempotency_key
    assert original.candle_canonical_observation_sha256 != (
        corrected.candle_canonical_observation_sha256
    )
    assert original.canonical_timing_evidence_sha256 != (
        corrected.canonical_timing_evidence_sha256
    )


def test_calendar_correction_keeps_identity_but_changes_timing_hash() -> None:
    original = build_daily_candle_timing_evidence(_candle(), _session())
    corrected = build_daily_candle_timing_evidence(
        _candle(),
        _session(regular_end_at=REGULAR_END - timedelta(minutes=10)),
    )

    assert original.calendar_idempotency_key == corrected.calendar_idempotency_key
    assert original.idempotency_key == corrected.idempotency_key
    assert original.calendar_canonical_evidence_sha256 != (
        corrected.calendar_canonical_evidence_sha256
    )
    assert original.canonical_timing_evidence_sha256 != (
        corrected.canonical_timing_evidence_sha256
    )


def test_direct_constructor_revalidates_derived_fields() -> None:
    evidence = build_daily_candle_timing_evidence(_candle(), _session())

    _assert_rejected(
        "daily_candle_timing_available_at_mismatch",
        lambda: replace(
            evidence,
            evidence_available_at=CUTOFF + timedelta(seconds=1),
        ),
    )


@pytest.mark.parametrize(
    ("field_name", "reason"),
    [
        (
            "candle_idempotency_key",
            "daily_candle_timing_candle_identity_mismatch",
        ),
        (
            "calendar_idempotency_key",
            "daily_candle_timing_calendar_identity_mismatch",
        ),
    ],
)
def test_direct_constructor_rejects_source_identity_mismatch(
    field_name: str,
    reason: str,
) -> None:
    evidence = build_daily_candle_timing_evidence(_candle(), _session())

    def action() -> PointInTimeDailyCandleTimingEvidenceV1:
        if field_name == "candle_idempotency_key":
            return replace(
                evidence,
                candle_idempotency_key="0" * 64,
            )
        return replace(
            evidence,
            calendar_idempotency_key="0" * 64,
        )

    _assert_rejected(
        reason,
        action,
    )


def test_payload_rejects_field_injection_noncanonical_time_and_tampering() -> None:
    evidence = build_daily_candle_timing_evidence(_candle(), _session())
    payload = evidence.to_payload()
    injected = {**payload, "is_final": True}
    missing = dict(payload)
    del missing["calendar_observed_at"]
    noncanonical = {
        **payload,
        "cutoff_at": "2026-03-26T00:00:00+00:00",
    }
    tampered = {
        **payload,
        "canonical_timing_evidence_sha256": "0" * 64,
    }

    _assert_rejected(
        "daily_candle_timing_evidence_fields_mismatch",
        lambda: PointInTimeDailyCandleTimingEvidenceV1.from_payload(injected),
    )
    _assert_rejected(
        "daily_candle_timing_evidence_fields_mismatch",
        lambda: PointInTimeDailyCandleTimingEvidenceV1.from_payload(missing),
    )
    _assert_rejected(
        "daily_candle_timing_cutoff_at_must_be_canonical_utc",
        lambda: PointInTimeDailyCandleTimingEvidenceV1.from_payload(noncanonical),
    )
    _assert_rejected(
        "daily_candle_timing_evidence_sha256_mismatch",
        lambda: PointInTimeDailyCandleTimingEvidenceV1.from_payload(tampered),
    )


def _candle(
    *,
    provider: str = "toss",
    provider_event_at: datetime = CANDLE_EVENT,
    observed_at: datetime = CUTOFF,
    close_krw: int = 72_000,
    provider_contract_sha256: str = CANDLE_CONTRACT_SHA,
) -> PointInTimeCandleV1:
    return PointInTimeCandleV1.create(
        provider=provider,
        symbol="005930",
        market="KR",
        interval="1d",
        adjusted=True,
        provider_event_at=provider_event_at,
        observed_at=observed_at,
        currency="KRW",
        open_krw=71_600,
        high_krw=72_300,
        low_krw=71_500,
        close_krw=close_krw,
        volume=3_521_000,
        provider_contract_sha256=provider_contract_sha256,
    )


def _session(
    *,
    provider: str = "toss",
    regular_end_at: datetime = REGULAR_END,
    observed_at: datetime = CUTOFF,
    provider_contract_sha256: str = CALENDAR_CONTRACT_SHA,
) -> PointInTimeKrDailySessionV1:
    return PointInTimeKrDailySessionV1.create(
        provider=provider,
        market="KR",
        session_date=SESSION_DATE,
        is_open=True,
        regular_start_at=REGULAR_START,
        regular_end_at=regular_end_at,
        next_business_date=NEXT_DATE,
        next_regular_start_at=CUTOFF,
        next_regular_end_at=NEXT_END,
        observed_at=observed_at,
        provider_contract_sha256=provider_contract_sha256,
    )


def _closed_session() -> PointInTimeKrDailySessionV1:
    return PointInTimeKrDailySessionV1.create(
        provider="toss",
        market="KR",
        session_date=SESSION_DATE,
        is_open=False,
        regular_start_at=None,
        regular_end_at=None,
        next_business_date=NEXT_DATE,
        next_regular_start_at=CUTOFF,
        next_regular_end_at=NEXT_END,
        observed_at=CUTOFF,
        provider_contract_sha256=CALENDAR_CONTRACT_SHA,
    )


def _assert_rejected(reason: str, action: Callable[[], object]) -> None:
    with pytest.raises(DailyCandleTimeWindowError, match=reason):
        action()
