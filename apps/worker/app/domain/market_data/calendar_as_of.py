from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Self

from app.domain.common.errors import KnownFailClosedError
from app.domain.market_data.point_in_time_calendar import (
    PointInTimeCalendarError,
    PointInTimeKrDailySessionV1,
)

type _CalendarStreamKey = tuple[str, str, date]


class CalendarAsOfError(KnownFailClosedError):
    def __init__(self, safe_message: str) -> None:
        super().__init__("calendar_as_of", safe_message)


@dataclass(frozen=True, slots=True, init=False)
class SelectedPointInTimeKrDailySessionV1:
    session: PointInTimeKrDailySessionV1
    selected_as_of: datetime

    def __init__(self, *, session: object, selected_as_of: object) -> None:
        raise CalendarAsOfError("calendar_as_of_selection_requires_selector")

    @classmethod
    def _from_selector(
        cls,
        *,
        session: PointInTimeKrDailySessionV1,
        selected_as_of: datetime,
    ) -> Self:
        valid_as_of = _require_as_of(selected_as_of)
        valid_session = _revalidate_session(session)
        if valid_session.observed_at.astimezone(UTC) > valid_as_of:
            raise CalendarAsOfError("calendar_as_of_selected_session_is_future")
        selected = object.__new__(cls)
        object.__setattr__(selected, "session", valid_session)
        object.__setattr__(selected, "selected_as_of", valid_as_of)
        return selected

    @property
    def logical_session_id(self) -> str:
        return self.session.idempotency_key

    @property
    def available_at(self) -> datetime:
        return self.session.observed_at.astimezone(UTC)


def select_kr_daily_sessions_as_of(
    candidates: Sequence[object],
    *,
    as_of: object,
) -> tuple[SelectedPointInTimeKrDailySessionV1, ...]:
    """Select the latest unambiguous source observation for each KR date.

    Missing dates are intentionally absent from the result. Repeated adjacent
    evidence is a valid re-observation; returning to older evidence after a
    correction is ambiguous and fails closed.
    """

    valid_as_of = _require_as_of(as_of)
    if type(candidates) not in (list, tuple):
        raise CalendarAsOfError("calendar_as_of_candidates_invalid")

    eligible: list[PointInTimeKrDailySessionV1] = []
    for candidate in tuple(candidates):
        session = _revalidate_session(candidate)
        if session.observed_at.astimezone(UTC) <= valid_as_of:
            eligible.append(session)

    grouped: dict[_CalendarStreamKey, list[PointInTimeKrDailySessionV1]] = {}
    for session in eligible:
        grouped.setdefault(_stream_key(session), []).append(session)

    selected: list[SelectedPointInTimeKrDailySessionV1] = []
    for stream_key in sorted(grouped):
        latest = _select_latest_observation(grouped[stream_key])
        selected.append(
            SelectedPointInTimeKrDailySessionV1._from_selector(
                session=latest,
                selected_as_of=valid_as_of,
            )
        )
    return tuple(selected)


def _revalidate_session(source: object) -> PointInTimeKrDailySessionV1:
    if type(source) is not PointInTimeKrDailySessionV1:
        raise CalendarAsOfError("calendar_as_of_session_invalid")
    try:
        canonical = PointInTimeKrDailySessionV1.from_payload(source.to_payload())
    except (
        AttributeError,
        OverflowError,
        PointInTimeCalendarError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        raise CalendarAsOfError("calendar_as_of_session_invalid") from exc
    if canonical != source:
        raise CalendarAsOfError("calendar_as_of_session_invalid")
    return canonical


def _stream_key(session: PointInTimeKrDailySessionV1) -> _CalendarStreamKey:
    return session.provider, session.market, session.session_date


def _select_latest_observation(
    candidates: Sequence[PointInTimeKrDailySessionV1],
) -> PointInTimeKrDailySessionV1:
    identities = {candidate.idempotency_key for candidate in candidates}
    if len(identities) != 1:
        raise CalendarAsOfError("calendar_as_of_daily_identity_ambiguous")

    by_observed_at: dict[datetime, list[PointInTimeKrDailySessionV1]] = {}
    for candidate in candidates:
        observed_at = candidate.observed_at.astimezone(UTC)
        by_observed_at.setdefault(observed_at, []).append(candidate)

    observation_points: list[PointInTimeKrDailySessionV1] = []
    for observed_at in sorted(by_observed_at):
        at_same_clock = by_observed_at[observed_at]
        evidence_hashes = {
            candidate.canonical_evidence_sha256 for candidate in at_same_clock
        }
        semantic_values = {
            _semantic_content(candidate) for candidate in at_same_clock
        }
        if len(evidence_hashes) != 1 or len(semantic_values) != 1:
            raise CalendarAsOfError("calendar_as_of_revision_clock_conflict")
        observation_points.append(at_same_clock[0])

    correction_stream: list[PointInTimeKrDailySessionV1] = []
    seen_content_by_hash: dict[str, tuple[object, ...]] = {}
    for candidate in observation_points:
        evidence_hash = candidate.canonical_evidence_sha256
        semantic_content = _semantic_content(candidate)
        prior_semantic_content = seen_content_by_hash.get(evidence_hash)
        if prior_semantic_content is not None and (
            prior_semantic_content != semantic_content
        ):
            raise CalendarAsOfError(
                "calendar_as_of_evidence_hash_semantic_conflict"
            )
        if correction_stream and (
            correction_stream[-1].canonical_evidence_sha256 == evidence_hash
        ):
            correction_stream[-1] = candidate
            continue
        if prior_semantic_content is not None:
            raise CalendarAsOfError(
                "calendar_as_of_historical_hash_recurrence_ambiguous"
            )
        correction_stream.append(candidate)
        seen_content_by_hash[evidence_hash] = semantic_content

    return correction_stream[-1]


def _semantic_content(
    session: PointInTimeKrDailySessionV1,
) -> tuple[object, ...]:
    return (
        session.schema_version,
        session.provider,
        session.market,
        session.session_date,
        session.is_open,
        session.regular_start_at,
        session.regular_end_at,
        session.next_business_date,
        session.next_regular_start_at,
        session.next_regular_end_at,
        session.provider_contract_sha256,
        session.canonical_evidence_sha256,
    )


def _require_as_of(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None:
        raise CalendarAsOfError("calendar_as_of_as_of_requires_timezone")
    try:
        if value.utcoffset() is None:
            raise CalendarAsOfError("calendar_as_of_as_of_requires_timezone")
        return value.astimezone(UTC)
    except (OverflowError, RuntimeError, TypeError, ValueError) as exc:
        raise CalendarAsOfError(
            "calendar_as_of_as_of_requires_timezone"
        ) from exc
