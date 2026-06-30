from __future__ import annotations

from datetime import date, datetime

import pytest
from pytest import MonkeyPatch

from app.adapters.broker.toss_models import (
    TossKrMarketCalendarResponse,
    TossPriceResponse,
)
from app.adapters.market_data import toss_market_data
from app.adapters.market_data.toss_market_data import TossMarketData
from app.domain.common.errors import ProviderSchemaError
from app.domain.common.time import KST


class FakeToss:
    def __init__(
        self,
        prices: list[TossPriceResponse] | None = None,
        calendar: TossKrMarketCalendarResponse | None = None,
    ) -> None:
        self.prices = prices or []
        self.calendar = calendar

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
