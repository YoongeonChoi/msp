from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

TossResult = TypeVar("TossResult")


class TossApiResponse[TossResult](BaseModel):
    model_config = ConfigDict(frozen=True)

    result: TossResult


class TossOAuthToken(BaseModel):
    model_config = ConfigDict(frozen=True)

    access_token: str
    token_type: Literal["Bearer"]
    expires_in: int


class TossOAuthError(BaseModel):
    model_config = ConfigDict(frozen=True)

    error: str
    error_description: str | None = None
    error_uri: str | None = None


class TossApiErrorBody(BaseModel):
    model_config = ConfigDict(frozen=True)

    request_id: str = Field(alias="requestId")
    code: str
    message: str


class TossApiErrorEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True)

    error: TossApiErrorBody


class TossAccount(BaseModel):
    model_config = ConfigDict(frozen=True)

    account_no: str = Field(alias="accountNo")
    account_seq: int = Field(alias="accountSeq")
    account_type: str = Field(alias="accountType")


class TossPriceResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    timestamp: datetime | None = None
    last_price: Decimal = Field(alias="lastPrice")
    currency: str


class TossCurrencyAmounts(BaseModel):
    model_config = ConfigDict(frozen=True)

    krw: Decimal
    usd: Decimal | None = None


class TossMarketValue(BaseModel):
    model_config = ConfigDict(frozen=True)

    purchase_amount: Decimal = Field(alias="purchaseAmount")
    amount: Decimal
    amount_after_cost: Decimal = Field(alias="amountAfterCost")


class TossProfitLoss(BaseModel):
    model_config = ConfigDict(frozen=True)

    amount: Decimal
    amount_after_cost: Decimal = Field(alias="amountAfterCost")
    rate: Decimal
    rate_after_cost: Decimal = Field(alias="rateAfterCost")


class TossDailyProfitLoss(BaseModel):
    model_config = ConfigDict(frozen=True)

    amount: Decimal
    rate: Decimal


class TossCost(BaseModel):
    model_config = ConfigDict(frozen=True)

    commission: Decimal
    tax: Decimal | None = None


class TossHoldingItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    name: str
    market_country: str = Field(alias="marketCountry")
    currency: str
    quantity: Decimal
    last_price: Decimal = Field(alias="lastPrice")
    average_purchase_price: Decimal = Field(alias="averagePurchasePrice")
    market_value: TossMarketValue = Field(alias="marketValue")
    profit_loss: TossProfitLoss = Field(alias="profitLoss")
    daily_profit_loss: TossDailyProfitLoss = Field(alias="dailyProfitLoss")
    cost: TossCost


class TossOverviewMarketValue(BaseModel):
    model_config = ConfigDict(frozen=True)

    amount: TossCurrencyAmounts
    amount_after_cost: TossCurrencyAmounts = Field(alias="amountAfterCost")


class TossOverviewProfitLoss(BaseModel):
    model_config = ConfigDict(frozen=True)

    amount: TossCurrencyAmounts
    amount_after_cost: TossCurrencyAmounts = Field(alias="amountAfterCost")
    rate: Decimal
    rate_after_cost: Decimal = Field(alias="rateAfterCost")


class TossOverviewDailyProfitLoss(BaseModel):
    model_config = ConfigDict(frozen=True)

    amount: TossCurrencyAmounts
    rate: Decimal


class TossHoldingsOverview(BaseModel):
    model_config = ConfigDict(frozen=True)

    total_purchase_amount: TossCurrencyAmounts = Field(alias="totalPurchaseAmount")
    market_value: TossOverviewMarketValue = Field(alias="marketValue")
    profit_loss: TossOverviewProfitLoss = Field(alias="profitLoss")
    daily_profit_loss: TossOverviewDailyProfitLoss = Field(alias="dailyProfitLoss")
    items: list[TossHoldingItem]


class TossCandle(BaseModel):
    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    open_price: Decimal = Field(alias="openPrice")
    high_price: Decimal = Field(alias="highPrice")
    low_price: Decimal = Field(alias="lowPrice")
    close_price: Decimal = Field(alias="closePrice")
    volume: Decimal
    currency: str


class TossCandlePage(BaseModel):
    model_config = ConfigDict(frozen=True)

    candles: list[TossCandle]
    next_before: datetime | None = Field(default=None, alias="nextBefore")


class TossOrderExecution(BaseModel):
    model_config = ConfigDict(frozen=True)

    filled_quantity: Decimal = Field(alias="filledQuantity")
    average_filled_price: Decimal | None = Field(alias="averageFilledPrice")
    filled_amount: Decimal | None = Field(alias="filledAmount")
    commission: Decimal | None
    tax: Decimal | None
    filled_at: datetime | None = Field(alias="filledAt")
    settlement_date: date | None = Field(alias="settlementDate")


class TossOrder(BaseModel):
    model_config = ConfigDict(frozen=True)

    order_id: str = Field(alias="orderId")
    symbol: str
    side: str
    order_type: str = Field(alias="orderType")
    time_in_force: str = Field(alias="timeInForce")
    status: str
    price: Decimal | None = None
    quantity: Decimal
    order_amount: Decimal | None = Field(default=None, alias="orderAmount")
    currency: str
    ordered_at: datetime = Field(alias="orderedAt")
    canceled_at: datetime | None = Field(default=None, alias="canceledAt")
    execution: TossOrderExecution


class TossOrderLifecycleStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class TossOrderPage(BaseModel):
    model_config = ConfigDict(frozen=True)

    orders: list[TossOrder]
    next_cursor: str | None = Field(alias="nextCursor")
    has_next: bool = Field(alias="hasNext")


@dataclass(frozen=True, slots=True)
class TossCandleQuery:
    symbol: str
    interval: Literal["1m", "1d"] = "1d"
    count: int = 100
    before: datetime | None = None
    adjusted: bool = True


@dataclass(frozen=True, slots=True)
class TossOrderListQuery:
    account_seq: int | None = None
    status: TossOrderLifecycleStatus = TossOrderLifecycleStatus.OPEN
    symbol: str | None = None
    from_date: date | None = None
    to_date: date | None = None
    cursor: str | None = None
    limit: int | None = None
