from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

import pytest

from app.adapters.persistence.in_memory_calendar_observation_store import (
    InMemoryCalendarObservationStore,
)
from app.application.ports.calendar_observation_store_port import (
    CalendarObservationStoreError,
)
from app.domain.common.time import KST
from app.domain.market_data.point_in_time_calendar import (
    PointInTimeKrDailySessionV1,
)

SESSION_DATE = date(2026, 3, 25)
OBSERVED_AT = datetime(2026, 3, 25, 7, 0, tzinfo=UTC)
CONTRACT_SHA = "a" * 64


async def test_store_deep_canonicalizes_first_open_observation() -> None:
    store = InMemoryCalendarObservationStore()
    session = _session()

    receipt = await store.append_observation(session)

    assert receipt.status == "stored"
    assert receipt.calendar_idempotency_key == session.idempotency_key
    assert receipt.canonical_evidence_sha256 == session.canonical_evidence_sha256
    assert receipt.revision == 1
    assert receipt.revision_inserted is True
    assert receipt.occurrence_inserted is True
    assert receipt.occurrence_id.version == 4
    assert receipt.observed_at == OBSERVED_AT
    stored = store.revisions_for(session.idempotency_key)[0]
    assert stored.session == session
    assert stored.session is not session


async def test_store_accepts_closed_session_as_independent_observation() -> None:
    store = InMemoryCalendarObservationStore()
    closed = _session(is_open=False)

    receipt = await store.append_observation(closed)

    assert receipt.status == "stored"
    assert store.revisions_for(closed.idempotency_key)[0].session.is_open is False


async def test_store_exact_replay_reuses_revision_and_occurrence() -> None:
    store = InMemoryCalendarObservationStore()
    session = _session()
    first = await store.append_observation(session)

    replay = await store.append_observation(_session())

    assert replay.status == "replayed"
    assert replay.revision == 1
    assert replay.revision_inserted is False
    assert replay.occurrence_inserted is False
    assert replay.occurrence_id == first.occurrence_id
    assert len(store.revisions_for(session.idempotency_key)) == 1
    assert len(store.occurrences_for(session.idempotency_key)) == 1


async def test_store_tracks_later_unchanged_observation_as_new_occurrence() -> None:
    store = InMemoryCalendarObservationStore()
    session = _session()
    first = await store.append_observation(session)

    later = await store.append_observation(
        _session(observed_at=OBSERVED_AT + timedelta(minutes=5))
    )

    assert later.status == "replayed"
    assert later.revision == 1
    assert later.revision_inserted is False
    assert later.occurrence_inserted is True
    assert later.occurrence_id != first.occurrence_id
    assert later.observed_at == OBSERVED_AT + timedelta(minutes=5)
    assert len(store.revisions_for(session.idempotency_key)) == 1
    assert len(store.occurrences_for(session.idempotency_key)) == 2


async def test_store_replays_older_exact_occurrence_after_later_observation() -> None:
    store = InMemoryCalendarObservationStore()
    original = _session()
    first = await store.append_observation(original)
    await store.append_observation(
        _session(observed_at=OBSERVED_AT + timedelta(minutes=5))
    )

    replay = await store.append_observation(_session())

    assert replay.status == "replayed"
    assert replay.revision == 1
    assert replay.revision_inserted is False
    assert replay.occurrence_id == first.occurrence_id
    assert replay.occurrence_inserted is False
    assert replay.observed_at == OBSERVED_AT
    assert len(store.revisions_for(original.idempotency_key)) == 1
    assert len(store.occurrences_for(original.idempotency_key)) == 2


async def test_store_appends_changed_content_as_revision_and_occurrence() -> None:
    store = InMemoryCalendarObservationStore()
    original = _session()
    await store.append_observation(original)

    receipt = await store.append_observation(
        _session(
            observed_at=OBSERVED_AT + timedelta(minutes=5),
            provider_contract_sha256="b" * 64,
        )
    )

    assert receipt.status == "stored"
    assert receipt.revision == 2
    assert receipt.revision_inserted is True
    assert receipt.occurrence_inserted is True
    assert len(store.revisions_for(original.idempotency_key)) == 2
    assert len(store.occurrences_for(original.idempotency_key)) == 2


async def test_store_fails_closed_on_regression_after_later_replay() -> None:
    store = InMemoryCalendarObservationStore()
    session = _session()
    await store.append_observation(session)
    await store.append_observation(
        _session(observed_at=OBSERVED_AT + timedelta(minutes=10))
    )

    with pytest.raises(
        CalendarObservationStoreError,
        match="pit_calendar_observation_time_regressed",
    ):
        await store.append_observation(
            _session(observed_at=OBSERVED_AT + timedelta(minutes=5))
        )

    assert len(store.occurrences_for(session.idempotency_key)) == 2


async def test_store_fails_closed_on_same_clock_content_conflict() -> None:
    store = InMemoryCalendarObservationStore()
    session = _session()
    await store.append_observation(session)

    with pytest.raises(
        CalendarObservationStoreError,
        match="pit_calendar_revision_time_not_increasing",
    ):
        await store.append_observation(
            _session(provider_contract_sha256="b" * 64)
        )

    assert len(store.revisions_for(session.idempotency_key)) == 1
    assert len(store.occurrences_for(session.idempotency_key)) == 1


async def test_store_fails_closed_on_older_clock_content_conflict() -> None:
    store = InMemoryCalendarObservationStore()
    session = _session()
    await store.append_observation(session)
    await store.append_observation(
        _session(observed_at=OBSERVED_AT + timedelta(minutes=5))
    )

    with pytest.raises(
        CalendarObservationStoreError,
        match="pit_calendar_revision_time_not_increasing",
    ):
        await store.append_observation(
            _session(provider_contract_sha256="b" * 64)
        )

    assert len(store.revisions_for(session.idempotency_key)) == 1
    assert len(store.occurrences_for(session.idempotency_key)) == 2


async def test_store_fails_closed_on_historical_hash_recurrence() -> None:
    store = InMemoryCalendarObservationStore()
    original = _session()
    await store.append_observation(original)
    await store.append_observation(
        _session(
            observed_at=OBSERVED_AT + timedelta(minutes=5),
            provider_contract_sha256="b" * 64,
        )
    )

    with pytest.raises(
        CalendarObservationStoreError,
        match="pit_calendar_historical_hash_recurrence_ambiguous",
    ):
        await store.append_observation(
            _session(observed_at=OBSERVED_AT + timedelta(minutes=10))
        )

    assert len(store.revisions_for(original.idempotency_key)) == 2
    assert len(store.occurrences_for(original.idempotency_key)) == 2


async def test_store_rejects_invalid_or_tampered_input_without_mutation() -> None:
    store = InMemoryCalendarObservationStore()
    tampered = _session()
    object.__setattr__(tampered, "provider_contract_sha256", "b" * 64)

    for invalid in (cast(Any, object()), tampered):
        with pytest.raises(
            CalendarObservationStoreError,
            match="calendar_observation_store_item_invalid",
        ):
            await store.append_observation(invalid)

    assert store.revisions_for(_session().idempotency_key) == ()


async def test_same_loop_exact_delivery_inserts_once() -> None:
    store = InMemoryCalendarObservationStore()
    session = _session()

    receipts = await asyncio.gather(
        store.append_observation(session),
        store.append_observation(session),
    )

    assert sorted(item.revision_inserted for item in receipts) == [False, True]
    assert sorted(item.occurrence_inserted for item in receipts) == [False, True]
    assert len(store.revisions_for(session.idempotency_key)) == 1
    assert len(store.occurrences_for(session.idempotency_key)) == 1


async def test_receipt_and_stored_records_are_frozen() -> None:
    store = InMemoryCalendarObservationStore()
    session = _session()
    receipt = await store.append_observation(session)
    stored = store.revisions_for(session.idempotency_key)[0]

    with pytest.raises(FrozenInstanceError):
        receipt.revision = 2  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        stored.revision = 2  # type: ignore[misc]


async def test_source_and_accessor_mutation_cannot_change_internal_state() -> None:
    store = InMemoryCalendarObservationStore()
    source = _session()
    await store.append_observation(source)
    identity = source.idempotency_key

    object.__setattr__(source, "provider_contract_sha256", "b" * 64)
    exported_revision = store.revisions_for(identity)[0]
    exported_occurrence = store.occurrences_for(identity)[0]
    object.__setattr__(exported_revision.session, "provider", "tampered")
    object.__setattr__(exported_occurrence.session, "provider", "tampered")

    stored_revision = store.revisions_for(identity)[0]
    stored_occurrence = store.occurrences_for(identity)[0]
    assert stored_revision.session.provider == "toss"
    assert stored_revision.session.provider_contract_sha256 == CONTRACT_SHA
    assert stored_occurrence.session.provider == "toss"
    assert stored_occurrence.session.provider_contract_sha256 == CONTRACT_SHA


def _session(
    *,
    is_open: bool = True,
    observed_at: datetime = OBSERVED_AT,
    provider_contract_sha256: str = CONTRACT_SHA,
) -> PointInTimeKrDailySessionV1:
    return PointInTimeKrDailySessionV1.create(
        provider="toss",
        market="KR",
        session_date=SESSION_DATE,
        is_open=is_open,
        regular_start_at=(
            datetime(2026, 3, 25, 9, 0, tzinfo=KST) if is_open else None
        ),
        regular_end_at=(
            datetime(2026, 3, 25, 15, 30, tzinfo=KST) if is_open else None
        ),
        next_business_date=date(2026, 3, 26),
        next_regular_start_at=datetime(2026, 3, 26, 9, 0, tzinfo=KST),
        next_regular_end_at=datetime(2026, 3, 26, 15, 30, tzinfo=KST),
        observed_at=observed_at,
        provider_contract_sha256=provider_contract_sha256,
    )
