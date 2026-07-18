from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest

from app.application.ports.candle_data_port import (
    DailyCandleReadPage,
    DailyCandleReadRequest,
)
from app.application.services.data_collection_service import (
    CandleCollectionError,
    DataCollectionService,
)
from app.domain.market_data.point_in_time import PointInTimeCandleV1

CONTRACT_SHA = "a" * 64
STARTED_AT = datetime(2026, 3, 25, 0, 0, 0, tzinfo=UTC)
OBSERVED_AT = datetime(2026, 3, 25, 0, 0, 1, tzinfo=UTC)
COMPLETED_AT = datetime(2026, 3, 25, 0, 0, 2, tzinfo=UTC)
BEFORE = STARTED_AT
EVENT_AT = datetime(2026, 3, 24, 0, 0, 0, tzinfo=UTC)


class FakeDailyCandleSource:
    def __init__(
        self,
        page: object,
        *,
        error: Exception | None = None,
    ) -> None:
        self.page = page
        self.error = error
        self.calls: list[DailyCandleReadRequest] = []

    async def read_daily_candle_page(
        self,
        request: DailyCandleReadRequest,
    ) -> DailyCandleReadPage:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return cast(DailyCandleReadPage, self.page)


class SequenceClock:
    def __init__(self, *values: datetime) -> None:
        self.values = list(values)

    def __call__(self) -> datetime:
        if not self.values:
            raise AssertionError("clock exhausted")
        return self.values.pop(0)


def _request(**overrides: object) -> DailyCandleReadRequest:
    values: dict[str, object] = {
        "symbol": "005930",
        "before": BEFORE,
        "count": 1,
        "adjusted": True,
    }
    values.update(overrides)
    return DailyCandleReadRequest(**cast(dict[str, Any], values))


def _page(
    *,
    candles: tuple[PointInTimeCandleV1, ...] | None = None,
    next_before: datetime | None = None,
    observed_at: datetime = OBSERVED_AT,
) -> DailyCandleReadPage:
    resolved_candles = candles if candles is not None else (_candle(),)
    return DailyCandleReadPage(
        candles=resolved_candles,
        next_before=next_before,
        observed_at=observed_at,
    )


def _candle(
    *,
    symbol: str = "005930",
    provider: str = "toss",
    adjusted: bool = True,
    event_at: datetime = EVENT_AT,
    observed_at: datetime = OBSERVED_AT,
    contract_sha: str = CONTRACT_SHA,
) -> PointInTimeCandleV1:
    return PointInTimeCandleV1.create(
        provider=provider,
        symbol=symbol,
        market="KR",
        interval="1d",
        adjusted=adjusted,
        provider_event_at=event_at,
        observed_at=observed_at,
        currency="KRW",
        open_krw=71_600,
        high_krw=72_300,
        low_krw=71_500,
        close_krw=72_000,
        volume=3_521_000,
        provider_contract_sha256=contract_sha,
    )


def _previous_event() -> datetime:
    return datetime(2026, 3, 23, 0, 0, tzinfo=UTC)


async def test_collection_reads_exactly_one_page_without_following_cursor() -> None:
    request = _request()
    page = _page(next_before=EVENT_AT)
    source = FakeDailyCandleSource(page)
    service = DataCollectionService(
        source,
        clock=SequenceClock(STARTED_AT, COMPLETED_AT),
    )

    result = await service.collect_daily_candle_page(request)

    assert result is page
    assert source.calls == [request]
    assert result.next_before == EVENT_AT
    assert not hasattr(result, "is_complete")
    assert not hasattr(result, "persistence_receipt")


@pytest.mark.parametrize(
    ("read_request", "reason"),
    [
        (cast(Any, object()), "candle_collection_request_invalid"),
        (_request(symbol="AAPL"), "candle_collection_symbol_invalid"),
        (
            _request(symbol=cast(Any, 5930)),
            "candle_collection_symbol_invalid",
        ),
        (_request(count=0), "candle_collection_count_out_of_range"),
        (_request(count=201), "candle_collection_count_out_of_range"),
        (_request(count=cast(Any, True)), "candle_collection_count_out_of_range"),
        (
            _request(adjusted=cast(Any, 1)),
            "candle_collection_adjusted_must_be_boolean",
        ),
        (
            _request(before=datetime(2026, 3, 25, 0, 0)),
            "candle_collection_before_timezone_missing",
        ),
        (
            _request(before=COMPLETED_AT),
            "candle_collection_before_is_in_future",
        ),
    ],
)
async def test_collection_rejects_invalid_request_before_source_call(
    read_request: DailyCandleReadRequest,
    reason: str,
) -> None:
    source = FakeDailyCandleSource(_page())
    service = DataCollectionService(source, clock=SequenceClock(STARTED_AT))

    with pytest.raises(CandleCollectionError, match=reason):
        await service.collect_daily_candle_page(read_request)

    assert source.calls == []


async def test_collection_rejects_naive_service_clock_before_source_call() -> None:
    source = FakeDailyCandleSource(_page())
    service = DataCollectionService(
        source,
        clock=SequenceClock(datetime(2026, 3, 25, 0, 0)),
    )

    with pytest.raises(
        CandleCollectionError,
        match="candle_collection_clock_timezone_missing",
    ):
        await service.collect_daily_candle_page(_request())

    assert source.calls == []


async def test_collection_does_not_convert_source_error_to_success() -> None:
    provider_error = RuntimeError("provider failed")
    source = FakeDailyCandleSource(_page(), error=provider_error)
    service = DataCollectionService(
        source,
        clock=SequenceClock(STARTED_AT),
    )

    with pytest.raises(RuntimeError, match="provider failed") as exc_info:
        await service.collect_daily_candle_page(_request())

    assert exc_info.value is provider_error
    assert source.calls == [_request()]


@pytest.mark.parametrize(
    ("page", "reason"),
    [
        (cast(Any, object()), "candle_collection_page_invalid"),
        (
            DailyCandleReadPage(
                candles=cast(Any, [_candle()]),
                next_before=None,
                observed_at=OBSERVED_AT,
            ),
            "candle_collection_page_candles_invalid",
        ),
        (
            _page(observed_at=datetime(2026, 3, 25, 0, 0)),
            "candle_collection_observed_at_timezone_missing",
        ),
        (
            _page(observed_at=datetime(2026, 3, 24, 23, 59, 59, tzinfo=UTC)),
            "candle_collection_observed_at_outside_read",
        ),
        (
            _page(observed_at=datetime(2026, 3, 25, 0, 0, 3, tzinfo=UTC)),
            "candle_collection_observed_at_outside_read",
        ),
        (
            _page(candles=(_candle(), _candle())),
            "point_in_time_candle_page_duplicate_identity",
        ),
        (
            _page(
                candles=(
                    _candle(),
                    _candle(event_at=_previous_event()),
                    _candle(
                        event_at=datetime(2026, 3, 22, 0, 0, tzinfo=UTC)
                    ),
                )
            ),
            "candle_collection_page_exceeds_count",
        ),
        (
            _page(candles=(_candle(symbol="000660"),)),
            "candle_collection_symbol_mismatch",
        ),
        (
            _page(candles=(_candle(adjusted=False),)),
            "candle_collection_adjusted_mismatch",
        ),
        (
            _page(
                candles=(
                    _candle(
                        event_at=COMPLETED_AT,
                        observed_at=COMPLETED_AT,
                    ),
                )
            ),
            "candle_collection_candle_after_before",
        ),
        (
            _page(candles=(_candle(observed_at=COMPLETED_AT),)),
            "candle_collection_observation_time_mismatch",
        ),
        (
            _page(
                candles=(
                    _candle(),
                    _candle(event_at=_previous_event(), provider="alternate"),
                )
            ),
            "candle_collection_mixed_providers",
        ),
        (
            _page(
                candles=(
                    _candle(),
                    _candle(event_at=_previous_event(), contract_sha="b" * 64),
                )
            ),
            "candle_collection_mixed_contract_hashes",
        ),
        (
            _page(next_before=datetime(2026, 3, 24, 0, 0)),
            "candle_collection_next_before_timezone_missing",
        ),
        (
            _page(next_before=BEFORE),
            "candle_collection_next_before_not_progressing",
        ),
        (
            _page(next_before=datetime(2026, 3, 24, 0, 0, 1, tzinfo=UTC)),
            "candle_collection_next_before_after_oldest",
        ),
    ],
)
async def test_collection_rejects_invalid_source_page(
    page: DailyCandleReadPage,
    reason: str,
) -> None:
    request = _request(count=2)
    source = FakeDailyCandleSource(page)
    service = DataCollectionService(
        source,
        clock=SequenceClock(STARTED_AT, COMPLETED_AT),
    )

    with pytest.raises(CandleCollectionError, match=reason):
        await service.collect_daily_candle_page(request)

    assert source.calls == [request]


async def test_collection_rejects_backwards_clock_after_source_read() -> None:
    source = FakeDailyCandleSource(_page())
    service = DataCollectionService(
        source,
        clock=SequenceClock(STARTED_AT, _previous_event()),
    )

    with pytest.raises(
        CandleCollectionError,
        match="candle_collection_clock_moved_backwards",
    ):
        await service.collect_daily_candle_page(_request())
