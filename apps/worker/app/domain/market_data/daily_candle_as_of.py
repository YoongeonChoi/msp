from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Self

from app.domain.common.errors import KnownFailClosedError
from app.domain.market_data.daily_candle_timing import (
    DailyCandleTimeWindowError,
    PointInTimeDailyCandleTimingEvidenceV1,
)
from app.domain.market_data.point_in_time import (
    PointInTimeCandleV1,
    PointInTimeDataError,
)

type DailyCandleAsOfCandidate = tuple[
    PointInTimeCandleV1,
    PointInTimeDailyCandleTimingEvidenceV1,
]
type _DailyCandleKey = tuple[str, str, str, str, bool, date]


class DailyCandleAsOfError(KnownFailClosedError):
    def __init__(self, safe_message: str) -> None:
        super().__init__("daily_candle_as_of", safe_message)


@dataclass(frozen=True, slots=True, init=False)
class SelectedPointInTimeDailyCandleV1:
    candle: PointInTimeCandleV1
    timing_evidence: PointInTimeDailyCandleTimingEvidenceV1
    selected_as_of: datetime

    def __init__(
        self,
        *,
        candle: object,
        timing_evidence: object,
        selected_as_of: object,
    ) -> None:
        raise DailyCandleAsOfError(
            "daily_candle_as_of_selection_requires_selector"
        )

    @classmethod
    def _from_selector(
        cls,
        *,
        candle: PointInTimeCandleV1,
        timing_evidence: PointInTimeDailyCandleTimingEvidenceV1,
        selected_as_of: datetime,
    ) -> Self:
        as_of = _require_as_of(selected_as_of)
        valid_candle = _revalidate_candle(candle)
        timing = _revalidate_timing_evidence(timing_evidence)
        _assert_bound(valid_candle, timing)
        if timing.evidence_available_at > as_of:
            raise DailyCandleAsOfError(
                "daily_candle_as_of_selected_evidence_is_future"
            )
        selected = object.__new__(cls)
        object.__setattr__(selected, "candle", valid_candle)
        object.__setattr__(selected, "timing_evidence", timing)
        object.__setattr__(selected, "selected_as_of", as_of)
        return selected

    @property
    def logical_candle_id(self) -> str:
        return self.candle.idempotency_key

    @property
    def available_at(self) -> datetime:
        return self.timing_evidence.evidence_available_at


def select_daily_candles_as_of(
    candidates: Sequence[object],
    *,
    as_of: object,
) -> tuple[SelectedPointInTimeDailyCandleV1, ...]:
    valid_as_of = _require_as_of(as_of)
    if type(candidates) not in (list, tuple):
        raise DailyCandleAsOfError("daily_candle_as_of_candidates_invalid")
    snapshot = tuple(candidates)

    eligible: list[DailyCandleAsOfCandidate] = []
    for candidate in snapshot:
        candle, timing = _validate_candidate(candidate)
        if timing.evidence_available_at <= valid_as_of:
            eligible.append((candle, timing))

    grouped: dict[_DailyCandleKey, list[DailyCandleAsOfCandidate]] = {}
    for candidate in eligible:
        grouped.setdefault(_daily_key(candidate[1]), []).append(candidate)

    selected: list[SelectedPointInTimeDailyCandleV1] = []
    for daily_key in sorted(grouped):
        candle, timing = _select_latest_revision(grouped[daily_key])
        selected.append(
            SelectedPointInTimeDailyCandleV1._from_selector(
                candle=candle,
                timing_evidence=timing,
                selected_as_of=valid_as_of,
            )
        )
    return tuple(selected)


def _validate_candidate(candidate: object) -> DailyCandleAsOfCandidate:
    if type(candidate) is not tuple or len(candidate) != 2:
        raise DailyCandleAsOfError("daily_candle_as_of_candidate_invalid")
    candle = _revalidate_candle(candidate[0])
    timing = _revalidate_timing_evidence(candidate[1])
    _assert_bound(candle, timing)
    return candle, timing


def _revalidate_candle(source: object) -> PointInTimeCandleV1:
    if type(source) is not PointInTimeCandleV1:
        raise DailyCandleAsOfError("daily_candle_as_of_candle_invalid")
    try:
        return PointInTimeCandleV1.from_payload(source.to_payload())
    except (
        PointInTimeDataError,
        AttributeError,
        OverflowError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        raise DailyCandleAsOfError("daily_candle_as_of_candle_invalid") from exc


def _revalidate_timing_evidence(
    source: object,
) -> PointInTimeDailyCandleTimingEvidenceV1:
    if type(source) is not PointInTimeDailyCandleTimingEvidenceV1:
        raise DailyCandleAsOfError(
            "daily_candle_as_of_timing_evidence_invalid"
        )
    try:
        return PointInTimeDailyCandleTimingEvidenceV1.from_payload(
            source.to_payload()
        )
    except (
        DailyCandleTimeWindowError,
        AttributeError,
        OverflowError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        raise DailyCandleAsOfError(
            "daily_candle_as_of_timing_evidence_invalid"
        ) from exc


def _assert_bound(
    candle: PointInTimeCandleV1,
    timing: PointInTimeDailyCandleTimingEvidenceV1,
) -> None:
    comparisons = (
        (candle.provider, timing.provider, "provider"),
        (candle.market, timing.market, "market"),
        (candle.symbol, timing.symbol, "symbol"),
        (candle.interval, timing.interval, "interval"),
        (candle.adjusted, timing.adjusted, "adjusted"),
        (
            candle.provider_event_at,
            timing.candle_provider_event_at,
            "provider_event_at",
        ),
        (candle.observed_at, timing.candle_observed_at, "candle_observed_at"),
        (candle.idempotency_key, timing.candle_idempotency_key, "candle_identity"),
        (
            candle.provider_contract_sha256,
            timing.candle_provider_contract_sha256,
            "candle_provider_contract_sha256",
        ),
        (
            candle.canonical_observation_sha256,
            timing.candle_canonical_observation_sha256,
            "candle_observation_sha256",
        ),
    )
    for actual, expected, field_name in comparisons:
        if actual != expected:
            raise DailyCandleAsOfError(
                f"daily_candle_as_of_{field_name}_mismatch"
            )


def _daily_key(
    timing: PointInTimeDailyCandleTimingEvidenceV1,
) -> _DailyCandleKey:
    return (
        timing.provider,
        timing.market,
        timing.symbol,
        timing.interval,
        timing.adjusted,
        timing.session_date,
    )


def _select_latest_revision(
    candidates: Sequence[DailyCandleAsOfCandidate],
) -> DailyCandleAsOfCandidate:
    identities = {candle.idempotency_key for candle, _ in candidates}
    if len(identities) != 1:
        raise DailyCandleAsOfError(
            "daily_candle_as_of_daily_identity_ambiguous"
        )

    by_observed_at: dict[datetime, list[DailyCandleAsOfCandidate]] = {}
    for candidate in candidates:
        by_observed_at.setdefault(candidate[0].observed_at, []).append(candidate)

    observation_points: list[DailyCandleAsOfCandidate] = []
    for observed_at in sorted(by_observed_at):
        at_same_time = by_observed_at[observed_at]
        hashes = {
            candle.canonical_observation_sha256
            for candle, _ in at_same_time
        }
        if len(hashes) != 1:
            raise DailyCandleAsOfError(
                "daily_candle_as_of_revision_clock_conflict"
            )
        candles = {candle for candle, _ in at_same_time}
        if len(candles) != 1:
            raise DailyCandleAsOfError(
                "daily_candle_as_of_revision_clock_conflict"
            )
        observation_points.append(_latest_timing_evidence(at_same_time))

    correction_stream: list[DailyCandleAsOfCandidate] = []
    seen_content_by_hash: dict[str, tuple[object, ...]] = {}
    for candidate in observation_points:
        candle = candidate[0]
        observation_hash = candle.canonical_observation_sha256
        content = _candle_semantic_content(candle)
        if correction_stream and (
            correction_stream[-1][0].canonical_observation_sha256
            == observation_hash
        ):
            if seen_content_by_hash[observation_hash] != content:
                raise DailyCandleAsOfError(
                    "daily_candle_as_of_observation_hash_semantic_conflict"
                )
            continue
        if observation_hash in seen_content_by_hash:
            if seen_content_by_hash[observation_hash] != content:
                raise DailyCandleAsOfError(
                    "daily_candle_as_of_observation_hash_semantic_conflict"
                )
            raise DailyCandleAsOfError(
                "daily_candle_as_of_historical_hash_recurrence_ambiguous"
            )
        correction_stream.append(candidate)
        seen_content_by_hash[observation_hash] = content

    return correction_stream[-1]


def _latest_timing_evidence(
    candidates: Sequence[DailyCandleAsOfCandidate],
) -> DailyCandleAsOfCandidate:
    latest_available_at = max(
        timing.evidence_available_at for _, timing in candidates
    )
    latest = [
        candidate
        for candidate in candidates
        if candidate[1].evidence_available_at == latest_available_at
    ]
    unique = set(latest)
    if len(unique) != 1:
        raise DailyCandleAsOfError(
            "daily_candle_as_of_timing_evidence_conflict"
        )
    return unique.pop()


def _candle_semantic_content(
    candle: PointInTimeCandleV1,
) -> tuple[object, ...]:
    return (
        candle.schema_version,
        candle.provider,
        candle.symbol,
        candle.market,
        candle.interval,
        candle.adjusted,
        candle.provider_event_at,
        candle.currency,
        candle.open_krw,
        candle.high_krw,
        candle.low_krw,
        candle.close_krw,
        candle.volume,
        candle.provider_contract_sha256,
        candle.canonical_observation_sha256,
    )


def _require_as_of(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None:
        raise DailyCandleAsOfError(
            "daily_candle_as_of_as_of_requires_timezone"
        )
    try:
        offset = value.utcoffset()
    except (OverflowError, RuntimeError, TypeError, ValueError) as exc:
        raise DailyCandleAsOfError(
            "daily_candle_as_of_as_of_requires_timezone"
        ) from exc
    if offset is None:
        raise DailyCandleAsOfError(
            "daily_candle_as_of_as_of_requires_timezone"
        )
    try:
        return value.astimezone(UTC)
    except (OverflowError, RuntimeError, TypeError, ValueError) as exc:
        raise DailyCandleAsOfError(
            "daily_candle_as_of_as_of_requires_timezone"
        ) from exc
