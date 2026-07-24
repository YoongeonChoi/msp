from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from app.adapters.persistence.in_memory_candle_observation_store import (
    InMemoryCandleObservationStore,
)
from app.application.ports.candle_observation_store_port import (
    CandleObservationStoreError,
)
from app.domain.market_data.point_in_time import PointInTimeCandleV1

EVENT_AT = datetime(2026, 3, 24, 0, 0, tzinfo=UTC)
OBSERVED_AT = datetime(2026, 3, 25, 0, 0, tzinfo=UTC)
CONTRACT_SHA = "a" * 64


async def test_store_appends_first_observation_as_revision_one() -> None:
    store = InMemoryCandleObservationStore()
    candle = _candle()

    receipt = await store.append_observation(candle)

    assert receipt.idempotency_key == candle.idempotency_key
    assert receipt.canonical_observation_sha256 == (candle.canonical_observation_sha256)
    assert receipt.revision == 1
    assert receipt.inserted is True
    assert receipt.stored_observed_at == OBSERVED_AT
    stored = store.revisions_for(candle.idempotency_key)[0].candle
    assert stored == candle
    assert stored is not candle


async def test_store_deduplicates_latest_exact_replay_and_keeps_first_time() -> None:
    store = InMemoryCandleObservationStore()
    first = _candle()
    replay = _candle(observed_at=OBSERVED_AT + timedelta(minutes=5))
    await store.append_observation(first)

    receipt = await store.append_observation(replay)

    assert receipt.revision == 1
    assert receipt.inserted is False
    assert receipt.stored_observed_at == first.observed_at
    revisions = store.revisions_for(first.idempotency_key)
    assert revisions == (revisions[0],)
    assert revisions[0].candle == first
    assert revisions[0].candle is not first


async def test_store_rejects_exact_replay_with_regressed_observation_time() -> None:
    store = InMemoryCandleObservationStore()
    await store.append_observation(_candle())

    with pytest.raises(
        CandleObservationStoreError,
        match="candle_observation_store_observation_time_regressed",
    ):
        await store.append_observation(_candle(observed_at=OBSERVED_AT - timedelta(seconds=1)))


async def test_store_rejects_stale_correction_after_later_exact_replay() -> None:
    store = InMemoryCandleObservationStore()
    original = _candle()
    later_replay = _candle(observed_at=OBSERVED_AT + timedelta(minutes=10))
    stale_correction = _candle(
        observed_at=OBSERVED_AT + timedelta(minutes=5),
        close_krw=72_100,
    )
    await store.append_observation(original)
    replay_receipt = await store.append_observation(later_replay)

    with pytest.raises(
        CandleObservationStoreError,
        match="candle_observation_store_observation_time_regressed",
    ):
        await store.append_observation(stale_correction)

    assert replay_receipt.inserted is False
    assert replay_receipt.stored_observed_at == original.observed_at
    assert len(store.revisions_for(original.idempotency_key)) == 1


async def test_store_appends_changed_observation_as_next_revision() -> None:
    store = InMemoryCandleObservationStore()
    original = _candle()
    corrected = _candle(
        observed_at=OBSERVED_AT + timedelta(minutes=5),
        close_krw=72_100,
        high_krw=72_300,
    )
    await store.append_observation(original)

    receipt = await store.append_observation(corrected)

    assert receipt.revision == 2
    assert receipt.inserted is True
    assert receipt.stored_observed_at == corrected.observed_at
    revisions = store.revisions_for(original.idempotency_key)
    assert [(item.revision, item.candle) for item in revisions] == [
        (1, original),
        (2, corrected),
    ]
    assert not hasattr(receipt, "feature_ready")
    assert not hasattr(receipt, "is_complete")


async def test_store_deduplicates_exact_replay_of_latest_revision() -> None:
    store = InMemoryCandleObservationStore()
    original = _candle()
    corrected = _candle(
        observed_at=OBSERVED_AT + timedelta(minutes=5),
        close_krw=72_100,
    )
    replay = _candle(
        observed_at=OBSERVED_AT + timedelta(minutes=10),
        close_krw=72_100,
    )
    await store.append_observation(original)
    await store.append_observation(corrected)

    receipt = await store.append_observation(replay)

    assert receipt.revision == 2
    assert receipt.inserted is False
    assert receipt.stored_observed_at == corrected.observed_at
    assert len(store.revisions_for(original.idempotency_key)) == 2


async def test_store_rejects_historical_hash_recurrence_as_ambiguous() -> None:
    store = InMemoryCandleObservationStore()
    original = _candle()
    corrected = _candle(
        observed_at=OBSERVED_AT + timedelta(minutes=5),
        close_krw=72_100,
    )
    await store.append_observation(original)
    await store.append_observation(corrected)

    with pytest.raises(
        CandleObservationStoreError,
        match="candle_observation_store_historical_hash_recurrence_ambiguous",
    ):
        await store.append_observation(_candle(observed_at=OBSERVED_AT + timedelta(minutes=10)))

    assert len(store.revisions_for(original.idempotency_key)) == 2


@pytest.mark.parametrize(
    ("observed_at", "reason"),
    [
        (
            OBSERVED_AT - timedelta(seconds=1),
            "candle_observation_store_observation_time_regressed",
        ),
        (
            OBSERVED_AT,
            "candle_observation_store_revision_time_not_increasing",
        ),
    ],
)
async def test_store_rejects_changed_revision_without_increasing_time(
    observed_at: datetime,
    reason: str,
) -> None:
    store = InMemoryCandleObservationStore()
    original = _candle()
    await store.append_observation(original)

    with pytest.raises(
        CandleObservationStoreError,
        match=reason,
    ):
        await store.append_observation(_candle(observed_at=observed_at, close_krw=72_100))

    assert len(store.revisions_for(original.idempotency_key)) == 1


async def test_store_tracks_each_logical_candle_independently() -> None:
    store = InMemoryCandleObservationStore()
    first = _candle()
    second = _candle(symbol="000660")

    first_receipt = await store.append_observation(first)
    second_receipt = await store.append_observation(second)

    assert first_receipt.revision == 1
    assert second_receipt.revision == 1
    assert first_receipt.idempotency_key != second_receipt.idempotency_key


async def test_store_rejects_non_candle_without_mutation() -> None:
    store = InMemoryCandleObservationStore()

    with pytest.raises(
        CandleObservationStoreError,
        match="candle_observation_store_item_invalid",
    ):
        await store.append_observation(cast(Any, object()))

    assert store.revisions_for("missing") == ()


async def test_same_event_loop_exact_delivery_inserts_only_once() -> None:
    store = InMemoryCandleObservationStore()
    candle = _candle()

    receipts = await asyncio.gather(
        store.append_observation(candle),
        store.append_observation(candle),
    )

    assert sorted(receipt.inserted for receipt in receipts) == [False, True]
    assert {receipt.revision for receipt in receipts} == {1}
    assert len(store.revisions_for(candle.idempotency_key)) == 1


def _candle(
    *,
    symbol: str = "005930",
    observed_at: datetime = OBSERVED_AT,
    open_krw: int = 71_600,
    high_krw: int = 72_300,
    low_krw: int = 71_500,
    close_krw: int = 72_000,
) -> PointInTimeCandleV1:
    return PointInTimeCandleV1.create(
        provider="toss",
        symbol=symbol,
        market="KR",
        interval="1d",
        adjusted=True,
        provider_event_at=EVENT_AT,
        observed_at=observed_at,
        currency="KRW",
        open_krw=open_krw,
        high_krw=high_krw,
        low_krw=low_krw,
        close_krw=close_krw,
        volume=3_521_000,
        provider_contract_sha256=CONTRACT_SHA,
    )
