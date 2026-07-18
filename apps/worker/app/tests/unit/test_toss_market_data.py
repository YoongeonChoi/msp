from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast

import pytest
from pytest import MonkeyPatch

from app.adapters.broker.toss_contract import TOSS_CANDLE_OPENAPI_ARTIFACT_SHA256
from app.adapters.broker.toss_models import (
    TossCandle,
    TossCandlePage,
    TossCandleQuery,
    TossKrMarketCalendarResponse,
    TossPriceResponse,
)
from app.adapters.market_data import toss_market_data
from app.adapters.market_data.toss_market_data import TossMarketData
from app.application.ports.candle_data_port import DailyCandleReadRequest
from app.domain.common.errors import ProviderSchemaError
from app.domain.common.time import KST


class FakeToss:
    def __init__(
        self,
        prices: list[TossPriceResponse] | None = None,
        calendar: TossKrMarketCalendarResponse | None = None,
        candle_page: TossCandlePage | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.prices = prices or []
        self.calendar = calendar
        self.candle_page = candle_page
        self.events = events if events is not None else []
        self.candle_queries: list[TossCandleQuery] = []

    async def get_prices(self, symbols: list[str]) -> list[TossPriceResponse]:
        return self.prices

    async def get_kr_market_calendar(
        self,
        target_date: date | None = None,
    ) -> TossKrMarketCalendarResponse:
        del target_date
        if self.calendar is None:
            raise AssertionError("calendar not configured")
        return self.calendar

    async def get_candles(self, query: TossCandleQuery) -> TossCandlePage:
        self.events.append("fetch")
        self.candle_queries.append(query)
        if self.candle_page is None:
            raise AssertionError("candle page not configured")
        return self.candle_page


def _candle(**overrides: object) -> TossCandle:
    payload: dict[str, object] = {
        "timestamp": "2026-03-25T09:00:00+09:00",
        "openPrice": "71600",
        "highPrice": "72300",
        "lowPrice": "71500",
        "closePrice": "72000",
        "volume": "3521000",
        "currency": "KRW",
    }
    payload.update(overrides)
    return TossCandle.model_validate(payload)


def _candle_page(
    *,
    candles: list[TossCandle] | None = None,
) -> TossCandlePage:
    resolved_candles = (
        candles
        if candles is not None
        else [
            _candle(),
            _candle(
                timestamp="2026-03-24T09:00:00+09:00",
                openPrice="71200",
                highPrice="71800",
                lowPrice="71000",
                closePrice="71600",
                volume="2984000",
            ),
        ]
    )
    return TossCandlePage(
        candles=resolved_candles,
        nextBefore=datetime(2026, 3, 24, 9, 0, tzinfo=KST),
    )


async def test_toss_market_data_converts_krw_prices_to_quotes() -> None:
    service = TossMarketData(
        FakeToss(
            prices=[
                TossPriceResponse.model_validate(
                    {
                        "symbol": "005930",
                        "timestamp": "2026-03-25T09:30:00.123+09:00",
                        "lastPrice": "72000",
                        "currency": "KRW",
                    }
                )
            ]
        )
    )

    quotes = await service.get_quotes(["005930"])

    assert quotes["005930"].price_krw == 72_000
    assert quotes["005930"].source == "toss"
    assert quotes["005930"].as_of.tzinfo is not None


async def test_toss_market_data_rejects_missing_price_timestamp() -> None:
    service = TossMarketData(
        FakeToss(
            prices=[
                TossPriceResponse.model_validate(
                    {
                        "symbol": "005930",
                        "timestamp": None,
                        "lastPrice": "72000",
                        "currency": "KRW",
                    }
                )
            ]
        )
    )

    with pytest.raises(ProviderSchemaError, match="toss_price_timestamp_missing"):
        await service.get_quotes(["005930"])


async def test_toss_market_data_maps_daily_candle_page_to_pit_contract() -> None:
    events: list[str] = []
    observed_at = datetime(2026, 3, 25, 0, 0, 5, tzinfo=UTC)
    raw_page = _candle_page()
    fake = FakeToss(candle_page=raw_page, events=events)

    def clock() -> datetime:
        events.append("clock")
        return observed_at

    service = TossMarketData(fake, clock=clock)

    page = await service.get_daily_candles(
        TossCandleQuery(symbol="005930", interval="1d", count=2)
    )

    assert events == ["fetch", "clock"]
    assert page.observed_at == observed_at
    assert page.next_before == raw_page.next_before
    assert len(page.candles) == 2
    assert page.candles[0].provider == "toss"
    assert page.candles[0].symbol == "005930"
    assert page.candles[0].market == "KR"
    assert page.candles[0].interval == "1d"
    assert page.candles[0].open_krw == 71_600
    assert page.candles[0].high_krw == 72_300
    assert page.candles[0].low_krw == 71_500
    assert page.candles[0].close_krw == 72_000
    assert page.candles[0].volume == 3_521_000
    assert page.candles[0].observed_at == observed_at
    assert (
        page.candles[0].provider_contract_sha256
        == TOSS_CANDLE_OPENAPI_ARTIFACT_SHA256
    )
    assert len(page.candles[0].canonical_observation_sha256) == 64
    assert "is_complete" not in page.candles[0].to_payload()


async def test_toss_market_data_bridges_application_daily_candle_request() -> None:
    observed_at = datetime(2026, 3, 25, 0, 0, 5, tzinfo=UTC)
    before = datetime(2026, 3, 25, 9, 0, tzinfo=KST)
    fake = FakeToss(candle_page=_candle_page())
    service = TossMarketData(fake, clock=lambda: observed_at)
    request = DailyCandleReadRequest(
        symbol="005930",
        before=before,
        count=2,
        adjusted=False,
    )

    page = await service.read_daily_candle_page(request)

    assert fake.candle_queries == [
        TossCandleQuery(
            symbol="005930",
            interval="1d",
            count=2,
            before=before,
            adjusted=False,
        )
    ]
    assert page.candles[0].symbol == "005930"
    assert page.candles[0].adjusted is False
    assert page.next_before == datetime(2026, 3, 24, 9, 0, tzinfo=KST)
    assert page.observed_at == observed_at


@pytest.mark.parametrize(
    "query",
    [
        TossCandleQuery(symbol="AAPL", interval="1d"),
        TossCandleQuery(symbol="005930", interval="1m"),
        TossCandleQuery(symbol="005930", count=0),
        TossCandleQuery(symbol="005930", count=201),
        TossCandleQuery(symbol="005930", count=True),
        TossCandleQuery(
            symbol="005930",
            before=datetime(2026, 3, 25, 0, 0),
        ),
        TossCandleQuery(symbol="005930", adjusted=cast(Any, 1)),
    ],
)
async def test_toss_market_data_rejects_invalid_daily_query_before_fetch(
    query: TossCandleQuery,
) -> None:
    fake = FakeToss(candle_page=_candle_page())
    service = TossMarketData(fake)

    with pytest.raises(ProviderSchemaError):
        await service.get_daily_candles(query)

    assert fake.candle_queries == []
    assert fake.events == []


@pytest.mark.parametrize(
    ("candle", "reason"),
    [
        (_candle(currency="USD"), "toss_point_in_time_candle_currency_must_be_krw"),
        (_candle(openPrice="0"), "toss_candle_open_price_invalid"),
        (_candle(highPrice="72300.5"), "toss_candle_high_price_invalid"),
        (_candle(volume="-1"), "toss_candle_volume_invalid"),
        (_candle(volume="1.5"), "toss_candle_volume_invalid"),
        (
            _candle().model_copy(update={"open_price": Decimal("NaN")}),
            "toss_candle_open_price_invalid",
        ),
        (
            _candle().model_copy(update={"volume": Decimal("Infinity")}),
            "toss_candle_volume_invalid",
        ),
        (
            _candle(highPrice="71999"),
            "toss_point_in_time_candle_high_below_open_or_close",
        ),
        (
            _candle(timestamp="2026-03-25T00:00:06Z"),
            "toss_point_in_time_candle_observed_before_provider_event",
        ),
        (
            _candle(timestamp="2026-03-25T00:00:00"),
            "toss_point_in_time_candle_provider_event_at_requires_timezone",
        ),
    ],
)
async def test_toss_market_data_rejects_invalid_candle_observation(
    candle: TossCandle,
    reason: str,
) -> None:
    fake = FakeToss(candle_page=_candle_page(candles=[candle]))
    service = TossMarketData(
        fake,
        clock=lambda: datetime(2026, 3, 25, 0, 0, 5, tzinfo=UTC),
    )

    with pytest.raises(ProviderSchemaError, match=reason):
        await service.get_daily_candles(TossCandleQuery(symbol="005930"))


async def test_toss_market_data_rejects_duplicate_candle_identity() -> None:
    duplicate = _candle()
    fake = FakeToss(candle_page=_candle_page(candles=[duplicate, duplicate]))
    service = TossMarketData(
        fake,
        clock=lambda: datetime(2026, 3, 25, 0, 0, 5, tzinfo=UTC),
    )

    with pytest.raises(
        ProviderSchemaError,
        match="toss_point_in_time_candle_page_duplicate_identity",
    ):
        await service.get_daily_candles(TossCandleQuery(symbol="005930"))


async def test_toss_market_data_rejects_naive_observation_clock() -> None:
    fake = FakeToss(candle_page=_candle_page(candles=[]))
    service = TossMarketData(
        fake,
        clock=lambda: datetime(2026, 3, 25, 0, 0, 5),
    )

    with pytest.raises(
        ProviderSchemaError,
        match="toss_candle_observed_at_timezone_missing",
    ):
        await service.get_daily_candles(TossCandleQuery(symbol="005930"))


async def test_toss_market_data_rejects_naive_next_before_cursor() -> None:
    raw_page = _candle_page().model_copy(
        update={"next_before": datetime(2026, 3, 24, 9, 0)}
    )
    fake = FakeToss(candle_page=raw_page)
    service = TossMarketData(
        fake,
        clock=lambda: datetime(2026, 3, 25, 0, 0, 5, tzinfo=UTC),
    )

    with pytest.raises(
        ProviderSchemaError,
        match="toss_candle_next_before_timezone_missing",
    ):
        await service.get_daily_candles(TossCandleQuery(symbol="005930"))


async def test_toss_market_data_rejects_page_larger_than_requested_count() -> None:
    fake = FakeToss(candle_page=_candle_page())
    service = TossMarketData(
        fake,
        clock=lambda: datetime(2026, 3, 25, 0, 0, 5, tzinfo=UTC),
    )

    with pytest.raises(
        ProviderSchemaError,
        match="toss_candle_page_exceeds_requested_count",
    ):
        await service.get_daily_candles(
            TossCandleQuery(symbol="005930", count=1)
        )


async def test_toss_market_data_rejects_candle_after_before_cursor() -> None:
    fake = FakeToss(candle_page=_candle_page(candles=[_candle()]))
    service = TossMarketData(
        fake,
        clock=lambda: datetime(2026, 3, 25, 0, 0, 5, tzinfo=UTC),
    )

    with pytest.raises(
        ProviderSchemaError,
        match="toss_candle_page_exceeds_before_cursor",
    ):
        await service.get_daily_candles(
            TossCandleQuery(
                symbol="005930",
                before=datetime(2026, 3, 24, 9, 0, tzinfo=KST),
            )
        )


async def test_toss_market_data_rejects_next_cursor_after_query_cursor() -> None:
    raw_page = _candle_page(candles=[]).model_copy(
        update={"next_before": datetime(2026, 3, 25, 9, 0, tzinfo=KST)}
    )
    fake = FakeToss(candle_page=raw_page)
    service = TossMarketData(
        fake,
        clock=lambda: datetime(2026, 3, 25, 0, 0, 5, tzinfo=UTC),
    )

    with pytest.raises(
        ProviderSchemaError,
        match="toss_candle_next_before_not_progressing",
    ):
        await service.get_daily_candles(
            TossCandleQuery(
                symbol="005930",
                before=datetime(2026, 3, 24, 9, 0, tzinfo=KST),
            )
        )


async def test_toss_market_data_rejects_unchanged_next_cursor() -> None:
    cursor = datetime(2026, 3, 24, 9, 0, tzinfo=KST)
    raw_page = _candle_page(candles=[]).model_copy(
        update={"next_before": cursor}
    )
    fake = FakeToss(candle_page=raw_page)
    service = TossMarketData(
        fake,
        clock=lambda: datetime(2026, 3, 25, 0, 0, 5, tzinfo=UTC),
    )

    with pytest.raises(
        ProviderSchemaError,
        match="toss_candle_next_before_not_progressing",
    ):
        await service.get_daily_candles(
            TossCandleQuery(symbol="005930", before=cursor)
        )


async def test_toss_market_data_rejects_next_cursor_after_oldest_candle() -> None:
    raw_page = _candle_page(
        candles=[_candle(timestamp="2026-03-24T09:00:00+09:00")]
    ).model_copy(
        update={"next_before": datetime(2026, 3, 25, 9, 0, tzinfo=KST)}
    )
    fake = FakeToss(candle_page=raw_page)
    service = TossMarketData(
        fake,
        clock=lambda: datetime(2026, 3, 25, 0, 0, 5, tzinfo=UTC),
    )

    with pytest.raises(
        ProviderSchemaError,
        match="toss_candle_next_before_after_oldest_candle",
    ):
        await service.get_daily_candles(TossCandleQuery(symbol="005930"))


async def test_toss_market_data_requires_regular_market_session_and_execution_window(
    monkeypatch: MonkeyPatch,
) -> None:
    service = TossMarketData(FakeToss(calendar=_calendar_payload(integrated=True)))
    monkeypatch.setattr(
        toss_market_data,
        "now_kst",
        lambda: datetime(2026, 3, 25, 8, 0, tzinfo=KST),
    )

    assert await service.is_market_open() is False

    monkeypatch.setattr(
        toss_market_data,
        "now_kst",
        lambda: datetime(2026, 3, 25, 9, 0, tzinfo=KST),
    )

    assert await service.is_market_open() is True

    monkeypatch.setattr(
        toss_market_data,
        "now_kst",
        lambda: datetime(2026, 3, 25, 15, 19, tzinfo=KST),
    )

    assert await service.is_market_open() is True

    monkeypatch.setattr(
        toss_market_data,
        "now_kst",
        lambda: datetime(2026, 3, 25, 15, 20, tzinfo=KST),
    )

    assert await service.is_market_open() is False


async def test_toss_market_data_reports_closed_when_integrated_session_is_null(
    monkeypatch: MonkeyPatch,
) -> None:
    service = TossMarketData(FakeToss(calendar=_calendar_payload(integrated=False)))
    monkeypatch.setattr(
        toss_market_data,
        "now_kst",
        lambda: datetime(2026, 3, 25, 10, 0, tzinfo=KST),
    )

    assert await service.is_market_open() is False


def _calendar_payload(integrated: bool) -> TossKrMarketCalendarResponse:
    today: dict[str, object] = {
        "date": "2026-03-25",
        "integrated": None,
    }
    if integrated:
        today["integrated"] = {
            "regularMarket": {
                "startTime": "2026-03-25T09:00:00+09:00",
                "singlePriceAuctionStartTime": "2026-03-25T15:20:00+09:00",
                "endTime": "2026-03-25T15:30:00+09:00",
            }
        }
    return TossKrMarketCalendarResponse.model_validate(
        {
            "today": today,
            "previousBusinessDay": {"date": "2026-03-24", "integrated": None},
            "nextBusinessDay": {"date": "2026-03-26", "integrated": None},
        }
    )
