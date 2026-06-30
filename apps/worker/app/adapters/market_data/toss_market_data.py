from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

from app.adapters.broker.toss_models import (
    TossKrMarketCalendarResponse,
    TossPriceResponse,
)
from app.domain.common.errors import ProviderError, ProviderSchemaError
from app.domain.common.time import KST, now_kst
from app.domain.trading.entities import Quote

LIVE_EXECUTION_START_HOUR = 8
LIVE_EXECUTION_END_HOUR = 18


class TossMarketDataClient(Protocol):
    async def get_prices(self, symbols: list[str]) -> list[TossPriceResponse]:
        ...

    async def get_kr_market_calendar(
        self,
        target_date: date | None = None,
    ) -> TossKrMarketCalendarResponse:
        ...


class TossMarketData:
    def __init__(self, toss: TossMarketDataClient) -> None:
        self.toss = toss
        self._provider_health_details: dict[str, object] = {}

    async def provider_health(self) -> bool:
        self._provider_health_details = {}
        try:
            await self.toss.get_kr_market_calendar(now_kst().date())
        except ProviderError as exc:
            self._provider_health_details = {
                "error_type": type(exc).__name__,
                "reason": exc.safe_message,
            }
            return False
        return True

    def provider_health_details(self) -> dict[str, object]:
        return dict(self._provider_health_details)

    async def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        if not symbols:
            return {}
        prices = await self.toss.get_prices(symbols)
        quotes: dict[str, Quote] = {}
        for price in prices:
            if price.currency != "KRW":
                raise ProviderSchemaError("toss", "toss_price_currency_not_krw")
            if price.timestamp is None:
                raise ProviderSchemaError("toss", "toss_price_timestamp_missing")
            if price.timestamp.tzinfo is None:
                raise ProviderSchemaError("toss", "toss_price_timestamp_timezone_missing")
            quotes[price.symbol] = Quote(
                symbol=price.symbol,
                price_krw=_decimal_krw_to_int(price.last_price),
                as_of=price.timestamp,
                source="toss",
            )
        return quotes

    async def is_market_open(self) -> bool | None:
        current = now_kst()
        try:
            calendar = await self.toss.get_kr_market_calendar(current.date())
        except ProviderError:
            return None
        if calendar.today.date != current.date():
            return None
        if not _within_live_execution_window(current):
            return False
        integrated = calendar.today.integrated
        if integrated is None or integrated.regular_market is None:
            return False
        regular = integrated.regular_market
        if regular.start_time.tzinfo is None or regular.end_time.tzinfo is None:
            return None
        start = regular.start_time.astimezone(KST)
        end = regular.end_time.astimezone(KST)
        if regular.single_price_auction_start_time is not None:
            if regular.single_price_auction_start_time.tzinfo is None:
                return None
            end = regular.single_price_auction_start_time.astimezone(KST)
        return start <= current < end


def _decimal_krw_to_int(value: Decimal) -> int:
    if value <= 0 or value != value.to_integral_value():
        raise ProviderSchemaError("toss", "toss_price_not_positive_integer_krw")
    return int(value)


def _within_live_execution_window(value: datetime) -> bool:
    if value.weekday() >= 5:
        return False
    window_start = value.replace(
        hour=LIVE_EXECUTION_START_HOUR,
        minute=0,
        second=0,
        microsecond=0,
    )
    window_end = value.replace(
        hour=LIVE_EXECUTION_END_HOUR,
        minute=0,
        second=0,
        microsecond=0,
    )
    return window_start <= value < window_end
