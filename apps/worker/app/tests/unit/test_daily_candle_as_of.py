from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta, tzinfo
from itertools import permutations

import pytest

from app.domain.common.time import KST
from app.domain.market_data.daily_candle_as_of import (
    DailyCandleAsOfError,
    SelectedPointInTimeDailyCandleV1,
    select_daily_candles_as_of,
)
from app.domain.market_data.daily_candle_timing import (
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


def test_empty_and_not_yet_available_candidates_return_empty() -> None:
    candidate = _candidate()

    assert select_daily_candles_as_of([], as_of=CUTOFF) == ()
    assert select_daily_candles_as_of(
        [candidate],
        as_of=CUTOFF - timedelta(microseconds=1),
    ) == ()


def test_exact_availability_boundary_is_inclusive() -> None:
    candle, timing = _candidate()

    selected = select_daily_candles_as_of(
        [(candle, timing)],
        as_of=CUTOFF,
    )

    assert len(selected) == 1
    assert selected[0].candle == candle
    assert selected[0].timing_evidence == timing
    assert selected[0].available_at == CUTOFF


def test_late_calendar_prevents_candle_look_ahead() -> None:
    candle = _candle(observed_at=CUTOFF)
    late_availability = CUTOFF + timedelta(hours=2)
    candidate = _candidate(
        candle,
        calendar_observed_at=late_availability,
    )

    assert select_daily_candles_as_of(
        [candidate],
        as_of=late_availability - timedelta(microseconds=1),
    ) == ()
    assert select_daily_candles_as_of(
        [candidate],
        as_of=late_availability,
    )[0].candle == candle


def test_correction_becomes_visible_only_at_its_evidence_boundary() -> None:
    original = _candidate(_candle(close_krw=72_000))
    correction_at = CUTOFF + timedelta(hours=1)
    corrected = _candidate(
        _candle(observed_at=correction_at, close_krw=72_100)
    )

    before = select_daily_candles_as_of(
        [corrected, original],
        as_of=correction_at - timedelta(microseconds=1),
    )
    at_boundary = select_daily_candles_as_of(
        [original, corrected],
        as_of=correction_at,
    )

    assert before[0].candle.close_krw == 72_000
    assert at_boundary[0].candle.close_krw == 72_100


def test_late_stale_timing_proof_cannot_roll_back_newer_correction() -> None:
    late_stale_at = CUTOFF + timedelta(hours=3)
    original = _candle(close_krw=72_000)
    early_original = _candidate(original)
    late_original = _candidate(
        original,
        calendar_observed_at=late_stale_at,
    )
    corrected = _candidate(
        _candle(
            observed_at=CUTOFF + timedelta(hours=1),
            close_krw=72_100,
        )
    )

    selected = select_daily_candles_as_of(
        [late_original, corrected, early_original],
        as_of=late_stale_at,
    )

    assert selected[0].candle.close_krw == 72_100
    assert selected[0].candle.observed_at == CUTOFF + timedelta(hours=1)


def test_same_availability_uses_candle_revision_clock() -> None:
    shared_availability = CUTOFF + timedelta(hours=2)
    original = _candidate(
        _candle(close_krw=72_000),
        calendar_observed_at=shared_availability,
    )
    corrected = _candidate(
        _candle(
            observed_at=CUTOFF + timedelta(hours=1),
            close_krw=72_100,
        ),
        calendar_observed_at=shared_availability,
    )

    selected = select_daily_candles_as_of(
        [original, corrected],
        as_of=shared_availability,
    )

    assert selected[0].candle.close_krw == 72_100


def test_same_revision_clock_with_different_hashes_fails_closed() -> None:
    original = _candidate(_candle(close_krw=72_000))
    conflicting = _candidate(_candle(close_krw=72_100))

    for candidates in ([original, conflicting], [conflicting, original]):
        with pytest.raises(DailyCandleAsOfError) as caught:
            select_daily_candles_as_of(candidates, as_of=CUTOFF)
        assert caught.value.safe_message == (
            "daily_candle_as_of_revision_clock_conflict"
        )


def test_historical_hash_recurrence_fails_only_when_it_is_eligible() -> None:
    original = _candidate(_candle(close_krw=72_000))
    corrected_at = CUTOFF + timedelta(hours=1)
    corrected = _candidate(
        _candle(observed_at=corrected_at, close_krw=72_100)
    )
    recurrence_at = CUTOFF + timedelta(hours=2)
    recurrence = _candidate(
        _candle(observed_at=recurrence_at, close_krw=72_000)
    )

    before_recurrence = select_daily_candles_as_of(
        [recurrence, original, corrected],
        as_of=recurrence_at - timedelta(microseconds=1),
    )

    assert before_recurrence[0].candle.close_krw == 72_100
    _assert_rejected(
        "daily_candle_as_of_historical_hash_recurrence_ambiguous",
        lambda: select_daily_candles_as_of(
            [recurrence, original, corrected],
            as_of=recurrence_at,
        ),
    )


def test_consecutive_exact_replay_preserves_first_observation() -> None:
    original = _candidate(_candle())
    replay = _candidate(
        _candle(observed_at=CUTOFF + timedelta(hours=1))
    )

    selected = select_daily_candles_as_of(
        [replay, original, original],
        as_of=CUTOFF + timedelta(hours=1),
    )

    assert selected[0].candle.observed_at == CUTOFF
    assert selected[0].candle.canonical_observation_sha256 == (
        replay[0].canonical_observation_sha256
    )


def test_latest_timing_revision_is_preserved_for_same_candle_observation() -> None:
    candle = _candle()
    initial = _candidate(candle)
    later_at = CUTOFF + timedelta(hours=1)
    later = _candidate(candle, calendar_observed_at=later_at)

    selected = select_daily_candles_as_of(
        [later, initial],
        as_of=later_at,
    )

    assert selected[0].timing_evidence == later[1]


def test_same_timing_clock_with_different_evidence_fails_closed() -> None:
    candle = _candle()
    initial = _candidate(candle)
    corrected_calendar = _candidate(
        candle,
        regular_end_at=REGULAR_END - timedelta(minutes=10),
    )

    _assert_rejected(
        "daily_candle_as_of_timing_evidence_conflict",
        lambda: select_daily_candles_as_of(
            [initial, corrected_calendar],
            as_of=CUTOFF,
        ),
    )


def test_daily_identity_drift_isolated_until_future_candidate_is_eligible() -> None:
    original = _candidate()
    drift_at = CUTOFF + timedelta(hours=1)
    drift = _candidate(
        _candle(
            provider_event_at=REGULAR_START,
            observed_at=drift_at,
        )
    )

    before = select_daily_candles_as_of(
        [drift, original],
        as_of=CUTOFF,
    )

    assert before[0].logical_candle_id == original[0].idempotency_key
    _assert_rejected(
        "daily_candle_as_of_daily_identity_ambiguous",
        lambda: select_daily_candles_as_of(
            [drift, original],
            as_of=drift_at,
        ),
    )


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("provider", "daily_candle_as_of_provider_mismatch"),
        ("symbol", "daily_candle_as_of_symbol_mismatch"),
        ("adjusted", "daily_candle_as_of_adjusted_mismatch"),
        ("event", "daily_candle_as_of_provider_event_at_mismatch"),
        ("observed", "daily_candle_as_of_candle_observed_at_mismatch"),
        (
            "contract",
            "daily_candle_as_of_candle_provider_contract_sha256_mismatch",
        ),
        ("content", "daily_candle_as_of_candle_observation_sha256_mismatch"),
    ],
)
def test_rejects_cross_bound_candle_and_timing_evidence(
    case: str,
    reason: str,
) -> None:
    candle = _candle()
    other = {
        "provider": _candle(provider="other"),
        "symbol": _candle(symbol="000660"),
        "adjusted": _candle(adjusted=False),
        "event": _candle(provider_event_at=REGULAR_START),
        "observed": _candle(observed_at=CUTOFF + timedelta(hours=1)),
        "contract": _candle(provider_contract_sha256="c" * 64),
        "content": _candle(close_krw=72_100),
    }[case]
    timing = _candidate(other)[1]

    _assert_rejected(
        reason,
        lambda: select_daily_candles_as_of(
            [(candle, timing)],
            as_of=CUTOFF + timedelta(hours=1),
        ),
    )


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("short_tuple", "daily_candle_as_of_candidate_invalid"),
        ("list", "daily_candle_as_of_candidate_invalid"),
        ("candle", "daily_candle_as_of_candle_invalid"),
        ("timing", "daily_candle_as_of_timing_evidence_invalid"),
    ],
)
def test_rejects_malformed_candidate_envelopes(
    case: str,
    reason: str,
) -> None:
    candidate: object
    if case == "short_tuple":
        candidate = (object(),)
    elif case == "list":
        candidate = []
    elif case == "candle":
        candidate = (object(), object())
    else:
        candidate = (_candle(), object())
    _assert_rejected(
        reason,
        lambda: select_daily_candles_as_of([candidate], as_of=CUTOFF),
    )


def test_revalidates_frozen_sources_and_copies_canonical_values() -> None:
    original_candle, original_timing = _candidate()
    selected = select_daily_candles_as_of(
        [(original_candle, original_timing)],
        as_of=CUTOFF,
    )[0]
    selected_timing_hash = (
        selected.timing_evidence.canonical_timing_evidence_sha256
    )

    object.__setattr__(original_candle, "close_krw", 72_100)
    object.__setattr__(
        original_timing,
        "canonical_timing_evidence_sha256",
        "0" * 64,
    )

    assert selected.candle.close_krw == 72_000
    assert (
        selected.timing_evidence.canonical_timing_evidence_sha256
        == selected_timing_hash
    )
    _assert_rejected(
        "daily_candle_as_of_candle_invalid",
        lambda: select_daily_candles_as_of(
            [(original_candle, original_timing)],
            as_of=CUTOFF,
        ),
    )


def test_rejects_tampered_future_candidate_before_as_of_filtering() -> None:
    future_candle, future_timing = _candidate(
        _candle(observed_at=CUTOFF + timedelta(hours=1))
    )
    object.__setattr__(
        future_timing,
        "canonical_timing_evidence_sha256",
        "0" * 64,
    )

    _assert_rejected(
        "daily_candle_as_of_timing_evidence_invalid",
        lambda: select_daily_candles_as_of(
            [(future_candle, future_timing)],
            as_of=CUTOFF,
        ),
    )


@pytest.mark.parametrize(
    "as_of",
    [
        date(2026, 3, 26),
        datetime(2026, 3, 26, 9, 0),
    ],
)
def test_rejects_non_datetime_and_naive_as_of(as_of: object) -> None:
    _assert_rejected(
        "daily_candle_as_of_as_of_requires_timezone",
        lambda: select_daily_candles_as_of([], as_of=as_of),
    )


def test_rejects_datetime_subclass_as_of() -> None:
    class DerivedDateTime(datetime):
        pass

    derived = DerivedDateTime(2026, 3, 26, 9, 0, tzinfo=KST)

    _assert_rejected(
        "daily_candle_as_of_as_of_requires_timezone",
        lambda: select_daily_candles_as_of([], as_of=derived),
    )


def test_rejects_pathological_timezone_with_domain_error() -> None:
    class ExplodingTimezone(tzinfo):
        def utcoffset(self, value: datetime | None) -> timedelta | None:
            raise RuntimeError("timezone must not escape")

        def dst(self, value: datetime | None) -> timedelta | None:
            return timedelta(0)

        def tzname(self, value: datetime | None) -> str | None:
            return "exploding"

    pathological = datetime(2026, 3, 26, 9, 0, tzinfo=ExplodingTimezone())

    _assert_rejected(
        "daily_candle_as_of_as_of_requires_timezone",
        lambda: select_daily_candles_as_of([], as_of=pathological),
    )


def test_rejects_non_exact_candidate_snapshot_containers() -> None:
    class CandidateList(list[object]):
        pass

    for candidates in ("not-a-candidate-snapshot", CandidateList()):
        with pytest.raises(DailyCandleAsOfError) as caught:
            select_daily_candles_as_of(candidates, as_of=CUTOFF)
        assert caught.value.safe_message == (
            "daily_candle_as_of_candidates_invalid"
        )


def test_timezone_equivalence_and_candidate_permutations_are_deterministic() -> None:
    original = _candidate()
    corrected = _candidate(
        _candle(
            observed_at=CUTOFF + timedelta(hours=1),
            close_krw=72_100,
        )
    )
    other_symbol = _candidate(_candle(symbol="000660"))
    candidates = [original, corrected, other_symbol]
    as_of_kst = CUTOFF + timedelta(hours=1)
    expected = select_daily_candles_as_of(candidates, as_of=as_of_kst)

    for permutation in permutations(candidates):
        assert select_daily_candles_as_of(
            permutation,
            as_of=as_of_kst.astimezone(UTC),
        ) == expected

    assert [item.candle.symbol for item in expected] == ["000660", "005930"]
    assert expected[1].candle.close_krw == 72_100
    assert expected[1].selected_as_of.tzinfo is UTC

    utc_source = _candidate(
        _candle(
            provider_event_at=CANDLE_EVENT.astimezone(UTC),
            observed_at=CUTOFF.astimezone(UTC),
        )
    )
    assert select_daily_candles_as_of(
        [utc_source],
        as_of=as_of_kst.astimezone(UTC),
    ) == select_daily_candles_as_of(
        [original],
        as_of=as_of_kst,
    )


def test_selected_wrapper_cannot_bypass_selector_contract() -> None:
    candle, timing = _candidate()

    _assert_rejected(
        "daily_candle_as_of_selection_requires_selector",
        lambda: SelectedPointInTimeDailyCandleV1(
            candle=candle,
            timing_evidence=timing,
            selected_as_of=CUTOFF,
        ),
    )


def test_selection_does_not_claim_feature_or_order_readiness() -> None:
    selected = select_daily_candles_as_of([_candidate()], as_of=CUTOFF)[0]

    assert not hasattr(selected, "feature_ready")
    assert not hasattr(selected, "dq_passed")
    assert not hasattr(selected, "is_final")
    assert not hasattr(selected, "order_allowed")


def _candidate(
    candle: PointInTimeCandleV1 | None = None,
    *,
    calendar_observed_at: datetime = CUTOFF,
    regular_end_at: datetime = REGULAR_END,
) -> tuple[PointInTimeCandleV1, PointInTimeDailyCandleTimingEvidenceV1]:
    source = candle if candle is not None else _candle()
    timing = build_daily_candle_timing_evidence(
        source,
        _session(
            provider=source.provider,
            observed_at=calendar_observed_at,
            regular_end_at=regular_end_at,
        ),
    )
    return source, timing


def _candle(
    *,
    provider: str = "toss",
    symbol: str = "005930",
    adjusted: bool = True,
    provider_event_at: datetime = CANDLE_EVENT,
    observed_at: datetime = CUTOFF,
    close_krw: int = 72_000,
    provider_contract_sha256: str = CANDLE_CONTRACT_SHA,
) -> PointInTimeCandleV1:
    return PointInTimeCandleV1.create(
        provider=provider,
        symbol=symbol,
        market="KR",
        interval="1d",
        adjusted=adjusted,
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
    observed_at: datetime = CUTOFF,
    regular_end_at: datetime = REGULAR_END,
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
        provider_contract_sha256=CALENDAR_CONTRACT_SHA,
    )


def _assert_rejected(reason: str, action: Callable[[], object]) -> None:
    with pytest.raises(DailyCandleAsOfError) as caught:
        action()
    assert caught.value.component == "daily_candle_as_of"
    assert caught.value.safe_message == reason
