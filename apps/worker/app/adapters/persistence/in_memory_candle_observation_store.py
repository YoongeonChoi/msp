from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.application.ports.candle_observation_store_port import (
    CandleObservationStoreError,
    CandleObservationWriteReceipt,
)
from app.domain.market_data.point_in_time import PointInTimeCandleV1


@dataclass(frozen=True, slots=True)
class StoredCandleObservation:
    revision: int
    candle: PointInTimeCandleV1


class InMemoryCandleObservationStore:
    def __init__(self) -> None:
        self._revisions: dict[str, list[StoredCandleObservation]] = {}
        self._last_seen_observed_at: dict[str, datetime] = {}

    async def append_observation(
        self,
        candle: PointInTimeCandleV1,
    ) -> CandleObservationWriteReceipt:
        if not isinstance(candle, PointInTimeCandleV1):
            raise CandleObservationStoreError(
                "candle_observation_store_item_invalid"
            )
        identity = candle.idempotency_key
        revisions = self._revisions.setdefault(identity, [])
        if not revisions:
            receipt = self._insert(revisions, candle)
            self._last_seen_observed_at[identity] = candle.observed_at
            return receipt

        latest = revisions[-1]
        latest_candle = latest.candle
        last_seen_observed_at = self._last_seen_observed_at[identity]
        if candle.observed_at < last_seen_observed_at:
            raise CandleObservationStoreError(
                "candle_observation_store_observation_time_regressed"
            )
        if (
            candle.canonical_observation_sha256
            == latest_candle.canonical_observation_sha256
        ):
            self._last_seen_observed_at[identity] = candle.observed_at
            return _receipt(latest, inserted=False)

        if any(
            stored.candle.canonical_observation_sha256
            == candle.canonical_observation_sha256
            for stored in revisions[:-1]
        ):
            raise CandleObservationStoreError(
                "candle_observation_store_historical_hash_recurrence_ambiguous"
            )
        if candle.observed_at <= last_seen_observed_at:
            raise CandleObservationStoreError(
                "candle_observation_store_revision_time_not_increasing"
            )
        receipt = self._insert(revisions, candle)
        self._last_seen_observed_at[identity] = candle.observed_at
        return receipt

    def revisions_for(self, idempotency_key: str) -> tuple[StoredCandleObservation, ...]:
        return tuple(self._revisions.get(idempotency_key, ()))

    @staticmethod
    def _insert(
        revisions: list[StoredCandleObservation],
        candle: PointInTimeCandleV1,
    ) -> CandleObservationWriteReceipt:
        stored = StoredCandleObservation(
            revision=len(revisions) + 1,
            candle=candle,
        )
        revisions.append(stored)
        return _receipt(stored, inserted=True)


def _receipt(
    stored: StoredCandleObservation,
    *,
    inserted: bool,
) -> CandleObservationWriteReceipt:
    return CandleObservationWriteReceipt(
        idempotency_key=stored.candle.idempotency_key,
        canonical_observation_sha256=(
            stored.candle.canonical_observation_sha256
        ),
        revision=stored.revision,
        inserted=inserted,
        stored_observed_at=stored.candle.observed_at,
    )
