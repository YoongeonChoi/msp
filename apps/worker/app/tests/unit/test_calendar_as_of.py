from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.domain.market_data.calendar_as_of import (
    CalendarAsOfError,
    SelectedPointInTimeKrDailySessionV1,
    select_kr_daily_sessions_as_of,
)
from app.domain.market_data.point_in_time_calendar import (
    PointInTimeKrDailySessionV1,
)

SESSION_DATE = date(2026, 7, 20)
CONTRACT_SHA256 = "1" * 64


def test_selector_preserves_open_and_closed_days_without_fabricating_dates() -> None:
    open_day = _session(SESSION_DATE, observed_hour=1, is_open=True)
    closed_date = SESSION_DATE + timedelta(days=1)
    closed_day = _session(closed_date, observed_hour=2, is_open=False)

    selected = select_kr_daily_sessions_as_of(
        [closed_day, open_day],
        as_of=datetime(2026, 7, 19, 3, tzinfo=UTC),
    )

    assert [item.session.session_date for item in selected] == [
        SESSION_DATE,
        closed_date,
    ]
    assert selected[0].session.is_open is True
    assert selected[1].session.is_open is False
    assert selected[1].session.regular_start_at is None
    assert selected[1].session.regular_end_at is None
    assert len(selected) == 2


@pytest.mark.parametrize(
    ("states", "expected_is_open", "expected_hour"),
    [
        ((True, True), True, 2),
        ((True, False), False, 2),
        ((True, False, False), False, 3),
    ],
)
def test_selector_allows_adjacent_reobservation_and_forward_correction(
    states: tuple[bool, ...],
    expected_is_open: bool,
    expected_hour: int,
) -> None:
    candidates = [
        _session(SESSION_DATE, observed_hour=index + 1, is_open=is_open)
        for index, is_open in enumerate(states)
    ]

    selected = select_kr_daily_sessions_as_of(
        candidates,
        as_of=datetime(2026, 7, 19, 4, tzinfo=UTC),
    )

    assert len(selected) == 1
    assert selected[0].session.is_open is expected_is_open
    assert selected[0].session.observed_at == datetime(
        2026,
        7,
        19,
        expected_hour,
        tzinfo=UTC,
    )


def test_selector_rejects_conflicting_content_at_same_source_clock() -> None:
    candidates = [
        _session(SESSION_DATE, observed_hour=1, is_open=True),
        _session(SESSION_DATE, observed_hour=1, is_open=False),
    ]

    with pytest.raises(
        CalendarAsOfError,
        match="calendar_as_of_revision_clock_conflict",
    ):
        select_kr_daily_sessions_as_of(
            candidates,
            as_of=datetime(2026, 7, 19, 2, tzinfo=UTC),
        )


def test_selector_rejects_historical_content_recurrence() -> None:
    candidates = [
        _session(SESSION_DATE, observed_hour=1, is_open=True),
        _session(SESSION_DATE, observed_hour=2, is_open=False),
        _session(SESSION_DATE, observed_hour=3, is_open=True),
    ]

    with pytest.raises(
        CalendarAsOfError,
        match="calendar_as_of_historical_hash_recurrence_ambiguous",
    ):
        select_kr_daily_sessions_as_of(
            candidates,
            as_of=datetime(2026, 7, 19, 4, tzinfo=UTC),
        )


def test_selector_uses_source_observed_at_as_semantic_cutoff() -> None:
    before = _session(SESSION_DATE, observed_hour=1, is_open=True)
    after = _session(SESSION_DATE, observed_hour=3, is_open=False)

    selected = select_kr_daily_sessions_as_of(
        [before, after],
        as_of=datetime(2026, 7, 19, 2, tzinfo=UTC),
    )

    assert len(selected) == 1
    assert selected[0].session == before
    assert selected[0].selected_as_of == datetime(2026, 7, 19, 2, tzinfo=UTC)
    assert selected[0].available_at == before.observed_at
    assert selected[0].logical_session_id == before.idempotency_key


def test_selector_returns_empty_when_no_observation_is_eligible() -> None:
    selected = select_kr_daily_sessions_as_of(
        [_session(SESSION_DATE, observed_hour=3, is_open=True)],
        as_of=datetime(2026, 7, 19, 2, tzinfo=UTC),
    )

    assert selected == ()


@pytest.mark.parametrize(
    "bad_candidates",
    [None, {}, set(), "calendar"],
)
def test_selector_rejects_non_snapshot_sequences(bad_candidates: object) -> None:
    with pytest.raises(CalendarAsOfError, match="candidates_invalid"):
        select_kr_daily_sessions_as_of(
            bad_candidates,  # type: ignore[arg-type]
            as_of=datetime(2026, 7, 19, 2, tzinfo=UTC),
        )


def test_selector_rejects_non_calendar_candidates() -> None:
    with pytest.raises(CalendarAsOfError, match="session_invalid"):
        select_kr_daily_sessions_as_of(
            [object()],
            as_of=datetime(2026, 7, 19, 2, tzinfo=UTC),
        )


def test_selector_requires_timezone_aware_as_of() -> None:
    with pytest.raises(CalendarAsOfError, match="requires_timezone"):
        select_kr_daily_sessions_as_of(
            [],
            as_of=datetime(2026, 7, 19, 2),
        )


def test_selected_session_cannot_be_constructed_outside_selector() -> None:
    with pytest.raises(CalendarAsOfError, match="requires_selector"):
        SelectedPointInTimeKrDailySessionV1(
            session=_session(SESSION_DATE, observed_hour=1, is_open=True),
            selected_as_of=datetime(2026, 7, 19, 2, tzinfo=UTC),
        )


def _session(
    session_date: date,
    *,
    observed_hour: int,
    is_open: bool,
) -> PointInTimeKrDailySessionV1:
    next_business_date = session_date + timedelta(days=1)
    return PointInTimeKrDailySessionV1.create(
        provider="krx-calendar",
        market="KR",
        session_date=session_date,
        is_open=is_open,
        regular_start_at=(
            datetime.combine(session_date, datetime.min.time(), UTC)
            if is_open
            else None
        ),
        regular_end_at=(
            datetime.combine(session_date, datetime.min.time(), UTC)
            + timedelta(hours=6, minutes=30)
            if is_open
            else None
        ),
        next_business_date=next_business_date,
        next_regular_start_at=datetime.combine(
            next_business_date,
            datetime.min.time(),
            UTC,
        ),
        next_regular_end_at=datetime.combine(
            next_business_date,
            datetime.min.time(),
            UTC,
        )
        + timedelta(hours=6, minutes=30),
        observed_at=datetime(2026, 7, 19, observed_hour, tzinfo=UTC),
        provider_contract_sha256=CONTRACT_SHA256,
    )
