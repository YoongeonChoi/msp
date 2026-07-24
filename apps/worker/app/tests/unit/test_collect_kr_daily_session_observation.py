from __future__ import annotations

import asyncio
import traceback
from datetime import UTC, date, datetime, timedelta
from typing import cast
from uuid import UUID

import pytest

from app.application.ports.calendar_observation_store_port import (
    CalendarObservationStatus,
    CalendarObservationStoreError,
    CalendarObservationWriteReceipt,
)
from app.application.use_cases.collect_kr_daily_session_observation import (
    CollectKrDailySessionObservation,
    KrDailySessionCollectionError,
)
from app.domain.common.errors import ProviderUnavailableError
from app.domain.common.time import KST
from app.domain.market_data.point_in_time_calendar import (
    PointInTimeKrDailySessionV1,
)

TARGET_DATE = date(2026, 3, 25)
STARTED_AT = datetime(2026, 3, 24, 23, 0, 0, tzinfo=UTC)
OBSERVED_AT = STARTED_AT + timedelta(seconds=1)
COMPLETED_AT = STARTED_AT + timedelta(seconds=2)
CONTRACT_SHA256 = "c" * 64
OCCURRENCE_ID = UUID("00000000-0000-4000-8000-000000000001")


class FakeSource:
    def __init__(
        self,
        session: object,
        *,
        error: BaseException | None = None,
    ) -> None:
        self.session = session
        self.error = error
        self.calls: list[date] = []

    async def get_kr_daily_session_evidence(
        self,
        target_date: date,
    ) -> PointInTimeKrDailySessionV1:
        self.calls.append(target_date)
        if self.error is not None:
            raise self.error
        return cast(PointInTimeKrDailySessionV1, self.session)


class FakeStore:
    def __init__(
        self,
        receipt: object,
        *,
        error: BaseException | None = None,
    ) -> None:
        self.receipt = receipt
        self.error = error
        self.calls: list[PointInTimeKrDailySessionV1] = []

    async def append_observation(
        self,
        session: PointInTimeKrDailySessionV1,
    ) -> CalendarObservationWriteReceipt:
        self.calls.append(session)
        if self.error is not None:
            raise self.error
        return cast(CalendarObservationWriteReceipt, self.receipt)


class SequenceClock:
    def __init__(self, *values: object) -> None:
        self.values = list(values)
        self.calls = 0

    def __call__(self) -> datetime:
        value = self.values[self.calls]
        self.calls += 1
        if isinstance(value, Exception):
            raise value
        return cast(datetime, value)


@pytest.mark.parametrize("is_open", [True, False])
@pytest.mark.parametrize(
    ("status", "occurrence_inserted"),
    [
        ("stored", True),
        ("replayed", False),
        ("replayed", True),
    ],
)
async def test_collects_one_session_and_appends_one_canonical_observation(
    is_open: bool,
    status: CalendarObservationStatus,
    occurrence_inserted: bool,
) -> None:
    session = _session(is_open=is_open)
    receipt = _receipt(
        session,
        status=status,
        occurrence_inserted=occurrence_inserted,
    )
    source = FakeSource(session)
    store = FakeStore(receipt)
    clock = SequenceClock(STARTED_AT, COMPLETED_AT)

    result = await CollectKrDailySessionObservation(
        source,
        store,
        clock=clock,
    ).execute(TARGET_DATE)

    assert source.calls == [TARGET_DATE]
    assert len(store.calls) == 1
    assert store.calls[0] == session
    assert store.calls[0] is not session
    assert clock.calls == 2
    assert result == receipt
    assert result is not receipt


async def test_rejects_non_exact_date_before_clock_or_io() -> None:
    session = _session()
    source = FakeSource(session)
    store = FakeStore(_receipt(session))
    clock = SequenceClock(STARTED_AT, COMPLETED_AT)

    with pytest.raises(
        KrDailySessionCollectionError,
        match="kr_daily_session_collection_target_date_invalid",
    ):
        await CollectKrDailySessionObservation(
            source,
            store,
            clock=clock,
        ).execute(cast(date, datetime(2026, 3, 25, tzinfo=UTC)))

    assert source.calls == []
    assert store.calls == []
    assert clock.calls == 0


async def test_source_failure_is_sanitized_and_never_reaches_store() -> None:
    secret = "provider payload bearer=must-not-leak"
    source = FakeSource(
        object(),
        error=ProviderUnavailableError("toss", secret),
    )
    store = FakeStore(object())
    clock = SequenceClock(STARTED_AT)

    with pytest.raises(
        KrDailySessionCollectionError,
        match="^kr_daily_session_collection_source_failed$",
    ) as captured:
        await CollectKrDailySessionObservation(
            source,
            store,
            clock=clock,
        ).execute(TARGET_DATE)

    assert source.calls == [TARGET_DATE]
    assert store.calls == []
    assert captured.value.write_outcome == "not_attempted"
    _assert_secret_absent(captured.value, secret)


@pytest.mark.parametrize("observed_at", [STARTED_AT, COMPLETED_AT])
async def test_read_window_is_inclusive_at_both_boundaries(
    observed_at: datetime,
) -> None:
    session = _session(observed_at=observed_at)

    result = await CollectKrDailySessionObservation(
        FakeSource(session),
        FakeStore(_receipt(session)),
        clock=SequenceClock(STARTED_AT, COMPLETED_AT),
    ).execute(TARGET_DATE)

    assert result.observed_at == observed_at


@pytest.mark.parametrize(
    ("source_case", "reason"),
    [
        ("invalid", "source_evidence_invalid"),
        ("wrong_date", "session_date_mismatch"),
        ("too_early", "observed_at_outside_read"),
        ("too_late", "observed_at_outside_read"),
    ],
)
async def test_rejects_invalid_or_unbound_source_evidence_before_store(
    source_case: str,
    reason: str,
) -> None:
    source_values: dict[str, object] = {
        "invalid": object(),
        "wrong_date": _session(session_date=date(2026, 3, 24)),
        "too_early": _session(observed_at=STARTED_AT - timedelta(microseconds=1)),
        "too_late": _session(observed_at=COMPLETED_AT + timedelta(microseconds=1)),
    }
    source_value = source_values[source_case]
    source = FakeSource(source_value)
    store = FakeStore(object())

    with pytest.raises(
        KrDailySessionCollectionError,
        match=f"kr_daily_session_collection_{reason}",
    ):
        await CollectKrDailySessionObservation(
            source,
            store,
            clock=SequenceClock(STARTED_AT, COMPLETED_AT),
        ).execute(TARGET_DATE)

    assert source.calls == [TARGET_DATE]
    assert store.calls == []


async def test_rejects_mutated_source_evidence_before_store() -> None:
    session = _session()
    object.__setattr__(session, "canonical_evidence_sha256", "d" * 64)
    source = FakeSource(session)
    store = FakeStore(object())

    with pytest.raises(
        KrDailySessionCollectionError,
        match="kr_daily_session_collection_source_evidence_invalid",
    ):
        await CollectKrDailySessionObservation(
            source,
            store,
            clock=SequenceClock(STARTED_AT, COMPLETED_AT),
        ).execute(TARGET_DATE)

    assert store.calls == []


async def test_clock_regression_after_source_read_blocks_store() -> None:
    session = _session()
    source = FakeSource(session)
    store = FakeStore(object())

    with pytest.raises(
        KrDailySessionCollectionError,
        match="kr_daily_session_collection_clock_moved_backwards",
    ):
        await CollectKrDailySessionObservation(
            source,
            store,
            clock=SequenceClock(COMPLETED_AT, STARTED_AT),
        ).execute(TARGET_DATE)

    assert source.calls == [TARGET_DATE]
    assert store.calls == []


@pytest.mark.parametrize(
    "clock_values",
    [
        (datetime(2026, 3, 24, 23, 0, 0),),
        (STARTED_AT, datetime(2026, 3, 24, 23, 0, 2)),
        (RuntimeError("clock secret=must-not-leak"),),
        (STARTED_AT, RuntimeError("clock secret=must-not-leak")),
    ],
)
async def test_invalid_clock_fails_closed_without_store(
    clock_values: tuple[object, ...],
) -> None:
    session = _session()
    source = FakeSource(session)
    store = FakeStore(object())

    with pytest.raises(
        KrDailySessionCollectionError,
        match="kr_daily_session_collection_clock_invalid",
    ) as captured:
        await CollectKrDailySessionObservation(
            source,
            store,
            clock=SequenceClock(*clock_values),
        ).execute(TARGET_DATE)

    expected_source_calls = [] if len(clock_values) == 1 else [TARGET_DATE]
    assert source.calls == expected_source_calls
    assert store.calls == []
    _assert_secret_absent(captured.value, "clock secret=must-not-leak")


async def test_store_failure_is_sanitized_and_not_retried() -> None:
    secret = "database response service_key=must-not-leak"
    session = _session()
    source = FakeSource(session)
    store = FakeStore(
        object(),
        error=CalendarObservationStoreError(secret),
    )

    with pytest.raises(
        KrDailySessionCollectionError,
        match="^kr_daily_session_collection_store_outcome_unknown$",
    ) as captured:
        await CollectKrDailySessionObservation(
            source,
            store,
            clock=SequenceClock(STARTED_AT, COMPLETED_AT),
        ).execute(TARGET_DATE)

    assert source.calls == [TARGET_DATE]
    assert len(store.calls) == 1
    assert captured.value.write_outcome == "unknown"
    _assert_secret_absent(captured.value, secret)


@pytest.mark.parametrize("stage", ["source", "store"])
async def test_async_cancellation_is_never_converted_to_domain_failure(
    stage: str,
) -> None:
    session = _session()
    source = FakeSource(
        session,
        error=asyncio.CancelledError() if stage == "source" else None,
    )
    store = FakeStore(
        _receipt(session),
        error=asyncio.CancelledError() if stage == "store" else None,
    )

    with pytest.raises(asyncio.CancelledError):
        await CollectKrDailySessionObservation(
            source,
            store,
            clock=SequenceClock(STARTED_AT, COMPLETED_AT),
        ).execute(TARGET_DATE)

    assert source.calls == [TARGET_DATE]
    assert len(store.calls) == (1 if stage == "store" else 0)


@pytest.mark.parametrize("mismatch", ["identity", "evidence", "observed_at"])
async def test_rejects_receipt_that_is_not_bound_to_source_session(
    mismatch: str,
) -> None:
    session = _session()
    if mismatch == "identity":
        receipt = _receipt(
            session,
            calendar_idempotency_key="a" * 64,
        )
    elif mismatch == "evidence":
        receipt = _receipt(
            session,
            canonical_evidence_sha256="b" * 64,
        )
    else:
        receipt = _receipt(
            session,
            observed_at=session.observed_at + timedelta(seconds=1),
        )
    store = FakeStore(receipt)

    with pytest.raises(
        KrDailySessionCollectionError,
        match="kr_daily_session_collection_store_outcome_unknown",
    ) as captured:
        await CollectKrDailySessionObservation(
            FakeSource(session),
            store,
            clock=SequenceClock(STARTED_AT, COMPLETED_AT),
        ).execute(TARGET_DATE)

    assert len(store.calls) == 1
    assert captured.value.write_outcome == "unknown"


async def test_rejects_invalid_or_mutated_store_receipt() -> None:
    session = _session()
    mutated = _receipt(session)
    object.__setattr__(mutated, "revision", 0)

    for value in (object(), mutated):
        store = FakeStore(value)
        with pytest.raises(
            KrDailySessionCollectionError,
            match="kr_daily_session_collection_store_outcome_unknown",
        ) as captured:
            await CollectKrDailySessionObservation(
                FakeSource(session),
                store,
                clock=SequenceClock(STARTED_AT, COMPLETED_AT),
            ).execute(TARGET_DATE)
        assert len(store.calls) == 1
        assert captured.value.write_outcome == "unknown"


async def test_evidence_result_owns_canonical_session_and_receipt_copies() -> None:
    session = _session()
    receipt = _receipt(session)

    result = await CollectKrDailySessionObservation(
        FakeSource(session),
        FakeStore(receipt),
        clock=SequenceClock(STARTED_AT, COMPLETED_AT),
    ).execute_with_evidence(TARGET_DATE)

    assert result.target_date == TARGET_DATE
    assert result.session == session
    assert result.session is not session
    assert result.receipt == receipt
    assert result.receipt is not receipt


def _session(
    *,
    is_open: bool = True,
    session_date: date = TARGET_DATE,
    observed_at: datetime = OBSERVED_AT,
) -> PointInTimeKrDailySessionV1:
    regular_start_at = datetime(2026, 3, 25, 9, 0, tzinfo=KST)
    regular_end_at = datetime(2026, 3, 25, 15, 30, tzinfo=KST)
    if session_date != TARGET_DATE:
        regular_start_at -= timedelta(days=1)
        regular_end_at -= timedelta(days=1)
    return PointInTimeKrDailySessionV1.create(
        provider="toss",
        market="KR",
        session_date=session_date,
        is_open=is_open,
        regular_start_at=regular_start_at if is_open else None,
        regular_end_at=regular_end_at if is_open else None,
        next_business_date=date(2026, 3, 26),
        next_regular_start_at=datetime(2026, 3, 26, 9, 0, tzinfo=KST),
        next_regular_end_at=datetime(2026, 3, 26, 15, 30, tzinfo=KST),
        observed_at=observed_at,
        provider_contract_sha256=CONTRACT_SHA256,
    )


def _receipt(
    session: PointInTimeKrDailySessionV1,
    *,
    status: CalendarObservationStatus = "stored",
    calendar_idempotency_key: object | None = None,
    canonical_evidence_sha256: object | None = None,
    observed_at: object | None = None,
    occurrence_inserted: bool | None = None,
) -> CalendarObservationWriteReceipt:
    is_stored = status == "stored"
    return CalendarObservationWriteReceipt(
        status=status,
        calendar_idempotency_key=cast(
            str,
            session.idempotency_key
            if calendar_idempotency_key is None
            else calendar_idempotency_key,
        ),
        canonical_evidence_sha256=cast(
            str,
            session.canonical_evidence_sha256
            if canonical_evidence_sha256 is None
            else canonical_evidence_sha256,
        ),
        revision=1,
        revision_inserted=is_stored,
        occurrence_id=OCCURRENCE_ID,
        occurrence_inserted=(is_stored if occurrence_inserted is None else occurrence_inserted),
        observed_at=cast(
            datetime,
            session.observed_at if observed_at is None else observed_at,
        ),
    )


def _assert_secret_absent(error: BaseException, secret: str) -> None:
    assert secret not in str(error)
    assert secret not in "".join(traceback.format_exception(error))
    assert error.__cause__ is None
    assert error.__context__ is None
