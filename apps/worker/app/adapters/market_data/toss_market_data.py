from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

from app.adapters.broker.toss_contract import TOSS_CANDLE_OPENAPI_ARTIFACT_SHA256
from app.adapters.broker.toss_models import (
    TossCandle,
    TossCandlePage,
    TossCandleQuery,
    TossKrMarketCalendarResponse,
    TossPriceResponse,
)
from app.application.ports.candle_data_port import (
    DailyCandleReadPage,
    DailyCandleReadRequest,
)
from app.domain.common.errors import ProviderError, ProviderSchemaError
from app.domain.common.time import KST, now_kst, now_utc
from app.domain.market_data.point_in_time import (
    PointInTimeCandleV1,
    PointInTimeDataError,
    validate_point_in_time_candle_page,
)
from app.domain.trading.entities import Quote

LIVE_EXECUTION_START_HOUR = 8
LIVE_EXECUTION_END_HOUR = 18
_KR_SYMBOL_RE = re.compile(r"[0-9]{6}")


@dataclass(frozen=True, slots=True)
class TossPointInTimeCandlePage:
    candles: tuple[PointInTimeCandleV1, ...]
    next_before: datetime | None
    observed_at: datetime


class TossMarketDataClient(Protocol):
    async def get_prices(self, symbols: list[str]) -> list[TossPriceResponse]:
        ...

    async def get_kr_market_calendar(
        self,
        target_date: date | None = None,
    ) -> TossKrMarketCalendarResponse:
        ...

    async def get_candles(self, query: TossCandleQuery) -> TossCandlePage:
        ...


class TossMarketData:
    def __init__(
        self,
        toss: TossMarketDataClient,
        *,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        self.toss = toss
        self.clock = clock
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

    async def get_daily_candles(
        self,
        query: TossCandleQuery,
    ) -> TossPointInTimeCandlePage:
        _validate_daily_candle_query(query)
        raw_page = await self.toss.get_candles(query)
        observed_at = _require_aware_timestamp(
            self.clock(),
            "toss_candle_observed_at_timezone_missing",
        )
        next_before = raw_page.next_before
        if next_before is not None:
            next_before = _require_aware_timestamp(
                next_before,
                "toss_candle_next_before_timezone_missing",
            )
        try:
            candles = validate_point_in_time_candle_page(
                [
                    _to_point_in_time_candle(
                        raw_candle,
                        query=query,
                        observed_at=observed_at,
                    )
                    for raw_candle in raw_page.candles
                ]
            )
        except PointInTimeDataError as exc:
            raise ProviderSchemaError("toss", f"toss_{exc.safe_message}") from exc
        _validate_daily_candle_response(
            candles,
            query=query,
            next_before=next_before,
        )
        return TossPointInTimeCandlePage(
            candles=candles,
            next_before=next_before,
            observed_at=observed_at,
        )

    async def read_daily_candle_page(
        self,
        request: DailyCandleReadRequest,
    ) -> DailyCandleReadPage:
        page = await self.get_daily_candles(
            TossCandleQuery(
                symbol=request.symbol,
                interval="1d",
                count=request.count,
                before=request.before,
                adjusted=request.adjusted,
            )
        )
        return DailyCandleReadPage(
            candles=page.candles,
            next_before=page.next_before,
            observed_at=page.observed_at,
        )

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


def _validate_daily_candle_query(query: TossCandleQuery) -> None:
    if not isinstance(query.symbol, str) or _KR_SYMBOL_RE.fullmatch(query.symbol) is None:
        raise ProviderSchemaError("toss", "toss_candle_symbol_invalid")
    if query.interval != "1d":
        raise ProviderSchemaError("toss", "toss_candle_interval_must_be_1d")
    if (
        not isinstance(query.count, int)
        or isinstance(query.count, bool)
        or not 1 <= query.count <= 200
    ):
        raise ProviderSchemaError("toss", "toss_candle_count_out_of_range")
    if query.before is not None:
        _require_aware_timestamp(
            query.before,
            "toss_candle_before_timezone_missing",
        )
    if not isinstance(query.adjusted, bool):
        raise ProviderSchemaError("toss", "toss_candle_adjusted_must_be_boolean")


def _to_point_in_time_candle(
    candle: TossCandle,
    *,
    query: TossCandleQuery,
    observed_at: datetime,
) -> PointInTimeCandleV1:
    return PointInTimeCandleV1.create(
        provider="toss",
        symbol=query.symbol,
        market="KR",
        interval=query.interval,
        adjusted=query.adjusted,
        provider_event_at=candle.timestamp,
        observed_at=observed_at,
        currency=candle.currency,
        open_krw=_decimal_candle_value_to_int(
            candle.open_price,
            field_name="open_price",
            positive=True,
        ),
        high_krw=_decimal_candle_value_to_int(
            candle.high_price,
            field_name="high_price",
            positive=True,
        ),
        low_krw=_decimal_candle_value_to_int(
            candle.low_price,
            field_name="low_price",
            positive=True,
        ),
        close_krw=_decimal_candle_value_to_int(
            candle.close_price,
            field_name="close_price",
            positive=True,
        ),
        volume=_decimal_candle_value_to_int(
            candle.volume,
            field_name="volume",
            positive=False,
        ),
        provider_contract_sha256=TOSS_CANDLE_OPENAPI_ARTIFACT_SHA256,
    )


def _validate_daily_candle_response(
    candles: tuple[PointInTimeCandleV1, ...],
    *,
    query: TossCandleQuery,
    next_before: datetime | None,
) -> None:
    if len(candles) > query.count:
        raise ProviderSchemaError("toss", "toss_candle_page_exceeds_requested_count")
    if query.before is not None:
        if any(candle.provider_event_at > query.before for candle in candles):
            raise ProviderSchemaError("toss", "toss_candle_page_exceeds_before_cursor")
        if next_before is not None and next_before >= query.before:
            raise ProviderSchemaError("toss", "toss_candle_next_before_not_progressing")
    if candles and next_before is not None:
        oldest_event_at = min(candle.provider_event_at for candle in candles)
        if next_before > oldest_event_at:
            raise ProviderSchemaError(
                "toss",
                "toss_candle_next_before_after_oldest_candle",
            )


def _decimal_candle_value_to_int(
    value: Decimal,
    *,
    field_name: str,
    positive: bool,
) -> int:
    if not value.is_finite():
        raise ProviderSchemaError("toss", f"toss_candle_{field_name}_invalid")
    below_minimum = value <= 0 if positive else value < 0
    if below_minimum or value != value.to_integral_value():
        raise ProviderSchemaError("toss", f"toss_candle_{field_name}_invalid")
    return int(value)


def _require_aware_timestamp(value: object, reason: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ProviderSchemaError("toss", reason)
    return value


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
